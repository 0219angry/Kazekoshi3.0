import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kazekoshi.cogs.poll import (
    PollCog,
    SchedulePollRegistry,
    build_schedule_embed,
    eligible_voters_for_start,
    mark_start_time_announced,
    schedule_option_emojis,
    schedule_start_datetime,
    set_schedule_date_override,
)
from kazekoshi.schedule_lateness import (
    LATENESS_MAX_SECONDS,
    ScheduleLatenessRegistry,
    lateness_voice_quorum,
)


START_AT = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


class FakeReaction:
    def __init__(self, emoji, users):
        self.emoji = emoji
        self._users = users

    def users(self, *, limit=None):
        async def iterator():
            for user in self._users:
                yield user

        return iterator()


class ScheduleLatenessLogicTests(unittest.TestCase):
    def test_voice_quorum_is_two_below_minimum_with_floor_of_one(self):
        self.assertEqual(lateness_voice_quorum(5), 3)
        self.assertEqual(lateness_voice_quorum(4), 2)
        self.assertEqual(lateness_voice_quorum(3), 1)
        self.assertEqual(lateness_voice_quorum(1), 1)

    def test_start_datetime_handles_jst_and_24_hour_notation(self):
        self.assertEqual(
            schedule_start_datetime(date(2026, 8, 14), "21:00"),
            datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            schedule_start_datetime(date(2026, 8, 14), "24:00"),
            datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc),
        )

    def test_eligible_voters_are_cumulative_through_decided_time(self):
        self.assertEqual(
            eligible_voters_for_start(
                {
                    "20:00": {2, 3},
                    "20:30": {3, 4},
                    "21:00": {5},
                    "22:00": {6},
                    "NG": {7},
                },
                "21:00",
            ),
            {2, 3, 4, 5},
        )


class ScheduleLatenessRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.registry = ScheduleLatenessRegistry(
            f"{self.temp_directory.name}/schedule-polls.sqlite3"
        )

    def create_event(self, *, poll_message_id=100, finalized=False):
        return self.registry.upsert_event(
            poll_message_id=poll_message_id,
            guild_id=1,
            channel_id=10,
            event_date=date(2026, 8, 14),
            start_time="21:00",
            minimum=5,
            start_at=START_AT,
            finalized=finalized,
        )

    def test_snapshot_is_fixed_but_removals_cancel_and_reactions_restore(self):
        event = self.create_event()
        self.assertEqual(event.snapshot_at, START_AT - timedelta(minutes=15))
        self.assertEqual(event.tracking_until, START_AT + timedelta(hours=3))

        self.assertTrue(
            self.registry.snapshot_participants(
                event.poll_message_id,
                {2, 3, 4},
                snapshotted_at=event.snapshot_at,
            )
        )
        self.assertFalse(
            self.registry.snapshot_participants(
                event.poll_message_id,
                {2, 3, 4, 5},
                snapshotted_at=event.snapshot_at + timedelta(minutes=1),
            )
        )

        cancelled, restored = self.registry.sync_cancellations(
            event.poll_message_id,
            {2, 4, 5},
            changed_at=event.snapshot_at + timedelta(minutes=2),
        )
        self.assertEqual(cancelled, {3})
        self.assertEqual(restored, set())
        self.assertEqual(
            [participant.user_id for participant in self.registry.participants(100)],
            [2, 4],
        )
        self.assertNotIn(
            5,
            {
                participant.user_id
                for participant in self.registry.participants(
                    100,
                    include_cancelled=True,
                )
            },
        )

        cancelled, restored = self.registry.sync_cancellations(
            event.poll_message_id,
            {2, 3, 4, 5},
            changed_at=event.snapshot_at + timedelta(minutes=3),
        )
        self.assertEqual(cancelled, set())
        self.assertEqual(restored, {3})

    def test_quorum_activation_records_arrivals_and_monthly_stats(self):
        event = self.create_event()
        self.registry.snapshot_participants(
            event.poll_message_id,
            {2, 3, 4, 5},
            snapshotted_at=event.snapshot_at,
        )
        self.registry.sync_cancellations(
            event.poll_message_id,
            {2, 3, 4},
            changed_at=START_AT - timedelta(minutes=5),
        )

        activated = self.registry.activate(
            event.poll_message_id,
            voice_channel_id=20,
            activated_at=START_AT + timedelta(minutes=3),
            arrivals={
                2: START_AT - timedelta(minutes=1),
                3: START_AT + timedelta(minutes=2),
                4: START_AT + timedelta(minutes=3),
            },
        )
        self.assertTrue(activated)
        self.assertEqual(
            [attendance.late_seconds for attendance in self.registry.attendance(100)],
            [0, 120, 180],
        )

        self.assertEqual(
            self.registry.record_arrival(
                100,
                5,
                voice_channel_id=20,
                joined_at=START_AT + timedelta(minutes=4),
            ),
            None,
        )
        self.assertEqual(
            self.registry.record_arrival(
                100,
                3,
                voice_channel_id=20,
                joined_at=START_AT + timedelta(minutes=10),
            ),
            None,
        )

        stats = self.registry.monthly_stats(
            guild_id=1,
            month=date(2026, 8, 1),
        )
        self.assertEqual(
            [
                (
                    stat.user_id,
                    stat.count,
                    stat.total_seconds,
                    stat.average_seconds,
                    stat.maximum_seconds,
                )
                for stat in stats
            ],
            [
                (4, 1, 180, 180.0, 180),
                (3, 1, 120, 120.0, 120),
            ],
        )

    def test_pending_event_can_be_replaced_but_activated_event_is_frozen(self):
        event = self.create_event()
        moved = self.registry.upsert_event(
            poll_message_id=event.poll_message_id,
            guild_id=1,
            channel_id=10,
            event_date=date(2026, 8, 15),
            start_time="22:00",
            minimum=5,
            start_at=START_AT + timedelta(days=1, hours=1),
        )
        self.assertEqual(moved.event_date, date(2026, 8, 15))
        self.registry.snapshot_participants(
            moved.poll_message_id,
            {2, 3, 4},
            snapshotted_at=moved.snapshot_at,
        )
        self.registry.activate(
            moved.poll_message_id,
            voice_channel_id=20,
            activated_at=moved.start_at,
            arrivals={2: moved.start_at, 3: moved.start_at, 4: moved.start_at},
        )

        frozen = self.registry.upsert_event(
            poll_message_id=moved.poll_message_id,
            guild_id=1,
            channel_id=10,
            event_date=date(2026, 8, 16),
            start_time="23:00",
            minimum=3,
            start_at=moved.start_at + timedelta(days=1, hours=1),
            finalized=True,
        )
        self.assertEqual(frozen.event_date, moved.event_date)
        self.assertEqual(frozen.start_time, moved.start_time)
        self.assertEqual(frozen.minimum, 5)
        self.assertFalse(self.registry.cancel_pending(moved.poll_message_id))

    def test_three_hour_completion_records_absence_at_maximum_lateness(self):
        event = self.create_event()
        self.registry.snapshot_participants(
            event.poll_message_id,
            {2, 3, 4, 5},
            snapshotted_at=event.snapshot_at,
        )
        self.registry.activate(
            event.poll_message_id,
            voice_channel_id=20,
            activated_at=START_AT + timedelta(minutes=3),
            arrivals={
                2: START_AT + timedelta(minutes=1),
                3: START_AT + timedelta(minutes=2),
                4: START_AT + timedelta(minutes=3),
            },
        )

        self.assertEqual(
            self.registry.complete_tracking(
                event.poll_message_id,
                completed_at=START_AT + timedelta(hours=3),
            ),
            1,
        )
        attendance = {
            row.user_id: row.late_seconds
            for row in self.registry.attendance(event.poll_message_id)
        }
        self.assertEqual(attendance[5], LATENESS_MAX_SECONDS)
        self.assertEqual(
            self.registry.record_arrival(
                event.poll_message_id,
                5,
                voice_channel_id=20,
                joined_at=START_AT + timedelta(hours=3),
            ),
            None,
        )
        self.assertIsNotNone(
            self.registry.get_event(event.poll_message_id).tracking_completed_at
        )

    def test_disabled_poll_is_not_tracked_or_included_in_monthly_stats(self):
        event = self.create_event()
        self.registry.snapshot_participants(
            event.poll_message_id,
            {2},
            snapshotted_at=event.snapshot_at,
        )
        self.registry.activate(
            event.poll_message_id,
            voice_channel_id=20,
            activated_at=START_AT + timedelta(minutes=1),
            arrivals={2: START_AT + timedelta(minutes=1)},
        )
        self.assertTrue(
            self.registry.disable_poll(
                poll_message_id=event.poll_message_id,
                guild_id=1,
                channel_id=10,
                disabled_at=START_AT + timedelta(minutes=2),
            )
        )
        self.assertTrue(self.registry.is_disabled(event.poll_message_id))
        self.assertFalse(
            self.registry.disable_poll(
                poll_message_id=event.poll_message_id,
                guild_id=1,
                channel_id=10,
                disabled_at=START_AT + timedelta(minutes=3),
            )
        )
        self.assertEqual(
            self.registry.tracking_events(now=START_AT, guild_id=1),
            [],
        )
        self.assertEqual(
            self.registry.monthly_stats(
                guild_id=1,
                month=date(2026, 8, 1),
            ),
            [],
        )
        self.assertEqual(
            self.registry.complete_tracking(
                event.poll_message_id,
                completed_at=START_AT + timedelta(hours=3),
            ),
            0,
        )


class ScheduleLatenessVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        database_path = f"{self.temp_directory.name}/schedule-polls.sqlite3"
        self.schedule_registry = SchedulePollRegistry(database_path)
        self.lateness_registry = ScheduleLatenessRegistry(database_path)
        self.clock = {"now": START_AT - timedelta(minutes=16)}
        self.bot_user = SimpleNamespace(id=1, bot=True)
        self.members = {
            user_id: SimpleNamespace(
                id=user_id,
                bot=False,
                voice=SimpleNamespace(channel=None),
            )
            for user_id in range(2, 8)
        }
        self.guild = SimpleNamespace(
            id=1,
            get_member=lambda user_id: self.members.get(user_id),
        )
        for member in self.members.values():
            member.guild = self.guild
        self.channel = SimpleNamespace(id=10)
        self.bot = SimpleNamespace(
            user=self.bot_user,
            get_guild=lambda guild_id: self.guild if guild_id == 1 else None,
            get_channel=lambda channel_id: (
                self.channel if channel_id == self.channel.id else None
            ),
            fetch_channel=AsyncMock(return_value=self.channel),
        )
        self.cog = PollCog(
            self.bot,
            registry=self.schedule_registry,
            lateness_registry=self.lateness_registry,
            lateness_reaction_sleeper=AsyncMock(),
            now_provider=lambda: self.clock["now"],
        )

    def set_now(self, value):
        self.clock["now"] = value

    async def join_voice(self, user_id, voice_channel, *, at):
        self.set_now(at)
        member = self.members[user_id]
        before_channel = member.voice.channel
        member.voice = SimpleNamespace(channel=voice_channel)
        await self.cog.on_voice_state_update(
            member,
            SimpleNamespace(channel=before_channel),
            SimpleNamespace(channel=voice_channel),
        )

    async def test_post_snapshot_reaction_removal_cancels_without_adding_new_users(self):
        role = SimpleNamespace(name="GAME")
        creator = SimpleNamespace(id=77, display_name="creator")
        embed = build_schedule_embed(
            role,
            ["20:00", "21:00", "NG"],
            creator,
            auto_start=True,
        )
        set_schedule_date_override(embed, date(2026, 8, 14))
        mark_start_time_announced(embed, "21:00", 500)
        poll_message = SimpleNamespace(
            id=100,
            embeds=[embed],
            reactions=[],
            guild=self.guild,
        )
        self.channel.fetch_message = AsyncMock(return_value=poll_message)

        event = await self.cog._store_lateness_event(
            poll_message,
            guild_id=1,
            channel_id=10,
            start_time="21:00",
            current_eligible_user_ids={2, 3, 4},
            finalized=True,
        )
        self.assertIsNotNone(event)
        self.assertIsNone(event.snapshotted_at)

        self.set_now(START_AT - timedelta(minutes=15))
        event = await self.cog._store_lateness_event(
            poll_message,
            guild_id=1,
            channel_id=10,
            start_time="21:00",
            current_eligible_user_ids={2, 3, 4},
            finalized=True,
        )
        self.assertIsNotNone(event.snapshotted_at)

        humans = {
            user_id: SimpleNamespace(id=user_id, bot=False)
            for user_id in (2, 3, 4, 5)
        }
        emojis = schedule_option_emojis(["20:00", "21:00", "NG"])
        poll_message.reactions = [
            FakeReaction(emojis[0], [humans[2]]),
            FakeReaction(emojis[1], [humans[3], humans[5]]),
            FakeReaction(emojis[2], [humans[4]]),
        ]
        self.set_now(START_AT - timedelta(minutes=10))
        await self.cog._run_lateness_reaction_sync(poll_message.id)

        all_participants = self.lateness_registry.participants(
            poll_message.id,
            include_cancelled=True,
        )
        self.assertEqual(
            {
                participant.user_id: participant.cancelled_at is not None
                for participant in all_participants
            },
            {2: False, 3: False, 4: True},
        )

        poll_message.reactions[1] = FakeReaction(
            emojis[1],
            [humans[3], humans[4], humans[5]],
        )
        await self.cog._run_lateness_reaction_sync(poll_message.id)
        self.assertEqual(
            [
                participant.user_id
                for participant in self.lateness_registry.participants(
                    poll_message.id
                )
            ],
            [2, 3, 4],
        )

    async def test_three_participants_activate_vc_and_later_joins_are_recorded(self):
        event = self.lateness_registry.upsert_event(
            poll_message_id=100,
            guild_id=1,
            channel_id=10,
            event_date=date(2026, 8, 14),
            start_time="21:00",
            minimum=5,
            start_at=START_AT,
            finalized=True,
        )
        self.lateness_registry.snapshot_participants(
            event.poll_message_id,
            {2, 3, 4, 5, 6},
            snapshotted_at=event.snapshot_at,
        )
        self.lateness_registry.sync_cancellations(
            event.poll_message_id,
            {2, 3, 4, 5},
            changed_at=START_AT - timedelta(minutes=5),
        )
        voice_channel = SimpleNamespace(id=20)

        await self.join_voice(
            2,
            voice_channel,
            at=START_AT + timedelta(minutes=1),
        )
        await self.join_voice(
            3,
            voice_channel,
            at=START_AT + timedelta(minutes=2),
        )
        self.assertIsNone(
            self.lateness_registry.get_event(event.poll_message_id).activated_at
        )

        await self.join_voice(
            4,
            voice_channel,
            at=START_AT + timedelta(minutes=3),
        )
        activated = self.lateness_registry.get_event(event.poll_message_id)
        self.assertEqual(activated.voice_channel_id, voice_channel.id)
        self.assertEqual(
            [
                attendance.late_seconds
                for attendance in self.lateness_registry.attendance(
                    event.poll_message_id
                )
            ],
            [60, 120, 180],
        )

        await self.join_voice(
            5,
            voice_channel,
            at=START_AT + timedelta(minutes=7),
        )
        await self.join_voice(
            6,
            voice_channel,
            at=START_AT + timedelta(minutes=8),
        )
        self.assertEqual(
            {
                attendance.user_id: attendance.late_seconds
                for attendance in self.lateness_registry.attendance(
                    event.poll_message_id
                )
            },
            {2: 60, 3: 120, 4: 180, 5: 420},
        )

    async def test_lateoff_command_disables_future_tracking_for_latest_poll(self):
        creator = SimpleNamespace(id=77, display_name="creator")
        role = SimpleNamespace(name="GAME")
        poll_message = SimpleNamespace(
            id=100,
            author=self.bot_user,
            embeds=[
                build_schedule_embed(
                    role,
                    ["20:00", "21:00", "NG"],
                    creator,
                    auto_start=True,
                )
            ],
            reactions=[],
            guild=self.guild,
            jump_url="https://discord.com/channels/1/10/100",
        )
        self.channel.fetch_message = AsyncMock(return_value=poll_message)
        self.channel.permissions_for = lambda member: SimpleNamespace(
            manage_messages=False
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=self.bot_user),
            channel=self.channel,
            author=creator,
            interaction=object(),
            defer=AsyncMock(),
            send=AsyncMock(),
        )
        self.cog._resolve_schedule_message_id = AsyncMock(return_value=100)

        await PollCog.schedule_lateoff.callback(self.cog, ctx)

        self.cog._resolve_schedule_message_id.assert_awaited_once_with(ctx, None)
        self.assertTrue(self.lateness_registry.is_disabled(poll_message.id))
        self.assertIn("月次統計", ctx.send.await_args.args[0])
        self.assertIsNone(
            await self.cog._store_lateness_event(
                poll_message,
                guild_id=1,
                channel_id=10,
                start_time="21:00",
                current_eligible_user_ids={2, 3, 4},
            )
        )

    async def test_tracking_task_finalizes_absence_after_three_hours(self):
        event = self.lateness_registry.upsert_event(
            poll_message_id=100,
            guild_id=1,
            channel_id=10,
            event_date=date(2026, 8, 14),
            start_time="21:00",
            minimum=5,
            start_at=START_AT,
            finalized=True,
        )
        self.lateness_registry.snapshot_participants(
            event.poll_message_id,
            {2, 3, 4, 5},
            snapshotted_at=event.snapshot_at,
        )
        self.lateness_registry.activate(
            event.poll_message_id,
            voice_channel_id=20,
            activated_at=START_AT + timedelta(minutes=3),
            arrivals={
                2: START_AT + timedelta(minutes=1),
                3: START_AT + timedelta(minutes=2),
                4: START_AT + timedelta(minutes=3),
            },
        )
        self.set_now(START_AT + timedelta(hours=3))

        await self.cog._run_lateness_event_task(event.poll_message_id)

        self.assertEqual(
            {
                attendance.user_id: attendance.late_seconds
                for attendance in self.lateness_registry.attendance(
                    event.poll_message_id
                )
            }[5],
            LATENESS_MAX_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
