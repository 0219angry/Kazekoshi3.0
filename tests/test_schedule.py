import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands

from kazekoshi.cogs.poll import (
    AUTO_START_GRACE_SECONDS,
    DEFAULT_SCHEDULE_EMOJIS,
    DEFAULT_SCHEDULE_OPTIONS,
    DEFAULT_SCHEDULE_OPTION_LIST,
    EMOJI_NUMBERS,
    PollCog,
    ScheduleInputError,
    SchedulePollRegistry,
    announced_start_time,
    build_schedule_embed,
    choose_start_time,
    format_schedule_options,
    is_auto_start_schedule,
    normalize_schedule_time,
    parse_message_id,
    parse_schedule_options,
    schedule_author_id,
    schedule_option_emojis,
    schedule_options_from_embed,
    start_announcement,
)


FIXED_NOW = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


def schedule_snowflake(created_at: datetime) -> int:
    """テスト用の作成日時をDiscord Snowflakeへ決定的に変換する。"""
    return discord.utils.time_snowflake(created_at)


class FakeReaction:
    def __init__(self, emoji, users, *, me=False):
        self.emoji = emoji
        self._users = users
        self.me = me

    def users(self, *, limit=None):
        async def iterator():
            for user in self._users:
                yield user

        return iterator()


class ControlledSleeper:
    """実時間を進めず、デバウンス解除をテスト側で制御する。"""

    def __init__(self):
        self.calls = []
        self._called = asyncio.Event()

    async def __call__(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.calls.append(SimpleNamespace(delay=delay, future=future))
        self._called.set()
        await future

    async def wait_for_calls(self, count):
        while len(self.calls) < count:
            self._called.clear()
            if len(self.calls) >= count:
                break
            await self._called.wait()

    def release(self, index=-1):
        future = self.calls[index].future
        if not future.done():
            future.set_result(None)


class ScheduleHarness:
    """リアクション、投票投稿、通知投稿を変更可能な最小Discord環境。"""

    POLL_MESSAGE_ID = schedule_snowflake(FIXED_NOW - timedelta(days=1))
    CHANNEL_ID = 10

    def __init__(self, sleeper, registry):
        self.sleeper = sleeper
        self.bot_user = SimpleNamespace(id=1, bot=True)
        self.humans = {
            user_id: SimpleNamespace(
                id=user_id,
                bot=False,
                display_name=f"player-{user_id}",
                name=f"user-{user_id}",
            )
            for user_id in range(2, 20)
        }
        self.role = SimpleNamespace(
            id=88,
            name="GAME",
            mention="<@&88>",
        )
        self.creator = SimpleNamespace(id=77, display_name="creator")
        self.poll_message = SimpleNamespace(
            id=self.POLL_MESSAGE_ID,
            author=self.bot_user,
            embeds=[
                build_schedule_embed(
                    self.role,
                    list(DEFAULT_SCHEDULE_OPTION_LIST),
                    self.creator,
                    auto_start=True,
                )
            ],
            reactions=[],
            role_mentions=[self.role],
            content=self.role.mention,
            guild=SimpleNamespace(
                get_role=lambda role_id: self.role,
                get_member=lambda user_id: self.humans.get(user_id),
            ),
            jump_url=(
                "https://discord.com/channels/1/"
                f"{self.CHANNEL_ID}/{self.POLL_MESSAGE_ID}"
            ),
            edit=AsyncMock(),
            add_reaction=AsyncMock(),
            clear_reaction=AsyncMock(),
        )
        self.poll_message.edit.side_effect = self._edit_poll
        self.notifications = {}
        self._next_notification_id = 1000
        self.channel = SimpleNamespace(
            id=self.CHANNEL_ID,
            name="planning",
            parent=None,
            fetch_message=AsyncMock(side_effect=self._fetch_message),
            send=AsyncMock(side_effect=self._send_message),
        )
        self.bot = SimpleNamespace(
            user=self.bot_user,
            get_channel=lambda channel_id: (
                self.channel if channel_id == self.CHANNEL_ID else None
            ),
            fetch_channel=AsyncMock(return_value=self.channel),
            wait_until_ready=AsyncMock(),
            get_user=lambda user_id: self.humans.get(user_id),
        )
        self.cog = PollCog(self.bot, sleeper=sleeper, registry=registry)
        self.cog._register_schedule_poll(
            guild_id=1,
            channel_id=self.CHANNEL_ID,
            message_id=self.POLL_MESSAGE_ID,
        )
        self.set_voters({})

    async def _edit_poll(self, *, embed):
        self.poll_message.embeds = [embed]
        return self.poll_message

    async def _fetch_message(self, message_id):
        if message_id == self.POLL_MESSAGE_ID:
            return self.poll_message
        try:
            return self.notifications[message_id]
        except KeyError as error:
            response = Mock(status=404, reason="Not Found")
            raise discord.NotFound(
                response,
                {"message": "Unknown Message", "code": 10008},
            ) from error

    async def _send_message(self, *, content, allowed_mentions):
        message_id = self._next_notification_id
        self._next_notification_id += 1
        notification = SimpleNamespace(
            id=message_id,
            author=self.bot_user,
            content=content,
            allowed_mentions=allowed_mentions,
            edit=AsyncMock(),
            delete=AsyncMock(),
        )

        async def save_edit(**kwargs):
            if "content" in kwargs:
                notification.content = kwargs["content"]
            if "allowed_mentions" in kwargs:
                notification.allowed_mentions = kwargs["allowed_mentions"]
            return notification

        notification.edit.side_effect = save_edit
        self.notifications[message_id] = notification
        return notification

    def set_voters(self, voters_by_option):
        self.poll_message.reactions = []
        options = schedule_options_from_embed(self.poll_message.embeds[0])
        if options is None:
            raise AssertionError("schedule options could not be read from test embed")
        for option, emoji in zip(options, schedule_option_emojis(options)):
            users = [self.bot_user]
            users.extend(
                self.humans[user_id]
                for user_id in voters_by_option.get(option, set())
            )
            self.poll_message.reactions.append(FakeReaction(emoji, users, me=True))

    def payload(self, *, emoji="5️⃣", user_id=2):
        return SimpleNamespace(
            guild_id=1,
            channel_id=self.CHANNEL_ID,
            message_id=self.POLL_MESSAGE_ID,
            user_id=user_id,
            emoji=emoji,
        )

    def clear_payload(self):
        return SimpleNamespace(
            guild_id=1,
            channel_id=self.CHANNEL_ID,
            message_id=self.POLL_MESSAGE_ID,
        )


class ScheduleParsingTests(unittest.TestCase):
    def test_parse_space_separated_options(self):
        self.assertEqual(
            parse_schedule_options("21:00 22:00 23:00 NG"),
            ["21:00", "22:00", "23:00", "NG"],
        )

    def test_supported_time_formats_are_normalized_for_display(self):
        expected = ["15:00", "16:00", "17:00"]
        for value in (
            "15 16 17",
            "15:00 16:00 17:00",
            "1500 1600 1700",
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_schedule_options(value), expected)

        self.assertEqual(
            parse_schedule_options("1530 16:30 17 NG"),
            ["15:30", "16:30", "17:00", "NG"],
        )

    def test_time_normalization_rejects_invalid_clock_values(self):
        self.assertEqual(normalize_schedule_time("24"), "24:00")
        self.assertEqual(normalize_schedule_time("0900"), "09:00")
        self.assertIsNone(normalize_schedule_time("24:30"))
        self.assertIsNone(normalize_schedule_time("2360"))

    def test_parse_quoted_option(self):
        self.assertEqual(
            parse_schedule_options('"8/21 21時" "8/22 22時" ng'),
            ["8/21 21時", "8/22 22時", "ng"],
        )

    def test_parse_rejects_too_few_or_too_many_options(self):
        with self.assertRaisesRegex(ScheduleInputError, "2つ以上"):
            parse_schedule_options("21")
        with self.assertRaisesRegex(ScheduleInputError, "最大10個"):
            parse_schedule_options(" ".join(str(index) for index in range(11)))

    def test_parse_rejects_unclosed_quote_and_long_option(self):
        with self.assertRaisesRegex(ScheduleInputError, "引用符"):
            parse_schedule_options('"21 22')
        with self.assertRaisesRegex(ScheduleInputError, "100文字以内"):
            parse_schedule_options(f"{'x' * 101} NG")

    def test_format_uses_number_reactions_in_order(self):
        self.assertEqual(
            format_schedule_options(["21:00", "22:00", "NG"]),
            "1️⃣21:00, 2️⃣22:00, 🆖NG",
        )

    def test_parse_message_id_or_link(self):
        self.assertEqual(parse_message_id("123456"), (123456, None))
        self.assertEqual(
            parse_message_id(
                "https://discord.com/channels/111/222/333"
            ),
            (333, 222),
        )
        with self.assertRaisesRegex(ScheduleInputError, "投稿ID"):
            parse_message_id("not-a-message")

    def test_choose_start_time_counts_distinct_people_from_early_time(self):
        voters = {
            "20:00": set(),
            "20:30": set(),
            "21:00": {5},
            "21:30": set(),
            "22:00": {4},
            "22:30": set(),
            "23:00": {2, 3},
            "24:00": {1, 2},
            "NG": {6, 7, 8, 9, 10},
        }

        self.assertEqual(choose_start_time(voters), "24:00")
        self.assertIsNone(
            choose_start_time({"24:00": {1, 2}, "23:00": {1, 2}, "NG": {3, 4, 5}})
        )

    def test_choose_start_time_supports_custom_formats_and_sorts_by_time(self):
        voters = {
            "1700": {4, 5},
            "15": {1, 2},
            "16:00": {3},
        }

        self.assertEqual(choose_start_time(voters), "17:00")


class ScheduleDisplayTests(unittest.TestCase):
    def test_embed_contains_options_and_creator_marker(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)

        embed = build_schedule_embed(role, ["21:00", "22:00", "NG"], author)

        self.assertEqual(embed.title, "📅 VALORANT 開始時間")
        self.assertEqual(embed.description, "1️⃣21:00, 2️⃣22:00, 🆖NG")
        self.assertEqual(schedule_author_id(embed), 987)

    def test_non_schedule_embed_has_no_creator(self):
        self.assertIsNone(schedule_author_id(discord.Embed(title="other")))

    def test_supported_time_poll_has_auto_start_marker_when_enabled(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        time_embed = build_schedule_embed(
            role,
            parse_schedule_options("1500 1600 1700 NG"),
            author,
            auto_start=True,
        )
        markerless_embed = build_schedule_embed(
            role,
            list(DEFAULT_SCHEDULE_OPTION_LIST),
            author,
        )
        non_time_embed = build_schedule_embed(
            role,
            ["平日", "休日", "NG"],
            author,
            auto_start=True,
        )

        self.assertTrue(is_auto_start_schedule(time_embed))
        self.assertEqual(
            time_embed.description,
            "1️⃣15:00, 2️⃣16:00, 3️⃣17:00, 🆖NG",
        )
        self.assertIsNone(announced_start_time(time_embed))
        self.assertFalse(is_auto_start_schedule(markerless_embed))
        self.assertFalse(is_auto_start_schedule(non_time_embed))

    def test_legacy_multiline_options_remain_readable(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        embed = build_schedule_embed(
            role,
            ["15:00", "16:00", "17:00", "NG"],
            author,
            auto_start=True,
        )
        embed.description = "1️⃣：15\n2️⃣：16\n3️⃣：17\n4️⃣：ng"

        self.assertEqual(
            schedule_options_from_embed(embed),
            ["15", "16", "17", "ng"],
        )
        self.assertTrue(is_auto_start_schedule(embed))

    def test_creator_id_cannot_be_spoofed_by_display_name(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(
            display_name="fake | 作成者ID: 123 | name",
            id=987,
        )
        embed = build_schedule_embed(role, ["21:00", "NG"], author)

        self.assertEqual(schedule_author_id(embed), 987)

    def test_hybrid_group_exposes_add_and_update_slash_subcommands(self):
        self.assertIsInstance(PollCog.schedule, commands.HybridGroup)
        application_commands = PollCog.schedule.app_command.commands
        self.assertEqual(
            [command.name for command in application_commands],
            ["add", "update", "close"],
        )
        add_command = application_commands[0]
        options_parameter = next(
            parameter for parameter in add_command.parameters if parameter.name == "options"
        )
        self.assertFalse(options_parameter.required)


class SchedulePollRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.registry = SchedulePollRegistry(
            f"{self.temp_directory.name}/schedule-polls.sqlite3"
        )

    def test_prune_before_removes_only_ids_strictly_before_cutoff(self):
        cutoff_at = FIXED_NOW - timedelta(days=90)
        expired_id = schedule_snowflake(cutoff_at - timedelta(milliseconds=1))
        cutoff_id = schedule_snowflake(cutoff_at)
        recent_id = schedule_snowflake(cutoff_at + timedelta(milliseconds=1))
        for message_id in (expired_id, cutoff_id, recent_id):
            self.registry.register(
                guild_id=1,
                channel_id=10,
                message_id=message_id,
            )

        pruned = self.registry.prune_before(cutoff_id)

        self.assertEqual(
            [poll.message_id for poll in pruned],
            [expired_id],
        )
        self.assertEqual(
            {poll.message_id for poll in self.registry.all()},
            {cutoff_id, recent_id},
        )


class ScheduleCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.registry = SchedulePollRegistry(
            f"{self.temp_directory.name}/schedule-polls.sqlite3"
        )

    async def test_cog_registers_schedule_application_group(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        try:
            await bot.add_cog(PollCog(bot, registry=self.registry))
            schedule_group = bot.tree.get_command("schedule")
            self.assertIsNotNone(schedule_group)
            self.assertEqual(
                [command.name for command in schedule_group.commands],
                ["add", "update", "close"],
            )
        finally:
            await bot.close()

    async def test_add_posts_embed_and_number_reactions(self):
        bot = SimpleNamespace()
        cog = PollCog(bot, registry=self.registry)
        permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        channel = SimpleNamespace(
            id=10,
            name="general",
            parent=None,
            permissions_for=lambda _: permissions,
        )
        author = SimpleNamespace(id=77, display_name="tester")
        role = SimpleNamespace(
            id=88,
            name="RAID",
            mention="<@&88>",
            mentionable=True,
            is_default=lambda: False,
        )
        poll_message = SimpleNamespace(id=99, add_reaction=AsyncMock())
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(id=1, me=SimpleNamespace(id=1)),
            channel=channel,
            author=author,
            interaction=object(),
            send=AsyncMock(return_value=poll_message),
        )

        await PollCog.schedule_add.callback(
            cog,
            ctx,
            role,
        )

        sent = ctx.send.await_args.kwargs
        self.assertEqual(sent["content"], "<@&88>")
        self.assertEqual(sent["allowed_mentions"].roles, [role])
        self.assertEqual(sent["embed"].title, "📅 RAID 開始時間")
        self.assertTrue(is_auto_start_schedule(sent["embed"]))
        self.assertEqual(
            sent["embed"].description,
            "1️⃣20:00, 2️⃣20:30, 3️⃣21:00, 4️⃣21:30, 5️⃣22:00, "
            "6️⃣22:30, 7️⃣23:00, 8️⃣24:00, 🆖NG",
        )
        self.assertEqual(
            [call.args[0] for call in poll_message.add_reaction.await_args_list],
            list(DEFAULT_SCHEDULE_EMOJIS),
        )
        self.assertEqual(
            DEFAULT_SCHEDULE_OPTIONS,
            "20:00 20:30 21:00 21:30 22:00 22:30 23:00 24:00 NG",
        )
        registered = self.registry.all()
        self.assertEqual(len(registered), 1)
        self.assertEqual(
            (
                registered[0].guild_id,
                registered[0].channel_id,
                registered[0].message_id,
            ),
            (1, 10, 99),
        )

        ctx.send.reset_mock()
        poll_message.add_reaction.reset_mock()
        await PollCog.schedule_add.callback(
            cog,
            ctx,
            role,
            options="1500 1600 1700 NG",
        )
        custom_embed = ctx.send.await_args.kwargs["embed"]
        self.assertTrue(is_auto_start_schedule(custom_embed))
        self.assertEqual(
            custom_embed.description,
            "1️⃣15:00, 2️⃣16:00, 3️⃣17:00, 🆖NG",
        )
        self.assertEqual(
            [call.args[0] for call in poll_message.add_reaction.await_args_list],
            ["1️⃣", "2️⃣", "3️⃣", "🆖"],
        )

    async def test_add_prunes_expired_registry_rows_before_registering_new_poll(self):
        expired_id = schedule_snowflake(
            FIXED_NOW - timedelta(days=90, milliseconds=1)
        )
        new_poll_id = schedule_snowflake(FIXED_NOW - timedelta(days=1))
        self.registry.register(
            guild_id=1,
            channel_id=10,
            message_id=expired_id,
        )
        bot = SimpleNamespace()
        cog = PollCog(bot, registry=self.registry)
        prune_expired = cog._prune_expired_schedule_polls
        cog._prune_expired_schedule_polls = Mock(
            side_effect=lambda *, now=None: prune_expired(now=FIXED_NOW)
        )
        permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        channel = SimpleNamespace(
            id=10,
            name="valorant",
            parent=None,
            permissions_for=lambda _: permissions,
        )
        role = SimpleNamespace(
            id=88,
            name="VALORANT",
            mention="<@&88>",
            mentionable=True,
            is_default=lambda: False,
        )
        poll_message = SimpleNamespace(
            id=new_poll_id,
            add_reaction=AsyncMock(),
        )
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(id=1, me=SimpleNamespace(id=1)),
            channel=channel,
            author=SimpleNamespace(id=77, display_name="tester"),
            interaction=object(),
            send=AsyncMock(return_value=poll_message),
        )

        await PollCog.schedule_add.callback(cog, ctx, role)

        cog._prune_expired_schedule_polls.assert_called_once_with()
        self.assertEqual(
            [poll.message_id for poll in self.registry.all()],
            [new_poll_id],
        )

    async def test_add_checks_reaction_permission_before_posting(self):
        bot_member = SimpleNamespace(id=1)
        permissions = SimpleNamespace(
            send_messages=True,
            send_messages_in_threads=True,
            embed_links=True,
            read_message_history=True,
            add_reactions=False,
        )
        bot = SimpleNamespace()
        cog = PollCog(bot, registry=self.registry)
        role = SimpleNamespace(
            id=88,
            name="VALORANT",
            mention="<@&88>",
            mentionable=True,
            is_default=lambda: False,
        )
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(me=bot_member),
            channel=SimpleNamespace(
                id=10,
                name="valorant",
                parent=None,
                permissions_for=lambda _: permissions,
            ),
            author=SimpleNamespace(id=77, display_name="tester"),
            interaction=object(),
            send=AsyncMock(),
        )

        await PollCog.schedule_add.callback(cog, ctx, role)

        ctx.send.assert_awaited_once()
        self.assertIn("リアクションの追加", ctx.send.await_args.args[0])
        self.assertNotIn("embed", ctx.send.await_args.kwargs)

    async def test_update_edits_embed_and_resets_reactions(self):
        bot_user = SimpleNamespace(id=1)
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        cog._queue_auto_start_check_by_id = Mock()
        permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        author = SimpleNamespace(id=77, display_name="tester")
        role = SimpleNamespace(name="VALORANT")
        original_embed = build_schedule_embed(
            role,
            list(DEFAULT_SCHEDULE_OPTION_LIST),
            author,
            auto_start=True,
        )
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[original_embed],
            reactions=[
                SimpleNamespace(emoji="1️⃣"),
                SimpleNamespace(emoji="2️⃣"),
                SimpleNamespace(emoji="🎉"),
            ],
            jump_url="https://discord.com/channels/1/10/99",
            clear_reaction=AsyncMock(),
            edit=AsyncMock(),
            add_reaction=AsyncMock(),
        )

        async def remove_reaction_from_cache(emoji):
            poll_message.reactions[:] = [
                reaction
                for reaction in poll_message.reactions
                if reaction.emoji != emoji
            ]

        poll_message.clear_reaction.side_effect = remove_reaction_from_cache
        channel = SimpleNamespace(
            id=10,
            name="valorant",
            parent=None,
            permissions_for=lambda _: permissions,
            fetch_message=AsyncMock(return_value=poll_message),
        )
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(id=1, me=SimpleNamespace(id=1)),
            channel=channel,
            author=author,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_update.callback(
            cog,
            ctx,
            "99",
            options="1500 1600 1700 NG",
        )

        self.assertEqual(
            [call.args[0] for call in poll_message.clear_reaction.await_args_list],
            EMOJI_NUMBERS[:2],
        )
        edited_embed = poll_message.edit.await_args.kwargs["embed"]
        self.assertEqual(
            edited_embed.description,
            "1️⃣15:00, 2️⃣16:00, 3️⃣17:00, 🆖NG",
        )
        self.assertTrue(is_auto_start_schedule(edited_embed))
        self.assertIsNone(announced_start_time(edited_embed))
        self.assertEqual(
            [call.args[0] for call in poll_message.add_reaction.await_args_list],
            ["1️⃣", "2️⃣", "3️⃣", "🆖"],
        )
        cog._queue_auto_start_check_by_id.assert_called_once_with(
            guild_id=1,
            channel_id=10,
            message_id=99,
        )
        self.assertEqual([poll.message_id for poll in self.registry.all()], [99])
        self.assertIn("投票をリセット", ctx.send.await_args.args[0])

    async def test_prefix_add_requires_an_actual_role_mention(self):
        bot = SimpleNamespace()
        cog = PollCog(bot, registry=self.registry)
        role = SimpleNamespace(
            id=88,
            name="VALORANT",
            mention="<@&88>",
            mentionable=True,
            is_default=lambda: False,
        )
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(me=SimpleNamespace(id=1)),
            channel=SimpleNamespace(id=10, name="valorant", parent=None),
            author=SimpleNamespace(id=77, display_name="tester"),
            interaction=None,
            message=SimpleNamespace(role_mentions=[]),
            send=AsyncMock(),
        )

        await PollCog.schedule_add.callback(cog, ctx, role)

        self.assertIn("メンションで指定", ctx.send.await_args.args[0])

    async def test_update_rejects_non_creator_without_manage_messages(self):
        bot_user = SimpleNamespace(id=1)
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        creator = SimpleNamespace(id=77, display_name="creator")
        requester = SimpleNamespace(id=66, display_name="requester")
        role = SimpleNamespace(name="VALORANT")
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[build_schedule_embed(role, ["21:00", "22:00"], creator)],
            reactions=[],
            clear_reaction=AsyncMock(),
        )
        bot_member = SimpleNamespace(id=1)

        def permissions_for(member):
            return SimpleNamespace(
                manage_messages=member is bot_member,
                mention_everyone=False,
                read_message_history=True,
                add_reactions=True,
                embed_links=True,
            )

        channel = SimpleNamespace(
            id=10,
            name="valorant",
            parent=None,
            permissions_for=permissions_for,
            fetch_message=AsyncMock(return_value=poll_message),
        )
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(me=bot_member),
            channel=channel,
            author=requester,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_update.callback(
            cog,
            ctx,
            "99",
            options="22:00 23:00 NG",
        )

        poll_message.clear_reaction.assert_not_awaited()
        self.assertIn("作成者", ctx.send.await_args.args[0])



class ScheduleAutoStartTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.registry = SchedulePollRegistry(
            f"{self.temp_directory.name}/schedule-polls.sqlite3"
        )
        self.sleeper = ControlledSleeper()
        self.harness = ScheduleHarness(self.sleeper, self.registry)

    async def asyncTearDown(self):
        await self.harness.cog.cog_unload()

    async def queue_check(self, event="add", *, emoji="5️⃣", user_id=2):
        call_index = len(self.sleeper.calls)
        listener = {
            "add": self.harness.cog.on_raw_reaction_add,
            "remove": self.harness.cog.on_raw_reaction_remove,
            "clear_emoji": self.harness.cog.on_raw_reaction_clear_emoji,
            "clear": self.harness.cog.on_raw_reaction_clear,
        }[event]
        payload = (
            self.harness.clear_payload()
            if event == "clear"
            else self.harness.payload(emoji=emoji, user_id=user_id)
        )

        await listener(payload)
        await self.sleeper.wait_for_calls(call_index + 1)
        task = self.harness.cog._auto_start_tasks[
            self.harness.POLL_MESSAGE_ID
        ]
        return self.sleeper.calls[call_index], task

    async def finish_check(self, sleep_call, task):
        if not sleep_call.future.done():
            sleep_call.future.set_result(None)
        await task

    async def announce(self, voters_by_option, expected_time):
        self.harness.set_voters(voters_by_option)
        sleep_call, task = await self.queue_check()
        await self.finish_check(sleep_call, task)
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertIsNotNone(announcement)
        self.assertEqual(announcement.start_time, expected_time)
        return self.harness.notifications[announcement.message_id]

    def assert_allowed_mentions_none(self, allowed_mentions):
        self.assertEqual(
            allowed_mentions.to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )

    async def assert_remove_then_clear_uses_generic_reason(self, clear_event):
        await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")
        self.harness.channel.send.reset_mock()
        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        first_sleep, first_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )

        self.harness.poll_message.reactions = []
        clear_sleep, clear_task = await self.queue_check(
            clear_event,
            emoji="1️⃣",
        )

        self.assertTrue(first_sleep.future.cancelled())
        self.assertTrue(first_task.cancelled())
        await self.finish_check(clear_sleep, clear_task)

        cancellation = self.harness.channel.send.await_args.kwargs
        self.assertIn(
            "参加可能な投票者が5人未満になりました。",
            cancellation["content"],
        )
        self.assertNotIn(
            "player-6の参加がキャンセルされました。",
            cancellation["content"],
        )
        self.assert_allowed_mentions_none(cancellation["allowed_mentions"])

    async def test_waits_ten_seconds_and_persists_first_notification_id(self):
        self.assertEqual(AUTO_START_GRACE_SECONDS, 10)
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})

        sleep_call, task = await self.queue_check()

        self.assertEqual(sleep_call.delay, 10)
        self.assertFalse(task.done())
        self.harness.channel.fetch_message.assert_not_awaited()
        self.harness.channel.send.assert_not_awaited()

        await self.finish_check(sleep_call, task)

        self.harness.channel.send.assert_awaited_once()
        notification = self.harness.channel.send.await_args
        self.assertEqual(notification.kwargs["content"], "20:00 開始 <@&88>")
        self.assertEqual(notification.kwargs["allowed_mentions"].roles, [self.harness.role])
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "20:00")
        self.assertEqual(announcement.message_id, 1000)

    async def test_custom_compact_times_are_normalized_and_auto_evaluated(self):
        options = parse_schedule_options("1500 1600 1700")
        self.harness.poll_message.embeds = [
            build_schedule_embed(
                self.harness.role,
                options,
                self.harness.creator,
                auto_start=True,
            )
        ]
        self.harness.set_voters(
            {
                "15:00": {2, 3},
                "16:00": {4},
                "17:00": {5, 6},
            }
        )

        sleep_call, task = await self.queue_check(emoji="3️⃣", user_id=6)
        await self.finish_check(sleep_call, task)

        self.assertEqual(
            self.harness.poll_message.embeds[0].description,
            "1️⃣15:00, 2️⃣16:00, 3️⃣17:00",
        )
        self.assertEqual(
            self.harness.channel.send.await_args.kwargs["content"],
            "17:00 開始 <@&88>",
        )
        self.assertEqual(
            start_announcement(self.harness.poll_message.embeds[0]).start_time,
            "17:00",
        )

    async def test_cog_load_prunes_expired_rows_before_recovery(self):
        expired_id = schedule_snowflake(
            FIXED_NOW - timedelta(days=90, milliseconds=1)
        )
        self.registry.register(
            guild_id=1,
            channel_id=self.harness.CHANNEL_ID,
            message_id=expired_id,
        )
        prune_expired = self.harness.cog._prune_expired_schedule_polls
        self.harness.cog._prune_expired_schedule_polls = Mock(
            side_effect=lambda *, now=None: prune_expired(now=FIXED_NOW)
        )

        await self.harness.cog.cog_load()
        await self.harness.cog._registry_recovery_task
        await self.sleeper.wait_for_calls(1)

        self.harness.cog._prune_expired_schedule_polls.assert_called_once_with()
        self.assertEqual(
            [poll.message_id for poll in self.registry.all()],
            [self.harness.POLL_MESSAGE_ID],
        )
        self.assertNotIn(
            expired_id,
            self.harness.cog._registered_schedule_ids,
        )
        self.assertNotIn(expired_id, self.harness.cog._auto_start_tasks)
        self.assertIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._auto_start_tasks,
        )

    async def test_prune_removes_expired_memory_only_poll(self):
        # SQLite登録だけが失敗した状態を再現し、メモリ上の監視も期限終了する。
        self.registry.unregister(self.harness.POLL_MESSAGE_ID)
        self.harness.cog._persisted_schedule_ids.discard(
            self.harness.POLL_MESSAGE_ID
        )
        sleep_call, pending_task = await self.queue_check()

        expired = self.harness.cog._prune_expired_schedule_polls(
            now=FIXED_NOW + timedelta(days=91)
        )

        with self.assertRaises(asyncio.CancelledError):
            await pending_task
        self.assertTrue(sleep_call.future.cancelled())
        self.assertEqual(
            [poll.message_id for poll in expired],
            [self.harness.POLL_MESSAGE_ID],
        )
        self.assertEqual(self.registry.all(), [])
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._registered_schedule_ids,
        )
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._registered_schedule_polls,
        )

    async def test_prune_during_notification_send_deletes_transition_output(self):
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        send_started = asyncio.Event()
        release_send = asyncio.Event()
        original_send = self.harness._send_message

        async def blocked_send(*, content, allowed_mentions):
            send_started.set()
            await release_send.wait()
            return await original_send(
                content=content,
                allowed_mentions=allowed_mentions,
            )

        self.harness.channel.send.side_effect = blocked_send
        sleep_call, transition_task = await self.queue_check(
            "add",
            emoji="1️⃣",
        )
        sleep_call.future.set_result(None)
        await send_started.wait()

        expired = self.harness.cog._prune_expired_schedule_polls(
            now=FIXED_NOW + timedelta(days=91)
        )
        release_send.set()

        with self.assertRaises(asyncio.CancelledError):
            await transition_task
        self.assertEqual(
            [poll.message_id for poll in expired],
            [self.harness.POLL_MESSAGE_ID],
        )
        sent_notification = self.harness.notifications[1000]
        sent_notification.delete.assert_awaited_once()
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))
        self.assertEqual(self.registry.all(), [])
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._registered_schedule_ids,
        )
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._poll_notification_ids,
        )
        self.assertNotIn(1000, self.harness.cog._notification_poll_refs)

    async def test_prune_during_cancellation_send_restores_discord_state(self):
        old_notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )
        self.harness.channel.send.reset_mock()
        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        send_started = asyncio.Event()
        release_send = asyncio.Event()
        original_send = self.harness._send_message

        async def blocked_send(*, content, allowed_mentions):
            send_started.set()
            await release_send.wait()
            return await original_send(
                content=content,
                allowed_mentions=allowed_mentions,
            )

        self.harness.channel.send.side_effect = blocked_send
        sleep_call, transition_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )
        sleep_call.future.set_result(None)
        await send_started.wait()

        expired = self.harness.cog._prune_expired_schedule_polls(
            now=FIXED_NOW + timedelta(days=91)
        )
        release_send.set()

        with self.assertRaises(asyncio.CancelledError):
            await transition_task
        self.assertEqual(
            [poll.message_id for poll in expired],
            [self.harness.POLL_MESSAGE_ID],
        )
        cancellation_notice = self.harness.notifications[1001]
        cancellation_notice.delete.assert_awaited_once()
        self.assert_allowed_mentions_none(cancellation_notice.allowed_mentions)
        self.assertEqual(old_notification.content, "20:00 開始 <@&88>")
        self.assert_allowed_mentions_none(old_notification.allowed_mentions)
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "20:00")
        self.assertEqual(announcement.message_id, old_notification.id)
        self.assertEqual(self.registry.all(), [])
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._poll_notification_ids,
        )
        self.assertNotIn(
            old_notification.id,
            self.harness.cog._notification_poll_refs,
        )

    async def test_cog_load_recovers_registered_poll_after_ten_second_grace(self):
        self.registry.register(
            guild_id=1,
            channel_id=self.harness.CHANNEL_ID,
            message_id=self.harness.POLL_MESSAGE_ID,
        )
        self.harness.set_voters({"21:00": {2, 3, 4, 5, 6}})
        prune_expired = self.harness.cog._prune_expired_schedule_polls
        self.harness.cog._prune_expired_schedule_polls = Mock(
            side_effect=lambda *, now=None: prune_expired(now=FIXED_NOW)
        )

        await self.harness.cog.cog_load()
        await self.harness.cog._registry_recovery_task
        await self.sleeper.wait_for_calls(1)
        pending_task = self.harness.cog._auto_start_tasks[
            self.harness.POLL_MESSAGE_ID
        ]

        self.harness.bot.wait_until_ready.assert_awaited_once()
        self.assertEqual(self.sleeper.calls[0].delay, 10)
        self.harness.channel.fetch_message.assert_not_awaited()
        self.harness.channel.send.assert_not_awaited()

        await self.finish_check(self.sleeper.calls[0], pending_task)

        self.harness.channel.send.assert_awaited_once()
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "21:00")
        self.assertEqual(announcement.message_id, 1000)

    async def test_reaction_removed_within_ten_seconds_prevents_notification(self):
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        first_sleep, first_task = await self.queue_check()

        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        second_sleep, second_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
        )

        self.assertEqual(first_sleep.delay, 10)
        self.assertTrue(first_sleep.future.cancelled())
        self.assertTrue(first_task.cancelled())
        self.harness.channel.send.assert_not_awaited()

        await self.finish_check(second_sleep, second_task)

        self.harness.channel.send.assert_not_awaited()
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))

    async def test_continuous_events_restart_debounce_and_use_only_latest_state(self):
        self.harness.set_voters({"24:00": {2, 3, 4}})
        first_sleep, first_task = await self.queue_check()

        self.harness.set_voters({"24:00": {2, 3, 4, 5}})
        second_sleep, second_task = await self.queue_check()

        self.harness.set_voters({"24:00": {2, 3, 4, 5, 6}})
        third_sleep, third_task = await self.queue_check()

        self.assertTrue(first_sleep.future.cancelled())
        self.assertTrue(second_sleep.future.cancelled())
        self.assertTrue(first_task.cancelled())
        self.assertTrue(second_task.cancelled())
        self.assertEqual([call.delay for call in self.sleeper.calls], [10, 10, 10])
        self.harness.channel.fetch_message.assert_not_awaited()

        await self.finish_check(third_sleep, third_task)

        self.harness.channel.fetch_message.assert_awaited_once_with(
            self.harness.POLL_MESSAGE_ID
        )
        self.harness.channel.send.assert_awaited_once()
        self.assertEqual(
            start_announcement(self.harness.poll_message.embeds[0]).start_time,
            "24:00",
        )

    async def test_remove_below_five_strikes_old_notification_and_posts_cancellation(self):
        old_notification = await self.announce(
            {
                "20:00": {2},
                "21:00": {3},
                "22:00": {4},
                "23:00": {5},
                "24:00": {6},
            },
            "24:00",
        )
        self.harness.channel.send.reset_mock()
        self.harness.poll_message.edit.reset_mock()
        self.harness.set_voters(
            {
                "20:00": {2},
                "21:00": {3},
                "22:00": {4},
                "23:00": {5},
            }
        )

        sleep_call, task = await self.queue_check(
            "remove",
            emoji="8️⃣",
            user_id=999,
        )

        old_notification.edit.assert_not_awaited()
        await self.finish_check(sleep_call, task)

        old_notification.edit.assert_awaited_once()
        cancelled_content = old_notification.edit.await_args.kwargs["content"]
        self.assertIn("~~24:00 開始 <@&88>~~", cancelled_content)
        self.assertIn("取り消されました", cancelled_content)
        self.assertFalse(
            old_notification.edit.await_args.kwargs["allowed_mentions"].roles
        )
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))
        self.assertTrue(is_auto_start_schedule(self.harness.poll_message.embeds[0]))
        self.harness.channel.send.assert_awaited_once()
        cancellation = self.harness.channel.send.await_args.kwargs
        self.assertIn("24:00 開始の通知を取り消しました", cancellation["content"])
        self.assertIn("5人未満", cancellation["content"])
        self.assertFalse(cancellation["allowed_mentions"].roles)

    async def test_remove_below_five_names_cancelled_participant_without_mentions(self):
        await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")
        self.harness.channel.send.reset_mock()
        self.harness.humans[6].display_name = "Alice"
        self.harness.set_voters({"20:00": {2, 3, 4, 5}})

        sleep_call, task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )
        await self.finish_check(sleep_call, task)

        cancellation = self.harness.channel.send.await_args.kwargs
        self.assertEqual(
            cancellation["content"],
            "↩️ 20:00 開始の通知を取り消しました <@&88>\n"
            "Aliceの参加がキャンセルされました。",
        )
        self.assert_allowed_mentions_none(cancellation["allowed_mentions"])

    async def test_second_remove_replaces_first_cancelled_participant(self):
        await self.announce({"20:00": {2, 3, 4, 5, 6, 7}}, "20:00")
        self.harness.channel.send.reset_mock()
        self.harness.humans[7].display_name = "Alice"
        self.harness.humans[6].display_name = "Bob"
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        first_sleep, first_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=7,
        )

        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        second_sleep, second_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )

        self.assertTrue(first_sleep.future.cancelled())
        self.assertTrue(first_task.cancelled())
        await self.finish_check(second_sleep, second_task)

        cancellation = self.harness.channel.send.await_args.kwargs
        self.assertIn(
            "Bobの参加がキャンセルされました。",
            cancellation["content"],
        )
        self.assertNotIn("Alice", cancellation["content"])
        self.assert_allowed_mentions_none(cancellation["allowed_mentions"])

    async def test_add_after_remove_keeps_cancelled_participant(self):
        await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")
        self.harness.channel.send.reset_mock()
        self.harness.humans[6].display_name = "Alice"
        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        remove_sleep, remove_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )

        # 既に20:00へ投票中の人が21:00にも投票しても、distinct人数は4人のまま。
        self.harness.set_voters(
            {
                "20:00": {2, 3, 4, 5},
                "21:00": {2},
            }
        )
        add_sleep, add_task = await self.queue_check(
            "add",
            emoji="3️⃣",
            user_id=2,
        )

        self.assertTrue(remove_sleep.future.cancelled())
        self.assertTrue(remove_task.cancelled())
        await self.finish_check(add_sleep, add_task)

        cancellation = self.harness.channel.send.await_args.kwargs
        self.assertIn(
            "Aliceの参加がキャンセルされました。",
            cancellation["content"],
        )
        self.assert_allowed_mentions_none(cancellation["allowed_mentions"])

    async def test_clear_emoji_after_remove_uses_generic_reason(self):
        await self.assert_remove_then_clear_uses_generic_reason("clear_emoji")

    async def test_clear_all_after_remove_uses_generic_reason(self):
        await self.assert_remove_then_clear_uses_generic_reason("clear")

    async def test_cancelled_participant_name_is_sanitized(self):
        await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")
        self.harness.channel.send.reset_mock()
        self.harness.humans[6].display_name = "@everyone\n**danger** _x_"
        self.harness.set_voters({"20:00": {2, 3, 4, 5}})

        sleep_call, task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )
        await self.finish_check(sleep_call, task)

        cancellation = self.harness.channel.send.await_args.kwargs
        detail = cancellation["content"].splitlines()[1]
        self.assertEqual(
            detail,
            "@\u200beveryone \\*\\*danger\\*\\* \\_x\\_"
            "の参加がキャンセルされました。",
        )
        self.assertNotIn("@everyone", detail)
        self.assert_allowed_mentions_none(cancellation["allowed_mentions"])

    async def test_cancellation_notice_retries_once_with_same_participant(self):
        await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")
        self.harness.channel.send.reset_mock()
        self.harness.humans[6].display_name = "Alice"
        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        retry_sleeper = ControlledSleeper()
        self.harness.cog._retry_sleep = retry_sleeper
        response = Mock(status=500, reason="Server Error")
        failure = discord.HTTPException(
            response,
            {"message": "temporary", "code": 0},
        )
        attempts = 0

        async def fail_first_notice(*, content, allowed_mentions):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise failure
            return await self.harness._send_message(
                content=content,
                allowed_mentions=allowed_mentions,
            )

        self.harness.channel.send.side_effect = fail_first_notice
        grace_sleep, task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )
        grace_sleep.future.set_result(None)
        await retry_sleeper.wait_for_calls(1)

        self.assertEqual(attempts, 1)
        self.assertFalse(task.done())
        self.assertEqual(len(retry_sleeper.calls), 1)
        self.assertEqual(retry_sleeper.calls[0].delay, 2)

        retry_sleeper.release()
        await task

        self.assertEqual(attempts, 2)
        self.assertEqual(self.harness.channel.send.await_count, 2)
        cancellation = self.harness.channel.send.await_args.kwargs
        self.assertIn(
            "Aliceの参加がキャンセルされました。",
            cancellation["content"],
        )
        self.assert_allowed_mentions_none(cancellation["allowed_mentions"])

    async def test_remove_changes_start_time_and_replaces_notification(self):
        old_notification = await self.announce(
            {
                "20:00": {2},
                "21:00": {3},
                "22:00": {4},
                "23:00": {5, 6},
                "24:00": {7},
            },
            "23:00",
        )
        self.harness.channel.send.reset_mock()
        self.harness.poll_message.edit.reset_mock()
        self.harness.set_voters(
            {
                "20:00": {2},
                "21:00": {3},
                "22:00": {4},
                "23:00": {5},
                "24:00": {7},
            }
        )

        sleep_call, task = await self.queue_check("remove", emoji="7️⃣")
        await self.finish_check(sleep_call, task)

        old_notification.edit.assert_awaited_once()
        old_content = old_notification.edit.await_args.kwargs["content"]
        self.assertIn("~~23:00 開始 <@&88>~~", old_content)
        self.assertIn("24:00 開始へ変更", old_content)
        self.harness.channel.send.assert_awaited_once()
        replacement = self.harness.channel.send.await_args.kwargs
        self.assertIn("24:00 開始 <@&88>", replacement["content"])
        self.assertIn("23:00 開始から変更", replacement["content"])
        self.assertFalse(replacement["allowed_mentions"].roles)
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "24:00")
        self.assertEqual(announcement.message_id, 1001)

    async def test_deleted_target_role_does_not_leave_old_start_time_active(self):
        old_notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )
        self.harness.channel.send.reset_mock()
        self.harness.poll_message.edit.reset_mock()
        self.harness.poll_message.role_mentions = []
        self.harness.poll_message.guild = SimpleNamespace(
            get_role=lambda role_id: None
        )
        self.harness.set_voters(
            {
                "20:00": {2, 3, 4, 5},
                "21:00": {6},
            }
        )

        sleep_call, task = await self.queue_check("remove", emoji="1️⃣")
        await self.finish_check(sleep_call, task)

        self.assertIn("~~20:00 開始 <@&88>~~", old_notification.content)
        self.assertIn("21:00 開始へ変更", old_notification.content)
        replacement = self.harness.channel.send.await_args.kwargs
        self.assertIn("21:00 開始 <@&88>", replacement["content"])
        self.assertFalse(replacement["allowed_mentions"].roles)
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "21:00")
        self.assertEqual(announcement.message_id, 1001)

    async def test_vote_change_with_same_start_time_is_noop(self):
        old_notification = await self.announce(
            {
                "20:00": {2},
                "21:00": {3},
                "22:00": {4},
                "23:00": {5, 6},
                "24:00": {6, 7},
            },
            "23:00",
        )
        self.harness.channel.send.reset_mock()
        self.harness.poll_message.edit.reset_mock()
        self.harness.set_voters(
            {
                "20:00": {2},
                "21:00": {3},
                "22:00": {4},
                "23:00": {5, 6},
                "24:00": {6},
            }
        )

        sleep_call, task = await self.queue_check("remove", emoji="8️⃣")
        await self.finish_check(sleep_call, task)

        old_notification.edit.assert_not_awaited()
        self.harness.channel.send.assert_not_awaited()
        self.harness.poll_message.edit.assert_not_awaited()
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "23:00")
        self.assertEqual(announcement.message_id, old_notification.id)

    async def test_clear_emoji_and_clear_all_events_are_debounced(self):
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        first_sleep, first_task = await self.queue_check(
            "clear_emoji",
            emoji="5️⃣",
        )

        self.harness.channel.send.assert_not_awaited()
        await self.finish_check(first_sleep, first_task)
        old_notification = self.harness.notifications[1000]
        self.assertEqual(
            start_announcement(self.harness.poll_message.embeds[0]).start_time,
            "20:00",
        )

        self.harness.channel.send.reset_mock()
        self.harness.poll_message.reactions = []
        second_sleep, second_task = await self.queue_check("clear")
        old_notification.edit.assert_not_awaited()

        await self.finish_check(second_sleep, second_task)

        old_notification.edit.assert_awaited_once()
        self.assertIn(
            "~~20:00 開始 <@&88>~~",
            old_notification.edit.await_args.kwargs["content"],
        )
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))
        self.harness.channel.send.assert_awaited_once()
        self.assertEqual(
            [call.args[0] for call in self.harness.poll_message.add_reaction.await_args_list],
            list(DEFAULT_SCHEDULE_EMOJIS),
        )

    async def test_clear_ng_reseeds_all_options_without_start_notification(self):
        self.harness.set_voters({})
        self.harness.poll_message.reactions = [
            reaction
            for reaction in self.harness.poll_message.reactions
            if str(reaction.emoji) != "🆖"
        ]
        self.harness.poll_message.add_reaction.reset_mock()

        sleep_call, task = await self.queue_check(
            "clear_emoji",
            emoji="🆖",
        )

        self.assertEqual(sleep_call.delay, 10)
        self.harness.poll_message.add_reaction.assert_not_awaited()
        self.harness.channel.send.assert_not_awaited()

        await self.finish_check(sleep_call, task)

        self.harness.poll_message.add_reaction.assert_awaited_once_with("🆖")
        seeded_emojis = {
            str(reaction.emoji)
            for reaction in self.harness.poll_message.reactions
            if reaction.me
        }
        seeded_emojis.update(
            call.args[0]
            for call in self.harness.poll_message.add_reaction.await_args_list
        )
        self.assertEqual(seeded_emojis, set(DEFAULT_SCHEDULE_EMOJIS))
        self.harness.channel.send.assert_not_awaited()
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))

    async def test_cancelled_poll_can_recover_without_reping_role(self):
        await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")
        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        cancel_sleep, cancel_task = await self.queue_check("remove", emoji="1️⃣")
        await self.finish_check(cancel_sleep, cancel_task)
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))

        self.harness.channel.send.reset_mock()
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        restore_sleep, restore_task = await self.queue_check("add", emoji="1️⃣")
        await self.finish_check(restore_sleep, restore_task)

        restored = self.harness.channel.send.await_args.kwargs
        self.assertEqual(restored["content"], "20:00 開始 <@&88>")
        self.assertFalse(restored["allowed_mentions"].roles)
        self.assertEqual(
            start_announcement(self.harness.poll_message.embeds[0]).start_time,
            "20:00",
        )

    async def test_deleted_start_notification_is_recreated_without_reping(self):
        notification = await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")
        self.harness.notifications.pop(notification.id)
        self.harness.channel.send.reset_mock()
        call_index = len(self.sleeper.calls)

        await self.harness.cog.on_raw_message_delete(
            SimpleNamespace(
                message_id=notification.id,
                channel_id=self.harness.CHANNEL_ID,
            )
        )
        await self.sleeper.wait_for_calls(call_index + 1)
        task = self.harness.cog._auto_start_tasks[self.harness.POLL_MESSAGE_ID]
        await self.finish_check(self.sleeper.calls[call_index], task)

        replacement = self.harness.channel.send.await_args.kwargs
        self.assertEqual(replacement["content"], "20:00 開始 <@&88>")
        self.assertFalse(replacement["allowed_mentions"].roles)
        self.assertNotEqual(
            start_announcement(self.harness.poll_message.embeds[0]).message_id,
            notification.id,
        )

    async def test_deleted_new_notification_during_poll_edit_requeues_check(self):
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()
        original_edit = self.harness._edit_poll

        async def blocked_edit(*, embed):
            edit_started.set()
            await release_edit.wait()
            return await original_edit(embed=embed)

        self.harness.poll_message.edit.side_effect = blocked_edit
        sleep_call, transition_task = await self.queue_check(
            "add",
            emoji="1️⃣",
        )
        sleep_call.future.set_result(None)
        await edit_started.wait()
        notification = self.harness.notifications.pop(1000)

        await self.harness.cog.on_raw_message_delete(
            SimpleNamespace(
                message_id=notification.id,
                channel_id=self.harness.CHANNEL_ID,
            )
        )
        await self.sleeper.wait_for_calls(2)
        pending_check = self.harness.cog._auto_start_tasks[
            self.harness.POLL_MESSAGE_ID
        ]
        release_edit.set()

        with self.assertRaises(asyncio.CancelledError):
            await transition_task
        self.assertFalse(pending_check.done())
        self.assertEqual(self.sleeper.calls[1].delay, 10)
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))
        self.assertEqual(self.harness.poll_message.edit.await_count, 2)
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._poll_notification_ids,
        )
        self.assertNotIn(
            notification.id,
            self.harness.cog._notification_poll_refs,
        )
        notification.delete.assert_not_awaited()

        await self.finish_check(self.sleeper.calls[1], pending_check)

        replacement = self.harness.notifications[1001]
        self.assertEqual(replacement.content, "20:00 開始 <@&88>")
        self.assert_allowed_mentions_none(replacement.allowed_mentions)
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "20:00")
        self.assertEqual(announcement.message_id, replacement.id)

    async def test_deleted_poll_cancels_active_notification(self):
        notification = await self.announce({"20:00": {2, 3, 4, 5, 6}}, "20:00")

        await self.harness.cog.on_raw_message_delete(
            SimpleNamespace(
                message_id=self.harness.POLL_MESSAGE_ID,
                channel_id=self.harness.CHANNEL_ID,
            )
        )

        self.assertIn("~~20:00 開始 <@&88>~~", notification.content)
        self.assertIn("投票が削除", notification.content)
        self.assertEqual(self.registry.all(), [])

    async def test_deleted_poll_wins_against_in_progress_transition(self):
        old_notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )
        self.harness.channel.send.reset_mock()
        self.harness.set_voters(
            {
                "20:00": {2, 3, 4, 5},
                "21:00": {6},
            }
        )
        transition_edit_started = asyncio.Event()
        release_transition_edit = asyncio.Event()
        response = Mock(status=404, reason="Not Found")

        async def fail_poll_edit_after_delete(*, embed):
            transition_edit_started.set()
            await release_transition_edit.wait()
            raise discord.NotFound(
                response,
                {"message": "Unknown Message", "code": 10008},
            )

        self.harness.poll_message.edit.side_effect = fail_poll_edit_after_delete
        sleep_call, transition_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
        )
        sleep_call.future.set_result(None)
        await transition_edit_started.wait()

        deletion_invalidated_transition = asyncio.Event()
        original_invalidate = self.harness.cog._invalidate_auto_start_check

        def invalidate_and_signal(message_id):
            original_invalidate(message_id)
            deletion_invalidated_transition.set()

        self.harness.cog._invalidate_auto_start_check = invalidate_and_signal
        deletion_task = asyncio.create_task(
            self.harness.cog.on_raw_message_delete(
                SimpleNamespace(
                    message_id=self.harness.POLL_MESSAGE_ID,
                    channel_id=self.harness.CHANNEL_ID,
                )
            )
        )
        await deletion_invalidated_transition.wait()
        release_transition_edit.set()

        with self.assertRaises(asyncio.CancelledError):
            await transition_task
        await deletion_task

        self.assertIn("~~20:00 開始 <@&88>~~", old_notification.content)
        self.assertIn("投票が削除", old_notification.content)
        replacement = self.harness.notifications[1001]
        replacement.delete.assert_awaited_once()
        self.assertEqual(self.registry.all(), [])
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._registered_schedule_ids,
        )

    async def test_unregistered_message_reaction_is_ignored(self):
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=self.harness.CHANNEL_ID,
            message_id=999,
            user_id=2,
            emoji="1️⃣",
        )

        await self.harness.cog.on_raw_reaction_add(payload)
        await asyncio.sleep(0)

        self.assertNotIn(999, self.harness.cog._auto_start_tasks)

    async def test_temporary_send_failure_retries_after_another_grace_period(self):
        response = Mock(status=500, reason="Server Error")
        failure = discord.HTTPException(
            response,
            {"message": "temporary", "code": 0},
        )
        attempts = 0

        async def flaky_send(*, content, allowed_mentions):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise failure
            return await self.harness._send_message(
                content=content,
                allowed_mentions=allowed_mentions,
            )

        self.harness.channel.send.side_effect = flaky_send
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        first_sleep, first_task = await self.queue_check()
        await self.finish_check(first_sleep, first_task)
        await self.sleeper.wait_for_calls(2)

        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))
        retry_task = self.harness.cog._auto_start_tasks[
            self.harness.POLL_MESSAGE_ID
        ]
        await self.finish_check(self.sleeper.calls[1], retry_task)

        self.assertEqual(attempts, 2)
        self.assertEqual(
            start_announcement(self.harness.poll_message.embeds[0]).start_time,
            "20:00",
        )

    async def test_poll_edit_failure_retries_without_reping_role(self):
        response = Mock(status=500, reason="Server Error")
        failure = discord.HTTPException(
            response,
            {"message": "temporary", "code": 0},
        )
        edit_attempts = 0

        async def fail_first_edit(*, embed):
            nonlocal edit_attempts
            edit_attempts += 1
            if edit_attempts == 1:
                raise failure
            return await self.harness._edit_poll(embed=embed)

        self.harness.poll_message.edit.side_effect = fail_first_edit
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        first_sleep, first_task = await self.queue_check()
        await self.finish_check(first_sleep, first_task)
        await self.sleeper.wait_for_calls(2)

        first_send = self.harness.channel.send.await_args_list[0].kwargs
        self.assertEqual(first_send["allowed_mentions"].roles, [self.harness.role])
        first_notification = self.harness.notifications[1000]
        first_notification.delete.assert_awaited_once()
        self.assertIsNone(start_announcement(self.harness.poll_message.embeds[0]))

        retry_task = self.harness.cog._auto_start_tasks[
            self.harness.POLL_MESSAGE_ID
        ]
        await self.finish_check(self.sleeper.calls[1], retry_task)

        self.assertEqual(self.harness.channel.send.await_count, 2)
        retry_send = self.harness.channel.send.await_args_list[1].kwargs
        self.assert_allowed_mentions_none(retry_send["allowed_mentions"])
        replacement = self.harness.notifications[1001]
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "20:00")
        self.assertEqual(announcement.message_id, replacement.id)
        self.assertEqual(edit_attempts, 3)

    async def test_schedule_update_cancels_pending_notification(self):
        permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        self.harness.channel.permissions_for = lambda _: permissions
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        sleep_call, pending_task = await self.queue_check()
        ctx = SimpleNamespace(
            bot=self.harness.bot,
            guild=SimpleNamespace(me=self.harness.bot_user),
            channel=self.harness.channel,
            author=self.harness.creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_update.callback(
            self.harness.cog,
            ctx,
            str(self.harness.POLL_MESSAGE_ID),
            options="平日 休日 NG",
        )

        with self.assertRaises(asyncio.CancelledError):
            await pending_task
        self.assertTrue(sleep_call.future.cancelled())
        self.assertNotIn(
            self.harness.POLL_MESSAGE_ID,
            self.harness.cog._auto_start_tasks,
        )
        self.harness.channel.send.assert_not_awaited()
        self.assertFalse(is_auto_start_schedule(self.harness.poll_message.embeds[0]))
        self.assertIn("投票をリセット", ctx.send.await_args.args[0])

    async def test_rejected_update_does_not_cancel_pending_notification(self):
        bot_permissions = SimpleNamespace(
            manage_messages=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        user_permissions = SimpleNamespace(manage_messages=False)

        def permissions_for(member):
            return (
                bot_permissions
                if member is self.harness.bot_user
                else user_permissions
            )

        self.harness.channel.permissions_for = permissions_for
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        sleep_call, pending_task = await self.queue_check()
        ctx = SimpleNamespace(
            bot=self.harness.bot,
            guild=SimpleNamespace(id=1, me=self.harness.bot_user),
            channel=self.harness.channel,
            author=SimpleNamespace(id=999, display_name="other"),
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_update.callback(
            self.harness.cog,
            ctx,
            str(self.harness.POLL_MESSAGE_ID),
            options="22:00 23:00 NG",
        )

        self.assertFalse(pending_task.cancelled())
        self.assertIn("作成者", ctx.send.await_args.args[0])
        await self.finish_check(sleep_call, pending_task)
        self.harness.channel.send.assert_awaited_once()

    async def test_schedule_close_disables_poll_and_cancels_active_notification(self):
        old_notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )
        permissions = SimpleNamespace(
            manage_messages=True,
            read_message_history=True,
            embed_links=True,
        )
        self.harness.channel.permissions_for = lambda _: permissions
        ctx = SimpleNamespace(
            bot=self.harness.bot,
            guild=SimpleNamespace(id=1, me=self.harness.bot_user),
            channel=self.harness.channel,
            author=self.harness.creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_close.callback(
            self.harness.cog,
            ctx,
            str(self.harness.POLL_MESSAGE_ID),
        )

        embed = self.harness.poll_message.embeds[0]
        self.assertFalse(is_auto_start_schedule(embed))
        self.assertTrue(embed.title.endswith("（終了）"))
        self.assertIn("投票終了", embed.footer.text)
        self.assertIn("~~20:00 開始 <@&88>~~", old_notification.content)
        self.assertIn("投票が終了", old_notification.content)
        self.assertEqual(self.registry.all(), [])
        self.assertIn("投票を終了", ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
