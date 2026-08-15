import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


LATENESS_SNAPSHOT_MINUTES = 15
LATENESS_TRACKING_HOURS = 3
LATENESS_MAX_SECONDS = LATENESS_TRACKING_HOURS * 60 * 60
LATENESS_REMINDER_MINUTES = (15, 30, 60, 120)


@dataclass(frozen=True)
class ScheduleLatenessEvent:
    poll_message_id: int
    guild_id: int
    channel_id: int
    event_date: date
    start_time: str
    minimum: int
    start_at: datetime
    snapshot_at: datetime
    tracking_until: datetime
    snapshotted_at: datetime | None = None
    activated_at: datetime | None = None
    tracking_completed_at: datetime | None = None
    voice_channel_id: int | None = None
    finalized: bool = False


@dataclass(frozen=True)
class ScheduleLatenessParticipant:
    poll_message_id: int
    user_id: int
    cancelled_at: datetime | None = None


@dataclass(frozen=True)
class ScheduleLatenessAttendance:
    poll_message_id: int
    guild_id: int
    event_date: date
    user_id: int
    voice_channel_id: int
    joined_at: datetime
    late_seconds: int


@dataclass(frozen=True)
class MonthlyLatenessStat:
    user_id: int
    count: int
    total_seconds: int
    average_seconds: float
    maximum_seconds: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _from_timestamp(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc)


def lateness_voice_quorum(minimum: int) -> int:
    """VC開催判定人数。最低人数が小さい場合も最低1人で判定する。"""
    if minimum < 1:
        raise ValueError("minimum must be positive")
    return max(1, minimum - 2)


class ScheduleLatenessRegistry:
    """開始15分前の参加予定とVC到着時刻を保存するSQLiteレジストリ。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schedule_lateness_events (
                poll_message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                event_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                minimum INTEGER NOT NULL DEFAULT 5,
                start_at REAL NOT NULL,
                snapshot_at REAL NOT NULL,
                tracking_until REAL NOT NULL,
                snapshotted_at REAL,
                activated_at REAL,
                tracking_completed_at REAL,
                voice_channel_id INTEGER,
                finalized INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS schedule_lateness_participants (
                poll_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                cancelled_at REAL,
                PRIMARY KEY (poll_message_id, user_id),
                FOREIGN KEY (poll_message_id)
                    REFERENCES schedule_lateness_events (poll_message_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS schedule_lateness_presence (
                poll_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                joined_at REAL NOT NULL,
                PRIMARY KEY (poll_message_id, user_id),
                FOREIGN KEY (poll_message_id)
                    REFERENCES schedule_lateness_events (poll_message_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS schedule_lateness_attendance (
                poll_message_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                event_date TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                joined_at REAL NOT NULL,
                late_seconds INTEGER NOT NULL,
                PRIMARY KEY (poll_message_id, user_id),
                FOREIGN KEY (poll_message_id)
                    REFERENCES schedule_lateness_events (poll_message_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS schedule_lateness_disabled (
                poll_message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                disabled_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedule_lateness_reminders (
                poll_message_id INTEGER NOT NULL,
                threshold_minutes INTEGER NOT NULL,
                processed_at REAL NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (poll_message_id, threshold_minutes),
                FOREIGN KEY (poll_message_id)
                    REFERENCES schedule_lateness_events (poll_message_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS schedule_lateness_events_guild_time
                ON schedule_lateness_events (guild_id, snapshot_at, tracking_until);
            CREATE INDEX IF NOT EXISTS schedule_lateness_attendance_month
                ON schedule_lateness_attendance (guild_id, event_date);
            """
        )
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(schedule_lateness_events)"
            )
        }
        if "minimum" not in columns:
            connection.execute(
                """
                ALTER TABLE schedule_lateness_events
                ADD COLUMN minimum INTEGER NOT NULL DEFAULT 5
                """
            )
        if "tracking_completed_at" not in columns:
            connection.execute(
                """
                ALTER TABLE schedule_lateness_events
                ADD COLUMN tracking_completed_at REAL
                """
            )
        return connection

    def upsert_event(
        self,
        *,
        poll_message_id: int,
        guild_id: int,
        channel_id: int,
        event_date: date,
        start_time: str,
        minimum: int,
        start_at: datetime,
        finalized: bool = False,
    ) -> ScheduleLatenessEvent:
        if minimum < 1:
            raise ValueError("minimum must be positive")
        start_at = _as_utc(start_at)
        snapshot_at = start_at - timedelta(minutes=LATENESS_SNAPSHOT_MINUTES)
        tracking_until = start_at + timedelta(hours=LATENESS_TRACKING_HOURS)
        with closing(self._connect()) as connection:
            with connection:
                existing_row = connection.execute(
                    """
                    SELECT poll_message_id, guild_id, channel_id, event_date,
                           start_time, minimum, start_at, snapshot_at, tracking_until,
                           snapshotted_at, activated_at, tracking_completed_at,
                           voice_channel_id, finalized
                    FROM schedule_lateness_events
                    WHERE poll_message_id = ?
                    """,
                    (poll_message_id,),
                ).fetchone()
                if existing_row is not None:
                    existing = self._event_from_row(existing_row)
                    if existing.activated_at is not None:
                        if finalized and not existing.finalized:
                            connection.execute(
                                """
                                UPDATE schedule_lateness_events
                                SET finalized = 1
                                WHERE poll_message_id = ?
                                """,
                                (poll_message_id,),
                            )
                            return replace(existing, finalized=True)
                        return existing
                    same_schedule = (
                        existing.guild_id == guild_id
                        and existing.channel_id == channel_id
                        and existing.event_date == event_date
                        and existing.start_time == start_time
                        and existing.start_at == start_at
                    )
                    if not same_schedule:
                        connection.execute(
                            "DELETE FROM schedule_lateness_events WHERE poll_message_id = ?",
                            (poll_message_id,),
                        )

                connection.execute(
                    """
                    INSERT INTO schedule_lateness_events (
                        poll_message_id, guild_id, channel_id, event_date,
                        start_time, minimum, start_at, snapshot_at,
                        tracking_until, finalized
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(poll_message_id) DO UPDATE SET
                        guild_id = excluded.guild_id,
                        channel_id = excluded.channel_id,
                        minimum = excluded.minimum,
                        finalized = MAX(
                            schedule_lateness_events.finalized,
                            excluded.finalized
                        )
                    """,
                    (
                        poll_message_id,
                        guild_id,
                        channel_id,
                        event_date.isoformat(),
                        start_time,
                        minimum,
                        start_at.timestamp(),
                        snapshot_at.timestamp(),
                        tracking_until.timestamp(),
                        int(finalized),
                    ),
                )
        event = self.get_event(poll_message_id)
        if event is None:
            raise RuntimeError("failed to store schedule lateness event")
        return event

    def get_event(self, poll_message_id: int) -> ScheduleLatenessEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT poll_message_id, guild_id, channel_id, event_date,
                       start_time, minimum, start_at, snapshot_at, tracking_until,
                       snapshotted_at, activated_at, tracking_completed_at,
                       voice_channel_id, finalized
                FROM schedule_lateness_events
                WHERE poll_message_id = ?
                """,
                (poll_message_id,),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def tracking_events(
        self,
        *,
        now: datetime,
        guild_id: int | None = None,
    ) -> list[ScheduleLatenessEvent]:
        query = """
            SELECT event.poll_message_id, event.guild_id, event.channel_id,
                   event.event_date, event.start_time, event.minimum,
                   event.start_at, event.snapshot_at, event.tracking_until,
                   event.snapshotted_at, event.activated_at,
                   event.tracking_completed_at,
                   event.voice_channel_id, event.finalized
            FROM schedule_lateness_events AS event
            WHERE event.tracking_completed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM schedule_lateness_disabled AS disabled
                  WHERE disabled.poll_message_id = event.poll_message_id
              )
        """
        parameters: list[int] = []
        if guild_id is not None:
            query += " AND event.guild_id = ?"
            parameters.append(guild_id)
        query += " ORDER BY event.start_at, event.poll_message_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._event_from_row(row) for row in rows]

    def disable_poll(
        self,
        *,
        poll_message_id: int,
        guild_id: int,
        channel_id: int,
        disabled_at: datetime,
    ) -> bool:
        """募集単位で遅刻記録・集計・通知を恒久的に無効化する。"""
        with closing(self._connect()) as connection:
            with connection:
                already_disabled = connection.execute(
                    """
                    SELECT 1 FROM schedule_lateness_disabled
                    WHERE poll_message_id = ?
                    """,
                    (poll_message_id,),
                ).fetchone() is not None
                connection.execute(
                    """
                    INSERT INTO schedule_lateness_disabled (
                        poll_message_id, guild_id, channel_id, disabled_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(poll_message_id) DO UPDATE SET
                        guild_id = excluded.guild_id,
                        channel_id = excluded.channel_id
                    """,
                    (
                        poll_message_id,
                        guild_id,
                        channel_id,
                        _as_utc(disabled_at).timestamp(),
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM schedule_lateness_presence
                    WHERE poll_message_id = ?
                    """,
                    (poll_message_id,),
                )
        return not already_disabled

    def is_disabled(self, poll_message_id: int) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM schedule_lateness_disabled
                WHERE poll_message_id = ?
                """,
                (poll_message_id,),
            ).fetchone()
        return row is not None

    def delete_poll(self, poll_message_id: int) -> bool:
        """募集に紐づく遅刻判定・参加者・到着・無効化設定を完全削除する。"""
        with closing(self._connect()) as connection:
            with connection:
                event_cursor = connection.execute(
                    """
                    DELETE FROM schedule_lateness_events
                    WHERE poll_message_id = ?
                    """,
                    (poll_message_id,),
                )
                disabled_cursor = connection.execute(
                    """
                    DELETE FROM schedule_lateness_disabled
                    WHERE poll_message_id = ?
                    """,
                    (poll_message_id,),
                )
        return event_cursor.rowcount > 0 or disabled_cursor.rowcount > 0

    def cancel_pending(self, poll_message_id: int) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM schedule_lateness_events
                    WHERE poll_message_id = ? AND activated_at IS NULL
                    """,
                    (poll_message_id,),
                )
        return cursor.rowcount > 0

    def snapshot_participants(
        self,
        poll_message_id: int,
        user_ids: set[int],
        *,
        snapshotted_at: datetime,
    ) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                event_row = connection.execute(
                    """
                    SELECT snapshotted_at, activated_at
                    FROM schedule_lateness_events
                    WHERE poll_message_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM schedule_lateness_disabled
                          WHERE schedule_lateness_disabled.poll_message_id =
                                schedule_lateness_events.poll_message_id
                      )
                    """,
                    (poll_message_id,),
                ).fetchone()
                if (
                    event_row is None
                    or event_row[0] is not None
                    or event_row[1] is not None
                ):
                    return False
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO schedule_lateness_participants
                        (poll_message_id, user_id, cancelled_at)
                    VALUES (?, ?, NULL)
                    """,
                    (
                        (poll_message_id, user_id)
                        for user_id in sorted(user_ids)
                    ),
                )
                connection.execute(
                    """
                    UPDATE schedule_lateness_events
                    SET snapshotted_at = ?
                    WHERE poll_message_id = ?
                    """,
                    (_as_utc(snapshotted_at).timestamp(), poll_message_id),
                )
        return True

    def participants(
        self,
        poll_message_id: int,
        *,
        include_cancelled: bool = False,
    ) -> list[ScheduleLatenessParticipant]:
        query = """
            SELECT poll_message_id, user_id, cancelled_at
            FROM schedule_lateness_participants
            WHERE poll_message_id = ?
        """
        if not include_cancelled:
            query += " AND cancelled_at IS NULL"
        query += " ORDER BY user_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, (poll_message_id,)).fetchall()
        return [
            ScheduleLatenessParticipant(
                poll_message_id=row[0],
                user_id=row[1],
                cancelled_at=_from_timestamp(row[2]),
            )
            for row in rows
        ]

    def unarrived_participant_user_ids(
        self,
        poll_message_id: int,
    ) -> list[int]:
        """開催VCへ一度も入っていない、未キャンセルの固定メンバーを返す。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT participant.user_id
                FROM schedule_lateness_participants AS participant
                JOIN schedule_lateness_events AS event
                  ON event.poll_message_id = participant.poll_message_id
                LEFT JOIN schedule_lateness_attendance AS attendance
                  ON attendance.poll_message_id = participant.poll_message_id
                 AND attendance.user_id = participant.user_id
                WHERE participant.poll_message_id = ?
                  AND participant.cancelled_at IS NULL
                  AND attendance.user_id IS NULL
                  AND event.activated_at IS NOT NULL
                  AND event.tracking_completed_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM schedule_lateness_disabled AS disabled
                      WHERE disabled.poll_message_id = event.poll_message_id
                  )
                ORDER BY participant.user_id
                """,
                (poll_message_id,),
            ).fetchall()
        return [row[0] for row in rows]

    def reminder_history(self, poll_message_id: int) -> dict[int, bool]:
        """処理済みの節目と、実際に通知したかどうかを返す。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT threshold_minutes, notified
                FROM schedule_lateness_reminders
                WHERE poll_message_id = ?
                ORDER BY threshold_minutes
                """,
                (poll_message_id,),
            ).fetchall()
        return {row[0]: bool(row[1]) for row in rows}

    def record_processed_reminders(
        self,
        poll_message_id: int,
        thresholds: set[int],
        *,
        processed_at: datetime,
        notified_threshold: int | None = None,
    ) -> int:
        """期限到達済みの節目を記録し、再起動後の重複通知を防ぐ。"""
        if notified_threshold is not None and notified_threshold not in thresholds:
            raise ValueError("notified threshold must be processed")
        if any(threshold <= 0 for threshold in thresholds):
            raise ValueError("reminder thresholds must be positive")
        processed_timestamp = _as_utc(processed_at).timestamp()
        inserted = 0
        with closing(self._connect()) as connection:
            with connection:
                for threshold in sorted(thresholds):
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO schedule_lateness_reminders (
                            poll_message_id, threshold_minutes,
                            processed_at, notified
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            poll_message_id,
                            threshold,
                            processed_timestamp,
                            int(threshold == notified_threshold),
                        ),
                    )
                    inserted += cursor.rowcount
        return inserted

    def sync_cancellations(
        self,
        poll_message_id: int,
        current_eligible_user_ids: set[int],
        *,
        changed_at: datetime,
    ) -> tuple[set[int], set[int]]:
        """固定メンバーだけを対象に、解除・再追加状態を同期する。"""
        cancelled: set[int] = set()
        restored: set[int] = set()
        changed_timestamp = _as_utc(changed_at).timestamp()
        with closing(self._connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    SELECT participant.user_id, participant.cancelled_at,
                           attendance.user_id IS NOT NULL
                    FROM schedule_lateness_participants AS participant
                    LEFT JOIN schedule_lateness_attendance AS attendance
                      ON attendance.poll_message_id = participant.poll_message_id
                     AND attendance.user_id = participant.user_id
                    WHERE participant.poll_message_id = ?
                    """,
                    (poll_message_id,),
                ).fetchall()
                for user_id, cancelled_at, has_attended in rows:
                    if user_id in current_eligible_user_ids:
                        if cancelled_at is not None:
                            connection.execute(
                                """
                                UPDATE schedule_lateness_participants
                                SET cancelled_at = NULL
                                WHERE poll_message_id = ? AND user_id = ?
                                """,
                                (poll_message_id, user_id),
                            )
                            restored.add(user_id)
                    elif cancelled_at is None and not has_attended:
                        connection.execute(
                            """
                            UPDATE schedule_lateness_participants
                            SET cancelled_at = ?
                            WHERE poll_message_id = ? AND user_id = ?
                            """,
                            (changed_timestamp, poll_message_id, user_id),
                        )
                        cancelled.add(user_id)
        return cancelled, restored

    def set_presence(
        self,
        poll_message_id: int,
        user_id: int,
        *,
        voice_channel_id: int,
        joined_at: datetime,
    ) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                eligible = connection.execute(
                    """
                    SELECT 1
                    FROM schedule_lateness_events AS event
                    JOIN schedule_lateness_participants AS participant
                      ON participant.poll_message_id = event.poll_message_id
                    WHERE event.poll_message_id = ?
                      AND participant.user_id = ?
                      AND participant.cancelled_at IS NULL
                      AND event.snapshotted_at IS NOT NULL
                      AND event.activated_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM schedule_lateness_disabled AS disabled
                          WHERE disabled.poll_message_id = event.poll_message_id
                      )
                    """,
                    (poll_message_id, user_id),
                ).fetchone()
                if eligible is None:
                    return False
                connection.execute(
                    """
                    INSERT INTO schedule_lateness_presence (
                        poll_message_id, user_id, voice_channel_id, joined_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(poll_message_id, user_id) DO UPDATE SET
                        voice_channel_id = excluded.voice_channel_id,
                        joined_at = excluded.joined_at
                    """,
                    (
                        poll_message_id,
                        user_id,
                        voice_channel_id,
                        _as_utc(joined_at).timestamp(),
                    ),
                )
        return True

    def clear_presence(
        self,
        poll_message_id: int,
        user_id: int,
        *,
        voice_channel_id: int | None = None,
    ) -> None:
        query = """
            DELETE FROM schedule_lateness_presence
            WHERE poll_message_id = ? AND user_id = ?
        """
        parameters: list[int] = [poll_message_id, user_id]
        if voice_channel_id is not None:
            query += " AND voice_channel_id = ?"
            parameters.append(voice_channel_id)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(query, parameters)

    def presence(self, poll_message_id: int) -> dict[int, tuple[int, datetime]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT user_id, voice_channel_id, joined_at
                FROM schedule_lateness_presence
                WHERE poll_message_id = ?
                """,
                (poll_message_id,),
            ).fetchall()
        return {
            row[0]: (row[1], datetime.fromtimestamp(row[2], timezone.utc))
            for row in rows
        }

    def activate(
        self,
        poll_message_id: int,
        *,
        voice_channel_id: int,
        activated_at: datetime,
        arrivals: dict[int, datetime],
    ) -> bool:
        activated_at = _as_utc(activated_at)
        with closing(self._connect()) as connection:
            with connection:
                event_row = connection.execute(
                    """
                    SELECT guild_id, event_date, start_at, tracking_until,
                           activated_at, tracking_completed_at
                    FROM schedule_lateness_events
                    WHERE poll_message_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM schedule_lateness_disabled
                          WHERE schedule_lateness_disabled.poll_message_id =
                                schedule_lateness_events.poll_message_id
                      )
                    """,
                    (poll_message_id,),
                ).fetchone()
                if (
                    event_row is None
                    or event_row[4] is not None
                    or event_row[5] is not None
                ):
                    return False
                (
                    guild_id,
                    event_date,
                    start_timestamp,
                    tracking_timestamp,
                    _,
                    _,
                ) = event_row
                if activated_at.timestamp() > tracking_timestamp:
                    return False
                start_at = datetime.fromtimestamp(start_timestamp, timezone.utc)
                active_users = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT user_id
                        FROM schedule_lateness_participants
                        WHERE poll_message_id = ? AND cancelled_at IS NULL
                        """,
                        (poll_message_id,),
                    )
                }
                connection.execute(
                    """
                    UPDATE schedule_lateness_events
                    SET activated_at = ?, voice_channel_id = ?
                    WHERE poll_message_id = ?
                    """,
                    (
                        activated_at.timestamp(),
                        voice_channel_id,
                        poll_message_id,
                    ),
                )
                for user_id, joined_at in arrivals.items():
                    if user_id not in active_users:
                        continue
                    joined_at = _as_utc(joined_at)
                    late_seconds = min(
                        LATENESS_MAX_SECONDS,
                        max(
                            0,
                            int((joined_at - start_at).total_seconds()),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO schedule_lateness_attendance (
                            poll_message_id, guild_id, event_date, user_id,
                            voice_channel_id, joined_at, late_seconds
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            poll_message_id,
                            guild_id,
                            event_date,
                            user_id,
                            voice_channel_id,
                            joined_at.timestamp(),
                            late_seconds,
                        ),
                    )
                connection.execute(
                    "DELETE FROM schedule_lateness_presence WHERE poll_message_id = ?",
                    (poll_message_id,),
                )
        return True

    def record_arrival(
        self,
        poll_message_id: int,
        user_id: int,
        *,
        voice_channel_id: int,
        joined_at: datetime,
    ) -> int | None:
        joined_at = _as_utc(joined_at)
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute(
                    """
                    SELECT event.guild_id, event.event_date, event.start_at,
                           event.tracking_until
                    FROM schedule_lateness_events AS event
                    JOIN schedule_lateness_participants AS participant
                      ON participant.poll_message_id = event.poll_message_id
                    WHERE event.poll_message_id = ?
                      AND participant.user_id = ?
                      AND participant.cancelled_at IS NULL
                      AND event.activated_at IS NOT NULL
                      AND event.tracking_completed_at IS NULL
                      AND event.voice_channel_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM schedule_lateness_disabled AS disabled
                          WHERE disabled.poll_message_id = event.poll_message_id
                      )
                    """,
                    (poll_message_id, user_id, voice_channel_id),
                ).fetchone()
                if row is None:
                    return None
                guild_id, event_date, start_timestamp, tracking_timestamp = row
                if joined_at.timestamp() > tracking_timestamp:
                    return None
                start_at = datetime.fromtimestamp(start_timestamp, timezone.utc)
                late_seconds = min(
                    LATENESS_MAX_SECONDS,
                    max(
                        0,
                        int((joined_at - start_at).total_seconds()),
                    ),
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO schedule_lateness_attendance (
                        poll_message_id, guild_id, event_date, user_id,
                        voice_channel_id, joined_at, late_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        poll_message_id,
                        guild_id,
                        event_date,
                        user_id,
                        voice_channel_id,
                        joined_at.timestamp(),
                        late_seconds,
                    ),
                )
        return late_seconds if cursor.rowcount > 0 else None

    def complete_tracking_without_absences(
        self,
        poll_message_id: int,
        *,
        completed_at: datetime,
    ) -> bool:
        """最低人数がVCにそろった追跡を、欠席記録なしで正常終了する。"""
        completed_timestamp = _as_utc(completed_at).timestamp()
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE schedule_lateness_events
                    SET tracking_completed_at = ?
                    WHERE poll_message_id = ?
                      AND activated_at IS NOT NULL
                      AND tracking_completed_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM schedule_lateness_disabled AS disabled
                          WHERE disabled.poll_message_id =
                                schedule_lateness_events.poll_message_id
                      )
                    """,
                    (completed_timestamp, poll_message_id),
                )
                connection.execute(
                    """
                    DELETE FROM schedule_lateness_presence
                    WHERE poll_message_id = ?
                    """,
                    (poll_message_id,),
                )
        return cursor.rowcount > 0

    def complete_tracking(
        self,
        poll_message_id: int,
        *,
        completed_at: datetime,
    ) -> int:
        """3時間で追跡を閉じ、未参加者を最大遅刻（欠席）として記録する。"""
        completed_at = _as_utc(completed_at)
        recorded_absences = 0
        with closing(self._connect()) as connection:
            with connection:
                event_row = connection.execute(
                    """
                    SELECT guild_id, event_date, start_at, tracking_until,
                           activated_at, tracking_completed_at, voice_channel_id
                    FROM schedule_lateness_events
                    WHERE poll_message_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM schedule_lateness_disabled
                          WHERE schedule_lateness_disabled.poll_message_id =
                                schedule_lateness_events.poll_message_id
                      )
                    """,
                    (poll_message_id,),
                ).fetchone()
                if event_row is None or event_row[5] is not None:
                    return 0
                (
                    guild_id,
                    event_date,
                    start_timestamp,
                    tracking_timestamp,
                    activated_at,
                    _,
                    voice_channel_id,
                ) = event_row
                if completed_at.timestamp() < tracking_timestamp:
                    return 0
                if activated_at is not None and voice_channel_id is not None:
                    absent_user_ids = [
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT participant.user_id
                            FROM schedule_lateness_participants AS participant
                            LEFT JOIN schedule_lateness_attendance AS attendance
                              ON attendance.poll_message_id =
                                 participant.poll_message_id
                             AND attendance.user_id = participant.user_id
                            WHERE participant.poll_message_id = ?
                              AND participant.cancelled_at IS NULL
                              AND attendance.user_id IS NULL
                            """,
                            (poll_message_id,),
                        )
                    ]
                    late_seconds = min(
                        LATENESS_MAX_SECONDS,
                        max(0, int(tracking_timestamp - start_timestamp)),
                    )
                    for user_id in absent_user_ids:
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO schedule_lateness_attendance (
                                poll_message_id, guild_id, event_date, user_id,
                                voice_channel_id, joined_at, late_seconds
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                poll_message_id,
                                guild_id,
                                event_date,
                                user_id,
                                voice_channel_id,
                                tracking_timestamp,
                                late_seconds,
                            ),
                        )
                        recorded_absences += cursor.rowcount
                connection.execute(
                    """
                    UPDATE schedule_lateness_events
                    SET tracking_completed_at = ?
                    WHERE poll_message_id = ?
                    """,
                    (completed_at.timestamp(), poll_message_id),
                )
                connection.execute(
                    """
                    DELETE FROM schedule_lateness_presence
                    WHERE poll_message_id = ?
                    """,
                    (poll_message_id,),
                )
        return recorded_absences

    def attendance(
        self,
        poll_message_id: int,
    ) -> list[ScheduleLatenessAttendance]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT poll_message_id, guild_id, event_date, user_id,
                       voice_channel_id, joined_at, late_seconds
                FROM schedule_lateness_attendance
                WHERE poll_message_id = ?
                ORDER BY user_id
                """,
                (poll_message_id,),
            ).fetchall()
        return [
            ScheduleLatenessAttendance(
                poll_message_id=row[0],
                guild_id=row[1],
                event_date=date.fromisoformat(row[2]),
                user_id=row[3],
                voice_channel_id=row[4],
                joined_at=datetime.fromtimestamp(row[5], timezone.utc),
                late_seconds=row[6],
            )
            for row in rows
        ]

    def monthly_stats(
        self,
        *,
        guild_id: int,
        month: date,
    ) -> list[MonthlyLatenessStat]:
        month_start = month.replace(day=1)
        month_end = (
            month_start.replace(year=month_start.year + 1, month=1)
            if month_start.month == 12
            else month_start.replace(month=month_start.month + 1)
        )
        return self.stats_between(
            guild_id=guild_id,
            start_date=month_start,
            end_date=month_end,
        )

    def stats_between(
        self,
        *,
        guild_id: int,
        start_date: date,
        end_date: date,
    ) -> list[MonthlyLatenessStat]:
        if end_date <= start_date:
            raise ValueError("end_date must be after start_date")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT attendance.user_id, COUNT(*),
                       SUM(attendance.late_seconds),
                       AVG(attendance.late_seconds),
                       MAX(attendance.late_seconds)
                FROM schedule_lateness_attendance AS attendance
                WHERE attendance.guild_id = ?
                  AND attendance.event_date >= ?
                  AND attendance.event_date < ?
                  AND attendance.late_seconds > 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM schedule_lateness_disabled AS disabled
                      WHERE disabled.poll_message_id = attendance.poll_message_id
                  )
                GROUP BY attendance.user_id
                ORDER BY SUM(attendance.late_seconds) DESC,
                         COUNT(*) DESC, attendance.user_id
                """,
                (guild_id, start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            MonthlyLatenessStat(
                user_id=row[0],
                count=row[1],
                total_seconds=row[2],
                average_seconds=row[3],
                maximum_seconds=row[4],
            )
            for row in rows
        ]

    def prune_expired_presence(self, *, now: datetime) -> int:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM schedule_lateness_presence
                    WHERE poll_message_id IN (
                        SELECT poll_message_id
                        FROM schedule_lateness_events
                        WHERE tracking_until < ?
                    )
                    """,
                    (_as_utc(now).timestamp(),),
                )
        return cursor.rowcount

    @staticmethod
    def _event_from_row(row) -> ScheduleLatenessEvent:
        return ScheduleLatenessEvent(
            poll_message_id=row[0],
            guild_id=row[1],
            channel_id=row[2],
            event_date=date.fromisoformat(row[3]),
            start_time=row[4],
            minimum=row[5],
            start_at=datetime.fromtimestamp(row[6], timezone.utc),
            snapshot_at=datetime.fromtimestamp(row[7], timezone.utc),
            tracking_until=datetime.fromtimestamp(row[8], timezone.utc),
            snapshotted_at=_from_timestamp(row[9]),
            activated_at=_from_timestamp(row[10]),
            tracking_completed_at=_from_timestamp(row[11]),
            voice_channel_id=row[12],
            finalized=bool(row[13]),
        )
