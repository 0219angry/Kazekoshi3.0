import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands

from kazekoshi.cogs.poll import (
    DEFAULT_SCHEDULE_OPTIONS,
    DEFAULT_SCHEDULE_OPTION_LIST,
    EMOJI_NUMBERS,
    PollCog,
    ScheduleInputError,
    announced_start_time,
    build_schedule_embed,
    choose_start_time,
    format_schedule_options,
    is_auto_start_schedule,
    is_schedule_channel,
    parse_message_id,
    parse_schedule_options,
    schedule_author_id,
)


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


class ScheduleParsingTests(unittest.TestCase):
    def test_parse_space_separated_options(self):
        self.assertEqual(
            parse_schedule_options("21 22 23 ng"),
            ["21", "22", "23", "ng"],
        )

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
            parse_schedule_options(f"{'x' * 101} ng")

    def test_format_uses_number_reactions_in_order(self):
        self.assertEqual(
            format_schedule_options(["21", "22", "ng"]),
            "1️⃣：21\n2️⃣：22\n3️⃣：ng",
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
            "20": set(),
            "21": {5},
            "22": {4},
            "23": {2, 3},
            "24": {1, 2},
            "ng": {6, 7, 8, 9, 10},
        }

        self.assertEqual(choose_start_time(voters), "24")
        self.assertIsNone(
            choose_start_time({"24": {1, 2}, "23": {1, 2}, "ng": {3, 4, 5}})
        )


class ScheduleDisplayTests(unittest.TestCase):
    def test_embed_contains_options_and_creator_marker(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)

        embed = build_schedule_embed(role, ["21", "22", "ng"], author)

        self.assertEqual(embed.title, "📅 VALORANT 開始時間")
        self.assertEqual(embed.description, "1️⃣：21\n2️⃣：22\n3️⃣：ng")
        self.assertEqual(schedule_author_id(embed), 987)

    def test_non_schedule_embed_has_no_creator(self):
        self.assertIsNone(schedule_author_id(discord.Embed(title="other")))

    def test_only_omitted_default_poll_has_auto_start_marker(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        default_embed = build_schedule_embed(
            role,
            list(DEFAULT_SCHEDULE_OPTION_LIST),
            author,
            auto_start=True,
        )
        custom_embed = build_schedule_embed(
            role,
            list(DEFAULT_SCHEDULE_OPTION_LIST),
            author,
        )

        self.assertTrue(is_auto_start_schedule(default_embed))
        self.assertIsNone(announced_start_time(default_embed))
        self.assertFalse(is_auto_start_schedule(custom_embed))

    def test_creator_id_cannot_be_spoofed_by_display_name(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(
            display_name="fake | 作成者ID: 123 | name",
            id=987,
        )
        embed = build_schedule_embed(role, ["21", "ng"], author)

        self.assertEqual(schedule_author_id(embed), 987)

    def test_channel_name_id_and_parent_thread_are_supported(self):
        named_ctx = SimpleNamespace(
            channel=SimpleNamespace(id=1, name="VaLoRaNt", parent=None),
            bot=SimpleNamespace(valorant_channel_id=0),
        )
        configured_ctx = SimpleNamespace(
            channel=SimpleNamespace(id=222, name="other", parent=None),
            bot=SimpleNamespace(valorant_channel_id=222),
        )
        thread = Mock(spec=discord.Thread)
        thread.id = 333
        thread.name = "schedule-thread"
        thread.parent = SimpleNamespace(id=222, name="valorant")
        thread_ctx = SimpleNamespace(
            channel=thread,
            bot=SimpleNamespace(valorant_channel_id=222),
        )
        category_child_ctx = SimpleNamespace(
            channel=SimpleNamespace(
                id=444,
                name="general",
                parent=SimpleNamespace(id=555, name="valorant"),
            ),
            bot=SimpleNamespace(valorant_channel_id=0),
        )
        wrong_parent_thread = Mock(spec=discord.Thread)
        wrong_parent_thread.id = 666
        wrong_parent_thread.name = "valorant"
        wrong_parent_thread.parent = SimpleNamespace(id=777, name="general")
        wrong_parent_thread_ctx = SimpleNamespace(
            channel=wrong_parent_thread,
            bot=SimpleNamespace(valorant_channel_id=0),
        )

        self.assertTrue(is_schedule_channel(named_ctx))
        self.assertTrue(is_schedule_channel(configured_ctx))
        self.assertTrue(is_schedule_channel(thread_ctx))
        self.assertFalse(is_schedule_channel(category_child_ctx))
        self.assertFalse(is_schedule_channel(wrong_parent_thread_ctx))

    def test_hybrid_group_exposes_add_and_update_slash_subcommands(self):
        self.assertIsInstance(PollCog.schedule, commands.HybridGroup)
        application_commands = PollCog.schedule.app_command.commands
        self.assertEqual([command.name for command in application_commands], ["add", "update"])
        add_command = application_commands[0]
        options_parameter = next(
            parameter for parameter in add_command.parameters if parameter.name == "options"
        )
        self.assertFalse(options_parameter.required)


class ScheduleCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_registers_schedule_application_group(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        try:
            await bot.add_cog(PollCog(bot))
            schedule_group = bot.tree.get_command("schedule")
            self.assertIsNotNone(schedule_group)
            self.assertEqual(
                [command.name for command in schedule_group.commands],
                ["add", "update"],
            )
        finally:
            await bot.close()

    async def test_add_posts_embed_and_number_reactions(self):
        bot = SimpleNamespace(valorant_channel_id=0)
        cog = PollCog(bot)
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
        author = SimpleNamespace(id=77, display_name="tester")
        role = SimpleNamespace(
            id=88,
            name="VALORANT",
            mention="<@&88>",
            mentionable=True,
            is_default=lambda: False,
        )
        poll_message = SimpleNamespace(id=99, add_reaction=AsyncMock())
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(me=SimpleNamespace(id=1)),
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
        self.assertTrue(is_auto_start_schedule(sent["embed"]))
        self.assertEqual(
            sent["embed"].description,
            "1️⃣：20\n2️⃣：21\n3️⃣：22\n4️⃣：23\n5️⃣：24\n6️⃣：ng",
        )
        self.assertEqual(
            [call.args[0] for call in poll_message.add_reaction.await_args_list],
            EMOJI_NUMBERS[:6],
        )
        self.assertEqual(DEFAULT_SCHEDULE_OPTIONS, "20 21 22 23 24 ng")

        ctx.send.reset_mock()
        await PollCog.schedule_add.callback(
            cog,
            ctx,
            role,
            options=DEFAULT_SCHEDULE_OPTIONS,
        )
        self.assertFalse(is_auto_start_schedule(ctx.send.await_args.kwargs["embed"]))

    async def test_add_checks_reaction_permission_before_posting(self):
        bot_member = SimpleNamespace(id=1)
        permissions = SimpleNamespace(
            send_messages=True,
            send_messages_in_threads=True,
            embed_links=True,
            read_message_history=True,
            add_reactions=False,
        )
        bot = SimpleNamespace(valorant_channel_id=0)
        cog = PollCog(bot)
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
        bot = SimpleNamespace(valorant_channel_id=0, user=bot_user)
        cog = PollCog(bot)
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
            guild=SimpleNamespace(me=SimpleNamespace(id=1)),
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
            options="22 23 ng",
        )

        self.assertEqual(
            [call.args[0] for call in poll_message.clear_reaction.await_args_list],
            EMOJI_NUMBERS[:2],
        )
        edited_embed = poll_message.edit.await_args.kwargs["embed"]
        self.assertEqual(edited_embed.description, "1️⃣：22\n2️⃣：23\n3️⃣：ng")
        self.assertFalse(is_auto_start_schedule(edited_embed))
        self.assertIsNone(announced_start_time(edited_embed))
        self.assertEqual(
            [call.args[0] for call in poll_message.add_reaction.await_args_list],
            EMOJI_NUMBERS[:3],
        )
        self.assertIn("投票をリセット", ctx.send.await_args.args[0])

    async def test_prefix_add_requires_an_actual_role_mention(self):
        bot = SimpleNamespace(valorant_channel_id=0)
        cog = PollCog(bot)
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
        bot = SimpleNamespace(valorant_channel_id=0, user=bot_user)
        cog = PollCog(bot)
        creator = SimpleNamespace(id=77, display_name="creator")
        requester = SimpleNamespace(id=66, display_name="requester")
        role = SimpleNamespace(name="VALORANT")
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[build_schedule_embed(role, ["21", "22"], creator)],
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
            options="22 23 ng",
        )

        poll_message.clear_reaction.assert_not_awaited()
        self.assertIn("作成者", ctx.send.await_args.args[0])

    async def test_default_poll_announces_once_at_five_distinct_voters(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        other_bot = SimpleNamespace(id=99, bot=True)
        humans = {
            user_id: SimpleNamespace(id=user_id, bot=False)
            for user_id in range(2, 12)
        }
        role = SimpleNamespace(id=88, name="VALORANT", mention="<@&88>")
        author = SimpleNamespace(id=77, display_name="creator")
        embed = build_schedule_embed(
            role,
            list(DEFAULT_SCHEDULE_OPTION_LIST),
            author,
            auto_start=True,
        )
        reactions = [
            FakeReaction("6️⃣", list(humans.values())),
            FakeReaction("2️⃣", [humans[6]]),
            FakeReaction("5️⃣", [bot_user, other_bot, humans[2], humans[3]]),
            FakeReaction("4️⃣", [humans[3], humans[4]]),
            FakeReaction("3️⃣", [humans[5]]),
            FakeReaction("1️⃣", [bot_user]),
        ]
        poll_message = SimpleNamespace(
            id=123,
            author=bot_user,
            embeds=[embed],
            reactions=reactions,
            role_mentions=[role],
            content=role.mention,
            guild=SimpleNamespace(get_role=lambda _: role),
            edit=AsyncMock(),
        )

        async def save_edited_embed(*, embed):
            poll_message.embeds = [embed]
            return poll_message

        poll_message.edit.side_effect = save_edited_embed
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=poll_message),
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            user=bot_user,
            get_channel=lambda _: channel,
        )
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=10,
            message_id=123,
            user_id=humans[6].id,
            emoji="2️⃣",
        )

        await PollCog(bot).on_raw_reaction_add(payload)
        # Cogを作り直しても、footerの永続マーカーにより再通知しない。
        await PollCog(bot).on_raw_reaction_add(payload)

        poll_message.edit.assert_awaited_once()
        self.assertEqual(announced_start_time(poll_message.embeds[0]), "24")
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.kwargs["content"], "24時開始 <@&88>")
        self.assertEqual(channel.send.await_args.kwargs["allowed_mentions"].roles, [role])


if __name__ == "__main__":
    unittest.main()
