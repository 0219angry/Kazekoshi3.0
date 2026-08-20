import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView

from kazekoshi.cogs.poll import (
    AUTO_START_GRACE_SECONDS,
    DEFAULT_SCHEDULE_EMOJIS,
    DEFAULT_SCHEDULE_OPTIONS,
    DEFAULT_SCHEDULE_OPTION_LIST,
    EMOJI_NUMBERS,
    LATENESS_REACTION_GRACE_SECONDS,
    OPTION_EMOJIS,
    PollCog,
    ScheduleEffectConfig,
    ScheduleInputError,
    SchedulePollRegistry,
    ScheduleTarget,
    announced_start_time,
    auto_start_minimum,
    build_schedule_embed,
    build_schedule_status_embed,
    choose_start_time,
    format_schedule_deadline,
    format_schedule_options,
    is_auto_start_schedule,
    is_schedule_closed,
    load_schedule_effect_config,
    mark_schedule_closed,
    mark_start_time_announced,
    merge_schedule_options,
    normalize_schedule_time,
    parse_schedule_add_options,
    parse_schedule_date,
    parse_schedule_deadline,
    parse_message_id,
    parse_schedule_option_additions,
    parse_schedule_options,
    parse_schedule_effect_user_ids,
    resolve_schedule_target,
    schedule_author_id,
    schedule_date_override,
    schedule_decided_start_time,
    schedule_deadline_at,
    schedule_event_date,
    schedule_effect_frames,
    schedule_effect_for_roll,
    schedule_option_emojis,
    schedule_options_from_embed,
    schedule_related_notification_id,
    set_auto_start_minimum,
    set_schedule_decision,
    set_schedule_date_override,
    set_schedule_deadline,
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
    def test_schedule_effect_roll_probabilities_use_sixteen_outcomes(self):
        outcomes = [schedule_effect_for_roll(roll) for roll in range(1, 17)]
        self.assertEqual(outcomes.count("rush"), 1)
        self.assertEqual(outcomes.count("chance"), 2)
        self.assertEqual(outcomes.count("miss"), 5)
        self.assertEqual(outcomes.count(None), 8)

    def test_schedule_effect_user_ids_accept_spaces_commas_and_duplicates(self):
        self.assertEqual(
            parse_schedule_effect_user_ids("123, 456\n123 789"),
            frozenset({123, 456, 789}),
        )
        with self.assertRaisesRegex(ValueError, "invalid Discord user ID"):
            parse_schedule_effect_user_ids("123, player")

    def test_load_schedule_effect_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.ini"
            config_path.write_text(
                "[SCHEDULE_EFFECT]\n"
                "ENABLED = true\n"
                "USER_IDS = 123, 456\n"
                "DELETE_AFTER_SECONDS = 8\n",
                encoding="UTF-8",
            )

            config = load_schedule_effect_config(config_path)

        self.assertTrue(config.enabled)
        self.assertEqual(config.user_ids, frozenset({123, 456}))
        self.assertEqual(config.delete_after_seconds, 8)

    def test_schedule_effect_frames_are_fixed_for_message_edits(self):
        self.assertEqual(
            schedule_effect_frames("rush", user_id=123),
            (
                "🔴 先バレ <@123>",
                "🔴 先バレ <@123>\n\nﾌﾟﾁｭﾝ……",
                "🌈 ７ ７ ７ 🌈\n**風越RUSH突入!!**\n<@123> 参戦決定!!",
            ),
        )
        self.assertEqual(
            schedule_effect_frames("rush", user_id=123, start_time="20:00"),
            (
                "🔴 先バレ <@123>",
                "🔴 先バレ <@123>\n\nﾌﾟﾁｭﾝ……",
                "🌈 ７ ７ ７ 🌈\n**風越RUSH突入!!**\n20:00 開始",
            ),
        )
        self.assertEqual(
            schedule_effect_frames("chance", user_id=123),
            (
                "🟡 保留変化 <@123>",
                "🟠 チャンス……？ <@123>",
                "✨ <@123> 参戦決定!!",
            ),
        )
        self.assertEqual(
            schedule_effect_frames("miss", user_id=123),
            (
                "⚪ 通常保留 <@123>",
                "⚪ 通常保留 <@123>\n\n……",
                "💨 ハズレ <@123>",
            ),
        )

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

    def test_add_options_support_trailing_minimum(self):
        self.assertEqual(
            parse_schedule_add_options("15 16 17 NG [3]"),
            (["15:00", "16:00", "17:00", "NG"], 3),
        )
        self.assertEqual(
            parse_schedule_add_options("[3]"),
            (list(DEFAULT_SCHEDULE_OPTION_LIST), 3),
        )
        self.assertEqual(
            parse_schedule_add_options("15 16 17"),
            (["15:00", "16:00", "17:00"], 5),
        )

    def test_add_options_reject_invalid_or_non_time_minimum(self):
        with self.assertRaisesRegex(ScheduleInputError, "1〜999人"):
            parse_schedule_add_options("15 16 [0]")
        with self.assertRaisesRegex(ScheduleInputError, "時刻形式"):
            parse_schedule_add_options("平日 休日 [3]")

    def test_parse_quoted_option(self):
        self.assertEqual(
            parse_schedule_options('"8/21 21時" "8/22 22時" ng'),
            ["8/21 21時", "8/22 22時", "ng"],
        )

    def test_parse_and_merge_option_additions_preserves_terminal_ng(self):
        existing = ["21:00", "22:00", "NG"]

        additions = parse_schedule_option_additions('23 2330 "8/23 24時"')
        self.assertEqual(additions, ["23", "2330", "8/23 24時"])
        self.assertEqual(parse_schedule_option_additions("24"), ["24"])

        merged = merge_schedule_options(
            existing,
            additions[:2],
            auto_start_enabled=True,
        )

        self.assertEqual(
            merged,
            ["21:00", "22:00", "23:00", "23:30", "NG"],
        )
        self.assertEqual(existing, ["21:00", "22:00", "NG"])
        self.assertEqual(
            schedule_option_emojis(merged)[:2],
            schedule_option_emojis(existing)[:2],
        )
        self.assertEqual(schedule_option_emojis(merged)[-1], "🆖")

    def test_merge_option_additions_rejects_duplicates_ng_errors_and_limit(self):
        cases = (
            (
                ["21:00", "22:00", "NG"],
                ["2100"],
                True,
                "同じ候補",
            ),
            (
                ["21:00", "22:00", "NG"],
                ["23", "2300"],
                True,
                "同じ候補",
            ),
            (
                ["21:00", "22:00", "NG"],
                ["ng"],
                True,
                "すでに候補",
            ),
            (
                ["21:00", "22:00"],
                ["NG", "23"],
                True,
                "NGは追加候補の末尾",
            ),
            (
                [*[f"候補{index}" for index in range(20)]],
                ["追加"],
                False,
                "最大20個",
            ),
            (
                ["21:00", "22:00", "NG"],
                ["平日"],
                True,
                "自動開始の投票",
            ),
        )

        for existing, additions, auto_start, expected_message in cases:
            with self.subTest(
                existing=existing,
                additions=additions,
                auto_start=auto_start,
            ):
                original = list(existing)
                with self.assertRaisesRegex(
                    ScheduleInputError,
                    expected_message,
                ):
                    merge_schedule_options(
                        existing,
                        additions,
                        auto_start_enabled=auto_start,
                    )
                self.assertEqual(existing, original)

        with self.assertRaisesRegex(ScheduleInputError, "1つ以上"):
            parse_schedule_option_additions("")
        with self.assertRaisesRegex(ScheduleInputError, "100文字以内"):
            parse_schedule_option_additions("x" * 101)

    def test_parse_rejects_too_few_or_too_many_options(self):
        with self.assertRaisesRegex(ScheduleInputError, "2つ以上"):
            parse_schedule_options("21")
        self.assertEqual(
            len(parse_schedule_options(" ".join(str(index) for index in range(20)))),
            20,
        )
        with self.assertRaisesRegex(ScheduleInputError, "最大20個"):
            parse_schedule_options(" ".join(str(index) for index in range(21)))

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

    def test_reactions_continue_with_letters_after_nine(self):
        options = [f"候補{index}" for index in range(1, 12)]

        self.assertEqual(
            schedule_option_emojis(options),
            [*EMOJI_NUMBERS, "🇦", "🇧"],
        )
        self.assertEqual(schedule_option_emojis(options), OPTION_EMOJIS[:11])

    def test_twentieth_ng_uses_ng_reaction(self):
        options = [*[f"候補{index}" for index in range(1, 20)], "NG"]

        self.assertEqual(schedule_option_emojis(options)[-2:], ["🇯", "🆖"])

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

    def test_parse_deadline_uses_japan_time_and_supports_clear(self):
        expected = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)

        self.assertEqual(
            parse_schedule_deadline("2026-08-14 19:00", now=FIXED_NOW),
            expected,
        )
        self.assertEqual(
            parse_schedule_deadline("2026/08/14 19:00", now=FIXED_NOW),
            expected,
        )
        self.assertIsNone(parse_schedule_deadline("解除", now=FIXED_NOW))
        self.assertIsNone(parse_schedule_deadline("CLEAR", now=FIXED_NOW))

    def test_parse_deadline_rejects_past_invalid_and_too_distant_values(self):
        with self.assertRaisesRegex(ScheduleInputError, "現在より後"):
            parse_schedule_deadline("2026-08-10 08:59", now=FIXED_NOW)
        with self.assertRaisesRegex(ScheduleInputError, "YYYY-MM-DD"):
            parse_schedule_deadline("tomorrow", now=FIXED_NOW)
        with self.assertRaisesRegex(ScheduleInputError, "90日以内"):
            parse_schedule_deadline("2026-11-09 09:01", now=FIXED_NOW)

    def test_parse_schedule_date_supports_japan_today_and_clear(self):
        posted_date = date(2026, 8, 10)
        for value in (
            "2026-08-14",
            "2026/08/14",
            "20260814",
            "08-14",
            "8-14",
            "0814",
            "14",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_schedule_date(
                        value,
                        now=FIXED_NOW,
                        posted_date=posted_date,
                    ),
                    date(2026, 8, 14),
                )
        self.assertEqual(
            parse_schedule_date("today", now=FIXED_NOW),
            date(2026, 8, 10),
        )
        self.assertIsNone(parse_schedule_date("clear", now=FIXED_NOW))
        with self.assertRaisesRegex(ScheduleInputError, "YYYY-MM-DD"):
            parse_schedule_date("tomorrow", now=FIXED_NOW)
        with self.assertRaisesRegex(ScheduleInputError, "前後90日以内"):
            parse_schedule_date("2026-11-09", now=FIXED_NOW)

    def test_partial_schedule_date_uses_post_year_and_month(self):
        now = datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)
        posted_date = date(2026, 12, 31)

        for value in ("12-30", "1230", "30"):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_schedule_date(
                        value,
                        now=now,
                        posted_date=posted_date,
                    ),
                    date(2026, 12, 30),
                )

    def test_compact_schedule_date_rejects_invalid_calendar_dates(self):
        with self.assertRaisesRegex(ScheduleInputError, "YYYYMMDD"):
            parse_schedule_date(
                "0230",
                now=FIXED_NOW,
                posted_date=date(2026, 8, 10),
            )
        with self.assertRaisesRegex(ScheduleInputError, "YYYYMMDD"):
            parse_schedule_date(
                "31",
                now=datetime(2026, 4, 1, tzinfo=timezone.utc),
                posted_date=date(2026, 4, 1),
            )

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

    def test_choose_start_time_uses_custom_minimum(self):
        voters = {
            "15:00": {1},
            "16:00": {2, 3},
            "17:00": {4},
        }

        self.assertIsNone(choose_start_time(voters))
        self.assertEqual(choose_start_time(voters, minimum=3), "16:00")


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
        self.assertEqual(auto_start_minimum(time_embed), 5)
        self.assertEqual(time_embed.title, "📅 VALORANT 開始時間 [5人]")
        self.assertEqual(
            time_embed.description,
            "1️⃣15:00, 2️⃣16:00, 3️⃣17:00, 🆖NG",
        )
        self.assertIsNone(announced_start_time(time_embed))
        self.assertFalse(is_auto_start_schedule(markerless_embed))
        self.assertFalse(is_auto_start_schedule(non_time_embed))

    def test_minimum_update_preserves_active_announcement(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        embed = build_schedule_embed(
            role,
            ["15:00", "16:00", "17:00", "NG"],
            author,
            auto_start=True,
        )
        mark_start_time_announced(embed, "16:00", 123456)

        self.assertTrue(set_auto_start_minimum(embed, 3))

        self.assertEqual(auto_start_minimum(embed), 3)
        self.assertEqual(embed.title, "📅 VALORANT 開始時間 [3人]")
        announcement = start_announcement(embed)
        self.assertEqual(announcement.start_time, "16:00")
        self.assertEqual(announcement.message_id, 123456)
        self.assertIn("初回通知済み", embed.footer.text)
        self.assertFalse(set_auto_start_minimum(embed, 3))

    def test_legacy_auto_start_footer_remains_readable(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        embed = build_schedule_embed(
            role,
            ["15:00", "16:00", "NG"],
            author,
            auto_start=True,
        )
        embed.set_footer(
            text=embed.footer.text.replace("人で開始判定", "人で自動開始判定")
        )

        self.assertTrue(is_auto_start_schedule(embed))
        self.assertEqual(auto_start_minimum(embed), 5)

    def test_status_shows_votes_distinct_totals_and_current_result(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        embed = build_schedule_embed(
            role,
            ["20:00", "20:30", "21:00", "NG"],
            author,
            auto_start=True,
            minimum=5,
        )

        status = build_schedule_status_embed(
            embed,
            {
                "20:00": {2, 3},
                "20:30": {3, 4},
                "21:00": {5, 6},
                "NG": {7, 8},
            },
        )

        self.assertIn("状態: 🟢 自動判定中", status.description)
        self.assertIn("最低人数: 5人", status.description)
        self.assertIn("現在の成立時刻: 21:00 開始", status.description)
        self.assertIn("1️⃣20:00: 2票（累計2人）", status.description)
        self.assertIn("2️⃣20:30: 2票（累計3人）", status.description)
        self.assertIn("3️⃣21:00: 2票（累計5人）", status.description)
        self.assertIn("🆖NG: 2票", status.description)

    def test_deadline_field_can_be_set_read_and_cleared(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        embed = build_schedule_embed(
            role,
            ["20:00", "21:00", "NG"],
            author,
            auto_start=True,
        )
        deadline_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)

        set_schedule_deadline(embed, deadline_at)

        self.assertEqual(schedule_deadline_at(embed), deadline_at)
        self.assertEqual(
            next(field.value for field in embed.fields if field.name == "締切"),
            format_schedule_deadline(deadline_at),
        )
        set_schedule_deadline(embed, None)
        self.assertIsNone(schedule_deadline_at(embed))

    def test_decision_field_is_normalized_and_status_marks_poll_decided(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        embed = build_schedule_embed(
            role,
            ["20:00", "21:00", "NG"],
            author,
            auto_start=True,
            minimum=3,
        )

        set_schedule_decision(embed, "2100")
        mark_schedule_closed(embed)
        embed.title += "（終了）"

        self.assertEqual(schedule_decided_start_time(embed), "21:00")
        status = build_schedule_status_embed(embed, {})
        self.assertIn("状態: ✅ 確定済み", status.description)
        self.assertIn("確定開始: 21:00 開始", status.description)
        set_schedule_decision(embed, None)
        self.assertIsNone(schedule_decided_start_time(embed))

    def test_schedule_date_override_replaces_message_date(self):
        role = SimpleNamespace(name="VALORANT")
        author = SimpleNamespace(display_name="tester", id=987)
        embed = build_schedule_embed(role, ["20:00", "21:00"], author)
        message = SimpleNamespace(
            id=schedule_snowflake(FIXED_NOW),
            created_at=FIXED_NOW,
            embeds=[embed],
        )

        self.assertEqual(schedule_event_date(message), date(2026, 8, 10))
        set_schedule_date_override(embed, date(2026, 8, 14))
        self.assertEqual(schedule_date_override(embed), date(2026, 8, 14))
        self.assertEqual(schedule_event_date(message), date(2026, 8, 14))
        set_schedule_date_override(embed, None)
        self.assertEqual(schedule_event_date(message), date(2026, 8, 10))

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
            [
                "add",
                "status",
                "clone",
                "date",
                "minimum",
                "deadline",
                "option-add",
                "update",
                "decide",
                "late",
                "lateoff",
                "close",
                "delete",
            ],
        )
        add_command = application_commands[0]
        target_parameter = next(
            parameter for parameter in add_command.parameters if parameter.name == "target"
        )
        self.assertTrue(target_parameter.required)
        self.assertNotIn(
            "role",
            [parameter.name for parameter in add_command.parameters],
        )
        options_parameter = next(
            parameter for parameter in add_command.parameters if parameter.name == "options"
        )
        self.assertFalse(options_parameter.required)
        option_add_command = next(
            command
            for command in application_commands
            if command.name == "option-add"
        )
        self.assertEqual(
            [parameter.name for parameter in option_add_command.parameters],
            ["message", "options"],
        )
        self.assertTrue(
            all(
                not parameter.required
                for parameter in option_add_command.parameters
            )
        )
        late_command = next(
            command
            for command in application_commands
            if command.name == "late"
        )
        period_parameter = next(
            parameter
            for parameter in late_command.parameters
            if parameter.name == "period"
        )
        self.assertFalse(period_parameter.required)
        for command_name in (
            "status",
            "clone",
            "date",
            "minimum",
            "deadline",
            "option-add",
            "update",
            "decide",
            "lateoff",
            "close",
            "delete",
        ):
            command = next(
                command
                for command in application_commands
                if command.name == command_name
            )
            message_parameter = next(
                parameter
                for parameter in command.parameters
                if parameter.name == "message"
            )
            self.assertFalse(message_parameter.required, command_name)


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

    def test_deadline_round_trips_and_regular_registration_preserves_it(self):
        message_id = schedule_snowflake(FIXED_NOW - timedelta(days=1))
        deadline_at = FIXED_NOW + timedelta(days=2)

        self.registry.set_deadline(
            guild_id=1,
            channel_id=10,
            message_id=message_id,
            deadline_at=deadline_at,
        )
        self.registry.register(
            guild_id=1,
            channel_id=11,
            message_id=message_id,
        )

        stored = self.registry.all()[0]
        self.assertEqual(stored.channel_id, 11)
        self.assertEqual(stored.deadline_at, deadline_at)
        self.registry.clear_deadline(message_id)
        self.assertIsNone(self.registry.all()[0].deadline_at)

    def test_schedule_effect_draw_and_jackpot_claims_survive_restart(self):
        message_id = schedule_snowflake(FIXED_NOW - timedelta(days=1))
        self.registry.register(
            guild_id=1,
            channel_id=10,
            message_id=message_id,
        )

        self.assertEqual(
            self.registry.claim_effect_draw(message_id, 123),
            (True, False),
        )
        restarted_registry = SchedulePollRegistry(self.registry.path)
        self.assertEqual(
            restarted_registry.claim_effect_draw(message_id, 123),
            (False, False),
        )
        self.assertTrue(restarted_registry.claim_effect_jackpot(message_id, 123))
        self.assertFalse(restarted_registry.claim_effect_jackpot(message_id, 123))
        self.assertEqual(
            restarted_registry.claim_effect_draw(message_id, 123),
            (False, True),
        )

        restarted_registry.unregister(message_id)
        self.assertEqual(
            restarted_registry.claim_effect_draw(message_id, 123),
            (True, False),
        )

    def test_prune_preserves_poll_with_deadline(self):
        cutoff_id = schedule_snowflake(FIXED_NOW - timedelta(days=90))
        old_id = schedule_snowflake(FIXED_NOW - timedelta(days=100))
        self.registry.set_deadline(
            guild_id=1,
            channel_id=10,
            message_id=old_id,
            deadline_at=FIXED_NOW + timedelta(days=1),
        )

        self.assertEqual(self.registry.prune_before(cutoff_id), [])
        self.assertEqual(self.registry.all()[0].message_id, old_id)

    def test_existing_registry_schema_is_migrated_for_deadlines(self):
        database_path = f"{self.temp_directory.name}/legacy-schedule-polls.sqlite3"
        message_id = schedule_snowflake(FIXED_NOW - timedelta(days=1))
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE schedule_polls (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO schedule_polls (message_id, guild_id, channel_id)
                VALUES (?, ?, ?)
                """,
                (message_id, 1, 10),
            )
        registry = SchedulePollRegistry(database_path)

        registry.set_deadline(
            guild_id=1,
            channel_id=10,
            message_id=message_id,
            deadline_at=FIXED_NOW + timedelta(days=1),
        )

        self.assertEqual(
            registry.all()[0].deadline_at,
            FIXED_NOW + timedelta(days=1),
        )


class ScheduleCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.registry = SchedulePollRegistry(
            f"{self.temp_directory.name}/schedule-polls.sqlite3"
        )

    def make_option_add_fixture(
        self,
        *,
        options=None,
        auto_start=True,
        registered=True,
    ):
        bot_member = SimpleNamespace(id=1, bot=True)
        bot = SimpleNamespace(user=bot_member)
        creator = SimpleNamespace(id=77, display_name="creator")
        role = SimpleNamespace(
            id=88,
            name="GAME",
            mention="<@&88>",
        )
        option_list = options or ["21:00", "22:00", "NG"]
        embed = build_schedule_embed(
            role,
            option_list,
            creator,
            auto_start=auto_start,
            minimum=3,
        )
        reactions = []
        humans = {
            user_id: SimpleNamespace(id=user_id, bot=False)
            for user_id in range(2, 8)
        }
        voters = {
            "21:00": {2, 3},
            "22:00": {4, 5},
            "NG": {6},
        }
        for option, emoji in zip(option_list, schedule_option_emojis(option_list)):
            users = [bot_member]
            users.extend(humans[user_id] for user_id in voters.get(option, set()))
            reaction = FakeReaction(emoji, users, me=True)
            reaction.count = len(users)
            reactions.append(reaction)

        poll_message = SimpleNamespace(
            id=99,
            author=bot_member,
            content=role.mention,
            role_mentions=[role],
            mentions=[],
            guild=SimpleNamespace(get_role=lambda role_id: role),
            embeds=[embed],
            reactions=reactions,
            jump_url="https://discord.com/channels/1/10/99",
            add_reaction=AsyncMock(),
            remove_reaction=AsyncMock(),
            clear_reaction=AsyncMock(),
            edit=AsyncMock(),
        )

        async def save_edit(*, embed):
            poll_message.embeds = [embed]
            return poll_message

        poll_message.edit.side_effect = save_edit
        channel = SimpleNamespace(
            id=10,
            permissions_for=lambda _: SimpleNamespace(manage_messages=False),
            fetch_message=AsyncMock(return_value=poll_message),
        )
        guild = SimpleNamespace(id=1, me=bot_member)
        ctx = SimpleNamespace(
            bot=bot,
            guild=guild,
            channel=channel,
            author=creator,
            interaction=object(),
            send=AsyncMock(),
        )
        cog = PollCog(bot, registry=self.registry)
        cog._lateness_registry.get_event = Mock(return_value=None)
        cog._queue_auto_start_check_by_id = Mock()
        if auto_start and registered:
            cog._register_schedule_poll(
                guild_id=guild.id,
                channel_id=channel.id,
                message_id=poll_message.id,
            )
        return cog, ctx, poll_message, bot_member

    async def test_cog_registers_schedule_application_group(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        try:
            await bot.add_cog(PollCog(bot, registry=self.registry))
            schedule_group = bot.tree.get_command("schedule")
            self.assertIsNotNone(schedule_group)
            self.assertEqual(
                [command.name for command in schedule_group.commands],
                [
                    "add",
                    "status",
                    "clone",
                    "date",
                    "minimum",
                    "deadline",
                    "option-add",
                    "update",
                    "decide",
                    "late",
                    "lateoff",
                    "close",
                    "delete",
                ],
            )
        finally:
            await bot.close()

    async def test_add_target_autocomplete_prioritizes_safe_exact_matches(self):
        roles = [
            SimpleNamespace(
                name="@everyone",
                mention="<@&1>",
                is_default=lambda: True,
            ),
            SimpleNamespace(
                name="Raid",
                mention="<@&88>",
                is_default=lambda: False,
            ),
        ]
        members = [
            SimpleNamespace(
                name=f"raid-member-{index:02d}",
                display_name=f"Raid member {index:02d}",
                mention=f"<@{100 + index}>",
            )
            for index in range(30)
        ]
        interaction = SimpleNamespace(
            guild=SimpleNamespace(roles=roles, members=members)
        )
        cog = PollCog(SimpleNamespace(), registry=self.registry)

        partial = await cog.schedule_add_target_autocomplete(interaction, "@e")
        self.assertEqual(partial[0].value, "@e")
        self.assertEqual(partial[0].name, "表示のみ: @e")
        self.assertIn("@everyone", [choice.value for choice in partial])

        broadcast = await cog.schedule_add_target_autocomplete(
            interaction,
            "@everyone",
        )
        self.assertEqual(broadcast[0].value, "@everyone")
        self.assertNotIn(
            "表示のみ: @everyone",
            [choice.name for choice in broadcast],
        )

        capitalized_label = await cog.schedule_add_target_autocomplete(
            interaction,
            "@Everyone",
        )
        self.assertEqual(capitalized_label[0].value, "@Everyone")
        self.assertNotIn(
            "@everyone",
            [choice.value for choice in capitalized_label],
        )

        exact_role = await cog.schedule_add_target_autocomplete(
            interaction,
            "@Raid",
        )
        self.assertEqual(exact_role[0].value, "<@&88>")
        self.assertLessEqual(len(exact_role), 25)

    async def test_discordpy_prefix_parser_passes_target_and_time_options(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cog = PollCog(bot, registry=self.registry)
        try:
            await bot.add_cog(cog)
            schedule_command = bot.get_command("schedule")
            self.assertIsNotNone(schedule_command)
            bot_member = SimpleNamespace(id=1)
            author = SimpleNamespace(id=77, display_name="tester")
            permissions = SimpleNamespace(
                mention_everyone=False,
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
            poll_message = SimpleNamespace(id=99, add_reaction=AsyncMock())
            message = SimpleNamespace(
                _state=bot._connection,
                guild=SimpleNamespace(
                    id=1,
                    me=bot_member,
                    get_role=lambda _: None,
                    get_member=lambda _: None,
                ),
                author=author,
                channel=channel,
                content="!schedule add @ゲーム名 21 22 23 ng",
                role_mentions=[],
                mentions=[],
                mention_everyone=False,
                attachments=[],
            )
            ctx = commands.Context(
                message=message,
                bot=bot,
                view=StringView("add @ゲーム名 21 22 23 ng"),
                prefix="!",
                command=schedule_command,
                invoked_with="schedule",
            )
            ctx.send = AsyncMock(return_value=poll_message)

            await schedule_command.invoke(ctx)

            sent = ctx.send.await_args.kwargs
            self.assertEqual(sent["content"], "@ゲーム名")
            self.assertEqual(sent["embed"].title, "📅 @ゲーム名 開始時間 [5人]")
            self.assertEqual(
                sent["embed"].description,
                "1️⃣21:00, 2️⃣22:00, 3️⃣23:00, 🆖NG",
            )
            self.assertEqual(
                [
                    call.args[0]
                    for call in poll_message.add_reaction.await_args_list
                ],
                ["1️⃣", "2️⃣", "3️⃣", "🆖"],
            )
            self.assertEqual(len(self.registry.all()), 1)
        finally:
            await bot.close()

    async def test_discordpy_prefix_parser_passes_option_add_with_optional_id(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cog = PollCog(bot, registry=self.registry)
        try:
            await bot.add_cog(cog)
            schedule_command = bot.get_command("schedule")
            self.assertIsNotNone(schedule_command)
            bot_member = SimpleNamespace(id=1)
            guild = SimpleNamespace(id=1, me=bot_member)
            author = SimpleNamespace(id=77, display_name="tester")
            permissions = SimpleNamespace(
                read_message_history=True,
                add_reactions=True,
                embed_links=True,
            )
            channel = SimpleNamespace(
                id=10,
                permissions_for=lambda _: permissions,
            )
            cog._resolve_schedule_message_id = AsyncMock(return_value=999)
            cog._add_schedule_options_to_message = AsyncMock()

            async def invoke(argument_text):
                message = SimpleNamespace(
                    _state=bot._connection,
                    guild=guild,
                    author=author,
                    channel=channel,
                    content=f"!schedule {argument_text}",
                    attachments=[],
                )
                ctx = commands.Context(
                    message=message,
                    bot=bot,
                    view=StringView(argument_text),
                    prefix="!",
                    command=schedule_command,
                    invoked_with="schedule",
                )
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                await schedule_command.invoke(ctx)
                return ctx

            latest_ctx = await invoke('option-add "8/23 24時" 23')
            cog._resolve_schedule_message_id.assert_awaited_once_with(
                latest_ctx,
                None,
            )
            cog._add_schedule_options_to_message.assert_awaited_once_with(
                latest_ctx,
                999,
                ["8/23 24時", "23"],
            )

            cog._resolve_schedule_message_id.reset_mock()
            cog._add_schedule_options_to_message.reset_mock()
            message_id = schedule_snowflake(FIXED_NOW)
            explicit_ctx = await invoke(
                f'option-add {message_id} "8/23 23時"'
            )
            cog._resolve_schedule_message_id.assert_awaited_once_with(
                explicit_ctx,
                str(message_id),
            )
            cog._add_schedule_options_to_message.assert_awaited_once_with(
                explicit_ctx,
                999,
                ["8/23 23時"],
            )
        finally:
            await bot.close()

    async def test_option_add_preserves_votes_ng_registry_and_footer_state(self):
        cog, ctx, poll_message, _ = self.make_option_add_fixture()
        deadline_at = FIXED_NOW + timedelta(days=1)
        set_schedule_deadline(poll_message.embeds[0], deadline_at)
        mark_start_time_announced(poll_message.embeds[0], "22:00", 500)
        original_footer = poll_message.embeds[0].footer.text
        original_reactions = list(poll_message.reactions)
        foreign_reaction = SimpleNamespace(emoji="🎉", me=False, count=1)
        poll_message.reactions.append(foreign_reaction)
        registered_before = self.registry.all()

        await cog._add_schedule_options_to_message(ctx, 99, ["20"])

        updated_embed = poll_message.embeds[0]
        self.assertEqual(
            schedule_options_from_embed(updated_embed),
            ["21:00", "22:00", "20:00", "NG"],
        )
        self.assertEqual(
            updated_embed.description,
            "1️⃣21:00, 2️⃣22:00, 3️⃣20:00, 🆖NG",
        )
        self.assertEqual(updated_embed.footer.text, original_footer)
        self.assertEqual(schedule_deadline_at(updated_embed), deadline_at)
        self.assertEqual(start_announcement(updated_embed).start_time, "22:00")
        self.assertEqual(start_announcement(updated_embed).message_id, 500)
        self.assertEqual(self.registry.all(), registered_before)
        self.assertEqual(
            poll_message.reactions,
            [*original_reactions, foreign_reaction],
        )
        for original, current in zip(
            original_reactions,
            poll_message.reactions,
        ):
            self.assertIs(current, original)
            self.assertEqual(current._users, original._users)
        poll_message.add_reaction.assert_awaited_once_with("3️⃣")
        poll_message.clear_reaction.assert_not_awaited()
        poll_message.remove_reaction.assert_not_awaited()
        cog._queue_auto_start_check_by_id.assert_called_once_with(
            guild_id=1,
            channel_id=10,
            message_id=99,
        )
        self.assertIn("既存の投票は保持", ctx.send.await_args.args[0])

    async def test_option_add_rejects_unsafe_or_invalid_existing_poll(self):
        cases = ("legacy", "collision", "closed", "duplicate")
        for case_name in cases:
            with self.subTest(case=case_name):
                cog, ctx, poll_message, _ = self.make_option_add_fixture(
                    registered=False,
                )
                additions = ["23"]
                expected = None
                if case_name == "legacy":
                    poll_message.embeds[0].description = (
                        "1️⃣：21\n2️⃣：22\n3️⃣：ng"
                    )
                    expected = "旧形式"
                elif case_name == "collision":
                    poll_message.reactions.append(
                        SimpleNamespace(emoji="3️⃣", me=False, count=1)
                    )
                    expected = "すでに投票外で使われています"
                elif case_name == "closed":
                    mark_schedule_closed(poll_message.embeds[0])
                    expected = "終了済み"
                else:
                    additions = ["2100"]
                    expected = "同じ候補"
                original_description = poll_message.embeds[0].description
                registered_before = self.registry.all()

                await cog._add_schedule_options_to_message(
                    ctx,
                    poll_message.id,
                    additions,
                )

                self.assertIn(expected, ctx.send.await_args.args[0])
                self.assertEqual(
                    poll_message.embeds[0].description,
                    original_description,
                )
                self.assertEqual(self.registry.all(), registered_before)
                poll_message.add_reaction.assert_not_awaited()
                poll_message.edit.assert_not_awaited()
                poll_message.clear_reaction.assert_not_awaited()
                poll_message.remove_reaction.assert_not_awaited()
                cog._queue_auto_start_check_by_id.assert_not_called()

    async def test_option_add_rolls_back_own_reaction_when_embed_edit_fails(self):
        cog, ctx, poll_message, bot_member = self.make_option_add_fixture()
        response = Mock(status=500, reason="Server Error")
        poll_message.edit.side_effect = discord.HTTPException(
            response,
            {"message": "temporary", "code": 0},
        )
        original_embed = poll_message.embeds[0]
        registered_before = self.registry.all()

        await cog._add_schedule_options_to_message(ctx, 99, ["23"])

        poll_message.add_reaction.assert_awaited_once_with("3️⃣")
        poll_message.remove_reaction.assert_awaited_once_with(
            "3️⃣",
            bot_member,
        )
        poll_message.clear_reaction.assert_not_awaited()
        self.assertIs(poll_message.embeds[0], original_embed)
        self.assertEqual(
            schedule_options_from_embed(poll_message.embeds[0]),
            ["21:00", "22:00", "NG"],
        )
        self.assertEqual(self.registry.all(), registered_before)
        cog._queue_auto_start_check_by_id.assert_called_once_with(
            guild_id=1,
            channel_id=10,
            message_id=99,
        )
        self.assertIn("候補と既存票は変更していません", ctx.send.await_args.args[0])

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
        self.assertEqual(sent["embed"].title, "📅 RAID 開始時間 [5人]")
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
            options="1500 1600 1700 NG [3]",
        )
        custom_embed = ctx.send.await_args.kwargs["embed"]
        self.assertTrue(is_auto_start_schedule(custom_embed))
        self.assertEqual(auto_start_minimum(custom_embed), 3)
        self.assertEqual(custom_embed.title, "📅 RAID 開始時間 [3人]")
        self.assertEqual(
            custom_embed.description,
            "1️⃣15:00, 2️⃣16:00, 3️⃣17:00, 🆖NG",
        )
        self.assertEqual(
            [call.args[0] for call in poll_message.add_reaction.await_args_list],
            ["1️⃣", "2️⃣", "3️⃣", "🆖"],
        )

    async def test_add_supports_user_label_everyone_and_here_mentions(self):
        member = SimpleNamespace(
            id=66, name="alice", display_name="Alice", mention="<@66>"
        )
        permissions = SimpleNamespace(
            mention_everyone=True,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(id=1),
            roles=[],
            members=[member],
            get_role=lambda _: None,
            get_member=lambda user_id: member if user_id == member.id else None,
        )
        bot = SimpleNamespace()
        cog = PollCog(bot, registry=self.registry)
        ctx = SimpleNamespace(
            bot=bot,
            guild=guild,
            channel=SimpleNamespace(
                id=10,
                name="general",
                parent=None,
                permissions_for=lambda _: permissions,
            ),
            author=SimpleNamespace(id=77, display_name="tester"),
            interaction=object(),
        )
        cases = {
            "<@66>": discord.AllowedMentions(
                users=[member], roles=False, everyone=False, replied_user=False
            ),
            "@ゲーム名": discord.AllowedMentions.none(),
            "@everyone": discord.AllowedMentions(
                everyone=True, users=False, roles=False, replied_user=False
            ),
            "@here": discord.AllowedMentions(
                everyone=True, users=False, roles=False, replied_user=False
            ),
        }
        for message_id, (raw_target, expected_mentions) in enumerate(
            cases.items(), start=100
        ):
            with self.subTest(target=raw_target):
                poll = SimpleNamespace(id=message_id, add_reaction=AsyncMock())
                ctx.send = AsyncMock(return_value=poll)
                target = await resolve_schedule_target(ctx, raw_target)

                await PollCog.schedule_add.callback(
                    cog, ctx, target, options="15 16 NG [3]"
                )

                sent = ctx.send.await_args.kwargs
                self.assertEqual(sent["content"], raw_target)
                self.assertEqual(
                    sent["allowed_mentions"].to_dict(),
                    expected_mentions.to_dict(),
                )
                self.assertEqual(
                    sent["embed"].title,
                    f"📅 {target.name} 開始時間 [3人]",
                )

    async def test_prefix_add_accepts_display_label_without_ping(self):
        permissions = SimpleNamespace(
            mention_everyone=False,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        poll_message = SimpleNamespace(id=99, add_reaction=AsyncMock())
        ctx = SimpleNamespace(
            bot=SimpleNamespace(),
            guild=SimpleNamespace(id=1, me=SimpleNamespace(id=1)),
            channel=SimpleNamespace(
                id=10,
                name="general",
                parent=None,
                permissions_for=lambda _: permissions,
            ),
            author=SimpleNamespace(id=77, display_name="tester"),
            interaction=None,
            message=SimpleNamespace(
                content="!schedule add @ゲーム名 15 16 NG [3]",
                role_mentions=[],
                mentions=[],
                mention_everyone=False,
            ),
            send=AsyncMock(return_value=poll_message),
        )
        cog = PollCog(ctx.bot, registry=self.registry)

        await PollCog.schedule_add.callback(
            cog,
            ctx,
            "@ゲーム名",
            options="15 16 NG [3]",
        )

        sent = ctx.send.await_args.kwargs
        self.assertEqual(sent["content"], "@ゲーム名")
        self.assertEqual(sent["embed"].title, "📅 @ゲーム名 開始時間 [3人]")
        self.assertEqual(
            sent["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )

    async def test_broadcast_targets_require_author_and_bot_permissions(self):
        for target_kind, author_can_mention, bot_can_mention, expected in (
            ("everyone", False, True, "メンションする権限"),
            ("here", True, False, "Botに"),
        ):
            with self.subTest(
                target_kind=target_kind,
                author_can_mention=author_can_mention,
                bot_can_mention=bot_can_mention,
            ):
                bot_member = SimpleNamespace(id=1)
                author = SimpleNamespace(id=77, display_name="tester")
                bot_permissions = SimpleNamespace(
                    mention_everyone=bot_can_mention,
                    send_messages=True,
                    send_messages_in_threads=True,
                    read_message_history=True,
                    add_reactions=True,
                    embed_links=True,
                )
                author_permissions = SimpleNamespace(
                    mention_everyone=author_can_mention,
                )

                def permissions_for(member):
                    return (
                        bot_permissions
                        if member is bot_member
                        else author_permissions
                    )

                ctx = SimpleNamespace(
                    bot=SimpleNamespace(),
                    guild=SimpleNamespace(id=1, me=bot_member),
                    channel=SimpleNamespace(
                        id=10,
                        name="general",
                        parent=None,
                        permissions_for=permissions_for,
                    ),
                    author=author,
                    interaction=object(),
                    send=AsyncMock(),
                )
                cog = PollCog(ctx.bot, registry=self.registry)

                await PollCog.schedule_add.callback(
                    cog,
                    ctx,
                    ScheduleTarget(
                        target_kind,
                        f"@{target_kind}",
                        f"@{target_kind}",
                    ),
                )

                self.assertIn(expected, ctx.send.await_args.args[0])
                self.assertNotIn("embed", ctx.send.await_args.kwargs)
                self.assertEqual(
                    ctx.send.await_args.kwargs["allowed_mentions"].to_dict(),
                    discord.AllowedMentions.none().to_dict(),
                )

    async def test_clone_is_available_to_anyone_and_copies_only_poll_settings(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        creator = SimpleNamespace(id=77, display_name="creator")
        requester = SimpleNamespace(id=66, display_name="requester")
        role = SimpleNamespace(
            id=88,
            name="RAID",
            mention="<@&88>",
            mentionable=True,
            is_default=lambda: False,
        )
        source_embed = build_schedule_embed(
            role,
            ["15:00", "16:00", "17:00", "NG"],
            creator,
            auto_start=True,
            minimum=3,
        )
        set_schedule_deadline(source_embed, FIXED_NOW + timedelta(days=1))
        mark_start_time_announced(source_embed, "16:00", 500)
        mark_schedule_closed(source_embed)
        source_embed.title += "（終了）"
        source_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[source_embed],
            role_mentions=[role],
            content=role.mention,
        )
        cloned_message = SimpleNamespace(id=100, add_reaction=AsyncMock())
        bot_permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        requester_permissions = SimpleNamespace(
            manage_messages=False,
            mention_everyone=False,
        )

        def permissions_for(member):
            return bot_permissions if member is bot_user else requester_permissions

        channel = SimpleNamespace(
            id=10,
            name="general",
            parent=None,
            permissions_for=permissions_for,
            fetch_message=AsyncMock(return_value=source_message),
        )
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        cog._resolve_schedule_message_id = AsyncMock(return_value=99)
        cog._queue_auto_start_check_by_id = Mock()
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=requester,
            interaction=None,
            message=SimpleNamespace(role_mentions=[]),
            send=AsyncMock(return_value=cloned_message),
        )

        await PollCog.schedule_clone.callback(cog, ctx)

        cog._resolve_schedule_message_id.assert_awaited_once_with(ctx, None)
        sent = ctx.send.await_args.kwargs
        cloned_embed = sent["embed"]
        self.assertEqual(sent["content"], role.mention)
        self.assertEqual(sent["allowed_mentions"].roles, [role])
        self.assertEqual(
            schedule_options_from_embed(cloned_embed),
            ["15:00", "16:00", "17:00", "NG"],
        )
        self.assertEqual(auto_start_minimum(cloned_embed), 3)
        self.assertEqual(schedule_author_id(cloned_embed), requester.id)
        self.assertFalse(is_schedule_closed(cloned_embed))
        self.assertIsNone(schedule_deadline_at(cloned_embed))
        self.assertIsNone(start_announcement(cloned_embed))
        self.assertEqual(
            [call.args[0] for call in cloned_message.add_reaction.await_args_list],
            ["1️⃣", "2️⃣", "3️⃣", "🆖"],
        )
        self.assertEqual(
            [poll.message_id for poll in self.registry.all()],
            [cloned_message.id],
        )
        cog._queue_auto_start_check_by_id.assert_called_once_with(
            guild_id=1,
            channel_id=10,
            message_id=cloned_message.id,
        )

    async def test_date_sets_override_and_clear_restores_message_date(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        creator = SimpleNamespace(id=77, display_name="creator")
        role = SimpleNamespace(name="RAID")
        poll_message = SimpleNamespace(
            id=99,
            created_at=FIXED_NOW,
            author=bot_user,
            embeds=[
                build_schedule_embed(
                    role,
                    ["20:00", "21:00", "NG"],
                    creator,
                    auto_start=True,
                )
            ],
            jump_url="https://discord.com/channels/1/10/99",
            edit=AsyncMock(),
        )

        async def save_poll_edit(*, embed):
            poll_message.embeds = [embed]
            return poll_message

        poll_message.edit.side_effect = save_poll_edit
        bot_permissions = SimpleNamespace(
            read_message_history=True,
            embed_links=True,
        )
        creator_permissions = SimpleNamespace(manage_messages=False)

        def permissions_for(member):
            return bot_permissions if member is bot_user else creator_permissions

        channel = SimpleNamespace(
            id=10,
            permissions_for=permissions_for,
            fetch_message=AsyncMock(return_value=poll_message),
        )
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(
            bot,
            # 投稿日とは別の月に実行し、DD指定が投稿日基準になることも確認する。
            now_provider=lambda: FIXED_NOW + timedelta(days=25),
            registry=self.registry,
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_date.callback(cog, ctx, "99", "2026-08-14")

        self.assertEqual(
            schedule_date_override(poll_message.embeds[0]),
            date(2026, 8, 14),
        )
        self.assertIn("2026-08-14に設定", ctx.send.await_args.args[0])

        ctx.send.reset_mock()
        await PollCog.schedule_date.callback(cog, ctx, "99", "clear")

        self.assertIsNone(schedule_date_override(poll_message.embeds[0]))
        self.assertEqual(schedule_event_date(poll_message), date(2026, 8, 10))
        self.assertIn("投稿日（2026-08-10）", ctx.send.await_args.args[0])

        ctx.send.reset_mock()
        await PollCog.schedule_date.callback(cog, ctx, "99", "14")

        self.assertEqual(
            schedule_date_override(poll_message.embeds[0]),
            date(2026, 8, 14),
        )
        self.assertIn("2026-08-14に設定", ctx.send.await_args.args[0])

    async def test_decide_normalizes_candidate_notifies_role_and_closes_poll(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        creator = SimpleNamespace(id=77, display_name="creator")
        role = SimpleNamespace(
            id=88,
            name="RAID",
            mention="<@&88>",
            mentionable=True,
        )
        source_embed = build_schedule_embed(
            role,
            ["20:00", "21:00", "NG"],
            creator,
            auto_start=True,
            minimum=3,
        )
        set_schedule_deadline(source_embed, FIXED_NOW + timedelta(days=1))
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[source_embed],
            role_mentions=[role],
            content=role.mention,
            jump_url="https://discord.com/channels/1/10/99",
            edit=AsyncMock(),
        )

        async def save_poll_edit(*, embed):
            poll_message.embeds = [embed]
            return poll_message

        poll_message.edit.side_effect = save_poll_edit
        decision_notification = SimpleNamespace(
            id=500,
            author=bot_user,
            delete=AsyncMock(),
        )
        bot_permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            embed_links=True,
        )
        creator_permissions = SimpleNamespace(
            manage_messages=False,
            mention_everyone=False,
        )

        def permissions_for(member):
            return bot_permissions if member is bot_user else creator_permissions

        channel = SimpleNamespace(
            id=10,
            name="general",
            parent=None,
            permissions_for=permissions_for,
            fetch_message=AsyncMock(return_value=poll_message),
            send=AsyncMock(return_value=decision_notification),
        )
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        cog._register_schedule_poll(guild_id=1, channel_id=10, message_id=99)
        ctx = SimpleNamespace(
            bot=bot,
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_decide.callback(cog, ctx, "99", "2100")

        ctx.defer.assert_awaited_once_with(ephemeral=True)
        sent_notification = channel.send.await_args.kwargs
        self.assertEqual(
            sent_notification["content"],
            "21:00 開始 <@&88>\n✅ この時間で確定しました。",
        )
        self.assertEqual(sent_notification["allowed_mentions"].roles, [role])
        decided_embed = poll_message.embeds[0]
        self.assertTrue(is_schedule_closed(decided_embed))
        self.assertEqual(schedule_decided_start_time(decided_embed), "21:00")
        self.assertIsNone(schedule_deadline_at(decided_embed))
        self.assertIsNone(start_announcement(decided_embed))
        self.assertEqual(schedule_related_notification_id(decided_embed), 500)
        self.assertEqual(self.registry.all(), [])
        self.assertIn("21:00 開始で確定", ctx.send.await_args.args[0])

        ctx.send.reset_mock()
        channel.send.reset_mock()
        await PollCog.schedule_decide.callback(cog, ctx, "99", "20:00")

        channel.send.assert_not_awaited()
        self.assertIn("21:00 開始で確定しています", ctx.send.await_args.args[0])

    async def test_delete_removes_latest_poll_notification_and_related_data(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        creator = SimpleNamespace(id=77, display_name="creator")
        role = SimpleNamespace(name="RAID")
        embed = build_schedule_embed(
            role,
            ["20:00", "21:00", "NG"],
            creator,
            auto_start=True,
        )
        mark_start_time_announced(embed, "21:00", 500)
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[embed],
            delete=AsyncMock(),
        )
        notification = SimpleNamespace(
            id=500,
            author=bot_user,
            delete=AsyncMock(),
        )
        bot_permissions = SimpleNamespace(read_message_history=True)
        creator_permissions = SimpleNamespace(manage_messages=False)

        def permissions_for(member):
            return bot_permissions if member is bot_user else creator_permissions

        async def fetch_message(message_id):
            return {99: poll_message, 500: notification}[message_id]

        channel = SimpleNamespace(
            id=10,
            permissions_for=permissions_for,
            fetch_message=AsyncMock(side_effect=fetch_message),
        )
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        cog._register_schedule_poll(guild_id=1, channel_id=10, message_id=99)
        event = cog._lateness_registry.upsert_event(
            poll_message_id=99,
            guild_id=1,
            channel_id=10,
            event_date=date(2026, 8, 10),
            start_time="21:00",
            minimum=5,
            start_at=FIXED_NOW,
            finalized=True,
        )
        cog._lateness_registry.snapshot_participants(
            99,
            {2},
            snapshotted_at=event.snapshot_at,
        )
        cog._lateness_registry.activate(
            99,
            voice_channel_id=20,
            activated_at=FIXED_NOW + timedelta(minutes=1),
            arrivals={2: FIXED_NOW + timedelta(minutes=1)},
        )
        self.assertTrue(
            cog._lateness_registry.monthly_stats(
                guild_id=1,
                month=date(2026, 8, 1),
            )
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )
        cog._resolve_schedule_message_id = AsyncMock(return_value=99)

        await PollCog.schedule_delete.callback(cog, ctx)

        cog._resolve_schedule_message_id.assert_awaited_once_with(ctx, None)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        poll_message.delete.assert_awaited_once_with()
        notification.delete.assert_awaited_once_with()
        self.assertEqual(self.registry.all(), [])
        self.assertIsNone(cog._lateness_registry.get_event(99))
        self.assertEqual(
            cog._lateness_registry.monthly_stats(
                guild_id=1,
                month=date(2026, 8, 1),
            ),
            [],
        )
        self.assertNotIn(99, cog._registered_schedule_ids)
        self.assertIn("完全削除", ctx.send.await_args.args[0])

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

    async def test_minimum_rejects_out_of_range_before_fetching_poll(self):
        cog = PollCog(SimpleNamespace(), registry=self.registry)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(),
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        for minimum in (0, 1000):
            with self.subTest(minimum=minimum):
                ctx.send.reset_mock()
                await PollCog.schedule_minimum.callback(
                    cog,
                    ctx,
                    "99",
                    minimum,
                )
                self.assertIn("1〜999人", ctx.send.await_args.args[0])

        ctx.defer.assert_not_awaited()

    async def test_status_is_ephemeral_and_does_not_require_edit_permission(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        creator = SimpleNamespace(id=77, display_name="creator")
        requester = SimpleNamespace(id=66, display_name="requester")
        role = SimpleNamespace(name="RAID")
        voter = SimpleNamespace(id=2, bot=False)
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[
                build_schedule_embed(
                    role,
                    ["20:00", "21:00", "NG"],
                    creator,
                    auto_start=True,
                )
            ],
            reactions=[FakeReaction("1️⃣", [bot_user, voter], me=True)],
            jump_url="https://discord.com/channels/1/10/99",
            edit=AsyncMock(),
        )
        bot_permissions = SimpleNamespace(
            read_message_history=True,
            embed_links=True,
        )
        requester_permissions = SimpleNamespace(manage_messages=False)

        def permissions_for(member):
            return bot_permissions if member is bot_user else requester_permissions

        channel = SimpleNamespace(
            id=10,
            permissions_for=permissions_for,
            fetch_message=AsyncMock(return_value=poll_message),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=requester,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_status.callback(cog, ctx, "99")

        ctx.defer.assert_awaited_once_with(ephemeral=True)
        sent = ctx.send.await_args
        self.assertTrue(sent.kwargs["ephemeral"])
        self.assertIn("1️⃣20:00: 1票", sent.kwargs["embed"].description)
        poll_message.edit.assert_not_awaited()

    async def test_omitted_message_uses_latest_poll_for_status_and_close(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        creator = SimpleNamespace(id=77, display_name="creator")
        role = SimpleNamespace(name="RAID")
        poll_message = SimpleNamespace(
            id=100,
            created_at=FIXED_NOW,
            author=bot_user,
            embeds=[
                build_schedule_embed(
                    role,
                    ["20:00", "21:00", "NG"],
                    creator,
                    auto_start=True,
                )
            ],
            reactions=[],
            role_mentions=[],
            content="<@&88>",
            jump_url="https://discord.com/channels/1/10/100",
            edit=AsyncMock(),
        )

        async def save_poll_edit(*, embed):
            poll_message.embeds = [embed]
            return poll_message

        poll_message.edit.side_effect = save_poll_edit
        unrelated = SimpleNamespace(id=101, author=bot_user, embeds=[])
        older_poll = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[
                build_schedule_embed(
                    role,
                    ["19:00", "20:00"],
                    creator,
                    auto_start=True,
                )
            ],
        )
        history_limits = []

        def history(*, limit):
            history_limits.append(limit)

            async def iterator():
                for candidate in (unrelated, poll_message, older_poll):
                    yield candidate

            return iterator()

        bot_permissions = SimpleNamespace(
            read_message_history=True,
            embed_links=True,
        )
        creator_permissions = SimpleNamespace(manage_messages=False)

        def permissions_for(member):
            return bot_permissions if member is bot_user else creator_permissions

        channel = SimpleNamespace(
            id=10,
            permissions_for=permissions_for,
            history=history,
            fetch_message=AsyncMock(return_value=poll_message),
        )
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_status.callback(cog, ctx)

        channel.fetch_message.assert_awaited_once_with(poll_message.id)
        self.assertIn("20:00", ctx.send.await_args.kwargs["embed"].description)

        channel.fetch_message.reset_mock()
        ctx.defer.reset_mock()
        ctx.send.reset_mock()
        await PollCog.schedule_close.callback(cog, ctx)

        channel.fetch_message.assert_awaited_once_with(poll_message.id)
        self.assertTrue(is_schedule_closed(poll_message.embeds[0]))
        self.assertEqual(history_limits, [100, 100])

    async def test_deadline_sets_and_clears_persisted_deadline(self):
        bot_user = SimpleNamespace(id=1, bot=True)
        creator = SimpleNamespace(id=77, display_name="creator")
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(
            bot,
            now_provider=lambda: FIXED_NOW,
            registry=self.registry,
        )
        cog._queue_schedule_deadline = Mock()
        role = SimpleNamespace(name="RAID")
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            embeds=[
                build_schedule_embed(
                    role,
                    ["20:00", "21:00", "NG"],
                    creator,
                    auto_start=True,
                )
            ],
            jump_url="https://discord.com/channels/1/10/99",
            edit=AsyncMock(),
        )

        async def save_poll_edit(*, embed):
            poll_message.embeds = [embed]
            return poll_message

        poll_message.edit.side_effect = save_poll_edit
        bot_permissions = SimpleNamespace(
            read_message_history=True,
            embed_links=True,
        )
        creator_permissions = SimpleNamespace(manage_messages=False)

        def permissions_for(member):
            return bot_permissions if member is bot_user else creator_permissions

        channel = SimpleNamespace(
            id=10,
            permissions_for=permissions_for,
            fetch_message=AsyncMock(return_value=poll_message),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_deadline.callback(
            cog,
            ctx,
            "99",
            deadline="2026-08-14 19:00",
        )

        expected = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(schedule_deadline_at(poll_message.embeds[0]), expected)
        self.assertEqual(self.registry.all()[0].deadline_at, expected)
        cog._queue_schedule_deadline.assert_called_once()
        self.assertIn("締切を", ctx.send.await_args.args[0])

        ctx.send.reset_mock()
        await PollCog.schedule_deadline.callback(
            cog,
            ctx,
            "99",
            deadline="clear",
        )

        self.assertIsNone(schedule_deadline_at(poll_message.embeds[0]))
        self.assertIsNone(self.registry.all()[0].deadline_at)
        self.assertIn("締切を解除", ctx.send.await_args.args[0])

    async def test_update_edits_embed_and_resets_reactions(self):
        bot_user = SimpleNamespace(id=1)
        bot = SimpleNamespace(user=bot_user)
        cog = PollCog(bot, registry=self.registry)
        cog._queue_auto_start_check_by_id = Mock()
        cog._queue_schedule_deadline = Mock()
        permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            read_message_history=True,
            add_reactions=True,
            embed_links=True,
        )
        author = SimpleNamespace(id=77, display_name="tester")
        role = SimpleNamespace(
            id=55,
            name="VALORANT",
            mention="<@&55>",
            mentionable=True,
        )
        original_embed = build_schedule_embed(
            role,
            list(DEFAULT_SCHEDULE_OPTION_LIST),
            author,
            auto_start=True,
            minimum=3,
        )
        deadline_at = FIXED_NOW + timedelta(days=1)
        set_schedule_deadline(original_embed, deadline_at)
        cog._set_schedule_poll_deadline(
            guild_id=1,
            channel_id=10,
            message_id=99,
            deadline_at=deadline_at,
        )
        poll_message = SimpleNamespace(
            id=99,
            author=bot_user,
            content=role.mention,
            role_mentions=[role],
            mentions=[],
            guild=SimpleNamespace(get_role=lambda role_id: role),
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
        self.assertEqual(auto_start_minimum(edited_embed), 3)
        self.assertEqual(schedule_deadline_at(edited_embed), deadline_at)
        self.assertEqual(edited_embed.title, "📅 VALORANT 開始時間 [3人]")
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
        registered = self.registry.all()
        self.assertEqual([poll.message_id for poll in registered], [99])
        self.assertEqual(registered[0].deadline_at, deadline_at)
        self.assertEqual(cog._queue_schedule_deadline.call_count, 2)
        self.assertIn("投票をリセット", ctx.send.await_args.args[0])

    async def test_prefix_update_and_deadline_can_omit_message_id(self):
        bot_user = SimpleNamespace(id=1)
        permissions = SimpleNamespace(
            manage_messages=True,
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
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=bot_user),
            channel=channel,
            author=SimpleNamespace(id=77, display_name="creator"),
            interaction=None,
            defer=AsyncMock(),
            send=AsyncMock(),
        )
        cog = PollCog(
            SimpleNamespace(user=bot_user),
            now_provider=lambda: FIXED_NOW,
            registry=self.registry,
        )
        cog._resolve_schedule_message_id = AsyncMock(return_value=99)
        cog._update_schedule_message = AsyncMock()

        await PollCog.schedule_update.callback(
            cog,
            ctx,
            "15",
            options="16 17 NG",
        )

        cog._resolve_schedule_message_id.assert_awaited_once_with(ctx, None)
        cog._update_schedule_message.assert_awaited_once_with(
            ctx,
            99,
            ["15:00", "16:00", "17:00", "NG"],
        )

        cog._resolve_schedule_message_id.reset_mock()
        cog._change_schedule_deadline = AsyncMock()
        await PollCog.schedule_deadline.callback(
            cog,
            ctx,
            "2026-08-14",
            deadline="19:00",
        )

        cog._resolve_schedule_message_id.assert_awaited_once_with(ctx, None)
        cog._change_schedule_deadline.assert_awaited_once_with(
            ctx,
            99,
            datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
        )

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

    def enable_schedule_effect(self, user_ids, rolls):
        self.harness.cog._effect_config = ScheduleEffectConfig(
            enabled=True,
            user_ids=frozenset(user_ids),
            delete_after_seconds=8,
        )
        self.harness.cog._effect_roll = Mock(side_effect=rolls)
        self.harness.cog._effect_sleep = AsyncMock()

    async def run_effect_reaction(self, *, emoji="1️⃣", user_id=2):
        await self.harness.cog.on_raw_reaction_add(
            self.harness.payload(emoji=emoji, user_id=user_id)
        )
        effect_tasks = list(self.harness.cog._effect_tasks)
        self.assertEqual(len(effect_tasks), 1)
        await asyncio.gather(*effect_tasks)

    def assert_effect_message(self, message_id, expected_frames):
        effect_message = self.harness.notifications[message_id]
        self.assertEqual(effect_message.content, expected_frames[-1])
        self.assertEqual(
            [edit.kwargs["content"] for edit in effect_message.edit.await_args_list],
            list(expected_frames[1:]),
        )
        for edit in effect_message.edit.await_args_list:
            self.assert_allowed_mentions_none(edit.kwargs["allowed_mentions"])
        effect_message.delete.assert_awaited_once()

    def decision_context(self):
        permissions = SimpleNamespace(
            mention_everyone=True,
            manage_messages=True,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            embed_links=True,
        )
        self.harness.channel.permissions_for = lambda _: permissions
        return SimpleNamespace(
            bot=self.harness.bot,
            guild=SimpleNamespace(id=1, me=self.harness.bot_user),
            channel=self.harness.channel,
            author=self.harness.creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
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

    async def test_waits_eight_seconds_and_persists_first_notification_id(self):
        self.assertEqual(AUTO_START_GRACE_SECONDS, 8)
        self.assertEqual(LATENESS_REACTION_GRACE_SECONDS, 8)
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})

        sleep_call, task = await self.queue_check()

        self.assertEqual(sleep_call.delay, 8)
        self.assertFalse(task.done())
        self.harness.channel.fetch_message.assert_not_awaited()
        self.harness.channel.send.assert_not_awaited()

        await self.finish_check(sleep_call, task)

        self.harness.channel.send.assert_awaited_once()
        notification = self.harness.channel.send.await_args
        self.assertEqual(notification.kwargs["content"], "✅ 20:00 開始 <@&88>")
        self.assertEqual(notification.kwargs["allowed_mentions"].roles, [self.harness.role])
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "20:00")
        self.assertEqual(announcement.message_id, 1000)

    async def test_display_label_auto_start_is_display_only(self):
        target = ScheduleTarget("label", "@ゲーム名", "@ゲーム名")
        self.harness.poll_message.content = target.mention
        self.harness.poll_message.role_mentions = []
        self.harness.poll_message.embeds = [
            build_schedule_embed(
                target,
                list(DEFAULT_SCHEDULE_OPTION_LIST),
                self.harness.creator,
                auto_start=True,
            )
        ]

        notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )

        self.assertEqual(notification.content, "✅ 20:00 開始 @ゲーム名")
        self.assert_allowed_mentions_none(notification.allowed_mentions)

    async def test_everyone_auto_start_pings_once(self):
        target = ScheduleTarget("everyone", "@everyone", "@everyone")
        self.harness.poll_message.content = target.mention
        self.harness.poll_message.role_mentions = []
        self.harness.poll_message.embeds = [
            build_schedule_embed(
                target,
                list(DEFAULT_SCHEDULE_OPTION_LIST),
                self.harness.creator,
                auto_start=True,
            )
        ]

        first_notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )

        self.assertEqual(first_notification.content, "✅ 20:00 開始 @everyone")
        self.assertEqual(
            first_notification.allowed_mentions.to_dict(),
            discord.AllowedMentions(
                everyone=True,
                users=False,
                roles=False,
                replied_user=False,
            ).to_dict(),
        )

        self.harness.channel.send.reset_mock()
        self.harness.set_voters(
            {
                "20:00": {2, 3, 4, 5},
                "20:30": {6},
            }
        )
        sleep_call, task = await self.queue_check(
            "remove",
            emoji="1️⃣",
            user_id=6,
        )
        await self.finish_check(sleep_call, task)

        self.assert_allowed_mentions_none(first_notification.allowed_mentions)
        replacement = self.harness.channel.send.await_args.kwargs
        self.assertEqual(
            replacement["content"],
            "✅ 20:30 開始 @everyone（🔄 20:00 開始から変更されました）",
        )
        self.assert_allowed_mentions_none(replacement["allowed_mentions"])

    async def test_schedule_effect_roll_one_plays_rush_once_and_deletes_it(self):
        self.enable_schedule_effect({2}, [1])
        self.harness.set_voters({"20:00": {2}})

        await self.run_effect_reaction(user_id=2)

        self.assert_effect_message(
            1000,
            schedule_effect_frames("rush", user_id=2),
        )
        self.assertEqual(
            self.harness.cog._effect_sleep.await_args_list,
            [call(1.0), call(1.0), call(6.0)],
        )
        self.assert_allowed_mentions_none(
            self.harness.notifications[1000].allowed_mentions
        )

        self.harness.channel.send.reset_mock()
        self.harness.set_voters({"20:00": {2}, "20:30": {2}})
        await self.harness.cog.on_raw_reaction_add(
            self.harness.payload(emoji="2️⃣", user_id=2)
        )
        repeated_tasks = list(self.harness.cog._effect_tasks)
        await asyncio.gather(*repeated_tasks)

        self.harness.channel.send.assert_not_awaited()
        self.assertEqual(self.harness.cog._effect_roll.call_count, 1)

    async def test_schedule_effect_rolls_two_and_three_play_chance(self):
        self.enable_schedule_effect({2, 3}, [2, 3])
        self.harness.set_voters({"20:00": {2, 3}})

        await self.run_effect_reaction(user_id=2)
        await self.run_effect_reaction(user_id=3)

        self.assert_effect_message(
            1000,
            schedule_effect_frames("chance", user_id=2),
        )
        self.assert_effect_message(
            1001,
            schedule_effect_frames("chance", user_id=3),
        )

    async def test_schedule_effect_rolls_four_through_eight_play_miss(self):
        user_ids = set(range(2, 7))
        self.enable_schedule_effect(user_ids, [4, 5, 6, 7, 8])

        for user_id in sorted(user_ids):
            self.harness.set_voters({"20:00": {user_id}})
            await self.run_effect_reaction(user_id=user_id)

        for message_id, user_id in zip(range(1000, 1005), sorted(user_ids)):
            self.assert_effect_message(
                message_id,
                schedule_effect_frames("miss", user_id=user_id),
            )
        self.assertEqual(self.harness.cog._effect_roll.call_count, 5)

    async def test_schedule_effect_rolls_nine_through_sixteen_show_nothing(self):
        user_ids = set(range(2, 10))
        self.enable_schedule_effect(user_ids, list(range(9, 17)))

        for user_id in sorted(user_ids):
            self.harness.set_voters({"20:00": {user_id}})
            await self.run_effect_reaction(user_id=user_id)

        self.harness.channel.send.assert_not_awaited()
        self.assertEqual(self.harness.cog._effect_roll.call_count, 8)

    async def test_non_target_user_does_not_draw_or_play_effect(self):
        self.enable_schedule_effect({2}, [1])
        self.harness.set_voters({"20:00": {3}})

        await self.harness.cog.on_raw_reaction_add(
            self.harness.payload(user_id=3)
        )
        await asyncio.sleep(0)

        self.assertEqual(self.harness.cog._effect_tasks, {})
        self.harness.cog._effect_roll.assert_not_called()
        self.harness.channel.send.assert_not_awaited()

    async def test_deciding_vote_always_plays_rush_without_random_draw(self):
        self.enable_schedule_effect({6}, [16])
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})

        await self.run_effect_reaction(user_id=6)

        self.assert_effect_message(
            1000,
            schedule_effect_frames("rush", user_id=6, start_time="20:00"),
        )
        self.harness.cog._effect_roll.assert_not_called()

    async def test_deciding_readd_gets_rush_after_normal_draw_was_used(self):
        self.enable_schedule_effect({6}, [16])
        self.harness.set_voters({"20:00": {2, 3, 6}})
        await self.run_effect_reaction(user_id=6)
        self.harness.channel.send.assert_not_awaited()

        await self.harness.cog.on_raw_reaction_remove(
            self.harness.payload(user_id=6)
        )
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        await self.run_effect_reaction(user_id=6)

        self.assert_effect_message(
            1000,
            schedule_effect_frames("rush", user_id=6, start_time="20:00"),
        )
        self.assertEqual(self.harness.cog._effect_roll.call_count, 1)

    async def test_decide_reuses_matching_auto_start_notification(self):
        notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )
        self.harness.channel.send.reset_mock()
        ctx = self.decision_context()

        await PollCog.schedule_decide.callback(
            self.harness.cog,
            ctx,
            str(self.harness.POLL_MESSAGE_ID),
            "20",
        )

        self.harness.channel.send.assert_not_awaited()
        self.assertEqual(
            notification.content,
            "20:00 開始 <@&88>\n✅ この時間で確定しました。",
        )
        self.assert_allowed_mentions_none(notification.allowed_mentions)
        decided_embed = self.harness.poll_message.embeds[0]
        self.assertTrue(is_schedule_closed(decided_embed))
        self.assertEqual(schedule_decided_start_time(decided_embed), "20:00")
        self.assertEqual(
            schedule_related_notification_id(decided_embed),
            notification.id,
        )
        self.assertEqual(self.registry.all(), [])

    async def test_decide_replaces_different_auto_start_without_reping_role(self):
        old_notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )
        self.harness.channel.send.reset_mock()
        ctx = self.decision_context()

        await PollCog.schedule_decide.callback(
            self.harness.cog,
            ctx,
            str(self.harness.POLL_MESSAGE_ID),
            "2100",
        )

        self.assertIn("~~20:00 開始 <@&88>~~", old_notification.content)
        self.assertIn("21:00 開始へ変更", old_notification.content)
        replacement = self.harness.channel.send.await_args.kwargs
        self.assertEqual(
            replacement["content"],
            "21:00 開始 <@&88>\n✅ この時間で確定しました。",
        )
        self.assert_allowed_mentions_none(replacement["allowed_mentions"])
        decided_embed = self.harness.poll_message.embeds[0]
        self.assertTrue(is_schedule_closed(decided_embed))
        self.assertEqual(schedule_decided_start_time(decided_embed), "21:00")
        self.assertEqual(
            schedule_related_notification_id(decided_embed),
            self.harness.notifications[1001].id,
        )
        self.assertEqual(self.registry.all(), [])

    async def test_custom_compact_times_are_normalized_and_auto_evaluated(self):
        options = parse_schedule_options("1500 1600 1700")
        self.harness.poll_message.embeds = [
            build_schedule_embed(
                self.harness.role,
                options,
                self.harness.creator,
                auto_start=True,
                minimum=3,
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
            "✅ 16:00 開始 <@&88>",
        )
        self.assertEqual(
            start_announcement(self.harness.poll_message.embeds[0]).start_time,
            "16:00",
        )

    async def test_custom_minimum_is_used_in_cancellation_notice(self):
        self.harness.poll_message.embeds = [
            build_schedule_embed(
                self.harness.role,
                list(DEFAULT_SCHEDULE_OPTION_LIST),
                self.harness.creator,
                auto_start=True,
                minimum=3,
            )
        ]
        await self.announce({"20:00": {2, 3, 4}}, "20:00")
        self.harness.channel.send.reset_mock()
        self.harness.set_voters({"20:00": {2, 3}})

        sleep_call, task = await self.queue_check("clear")
        await self.finish_check(sleep_call, task)

        self.assertIn(
            "参加可能な投票者が3人未満になりました。",
            self.harness.channel.send.await_args.kwargs["content"],
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
        self.assertEqual(old_notification.content, "✅ 20:00 開始 <@&88>")
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

    async def test_cog_load_recovers_persisted_deadline(self):
        deadline_at = FIXED_NOW + timedelta(hours=1)
        self.registry.set_deadline(
            guild_id=1,
            channel_id=self.harness.CHANNEL_ID,
            message_id=self.harness.POLL_MESSAGE_ID,
            deadline_at=deadline_at,
        )
        deadline_sleeper = ControlledSleeper()
        self.harness.cog._deadline_sleep = deadline_sleeper
        self.harness.cog._now = lambda: FIXED_NOW

        await self.harness.cog.cog_load()
        await self.harness.cog._registry_recovery_task
        await deadline_sleeper.wait_for_calls(1)

        self.assertEqual(deadline_sleeper.calls[0].delay, 3600)
        self.assertEqual(
            self.harness.cog._registered_schedule_polls[
                self.harness.POLL_MESSAGE_ID
            ].deadline_at,
            deadline_at,
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
        self.assertEqual(self.sleeper.calls[0].delay, 8)
        self.harness.channel.fetch_message.assert_not_awaited()
        self.harness.channel.send.assert_not_awaited()

        await self.finish_check(self.sleeper.calls[0], pending_task)

        self.harness.channel.send.assert_awaited_once()
        announcement = start_announcement(self.harness.poll_message.embeds[0])
        self.assertEqual(announcement.start_time, "21:00")
        self.assertEqual(announcement.message_id, 1000)

    async def test_reaction_removed_within_eight_seconds_prevents_notification(self):
        self.harness.set_voters({"20:00": {2, 3, 4, 5, 6}})
        first_sleep, first_task = await self.queue_check()

        self.harness.set_voters({"20:00": {2, 3, 4, 5}})
        second_sleep, second_task = await self.queue_check(
            "remove",
            emoji="1️⃣",
        )

        self.assertEqual(first_sleep.delay, 8)
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
        self.assertEqual([call.delay for call in self.sleeper.calls], [8, 8, 8])
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
        self.assertEqual(
            replacement["content"],
            "✅ 24:00 開始 <@&88>（🔄 23:00 開始から変更されました）",
        )
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
        self.assertEqual(
            replacement["content"],
            "✅ 21:00 開始 <@&88>（🔄 20:00 開始から変更されました）",
        )
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

        self.assertEqual(sleep_call.delay, 8)
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
        self.assertEqual(restored["content"], "✅ 20:00 開始 <@&88>")
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
        self.assertEqual(replacement["content"], "✅ 20:00 開始 <@&88>")
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
        self.assertEqual(self.sleeper.calls[1].delay, 8)
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
        self.assertEqual(replacement.content, "✅ 20:00 開始 <@&88>")
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

        self.assertIn("~~✅ 20:00 開始 <@&88>~~", notification.content)
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

        self.assertIn("~~✅ 20:00 開始 <@&88>~~", old_notification.content)
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
        self.assertEqual(
            self.harness.poll_message.embeds[0].title,
            "📅 GAME 開始時間",
        )
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

    async def test_schedule_minimum_keeps_votes_and_rechecks(self):
        permissions = SimpleNamespace(
            manage_messages=True,
            read_message_history=True,
            embed_links=True,
        )
        self.harness.channel.permissions_for = lambda _: permissions
        self.harness.set_voters({"20:00": {2, 3, 4}})
        original_reactions = list(self.harness.poll_message.reactions)
        ctx = SimpleNamespace(
            bot=self.harness.bot,
            guild=SimpleNamespace(id=1, me=self.harness.bot_user),
            channel=self.harness.channel,
            author=self.harness.creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await PollCog.schedule_minimum.callback(
            self.harness.cog,
            ctx,
            str(self.harness.POLL_MESSAGE_ID),
            3,
        )

        embed = self.harness.poll_message.embeds[0]
        self.assertEqual(auto_start_minimum(embed), 3)
        self.assertEqual(embed.title, "📅 GAME 開始時間 [3人]")
        self.assertEqual(self.harness.poll_message.reactions, original_reactions)
        self.assertIn("最低人数を3人", ctx.send.await_args.args[0])

        await self.sleeper.wait_for_calls(1)
        pending_task = self.harness.cog._auto_start_tasks[
            self.harness.POLL_MESSAGE_ID
        ]
        await self.finish_check(self.sleeper.calls[0], pending_task)

        self.assertEqual(
            self.harness.channel.send.await_args.kwargs["content"],
            "✅ 20:00 開始 <@&88>",
        )

    async def test_deadline_survives_registry_and_closes_active_poll(self):
        old_notification = await self.announce(
            {"20:00": {2, 3, 4, 5, 6}},
            "20:00",
        )
        deadline_sleeper = ControlledSleeper()
        self.harness.cog._deadline_sleep = deadline_sleeper
        self.harness.cog._now = lambda: FIXED_NOW
        deadline_at = FIXED_NOW + timedelta(hours=1)
        updated_embed = self.harness.poll_message.embeds[0].copy()
        set_schedule_deadline(updated_embed, deadline_at)
        self.harness.poll_message.embeds = [updated_embed]

        persisted = self.harness.cog._set_schedule_poll_deadline(
            guild_id=1,
            channel_id=self.harness.CHANNEL_ID,
            message_id=self.harness.POLL_MESSAGE_ID,
            deadline_at=deadline_at,
        )
        await deadline_sleeper.wait_for_calls(1)
        deadline_task = self.harness.cog._deadline_tasks[
            self.harness.POLL_MESSAGE_ID
        ]

        self.assertTrue(persisted)
        self.assertEqual(deadline_sleeper.calls[0].delay, 3600)
        self.assertEqual(self.registry.all()[0].deadline_at, deadline_at)

        deadline_sleeper.release()
        await deadline_task

        closed_embed = self.harness.poll_message.embeds[0]
        self.assertTrue(is_schedule_closed(closed_embed))
        self.assertEqual(closed_embed.title, "📅 GAME 開始時間 [5人]（終了）")
        self.assertIn("締切時刻", old_notification.content)
        self.assertEqual(self.registry.all(), [])

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
        self.assertEqual(embed.title, "📅 GAME 開始時間 [5人]（終了）")
        self.assertIn("投票終了", embed.footer.text)
        self.assertEqual(
            schedule_related_notification_id(embed),
            old_notification.id,
        )
        self.assertIn("~~20:00 開始 <@&88>~~", old_notification.content)
        self.assertIn("投票が終了", old_notification.content)
        self.assertEqual(self.registry.all(), [])
        self.assertIn("投票を終了", ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
