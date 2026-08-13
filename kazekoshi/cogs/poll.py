import asyncio
import re
import shlex
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from typing import Optional
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from kazekoshi.schedule_lateness import (
    LATENESS_REMINDER_MINUTES,
    LATENESS_TRACKING_HOURS,
    MonthlyLatenessStat,
    ScheduleLatenessEvent,
    ScheduleLatenessRegistry,
    lateness_voice_quorum,
)

logger = getLogger(__name__)
EMOJI_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
EMOJI_LETTERS = [chr(0x1F1E6 + index) for index in range(26)]
MAX_MESSAGE_REACTIONS = 20
OPTION_EMOJIS = EMOJI_NUMBERS + EMOJI_LETTERS[
    :MAX_MESSAGE_REACTIONS - len(EMOJI_NUMBERS)
]
EMOJI_NG = "🆖"
SCHEDULE_TITLE_PREFIX = "📅 "
SCHEDULE_TITLE_MINIMUM_PATTERN = re.compile(
    r"\s+\[(?P<minimum>[1-9]\d{0,2})人\]$"
)
SCHEDULE_FOOTER_PATTERN = re.compile(r"\|\s*作成者ID:\s*(\d+)\s*$")
MESSAGE_LINK_PATTERN = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)"
)
ROLE_MENTION_PATTERN = re.compile(r"<@&(\d+)>")
MIN_SCHEDULE_OPTIONS = 2
MAX_SCHEDULE_OPTIONS = len(OPTION_EMOJIS)
MAX_SCHEDULE_OPTION_LENGTH = 100
DEFAULT_SCHEDULE_OPTION_LIST = (
    "20:00",
    "20:30",
    "21:00",
    "21:30",
    "22:00",
    "22:30",
    "23:00",
    "24:00",
    "NG",
)
DEFAULT_SCHEDULE_OPTIONS = " ".join(DEFAULT_SCHEDULE_OPTION_LIST)
DEFAULT_TIME_OPTIONS = DEFAULT_SCHEDULE_OPTION_LIST[:-1]
DEFAULT_TIME_EMOJIS = tuple(OPTION_EMOJIS[:len(DEFAULT_TIME_OPTIONS)])
DEFAULT_SCHEDULE_EMOJIS = DEFAULT_TIME_EMOJIS + (EMOJI_NG,)
SCHEDULE_REACTION_EMOJIS = tuple(OPTION_EMOJIS) + (EMOJI_NG,)
SCHEDULE_ENTRY_PATTERN = re.compile(
    rf"(?:^|, )(?P<emoji>{'|'.join(re.escape(emoji) for emoji in SCHEDULE_REACTION_EMOJIS)})"
)
AUTO_START_THRESHOLD = 5
AUTO_START_MINIMUM_MIN = 1
AUTO_START_MINIMUM_MAX = 999
AUTO_START_GRACE_SECONDS = 10
AUTO_START_MAX_RETRIES = 2
AUTO_START_NOTICE_RETRY_DELAY_SECONDS = 2
SCHEDULE_RETENTION_DAYS = 90
SCHEDULE_REGISTRY_PATH = Path("json/schedule_polls.sqlite3")
SCHEDULE_TIMEZONE = ZoneInfo("Asia/Tokyo")
SCHEDULE_DEADLINE_MAX_DAYS = 90
SCHEDULE_DEADLINE_FIELD_NAME = "締切"
SCHEDULE_DEADLINE_VALUE_PATTERN = re.compile(r"<t:(?P<timestamp>\d+):F>")
SCHEDULE_DECISION_FIELD_NAME = "確定開始"
SCHEDULE_RELATED_NOTIFICATION_ID_LABEL = "関連通知ID"
SCHEDULE_DATE_FIELD_NAME = "開催日"
SCHEDULE_DATE_MAX_DAYS = 90
LATEST_SCHEDULE_HISTORY_LIMIT = 100
LATENESS_REACTION_GRACE_SECONDS = 2
LATENESS_REMINDER_MAX_MENTIONS = 80
LATENESS_YEAR_MONTH_PATTERN = re.compile(
    r"(?P<year>[0-9]{4})[-/]?(?P<month>[0-9]{2})"
)
LATENESS_STATS_DESCRIPTION_LIMIT = 3800
AUTO_START_MINIMUM_MARKER_PATTERN = (
    rf"(?<!\d)(?P<minimum>[1-9]\d{{0,2}})人で(?:自動)?開始判定"
)
AUTO_START_MINIMUM_PATTERN = re.compile(AUTO_START_MINIMUM_MARKER_PATTERN)
AUTO_START_MINIMUM_SUFFIX_PATTERN = re.compile(
    r"(?:^|\s)\[(?P<minimum>-?[0-9]+)\]\s*$"
)
AUTO_START_NOTIFIED_MARKER = "初回通知済み"
AUTO_START_ANNOUNCED_LABEL = "開始通知済み"
AUTO_START_MESSAGE_ID_LABEL = "通知ID"
SCHEDULE_CLOSED_MARKER = "投票終了"
AUTO_START_TIME_PATTERN = r"[^|]+?"
AUTO_START_FOOTER_PATTERN = re.compile(
    rf"\|\s*複数選択可\s*\|\s*{AUTO_START_MINIMUM_MARKER_PATTERN}\s*"
    rf"(?:\|\s*{AUTO_START_NOTIFIED_MARKER}\s*)?"
    rf"(?:\|\s*{AUTO_START_ANNOUNCED_LABEL}:\s*{AUTO_START_TIME_PATTERN}\s*"
    rf"(?:\|\s*{AUTO_START_MESSAGE_ID_LABEL}:\s*\d+\s*)?)?"
    r"\|\s*作成者ID:\s*\d+\s*$"
)
AUTO_START_MARKER_REMOVAL_PATTERN = re.compile(
    rf"\s*\|\s*{AUTO_START_MINIMUM_MARKER_PATTERN}"
    rf"(?:\s*\|\s*{AUTO_START_NOTIFIED_MARKER})?"
    rf"(?:\s*\|\s*{AUTO_START_ANNOUNCED_LABEL}:\s*{AUTO_START_TIME_PATTERN}"
    rf"(?:\s*\|\s*{AUTO_START_MESSAGE_ID_LABEL}:\s*\d+)?)?"
    r"(?=\s*\|\s*作成者ID:\s*\d+\s*$)"
)
AUTO_START_STATE_PATTERN = re.compile(
    rf"\s*\|\s*{AUTO_START_ANNOUNCED_LABEL}:\s*(?P<start_time>{AUTO_START_TIME_PATTERN})\s*"
    rf"(?:\|\s*{AUTO_START_MESSAGE_ID_LABEL}:\s*(?P<message_id>\d+)\s*)?"
    r"(?=\|\s*作成者ID:\s*\d+\s*$)"
)
AUTO_START_NOTIFIED_PATTERN = re.compile(
    rf"\|\s*{AUTO_START_NOTIFIED_MARKER}\s*"
    rf"(?=(?:\|\s*{AUTO_START_ANNOUNCED_LABEL}:\s*{AUTO_START_TIME_PATTERN}\s*"
    rf"(?:\|\s*{AUTO_START_MESSAGE_ID_LABEL}:\s*\d+\s*)?)?"
    r"\|\s*作成者ID:\s*\d+\s*$)"
)
CREATOR_ID_SUFFIX_PATTERN = re.compile(r"\s*\|\s*作成者ID:\s*\d+\s*$")
SCHEDULE_RELATED_NOTIFICATION_ID_PATTERN = re.compile(
    rf"\s*\|\s*{SCHEDULE_RELATED_NOTIFICATION_ID_LABEL}:\s*(?P<message_id>\d+)"
)
_CANCELLED_USER_UNCHANGED = object()


class ScheduleInputError(ValueError):
    """開始時間投票の入力値が不正な場合に送出する例外。"""


@dataclass(frozen=True)
class StartAnnouncement:
    start_time: str
    message_id: int | None


@dataclass(frozen=True)
class RegisteredSchedulePoll:
    guild_id: int
    channel_id: int
    message_id: int
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class LatenessStatsPeriod:
    start_date: date
    end_date: date
    label: str


class SchedulePollRegistry:
    """再起動後に自動開始投票を再評価するための小さなSQLiteレジストリ。"""

    def __init__(self, path: str | Path = SCHEDULE_REGISTRY_PATH):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_polls (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                deadline_at REAL
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(schedule_polls)")
        }
        if "deadline_at" not in columns:
            connection.execute(
                "ALTER TABLE schedule_polls ADD COLUMN deadline_at REAL"
            )
        return connection

    def register(self, *, guild_id: int, channel_id: int, message_id: int) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO schedule_polls
                        (message_id, guild_id, channel_id, deadline_at)
                    VALUES (?, ?, ?, NULL)
                    ON CONFLICT(message_id) DO UPDATE SET
                        guild_id = excluded.guild_id,
                        channel_id = excluded.channel_id
                    """,
                    (message_id, guild_id, channel_id),
                )

    def set_deadline(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        deadline_at: datetime,
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO schedule_polls
                        (message_id, guild_id, channel_id, deadline_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        guild_id = excluded.guild_id,
                        channel_id = excluded.channel_id,
                        deadline_at = excluded.deadline_at
                    """,
                    (
                        message_id,
                        guild_id,
                        channel_id,
                        deadline_at.timestamp(),
                    ),
                )

    def clear_deadline(self, message_id: int) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE schedule_polls SET deadline_at = NULL WHERE message_id = ?",
                    (message_id,),
                )

    def unregister(self, message_id: int) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM schedule_polls WHERE message_id = ?",
                    (message_id,),
                )

    def all(self) -> list[RegisteredSchedulePoll]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT guild_id, channel_id, message_id, deadline_at
                FROM schedule_polls
                """
            ).fetchall()
        return [self._registered_poll_from_row(row) for row in rows]

    def prune_before(self, cutoff_message_id: int) -> list[RegisteredSchedulePoll]:
        """指定したDiscord Snowflakeより古い登録を削除して返す。"""
        with closing(self._connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    SELECT guild_id, channel_id, message_id, deadline_at
                    FROM schedule_polls
                    WHERE message_id < ? AND deadline_at IS NULL
                    """,
                    (cutoff_message_id,),
                ).fetchall()
                connection.execute(
                    """
                    DELETE FROM schedule_polls
                    WHERE message_id < ? AND deadline_at IS NULL
                    """,
                    (cutoff_message_id,),
                )
        return [self._registered_poll_from_row(row) for row in rows]

    @staticmethod
    def _registered_poll_from_row(row) -> RegisteredSchedulePoll:
        guild_id, channel_id, message_id, deadline_timestamp = row
        deadline_at = (
            datetime.fromtimestamp(deadline_timestamp, timezone.utc)
            if deadline_timestamp is not None
            else None
        )
        return RegisteredSchedulePoll(
            guild_id,
            channel_id,
            message_id,
            deadline_at,
        )


def parse_schedule_options(value: str) -> list[str]:
    """空白区切りの候補を解析する。引用符で空白を含む候補も指定できる。"""
    try:
        options = [option.strip() for option in shlex.split(value) if option.strip()]
    except ValueError as error:
        raise ScheduleInputError("引用符が閉じられていません") from error

    if len(options) < MIN_SCHEDULE_OPTIONS:
        raise ScheduleInputError("候補を2つ以上入力してください")
    if len(options) > MAX_SCHEDULE_OPTIONS:
        raise ScheduleInputError(f"候補は最大{MAX_SCHEDULE_OPTIONS}個です")
    if any(len(option) > MAX_SCHEDULE_OPTION_LENGTH for option in options):
        raise ScheduleInputError(
            f"候補は1つにつき{MAX_SCHEDULE_OPTION_LENGTH}文字以内にしてください"
        )
    normalized_options = normalize_auto_start_options(options)
    return normalized_options if normalized_options is not None else options


def normalize_schedule_time(value: str) -> str | None:
    """対応する時刻表記を、自動判定・通知用の HH:MM 形式へそろえる。"""
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,2}", value):
        hour = int(value)
        minute = 0
    elif match := re.fullmatch(
        r"(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})",
        value,
    ):
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
    elif re.fullmatch(r"[0-9]{4}", value):
        hour = int(value[:2])
        minute = int(value[2:])
    else:
        return None

    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        return None
    return f"{hour:02d}:{minute:02d}"


def normalize_auto_start_options(options: list[str]) -> list[str] | None:
    """全候補が時刻（末尾のNGを除く）なら表示用にも正規化する。"""
    if not options:
        return None

    has_ng = options[-1].casefold() == "ng"
    time_options = options[:-1] if has_ng else options
    if not time_options:
        return None

    normalized_times: list[str] = []
    seen_times: set[str] = set()
    for option in time_options:
        normalized_time = normalize_schedule_time(option)
        if normalized_time is None or normalized_time in seen_times:
            return None
        normalized_times.append(normalized_time)
        seen_times.add(normalized_time)

    if has_ng:
        normalized_times.append("NG")
    return normalized_times


def parse_schedule_add_options(value: str | None) -> tuple[list[str], int]:
    """add候補と、末尾の `[人数]` で指定された最低人数を解析する。"""
    minimum = AUTO_START_THRESHOLD
    option_text = DEFAULT_SCHEDULE_OPTIONS if value is None else value
    minimum_match = AUTO_START_MINIMUM_SUFFIX_PATTERN.search(option_text)
    if minimum_match is not None:
        minimum = int(minimum_match.group("minimum"))
        if not AUTO_START_MINIMUM_MIN <= minimum <= AUTO_START_MINIMUM_MAX:
            raise ScheduleInputError(
                f"最低人数は{AUTO_START_MINIMUM_MIN}〜{AUTO_START_MINIMUM_MAX}人で指定してください"
            )
        option_text = option_text[:minimum_match.start()].strip()
        if not option_text:
            option_text = DEFAULT_SCHEDULE_OPTIONS

    options = parse_schedule_options(option_text)
    if (
        minimum_match is not None
        and normalize_auto_start_options(options) is None
    ):
        raise ScheduleInputError("最低人数は時刻形式の候補でのみ指定できます")
    return options, minimum


def format_schedule_options(options: list[str]) -> str:
    emojis = schedule_option_emojis(options)
    return ", ".join(
        f"{emoji}{option}" for emoji, option in zip(emojis, options)
    )


def schedule_option_emojis(options: list[str]) -> list[str]:
    emojis: list[str] = []
    last_index = len(options) - 1
    for index, option in enumerate(options):
        if option.casefold() == "ng" and index == last_index:
            emojis.append(EMOJI_NG)
            continue
        emojis.append(OPTION_EMOJIS[index])
    return emojis


def format_start_label(start_time: str) -> str:
    normalized_time = normalize_schedule_time(start_time)
    return f"{normalized_time or start_time} 開始"


def schedule_options_from_embed(embed: discord.Embed) -> list[str] | None:
    """Botが生成した投票の説明欄から、リアクション順の候補を復元する。"""
    description = embed.description
    if not description:
        return None

    # 現行の「1️⃣20:00, 2️⃣20:30」形式を読み取る。
    options: list[str] = []
    actual_emojis: list[str] = []
    matches = list(SCHEDULE_ENTRY_PATTERN.finditer(description))
    if matches and matches[0].start() == 0:
        for index, match in enumerate(matches):
            option_end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(description)
            )
            option = description[match.end():option_end]
            if not option:
                break
            actual_emojis.append(match.group("emoji"))
            options.append(option)

    # 投稿済み投票との互換性のため、旧来の改行・全角コロン形式も読む。
    if not _valid_schedule_option_emojis(options, actual_emojis):
        options = []
        actual_emojis = []
        for line in description.splitlines():
            emoji, separator, option = line.partition("：")
            if not separator or not option:
                return None
            actual_emojis.append(emoji)
            options.append(option)

    if not _valid_schedule_option_emojis(options, actual_emojis):
        return None
    return options


def _valid_schedule_option_emojis(
    options: list[str],
    actual_emojis: list[str],
) -> bool:
    if (
        not MIN_SCHEDULE_OPTIONS <= len(options) <= MAX_SCHEDULE_OPTIONS
        or len(actual_emojis) != len(options)
    ):
        return False
    expected_emojis = schedule_option_emojis(options)
    for index, (actual, expected) in enumerate(
        zip(actual_emojis, expected_emojis)
    ):
        # 既存投票では末尾のngに番号リアクションを使っていたため読み替える。
        is_legacy_ng = (
            index == len(options) - 1
            and options[index].casefold() == "ng"
            and actual == OPTION_EMOJIS[index]
        )
        if actual != expected and not is_legacy_ng:
            return False
    return True


def parse_message_id(value: str) -> tuple[int, int | None]:
    """メッセージID、またはDiscordメッセージリンクからIDを取り出す。"""
    value = value.strip()
    if value.isdigit():
        return int(value), None

    match = MESSAGE_LINK_PATTERN.fullmatch(value)
    if match is None:
        raise ScheduleInputError("投稿IDまたはDiscordの投稿リンクを指定してください")
    return int(match.group("message_id")), int(match.group("channel_id"))


def looks_like_explicit_message_reference(value: str) -> bool:
    value = value.strip()
    if value.isdigit():
        return len(value) >= 15
    return MESSAGE_LINK_PATTERN.fullmatch(value) is not None


def parse_schedule_deadline(
    value: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """日本時間の締切入力をUTCへ変換する。解除指定ではNoneを返す。"""
    raw_value = value.strip()
    if raw_value.casefold() in {"clear", "none", "off", "解除", "なし"}:
        return None

    normalized_value = raw_value.replace("T", " ")
    deadline_local = None
    for date_format in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            deadline_local = datetime.strptime(normalized_value, date_format)
            break
        except ValueError:
            continue
    if deadline_local is None:
        raise ScheduleInputError(
            "締切は `YYYY-MM-DD HH:MM` 形式の日本時間、または `clear` で指定してください"
        )

    deadline_at = deadline_local.replace(tzinfo=SCHEDULE_TIMEZONE).astimezone(
        timezone.utc
    )
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)
    if deadline_at <= current_time:
        raise ScheduleInputError("締切は現在より後の日時を指定してください")
    if deadline_at > current_time + timedelta(days=SCHEDULE_DEADLINE_MAX_DAYS):
        raise ScheduleInputError(
            f"締切は{SCHEDULE_DEADLINE_MAX_DAYS}日以内で指定してください"
        )
    return deadline_at


def parse_schedule_date(
    value: str,
    *,
    now: datetime | None = None,
    posted_date: date | None = None,
) -> date | None:
    """開催日の入力を解析する。部分指定は投稿日を基準に補完する。"""
    raw_value = value.strip()
    if raw_value.casefold() in {"clear", "default", "投稿日", "解除"}:
        return None

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_date = current_time.astimezone(SCHEDULE_TIMEZONE).date()
    base_date = posted_date or current_date
    if raw_value.casefold() in {"today", "今日"}:
        event_date = current_date
    else:
        event_date = None
        for date_format in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                event_date = datetime.strptime(raw_value, date_format).date()
                break
            except ValueError:
                continue
        if event_date is None and re.fullmatch(r"[0-9]{8}", raw_value):
            try:
                event_date = date(
                    int(raw_value[:4]),
                    int(raw_value[4:6]),
                    int(raw_value[6:]),
                )
            except ValueError:
                pass
        if event_date is None:
            month_day_match = re.fullmatch(
                r"(?P<month>[0-9]{1,2})-(?P<day>[0-9]{1,2})",
                raw_value,
            )
            compact_month_day_match = re.fullmatch(
                r"(?P<month>[0-9]{2})(?P<day>[0-9]{2})",
                raw_value,
            )
            match = month_day_match or compact_month_day_match
            if match is not None:
                try:
                    event_date = date(
                        base_date.year,
                        int(match.group("month")),
                        int(match.group("day")),
                    )
                except ValueError:
                    pass
        if event_date is None and re.fullmatch(r"[0-9]{1,2}", raw_value):
            try:
                event_date = date(
                    base_date.year,
                    base_date.month,
                    int(raw_value),
                )
            except ValueError:
                pass
        if event_date is None:
            raise ScheduleInputError(
                "開催日は `YYYY-MM-DD`、`YYYYMMDD`、`MM-DD`、`MMDD`、"
                "`DD`、`today`、または `clear` で指定してください"
            )

    if abs((event_date - current_date).days) > SCHEDULE_DATE_MAX_DAYS:
        raise ScheduleInputError(
            f"開催日は今日から前後{SCHEDULE_DATE_MAX_DAYS}日以内で指定してください"
        )
    return event_date


def parse_lateness_period(
    value: str | None,
    *,
    now: datetime | None = None,
) -> LatenessStatsPeriod:
    """月次または年次集計の対象期間を解析する。"""
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_month = current_time.astimezone(SCHEDULE_TIMEZONE).date().replace(day=1)
    if value is None or value.strip().casefold() in {"current", "this", "今月"}:
        year = current_month.year
        month = current_month.month
        is_year = False
    else:
        raw_value = value.strip()
        if re.fullmatch(r"[0-9]{1,2}", raw_value):
            year = current_month.year
            month = int(raw_value)
            is_year = False
        elif re.fullmatch(r"[0-9]{4}", raw_value):
            year = int(raw_value)
            month = None
            is_year = True
        else:
            match = LATENESS_YEAR_MONTH_PATTERN.fullmatch(raw_value)
            if match is None:
                raise ScheduleInputError(
                    "対象期間は `10`、`2026`、`202704`、`2027-04` のいずれかの形式で指定してください"
                )
            year = int(match.group("year"))
            month = int(match.group("month"))
            is_year = False

    try:
        if is_year:
            start_date = date(year, 1, 1)
            end_date = date(year + 1, 1, 1)
            label = f"{year}年"
        else:
            start_date = date(year, month, 1)
            end_date = (
                date(year + 1, 1, 1)
                if month == 12
                else date(year, month + 1, 1)
            )
            label = f"{year:04d}-{month:02d}"
    except ValueError as error:
        raise ScheduleInputError(
            "対象期間は有効な年と1月から12月の範囲で指定してください"
        ) from error
    return LatenessStatsPeriod(start_date, end_date, label)


def format_lateness_duration(seconds: float) -> str:
    rounded_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}時間")
    if minutes:
        parts.append(f"{minutes}分")
    if remaining_seconds and not hours:
        parts.append(f"{remaining_seconds}秒")
    return "".join(parts) or "0秒"


def format_lateness_reminder_duration(minutes: int) -> str:
    if minutes <= 0:
        raise ValueError("reminder minutes must be positive")
    if minutes % 60 == 0:
        return f"{minutes // 60}時間"
    return f"{minutes}分"


def build_lateness_stats_embeds(
    stats: list[MonthlyLatenessStat],
    *,
    guild,
    period: LatenessStatsPeriod,
) -> list[discord.Embed]:
    """遅刻集計をDiscordのdescription上限内に分割する。"""
    title = f"⏱️ 遅刻・欠席集計 {period.label}"
    if not stats:
        return [
            discord.Embed(
                title=title,
                description="この期間の遅刻・欠席記録はありません。",
                color=discord.Color.green(),
            )
        ]

    lines: list[str] = []
    for rank, stat in enumerate(stats, start=1):
        member = guild.get_member(stat.user_id)
        display_name = (
            getattr(member, "display_name", None)
            or getattr(member, "name", None)
            or f"ユーザーID {stat.user_id}"
        )
        safe_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(
                " ".join(str(display_name).splitlines()).strip()
            )
        )
        lines.append(
            f"**{rank}. {safe_name}** — {stat.count}回｜"
            f"合計 {format_lateness_duration(stat.total_seconds)}｜"
            f"平均 {format_lateness_duration(stat.average_seconds)}｜"
            f"最大 {format_lateness_duration(stat.maximum_seconds)}"
        )

    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_length = 0
    for line in lines:
        added_length = len(line) + (1 if current_chunk else 0)
        if (
            current_chunk
            and current_length + added_length > LATENESS_STATS_DESCRIPTION_LIMIT
        ):
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0
            added_length = len(line)
        current_chunk.append(line)
        current_length += added_length
    if current_chunk:
        chunks.append(current_chunk)

    total_records = sum(stat.count for stat in stats)
    embeds: list[discord.Embed] = []
    for index, chunk in enumerate(chunks, start=1):
        page_suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
        embed = discord.Embed(
            title=title + page_suffix,
            description="\n".join(chunk),
            color=discord.Color.orange(),
        )
        embed.set_footer(
            text=(
                f"{len(stats)}人・{total_records}回｜"
                "lateoff済み募集と遅刻0秒は対象外"
            )
        )
        embeds.append(embed)
    return embeds


def format_schedule_deadline(deadline_at: datetime) -> str:
    timestamp = int(deadline_at.timestamp())
    return f"<t:{timestamp}:F>（<t:{timestamp}:R>）"


def schedule_author_id(embed: discord.Embed) -> int | None:
    if not embed.title or not embed.title.startswith(SCHEDULE_TITLE_PREFIX):
        return None
    footer_text = embed.footer.text or ""
    match = SCHEDULE_FOOTER_PATTERN.search(footer_text)
    return int(match.group(1)) if match else None


def auto_start_minimum(embed: discord.Embed) -> int | None:
    """投票に保存された自動開始の最低人数を返す。"""
    match = AUTO_START_FOOTER_PATTERN.search(embed.footer.text or "")
    return int(match.group("minimum")) if match is not None else None


def schedule_minimum(embed: discord.Embed) -> int | None:
    """自動判定中・終了済みにかかわらず、表示中の最低人数を返す。"""
    minimum = auto_start_minimum(embed)
    if minimum is not None:
        return minimum
    title = embed.title or ""
    if title.endswith("（終了）"):
        title = title[:-len("（終了）")]
    match = SCHEDULE_TITLE_MINIMUM_PATTERN.search(title)
    return int(match.group("minimum")) if match is not None else None


def is_schedule_closed(embed: discord.Embed) -> bool:
    return SCHEDULE_CLOSED_MARKER in (embed.footer.text or "")


def schedule_deadline_at(embed: discord.Embed) -> datetime | None:
    for field in embed.fields:
        if field.name != SCHEDULE_DEADLINE_FIELD_NAME:
            continue
        match = SCHEDULE_DEADLINE_VALUE_PATTERN.search(field.value)
        if match is not None:
            return datetime.fromtimestamp(
                int(match.group("timestamp")),
                timezone.utc,
            )
    return None


def set_schedule_deadline(
    embed: discord.Embed,
    deadline_at: datetime | None,
) -> None:
    field_index = next(
        (
            index
            for index, field in enumerate(embed.fields)
            if field.name == SCHEDULE_DEADLINE_FIELD_NAME
        ),
        None,
    )
    if deadline_at is None:
        if field_index is not None:
            embed.remove_field(field_index)
        return
    value = format_schedule_deadline(deadline_at)
    if field_index is None:
        embed.add_field(
            name=SCHEDULE_DEADLINE_FIELD_NAME,
            value=value,
            inline=False,
        )
    else:
        embed.set_field_at(
            field_index,
            name=SCHEDULE_DEADLINE_FIELD_NAME,
            value=value,
            inline=False,
        )


def schedule_decided_start_time(embed: discord.Embed) -> str | None:
    for field in embed.fields:
        if field.name != SCHEDULE_DECISION_FIELD_NAME:
            continue
        value = field.value.strip()
        if value.endswith("開始"):
            value = value[:-len("開始")].strip()
        return normalize_schedule_time(value)
    return None


def set_schedule_decision(
    embed: discord.Embed,
    start_time: str | None,
) -> None:
    field_index = next(
        (
            index
            for index, field in enumerate(embed.fields)
            if field.name == SCHEDULE_DECISION_FIELD_NAME
        ),
        None,
    )
    if start_time is None:
        if field_index is not None:
            embed.remove_field(field_index)
        return
    normalized_time = normalize_schedule_time(start_time)
    if normalized_time is None:
        raise ValueError("invalid schedule decision time")
    value = format_start_label(normalized_time)
    if field_index is None:
        embed.add_field(
            name=SCHEDULE_DECISION_FIELD_NAME,
            value=value,
            inline=False,
        )
    else:
        embed.set_field_at(
            field_index,
            name=SCHEDULE_DECISION_FIELD_NAME,
            value=value,
            inline=False,
        )


def schedule_related_notification_id(embed: discord.Embed) -> int | None:
    """終了後も削除対象として追跡する通知投稿IDを返す。"""
    match = SCHEDULE_RELATED_NOTIFICATION_ID_PATTERN.search(
        embed.footer.text or ""
    )
    return int(match.group("message_id")) if match is not None else None


def set_schedule_related_notification_id(
    embed: discord.Embed,
    message_id: int | None,
) -> None:
    footer_text = SCHEDULE_RELATED_NOTIFICATION_ID_PATTERN.sub(
        "",
        embed.footer.text or "",
    )
    if message_id is None:
        embed.set_footer(text=footer_text)
        return
    suffix_match = CREATOR_ID_SUFFIX_PATTERN.search(footer_text)
    if suffix_match is None:
        return
    embed.set_footer(
        text=(
            footer_text[:suffix_match.start()]
            + f" | {SCHEDULE_RELATED_NOTIFICATION_ID_LABEL}: {message_id}"
            + suffix_match.group(0)
        )
    )


def schedule_date_override(embed: discord.Embed) -> date | None:
    for field in embed.fields:
        if field.name != SCHEDULE_DATE_FIELD_NAME:
            continue
        try:
            return datetime.strptime(field.value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def set_schedule_date_override(
    embed: discord.Embed,
    event_date: date | None,
) -> None:
    field_index = next(
        (
            index
            for index, field in enumerate(embed.fields)
            if field.name == SCHEDULE_DATE_FIELD_NAME
        ),
        None,
    )
    if event_date is None:
        if field_index is not None:
            embed.remove_field(field_index)
        return
    value = event_date.isoformat()
    if field_index is None:
        embed.add_field(
            name=SCHEDULE_DATE_FIELD_NAME,
            value=value,
            inline=False,
        )
    else:
        embed.set_field_at(
            field_index,
            name=SCHEDULE_DATE_FIELD_NAME,
            value=value,
            inline=False,
        )


def schedule_default_date(poll_message: discord.Message) -> date:
    created_at = getattr(poll_message, "created_at", None)
    if created_at is None:
        created_at = discord.utils.snowflake_time(poll_message.id)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(SCHEDULE_TIMEZONE).date()


def schedule_event_date(poll_message: discord.Message) -> date:
    if poll_message.embeds:
        override = schedule_date_override(poll_message.embeds[0])
        if override is not None:
            return override
    return schedule_default_date(poll_message)


def schedule_start_datetime(event_date: date, start_time: str) -> datetime:
    """開催日と正規化済み時刻を、UTCの開始日時へ変換する。"""
    normalized_time = normalize_schedule_time(start_time)
    if normalized_time is None:
        raise ValueError("invalid schedule start time")
    hour, minute = (int(part) for part in normalized_time.split(":"))
    if hour == 24:
        event_date += timedelta(days=1)
        hour = 0
    local_start = datetime(
        event_date.year,
        event_date.month,
        event_date.day,
        hour,
        minute,
        tzinfo=SCHEDULE_TIMEZONE,
    )
    return local_start.astimezone(timezone.utc)


def eligible_voters_for_start(
    voters_by_option: dict[str, set[int]],
    start_time: str,
) -> set[int]:
    """確定時刻以前を選んだ、参加可能な投票者を重複なしで返す。"""
    normalized_start = normalize_schedule_time(start_time)
    if normalized_start is None:
        return set()
    start_hour, start_minute = (
        int(part) for part in normalized_start.split(":")
    )
    start_key = start_hour * 60 + start_minute
    eligible: set[int] = set()
    for option, user_ids in voters_by_option.items():
        normalized_option = normalize_schedule_time(option)
        if normalized_option is None:
            continue
        hour, minute = (int(part) for part in normalized_option.split(":"))
        if hour * 60 + minute <= start_key:
            eligible.update(user_ids)
    return eligible


def is_auto_start_schedule(embed: discord.Embed) -> bool:
    options = schedule_options_from_embed(embed)
    return (
        auto_start_minimum(embed) is not None
        and options is not None
        and normalize_auto_start_options(options) is not None
    )


def remove_auto_start_marker(embed: discord.Embed) -> None:
    footer_text = embed.footer.text or ""
    updated_footer = AUTO_START_MARKER_REMOVAL_PATTERN.sub(" ", footer_text)
    updated_footer = re.sub(
        r"\s+\|\s*作成者ID:",
        " | 作成者ID:",
        updated_footer,
    )
    if updated_footer != footer_text:
        embed.set_footer(text=updated_footer)


def set_schedule_title_minimum(
    embed: discord.Embed,
    minimum: int | None,
) -> None:
    """開始時間の直後に、自動判定の最低人数を表示する。"""
    if not embed.title or not embed.title.startswith(SCHEDULE_TITLE_PREFIX):
        return
    closed_suffix = "（終了）" if embed.title.endswith("（終了）") else ""
    title = embed.title[:-len(closed_suffix)] if closed_suffix else embed.title
    title = SCHEDULE_TITLE_MINIMUM_PATTERN.sub("", title)
    if minimum is not None:
        if not AUTO_START_MINIMUM_MIN <= minimum <= AUTO_START_MINIMUM_MAX:
            raise ValueError("auto-start minimum is out of range")
        title += f" [{minimum}人]"
    embed.title = title + closed_suffix


def set_auto_start_minimum(
    embed: discord.Embed,
    minimum: int,
) -> bool:
    """通知状態を保ったまま、自動開始の最低人数とタイトルを更新する。"""
    if not AUTO_START_MINIMUM_MIN <= minimum <= AUTO_START_MINIMUM_MAX:
        raise ValueError("auto-start minimum is out of range")
    footer_text = embed.footer.text or ""
    marker_match = AUTO_START_FOOTER_PATTERN.search(footer_text)
    if marker_match is None:
        return False

    minimum_start, minimum_end = marker_match.span("minimum")
    updated_footer = (
        footer_text[:minimum_start]
        + str(minimum)
        + footer_text[minimum_end:]
    )
    original_title = embed.title
    if updated_footer != footer_text:
        embed.set_footer(text=updated_footer)
    set_schedule_title_minimum(embed, minimum)
    return updated_footer != footer_text or embed.title != original_title


def set_auto_start_marker(
    embed: discord.Embed,
    *,
    enabled: bool,
    minimum: int = AUTO_START_THRESHOLD,
) -> None:
    """既存状態を消したうえで、自動開始判定マーカーを設定し直す。"""
    remove_auto_start_marker(embed)
    if not enabled:
        set_schedule_title_minimum(embed, None)
        return
    if not AUTO_START_MINIMUM_MIN <= minimum <= AUTO_START_MINIMUM_MAX:
        raise ValueError("auto-start minimum is out of range")
    footer_text = embed.footer.text or ""
    suffix_match = CREATOR_ID_SUFFIX_PATTERN.search(footer_text)
    if suffix_match is None:
        return
    set_schedule_title_minimum(embed, minimum)
    embed.set_footer(
        text=(
            footer_text[:suffix_match.start()]
            + f" | {minimum}人で開始判定"
            + suffix_match.group(0)
        )
    )


def mark_schedule_closed(embed: discord.Embed) -> None:
    remove_auto_start_marker(embed)
    footer_text = embed.footer.text or ""
    suffix_match = CREATOR_ID_SUFFIX_PATTERN.search(footer_text)
    if suffix_match is None or SCHEDULE_CLOSED_MARKER in footer_text:
        return
    embed.set_footer(
        text=(
            footer_text[:suffix_match.start()]
            + f" | {SCHEDULE_CLOSED_MARKER}"
            + suffix_match.group(0)
        )
    )


def start_announcement(embed: discord.Embed) -> StartAnnouncement | None:
    footer_text = embed.footer.text or ""
    match = AUTO_START_STATE_PATTERN.search(footer_text)
    if match is None:
        return None
    message_id = match.group("message_id")
    stored_start_time = match.group("start_time").strip()
    return StartAnnouncement(
        start_time=normalize_schedule_time(stored_start_time) or stored_start_time,
        message_id=int(message_id) if message_id is not None else None,
    )


def announced_start_time(embed: discord.Embed) -> str | None:
    announcement = start_announcement(embed)
    return announcement.start_time if announcement is not None else None


def clear_start_announcement(embed: discord.Embed) -> None:
    had_announcement = start_announcement(embed) is not None
    footer_text = embed.footer.text or ""
    updated_footer = AUTO_START_STATE_PATTERN.sub(" ", footer_text)
    if updated_footer != footer_text:
        embed.set_footer(text=updated_footer)
    if had_announcement:
        mark_start_notification_history(embed)


def has_announced_start_before(embed: discord.Embed) -> bool:
    footer_text = embed.footer.text or ""
    return (
        AUTO_START_NOTIFIED_PATTERN.search(footer_text) is not None
        or start_announcement(embed) is not None
    )


def mark_start_notification_history(embed: discord.Embed) -> None:
    if AUTO_START_NOTIFIED_PATTERN.search(embed.footer.text or "") is not None:
        return
    footer_text = embed.footer.text or ""
    suffix_match = CREATOR_ID_SUFFIX_PATTERN.search(footer_text)
    if (
        suffix_match is None
        or AUTO_START_MINIMUM_PATTERN.search(footer_text) is None
    ):
        return
    updated_footer = (
        footer_text[:suffix_match.start()]
        + f" | {AUTO_START_NOTIFIED_MARKER}"
        + suffix_match.group(0)
    )
    embed.set_footer(text=updated_footer)


def mark_start_time_announced(
    embed: discord.Embed,
    start_time: str,
    message_id: int | None = None,
) -> None:
    clear_start_announcement(embed)
    footer_text = embed.footer.text or ""
    suffix_match = CREATOR_ID_SUFFIX_PATTERN.search(footer_text)
    if suffix_match is None:
        return
    if not has_announced_start_before(embed):
        mark_start_notification_history(embed)
        footer_text = embed.footer.text or ""
        suffix_match = CREATOR_ID_SUFFIX_PATTERN.search(footer_text)
        if suffix_match is None:
            return
    state_text = f" | {AUTO_START_ANNOUNCED_LABEL}: {start_time}"
    if message_id is not None:
        state_text += f" | {AUTO_START_MESSAGE_ID_LABEL}: {message_id}"
    updated_footer = (
        footer_text[:suffix_match.start()]
        + state_text
        + suffix_match.group(0)
    )
    embed.set_footer(text=updated_footer)


def choose_start_time(
    voters_by_option: dict[str, set[int]],
    minimum: int = AUTO_START_THRESHOLD,
) -> str | None:
    """早い時刻側から重複を除いて集計し、最低人数に達する時刻を返す。"""
    voters_by_time: dict[int, set[int]] = {}
    labels_by_time: dict[int, str] = {}
    for option, voters in voters_by_option.items():
        normalized_time = normalize_schedule_time(option)
        if normalized_time is None:
            continue
        hour, minute = (int(part) for part in normalized_time.split(":"))
        time_key = hour * 60 + minute
        voters_by_time.setdefault(time_key, set()).update(voters)
        labels_by_time[time_key] = normalized_time

    distinct_voters: set[int] = set()
    for time_key in sorted(voters_by_time):
        distinct_voters.update(voters_by_time[time_key])
        if len(distinct_voters) >= minimum:
            return labels_by_time[time_key]
    return None


def build_schedule_embed(
    role: discord.Role,
    options: list[str],
    author,
    *,
    auto_start: bool = False,
    minimum: int = AUTO_START_THRESHOLD,
) -> discord.Embed:
    if (
        auto_start
        and not AUTO_START_MINIMUM_MIN <= minimum <= AUTO_START_MINIMUM_MAX
    ):
        raise ValueError("auto-start minimum is out of range")
    embed = discord.Embed(
        title=f"{SCHEDULE_TITLE_PREFIX}{role.name} 開始時間",
        description=format_schedule_options(options),
        color=discord.Color.blue(),
    )
    if auto_start:
        set_schedule_title_minimum(embed, minimum)
    footer_parts = [f"作成者: {author.display_name}", "複数選択可"]
    if auto_start:
        footer_parts.append(f"{minimum}人で開始判定")
    footer_parts.append(f"作成者ID: {author.id}")
    embed.set_footer(text=" | ".join(footer_parts))
    return embed


def build_schedule_status_embed(
    source_embed: discord.Embed,
    voters_by_option: dict[str, set[int]],
    *,
    event_date: date | None = None,
) -> discord.Embed:
    """現在の票数と、自動判定に使う重複除外の累計人数を表示する。"""
    options = schedule_options_from_embed(source_embed) or list(voters_by_option)
    emojis = schedule_option_emojis(options)
    time_entries: list[tuple[int, int, str]] = []
    if normalize_auto_start_options(options) is not None:
        for index, option in enumerate(options):
            normalized = normalize_schedule_time(option)
            if normalized is None:
                continue
            hour, minute = (int(part) for part in normalized.split(":"))
            time_entries.append((hour * 60 + minute, index, option))

    cumulative_counts: dict[int, int] = {}
    distinct_voters: set[int] = set()
    for _, index, option in sorted(time_entries):
        distinct_voters.update(voters_by_option.get(option, set()))
        cumulative_counts[index] = len(distinct_voters)

    minimum = schedule_minimum(source_embed)
    auto_start_enabled = is_auto_start_schedule(source_embed)
    decided_start_time = schedule_decided_start_time(source_embed)
    if is_schedule_closed(source_embed):
        if decided_start_time is not None:
            state = "✅ 確定済み"
            color = discord.Color.green()
        else:
            state = "⚫ 終了済み"
            color = discord.Color.dark_grey()
    elif auto_start_enabled:
        state = "🟢 自動判定中"
        color = discord.Color.green()
    else:
        state = "🔵 自動判定なし"
        color = discord.Color.blue()

    summary = [f"状態: {state}"]
    if event_date is not None:
        summary.append(f"開催日: {event_date.isoformat()}")
    if decided_start_time is not None:
        summary.append(f"確定開始: {format_start_label(decided_start_time)}")
    if minimum is not None:
        summary.append(f"最低人数: {minimum}人")
    deadline_at = schedule_deadline_at(source_embed)
    if deadline_at is not None:
        summary.append(f"締切: {format_schedule_deadline(deadline_at)}")
    if auto_start_enabled and minimum is not None:
        start_time = choose_start_time(voters_by_option, minimum)
        summary.append(
            "現在の成立時刻: "
            + (format_start_label(start_time) if start_time is not None else "未成立")
        )
    announcement = start_announcement(source_embed)
    if announcement is not None:
        summary.append(f"通知済み: {format_start_label(announcement.start_time)}")

    option_lines: list[str] = []
    for index, (option, emoji) in enumerate(zip(options, emojis)):
        escaped_option = discord.utils.escape_markdown(option)[:150]
        votes = len(voters_by_option.get(option, set()))
        if index in cumulative_counts:
            option_lines.append(
                f"{emoji}{escaped_option}: {votes}票（累計{cumulative_counts[index]}人）"
            )
        else:
            option_lines.append(f"{emoji}{escaped_option}: {votes}票")

    source_title = source_embed.title or "開始時間投票"
    if source_title.startswith(SCHEDULE_TITLE_PREFIX):
        source_title = source_title[len(SCHEDULE_TITLE_PREFIX):]
    return discord.Embed(
        title=f"📊 {source_title}",
        description="\n".join([*summary, "", "候補別:", *option_lines]),
        color=color,
    )


class PollCog(commands.Cog):
    def __init__(
        self,
        bot,
        *,
        auto_start_grace_seconds: float = AUTO_START_GRACE_SECONDS,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        deadline_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lateness_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lateness_reaction_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        registry: SchedulePollRegistry | None = None,
        lateness_registry: ScheduleLatenessRegistry | None = None,
    ):
        self.bot = bot
        self._auto_start_grace_seconds = auto_start_grace_seconds
        self._sleep = sleeper
        self._retry_sleep = retry_sleeper
        self._deadline_sleep = deadline_sleeper
        self._lateness_sleep = lateness_sleeper
        self._lateness_reaction_sleep = lateness_reaction_sleeper
        self._now = now_provider
        self._schedule_registry = registry or SchedulePollRegistry()
        self._lateness_registry = lateness_registry or ScheduleLatenessRegistry(
            self._schedule_registry.path
        )
        self._auto_start_locks: WeakValueDictionary[int, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._auto_start_tasks: dict[int, asyncio.Task] = {}
        self._auto_start_revisions: dict[int, int] = {}
        self._last_cancelled_user_ids: dict[int, int] = {}
        self._start_notified_poll_ids: set[int] = set()
        self._registered_schedule_ids: set[int] = set()
        self._persisted_schedule_ids: set[int] = set()
        self._registered_schedule_polls: dict[int, RegisteredSchedulePoll] = {}
        self._poll_notification_ids: dict[int, int] = {}
        self._notification_poll_refs: dict[int, RegisteredSchedulePoll] = {}
        self._deadline_tasks: dict[int, asyncio.Task] = {}
        self._lateness_tasks: dict[int, asyncio.Task] = {}
        self._lateness_reaction_tasks: dict[int, asyncio.Task] = {}
        self._lateness_tracking_started = False
        self._registry_recovery_task: asyncio.Task | None = None

    def _utc_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    async def cog_load(self) -> None:
        self._prune_expired_schedule_polls()
        polls: list[RegisteredSchedulePoll] = []
        try:
            polls = self._schedule_registry.all()
        except (sqlite3.Error, OSError):
            logger.exception("failed to load the schedule poll registry")
        else:
            loaded_ids = {poll.message_id for poll in polls}
            self._registered_schedule_ids.update(loaded_ids)
            self._persisted_schedule_ids.update(loaded_ids)
            self._registered_schedule_polls.update(
                {poll.message_id: poll for poll in polls}
            )
            if polls and hasattr(self.bot, "wait_until_ready"):
                self._registry_recovery_task = asyncio.create_task(
                    self._recover_registered_schedule_polls(polls),
                    name="schedule-poll-recovery",
                )

        self._lateness_tracking_started = True
        try:
            self._lateness_registry.prune_expired_presence(now=self._utc_now())
            lateness_events = self._lateness_registry.tracking_events(
                now=self._utc_now()
            )
        except (sqlite3.Error, OSError):
            logger.exception("failed to load schedule lateness tracking")
        else:
            for event in lateness_events:
                self._queue_lateness_event_task(event)

    async def cog_unload(self) -> None:
        if self._registry_recovery_task is not None:
            self._registry_recovery_task.cancel()
        tasks = list(self._auto_start_tasks.values())
        deadline_tasks = list(self._deadline_tasks.values())
        lateness_tasks = list(self._lateness_tasks.values())
        lateness_reaction_tasks = list(self._lateness_reaction_tasks.values())
        self._auto_start_tasks.clear()
        self._deadline_tasks.clear()
        self._lateness_tasks.clear()
        self._lateness_reaction_tasks.clear()
        self._lateness_tracking_started = False
        self._auto_start_revisions.clear()
        self._last_cancelled_user_ids.clear()
        self._start_notified_poll_ids.clear()
        for task in tasks:
            task.cancel()
        for task in deadline_tasks:
            task.cancel()
        for task in lateness_tasks:
            task.cancel()
        for task in lateness_reaction_tasks:
            task.cancel()
        pending_tasks = [
            *tasks,
            *deadline_tasks,
            *lateness_tasks,
            *lateness_reaction_tasks,
        ]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if self._registry_recovery_task is not None:
            await asyncio.gather(
                self._registry_recovery_task,
                return_exceptions=True,
            )
            self._registry_recovery_task = None

    async def _recover_registered_schedule_polls(
        self,
        polls: list[RegisteredSchedulePoll],
    ) -> None:
        await self.bot.wait_until_ready()
        for poll in polls:
            if poll.deadline_at is not None:
                self._queue_schedule_deadline(poll)
                if poll.deadline_at <= self._utc_now():
                    continue
            self._queue_auto_start_check_by_id(
                guild_id=poll.guild_id,
                channel_id=poll.channel_id,
                message_id=poll.message_id,
            )

    @commands.command(name="poll")
    async def poll(self, ctx, question: str, *, options: str):
        option_list = [o.strip() for o in options.split(",") if o.strip()]
        if len(option_list) < 2:
            await ctx.send("❌ 選択肢を2つ以上カンマ区切りで入力してください（例: `A,B,C`）")
            return
        if len(option_list) > len(OPTION_EMOJIS):
            await ctx.send(f"❌ 選択肢は最大{len(OPTION_EMOJIS)}個です")
            return
        description = "\n".join(f"{OPTION_EMOJIS[i]}　{opt}" for i, opt in enumerate(option_list))
        embed = discord.Embed(title=f"📊 {question}", description=description, color=discord.Color.blue())
        embed.set_footer(text=f"作成者: {ctx.author.display_name}")
        poll_msg = await ctx.send(embed=embed)
        for i in range(len(option_list)):
            await poll_msg.add_reaction(OPTION_EMOJIS[i])
        logger.info(f"{ctx.author} created poll: {question}")

    @commands.command(name="quickpoll")
    async def quickpoll(self, ctx, *, question: str):
        embed = discord.Embed(title=f"📊 {question}", color=discord.Color.blue())
        embed.set_footer(text=f"作成者: {ctx.author.display_name}")
        poll_msg = await ctx.send(embed=embed)
        await poll_msg.add_reaction("👍")
        await poll_msg.add_reaction("👎")
        logger.info(f"{ctx.author} created quickpoll: {question}")

    @commands.hybrid_group(
        name="schedule",
        description="ロールの開始時間投票を管理します",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def schedule(self, ctx):
        prefix = ctx.clean_prefix or "/"
        await ctx.send(
            "📅 開始時間投票コマンド\n"
            f"作成: `{prefix}schedule add @ロール [候補...]`\n"
            f"候補省略時: `{DEFAULT_SCHEDULE_OPTIONS}`（末尾の `[人数]` で最低人数を変更）\n"
            f"状況確認: `{prefix}schedule status [投稿IDまたはリンク]`\n"
            f"複製: `{prefix}schedule clone [投稿IDまたはリンク]`\n"
            f"開催日: `{prefix}schedule date [投稿IDまたはリンク] <日付>`\n"
            f"最低人数変更: `{prefix}schedule minimum [投稿IDまたはリンク] 3`\n"
            f"締切設定: `{prefix}schedule deadline [投稿IDまたはリンク] 2026-08-14 19:00`\n"
            f"更新: `{prefix}schedule update [投稿IDまたはリンク] 21:00 22:00 24:00 NG`\n"
            f"確定: `{prefix}schedule decide [投稿IDまたはリンク] 21:00`\n"
            f"遅刻集計: `{prefix}schedule late [期間]`\n"
            f"遅刻判定停止: `{prefix}schedule lateoff [投稿IDまたはリンク]`\n"
            f"終了: `{prefix}schedule close [投稿IDまたはリンク]`\n"
            f"完全削除: `{prefix}schedule delete [投稿IDまたはリンク]`"
        )

    @schedule.error
    async def schedule_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule.command(name="add", description="新しい開始時間投票を作成します")
    @app_commands.describe(
        role="開始時間調整の対象ロール",
        options=(
            f"空白区切りの候補（省略時: {DEFAULT_SCHEDULE_OPTIONS}）。"
            "末尾の [人数] で最低人数を指定"
        ),
    )
    @commands.guild_only()
    async def schedule_add(
        self,
        ctx: commands.Context,
        role: discord.Role,
        *,
        options: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return

        # 新規投票の作成を、古いSQLite登録を整理する機会として利用する。
        self._prune_expired_schedule_polls()

        try:
            option_list, minimum = parse_schedule_add_options(options)
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return

        if role.is_default():
            await self._send_notice(ctx, "❌ @everyone は日程調整の対象にできません")
            return

        if (
            ctx.interaction is None
            and role not in getattr(ctx.message, "role_mentions", [])
        ):
            await self._send_notice(
                ctx,
                "❌ プレフィックスコマンドでは対象ロールをメンションで指定してください",
            )
            return

        await self._create_schedule_poll(
            ctx,
            role,
            option_list,
            minimum,
            role_already_mentioned=ctx.interaction is None,
        )

    async def _create_schedule_poll(
        self,
        ctx: commands.Context,
        role: discord.Role,
        option_list: list[str],
        minimum: int,
        *,
        role_already_mentioned: bool,
    ) -> discord.Message | None:
        auto_start_enabled = normalize_auto_start_options(option_list) is not None
        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return None
        bot_permissions = ctx.channel.permissions_for(bot_member)
        can_send = (
            bot_permissions.send_messages_in_threads
            if isinstance(ctx.channel, discord.Thread)
            else bot_permissions.send_messages
        )
        required_permissions = {
            "メッセージの送信": can_send,
            "埋め込みリンク": bot_permissions.embed_links,
            "メッセージ履歴を読む": bot_permissions.read_message_history,
            "リアクションの追加": bot_permissions.add_reactions,
        }
        missing_permissions = [
            name for name, enabled in required_permissions.items() if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return None

        if not role.mentionable:
            author_permissions = ctx.channel.permissions_for(ctx.author)
            if not author_permissions.mention_everyone:
                await self._send_notice(
                    ctx,
                    "❌ メンション不可のロールを指定する権限がありません",
                )
                return None
            if not role_already_mentioned or auto_start_enabled:
                if not bot_permissions.mention_everyone:
                    await self._send_notice(
                        ctx,
                        "❌ Botに「@everyone、@here、すべてのロールにメンション」の権限が必要です",
                    )
                    return None

        embed = build_schedule_embed(
            role,
            option_list,
            ctx.author,
            auto_start=auto_start_enabled,
            minimum=minimum,
        )
        # addのPrefixコマンドだけは、元投稿で既に通知しているため二重通知を避ける。
        allowed_roles = False if role_already_mentioned else [role]
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=allowed_roles,
            replied_user=False,
        )

        poll_message = None
        try:
            poll_message = await ctx.send(
                content=role.mention,
                embed=embed,
                allowed_mentions=allowed_mentions,
            )
            if auto_start_enabled:
                self._register_schedule_poll(
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                    message_id=poll_message.id,
                )
            await self._add_schedule_reactions(poll_message, option_list)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to create schedule poll")
            if poll_message is not None:
                if auto_start_enabled:
                    self._unregister_schedule_poll(poll_message.id)
                try:
                    await poll_message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to remove incomplete schedule poll %s",
                        poll_message.id,
                    )
            await self._send_notice(
                ctx,
                "❌ 投票の作成に失敗しました。Botの送信・埋め込み・リアクション権限を確認してください",
            )
            return None

        if auto_start_enabled and getattr(self.bot, "user", None) is not None:
            self._queue_auto_start_check_by_id(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                message_id=poll_message.id,
            )

        logger.info(
            "%s created schedule poll %s for role %s with minimum %s",
            ctx.author,
            poll_message.id,
            role.id,
            minimum,
        )
        return poll_message

    @schedule.command(name="status", description="開始時間投票の現在状況を表示します")
    @app_commands.describe(
        message="確認する投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
    )
    @commands.guild_only()
    async def schedule_status(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        missing_permissions = [
            name
            for name, enabled in {
                "メッセージ履歴を読む": bot_permissions.read_message_history,
                "埋め込みリンク": bot_permissions.embed_links,
            }.items()
            if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        poll_message = await self._fetch_schedule_poll(ctx, message_id)
        if poll_message is None:
            return
        options = schedule_options_from_embed(poll_message.embeds[0])
        if options is None:
            await self._send_notice(ctx, "❌ 投票の候補を読み取れませんでした")
            return
        try:
            voters_by_option = await self._collect_all_schedule_voters(
                poll_message,
                options,
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to collect schedule poll status %s", message_id)
            await self._send_notice(ctx, "❌ 投票状況を取得できませんでした")
            return

        status_embed = build_schedule_status_embed(
            poll_message.embeds[0],
            voters_by_option,
            event_date=schedule_event_date(poll_message),
        )
        status_embed.url = poll_message.jump_url
        await self._send_embed_notice(ctx, status_embed)

    @schedule.command(name="clone", description="開始時間投票の設定を複製します")
    @app_commands.describe(
        message="複製元の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
    )
    @commands.guild_only()
    async def schedule_clone(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return

        source_message = await self._fetch_schedule_poll(ctx, message_id)
        if source_message is None:
            return
        source_embed = source_message.embeds[0]
        options = schedule_options_from_embed(source_embed)
        if options is None:
            await self._send_notice(ctx, "❌ 投票の候補を読み取れませんでした")
            return
        normalized_options = normalize_auto_start_options(options)
        if normalized_options is not None:
            options = normalized_options

        role = self._schedule_role(source_message)
        if role is None:
            await self._send_notice(
                ctx,
                "❌ 元の投票の対象ロールが削除されているため複製できません",
            )
            return
        if role.is_default():
            await self._send_notice(ctx, "❌ @everyone は日程調整の対象にできません")
            return

        self._prune_expired_schedule_polls()
        minimum = schedule_minimum(source_embed) or AUTO_START_THRESHOLD
        cloned_message = await self._create_schedule_poll(
            ctx,
            role,
            options,
            minimum,
            role_already_mentioned=False,
        )
        if cloned_message is not None:
            logger.info(
                "%s cloned schedule poll %s as %s",
                ctx.author,
                message_id,
                cloned_message.id,
            )

    @schedule.command(name="date", description="開始時間投票の開催日を設定します")
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
        date="開催日（YYYY-MM-DD / YYYYMMDD / MM-DD / MMDD / DD）。解除は clear",
    )
    @commands.guild_only()
    async def schedule_date(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
        date: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        if date is None:
            date = message
            message = None
        if date is None:
            await self._send_notice(
                ctx,
                "❌ 開催日を `YYYY-MM-DD`、`YYYYMMDD`、`MM-DD`、`MMDD`、"
                "`DD`、`today`、または `clear` で指定してください",
            )
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        missing_permissions = [
            name
            for name, enabled in {
                "メッセージ履歴を読む": bot_permissions.read_message_history,
                "埋め込みリンク": bot_permissions.embed_links,
            }.items()
            if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            await self._change_schedule_date(ctx, message_id, date)

    async def _change_schedule_date(
        self,
        ctx: commands.Context,
        message_id: int,
        date_value: str,
    ) -> None:
        poll_message = await self._fetch_editable_schedule_poll(ctx, message_id)
        if poll_message is None:
            return
        original_embed = poll_message.embeds[0]
        default_date = schedule_default_date(poll_message)
        try:
            event_date = parse_schedule_date(
                date_value,
                now=self._utc_now(),
                posted_date=default_date,
            )
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return
        stored_date = None if event_date == default_date else event_date
        current_stored_date = schedule_date_override(original_embed)
        if current_stored_date == stored_date:
            effective_date = stored_date or default_date
            await self._send_notice(
                ctx,
                f"ℹ️ 開催日はすでに{effective_date.isoformat()}です\n"
                f"{poll_message.jump_url}",
            )
            return

        effective_date = stored_date or default_date
        try:
            lateness_event = self._lateness_registry.get_event(message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to inspect lateness event while changing poll date %s",
                message_id,
            )
            await self._send_notice(ctx, "❌ 遅刻判定の状態を確認できませんでした")
            return
        if (
            lateness_event is not None
            and lateness_event.activated_at is not None
            and lateness_event.event_date != effective_date
        ):
            await self._send_notice(
                ctx,
                "❌ VCでの遅刻判定が始まった後は開催日を変更できません",
            )
            return

        updated_embed = original_embed.copy()
        set_schedule_date_override(updated_embed, stored_date)
        try:
            await poll_message.edit(embed=updated_embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to change schedule poll %s date", message_id)
            await self._send_notice(ctx, "❌ 開催日を変更できませんでした")
            return

        tracked_start_time = (
            schedule_decided_start_time(original_embed)
            or announced_start_time(original_embed)
        )
        if tracked_start_time is not None:
            await self._store_lateness_event(
                poll_message,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                start_time=tracked_start_time,
                finalized=schedule_decided_start_time(original_embed) is not None,
                event_date=effective_date,
            )
        action = (
            f"開催日を{effective_date.isoformat()}に設定しました"
            if stored_date is not None
            else f"開催日を投稿日（{effective_date.isoformat()}）に戻しました"
        )
        await self._send_notice(
            ctx,
            f"✅ {action}\n{poll_message.jump_url}",
        )
        logger.info(
            "%s changed schedule poll %s date to %s",
            ctx.author,
            message_id,
            stored_date,
        )

    @schedule.command(
        name="minimum",
        description="開始時間投票の自動判定人数を変更します",
    )
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
        minimum="自動開始と判定する最低人数（1〜999人）",
    )
    @commands.guild_only()
    async def schedule_minimum(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
        minimum: Optional[int] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        if minimum is None and message is not None:
            try:
                minimum = int(message)
            except ValueError:
                await self._send_notice(ctx, "❌ 最低人数は整数で指定してください")
                return
            message = None
        if minimum is None:
            await self._send_notice(ctx, "❌ 最低人数を指定してください")
            return
        if not AUTO_START_MINIMUM_MIN <= minimum <= AUTO_START_MINIMUM_MAX:
            await self._send_notice(
                ctx,
                f"❌ 最低人数は{AUTO_START_MINIMUM_MIN}〜{AUTO_START_MINIMUM_MAX}人で指定してください",
            )
            return
        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        missing_permissions = [
            name
            for name, enabled in {
                "メッセージ履歴を読む": bot_permissions.read_message_history,
                "埋め込みリンク": bot_permissions.embed_links,
            }.items()
            if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            await self._change_schedule_minimum(ctx, message_id, minimum)

    async def _change_schedule_minimum(
        self,
        ctx: commands.Context,
        message_id: int,
        minimum: int,
    ) -> None:
        poll_message = await self._fetch_editable_schedule_poll(ctx, message_id)
        if poll_message is None:
            return

        original_embed = poll_message.embeds[0]
        try:
            lateness_event = self._lateness_registry.get_event(message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to inspect lateness event while changing minimum %s",
                message_id,
            )
            lateness_event = None
        if lateness_event is not None and lateness_event.activated_at is not None:
            await self._send_notice(
                ctx,
                "❌ VCでの遅刻判定が始まった後は最低人数を変更できません",
            )
            return
        if not is_auto_start_schedule(original_embed):
            await self._send_notice(
                ctx,
                "❌ 自動開始判定が有効な開始時間投票を指定してください",
            )
            return

        updated_embed = original_embed.copy()
        changed = set_auto_start_minimum(updated_embed, minimum)
        if not changed:
            await self._send_notice(
                ctx,
                f"ℹ️ 最低人数はすでに{minimum}人です\n{poll_message.jump_url}",
            )
            return

        self._invalidate_auto_start_check(message_id)
        try:
            await poll_message.edit(embed=updated_embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "failed to change schedule poll minimum %s",
                message_id,
            )
            self._resume_auto_start_check(ctx, poll_message)
            await self._send_notice(ctx, "❌ 最低人数を変更できませんでした")
            return

        guild_id = getattr(ctx.guild, "id", None)
        if guild_id is not None:
            self._register_schedule_poll(
                guild_id=guild_id,
                channel_id=ctx.channel.id,
                message_id=poll_message.id,
            )
            if getattr(self.bot, "user", None) is not None:
                self._queue_auto_start_check_by_id(
                    guild_id=guild_id,
                    channel_id=ctx.channel.id,
                    message_id=poll_message.id,
                )

        await self._send_notice(
            ctx,
            f"✅ 最低人数を{minimum}人に変更しました\n{poll_message.jump_url}",
        )
        logger.info(
            "%s changed schedule poll %s minimum to %s",
            ctx.author,
            message_id,
            minimum,
        )

    @schedule.command(name="deadline", description="開始時間投票の締切を設定します")
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
        deadline="日本時間の YYYY-MM-DD HH:MM。解除は clear",
    )
    @commands.guild_only()
    async def schedule_deadline(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
        *,
        deadline: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        if (
            ctx.interaction is None
            and message is not None
            and not looks_like_explicit_message_reference(message)
        ):
            deadline = " ".join(
                part for part in (message, deadline) if part is not None
            )
            message = None
        if deadline is None:
            await self._send_notice(
                ctx,
                "❌ 締切を `YYYY-MM-DD HH:MM` または `clear` で指定してください",
            )
            return
        try:
            deadline_at = parse_schedule_deadline(deadline, now=self._utc_now())
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        missing_permissions = [
            name
            for name, enabled in {
                "メッセージ履歴を読む": bot_permissions.read_message_history,
                "埋め込みリンク": bot_permissions.embed_links,
            }.items()
            if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            await self._change_schedule_deadline(
                ctx,
                message_id,
                deadline_at,
            )

    async def _change_schedule_deadline(
        self,
        ctx: commands.Context,
        message_id: int,
        deadline_at: datetime | None,
    ) -> None:
        poll_message = await self._fetch_editable_schedule_poll(ctx, message_id)
        if poll_message is None:
            return
        original_embed = poll_message.embeds[0]
        registered_poll = self._registered_schedule_polls.get(message_id)
        current_deadline = (
            registered_poll.deadline_at
            if registered_poll is not None
            else schedule_deadline_at(original_embed)
        )
        if deadline_at is not None and not is_auto_start_schedule(original_embed):
            await self._send_notice(
                ctx,
                "❌ 締切は自動判定中の時刻投票に設定してください",
            )
            return
        if current_deadline == deadline_at and schedule_deadline_at(
            original_embed
        ) == deadline_at:
            if deadline_at is None:
                message_text = "ℹ️ 締切は設定されていません"
            else:
                message_text = (
                    "ℹ️ 締切はすでに"
                    f"{format_schedule_deadline(deadline_at)}です"
                )
            await self._send_notice(
                ctx,
                f"{message_text}\n{poll_message.jump_url}",
            )
            return

        updated_embed = original_embed.copy()
        set_schedule_deadline(updated_embed, deadline_at)
        try:
            await poll_message.edit(embed=updated_embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to edit schedule poll %s deadline", message_id)
            await self._send_notice(ctx, "❌ 締切を変更できませんでした")
            return

        if deadline_at is None:
            persisted = self._clear_schedule_poll_deadline(message_id)
            action = "締切を解除しました"
        else:
            persisted = self._set_schedule_poll_deadline(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                message_id=message_id,
                deadline_at=deadline_at,
            )
            action = f"締切を{format_schedule_deadline(deadline_at)}に設定しました"
        persistence_warning = (
            "" if persisted else "\n⚠️ 再起動後に締切を復元できない可能性があります"
        )
        await self._send_notice(
            ctx,
            f"✅ {action}{persistence_warning}\n{poll_message.jump_url}",
        )
        logger.info(
            "%s changed schedule poll %s deadline to %s",
            ctx.author,
            message_id,
            deadline_at,
        )

    @schedule.command(name="update", description="既存の開始時間投票を更新します")
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
        options="新しい候補（例: 21:00 22:00 24:00 NG）。更新時に投票はリセットされます",
    )
    @commands.guild_only()
    async def schedule_update(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
        *,
        options: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return

        if (
            ctx.interaction is None
            and message is not None
            and not looks_like_explicit_message_reference(message)
        ):
            options = " ".join(
                part for part in (message, options) if part is not None
            )
            message = None
        if options is None:
            await self._send_notice(ctx, "❌ 新しい候補を指定してください")
            return

        try:
            option_list = parse_schedule_options(options)
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        required_permissions = {
            "メッセージの管理": bot_permissions.manage_messages,
            "メッセージ履歴を読む": bot_permissions.read_message_history,
            "リアクションの追加": bot_permissions.add_reactions,
            "埋め込みリンク": bot_permissions.embed_links,
        }
        missing_permissions = [
            name for name, enabled in required_permissions.items() if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            await self._update_schedule_message(ctx, message_id, option_list)

    async def _update_schedule_message(
        self,
        ctx: commands.Context,
        message_id: int,
        option_list: list[str],
    ) -> None:
        poll_message = await self._fetch_editable_schedule_poll(ctx, message_id)
        if poll_message is None:
            return

        original_embed = poll_message.embeds[0]
        try:
            lateness_event = self._lateness_registry.get_event(message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to inspect lateness event while updating poll %s",
                message_id,
            )
            lateness_event = None
        if lateness_event is not None and lateness_event.activated_at is not None:
            await self._send_notice(
                ctx,
                "❌ VCでの遅刻判定が始まった後は候補を更新できません",
            )
            return
        if is_schedule_closed(original_embed):
            await self._send_notice(
                ctx,
                "❌ 終了済みの開始時間投票は更新できません。cloneで新しい投票を作成してください",
            )
            return

        self._invalidate_auto_start_check(message_id)
        active_announcement = start_announcement(original_embed)
        registered_poll = self._registered_schedule_polls.get(message_id)
        deadline_at = (
            registered_poll.deadline_at
            if registered_poll is not None
            else schedule_deadline_at(original_embed)
        )
        auto_start_enabled = normalize_auto_start_options(option_list) is not None
        minimum = auto_start_minimum(original_embed) or AUTO_START_THRESHOLD
        updated_embed = original_embed.copy()
        updated_embed.description = format_schedule_options(option_list)
        set_auto_start_marker(
            updated_embed,
            enabled=auto_start_enabled,
            minimum=minimum,
        )
        if not auto_start_enabled:
            set_schedule_deadline(updated_embed, None)
        try:
            await poll_message.edit(embed=updated_embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to edit schedule poll %s", message_id)
            self._resume_auto_start_check(ctx, poll_message)
            await self._send_notice(
                ctx,
                "❌ 投票の候補を更新できませんでした",
            )
            return

        cancellation_warning = ""
        if active_announcement is not None:
            role = self._schedule_role(poll_message)
            role_mention = self._schedule_role_mention(poll_message, role)
            try:
                notification = await self._fetch_start_notification(
                    ctx.channel,
                    active_announcement,
                )
                if notification is not None:
                    await notification.edit(
                        content=(
                            f"~~{format_start_label(active_announcement.start_time)} {role_mention}~~\n"
                            "↩️ 投票が更新されたため、この開始通知は取り消されました。"
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "failed to cancel start notification while updating poll %s",
                    message_id,
                )
                if not await self._post_public_cancellation_fallback(
                    ctx.channel,
                    active_announcement,
                    role_mention,
                    "投票が更新されました",
                ):
                    cancellation_warning = (
                        "\n⚠️ 以前の開始通知を取消表示に更新できませんでした"
                    )

        self._cancel_lateness_event(message_id)
        self._unregister_schedule_poll(message_id)

        try:
            await self._reset_schedule_reactions(poll_message, option_list)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to reset schedule poll reactions %s", message_id)
            await self._send_notice(
                ctx,
                "⚠️ 候補は更新しましたが、リアクションの再設定に失敗しました。"
                "権限を確認して同じ内容でもう一度 update してください\n"
                f"{poll_message.jump_url}",
            )
            return

        guild_id = getattr(ctx.guild, "id", None)
        if auto_start_enabled and guild_id is not None:
            self._register_schedule_poll(
                guild_id=guild_id,
                channel_id=ctx.channel.id,
                message_id=poll_message.id,
            )
            if deadline_at is not None:
                self._set_schedule_poll_deadline(
                    guild_id=guild_id,
                    channel_id=ctx.channel.id,
                    message_id=poll_message.id,
                    deadline_at=deadline_at,
                )
            if getattr(self.bot, "user", None) is not None:
                self._queue_auto_start_check_by_id(
                    guild_id=guild_id,
                    channel_id=ctx.channel.id,
                    message_id=poll_message.id,
                )

        await self._send_notice(
            ctx,
            "✅ 日程投票を更新し、投票をリセットしました"
            f"{cancellation_warning}\n{poll_message.jump_url}",
        )
        logger.info("%s updated schedule poll %s", ctx.author, message_id)

    @schedule.command(name="decide", description="開始時間を手動で確定します")
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
        time="候補に含まれる開始時刻（例: 21:00）",
    )
    @commands.guild_only()
    async def schedule_decide(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
        time: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        if time is None:
            time = message
            message = None
        if time is None:
            await self._send_notice(ctx, "❌ 確定する開始時刻を指定してください")
            return
        decided_start_time = normalize_schedule_time(time)
        if decided_start_time is None:
            await self._send_notice(
                ctx,
                "❌ 開始時刻は `21`、`21:00`、`2100` のいずれかの形式で指定してください",
            )
            return
        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        can_send = (
            bot_permissions.send_messages_in_threads
            if isinstance(ctx.channel, discord.Thread)
            else bot_permissions.send_messages
        )
        missing_permissions = [
            name
            for name, enabled in {
                "メッセージの送信": can_send,
                "メッセージ履歴を読む": bot_permissions.read_message_history,
                "埋め込みリンク": bot_permissions.embed_links,
            }.items()
            if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            await self._decide_schedule_message(
                ctx,
                message_id,
                decided_start_time,
            )

    async def _decide_schedule_message(
        self,
        ctx: commands.Context,
        message_id: int,
        decided_start_time: str,
    ) -> None:
        poll_message = await self._fetch_editable_schedule_poll(ctx, message_id)
        if poll_message is None:
            return
        original_embed = poll_message.embeds[0]
        existing_decision = schedule_decided_start_time(original_embed)
        if is_schedule_closed(original_embed):
            if existing_decision is not None:
                await self._send_notice(
                    ctx,
                    "ℹ️ この投票はすでに"
                    f"{format_start_label(existing_decision)}で確定しています\n"
                    f"{poll_message.jump_url}",
                )
            else:
                await self._send_notice(ctx, "❌ この開始時間投票は終了済みです")
            return

        try:
            lateness_event = self._lateness_registry.get_event(message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to inspect lateness event while deciding poll %s",
                message_id,
            )
            lateness_event = None
        if (
            lateness_event is not None
            and lateness_event.activated_at is not None
            and lateness_event.start_time != decided_start_time
        ):
            await self._send_notice(
                ctx,
                "❌ VCでの遅刻判定が始まった後は別の開始時刻に変更できません",
            )
            return

        options = schedule_options_from_embed(original_embed)
        candidate_times = (
            [
                normalized
                for option in options
                if (normalized := normalize_schedule_time(option)) is not None
            ]
            if options is not None
            else []
        )
        if decided_start_time not in candidate_times:
            candidates = "、".join(candidate_times) or "なし"
            await self._send_notice(
                ctx,
                "❌ 確定する時刻は投票の候補から指定してください"
                f"（時刻候補: {candidates}）",
            )
            return

        try:
            voters_by_option = await self._collect_schedule_voters(
                poll_message,
                options or [],
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "failed to collect voters while deciding schedule poll %s",
                message_id,
            )
            await self._send_notice(
                ctx,
                "❌ 参加予定者を取得できないため開始時間を確定できませんでした",
            )
            return
        current_eligible_user_ids = eligible_voters_for_start(
            voters_by_option,
            decided_start_time,
        )

        announcement = start_announcement(original_embed)
        already_notified = (
            has_announced_start_before(original_embed)
            or message_id in self._start_notified_poll_ids
        )
        role = self._schedule_role(poll_message)
        if role is None and not already_notified:
            await self._send_notice(
                ctx,
                "❌ 対象ロールが削除されているため開始通知を送れません",
            )
            return
        if (
            role is not None
            and not already_notified
            and not getattr(role, "mentionable", True)
        ):
            author_permissions = ctx.channel.permissions_for(ctx.author)
            bot_permissions = ctx.channel.permissions_for(ctx.guild.me)
            if not author_permissions.mention_everyone:
                await self._send_notice(
                    ctx,
                    "❌ メンション不可のロールを通知する権限がありません",
                )
                return
            if not bot_permissions.mention_everyone:
                await self._send_notice(
                    ctx,
                    "❌ Botに「@everyone、@here、すべてのロールにメンション」の権限が必要です",
                )
                return

        self._invalidate_auto_start_check(message_id)
        role_mention = self._schedule_role_mention(poll_message, role)
        old_notification = None
        new_notification = None
        old_notification_changed = False
        sent_first_ping = False
        try:
            old_notification = await self._fetch_start_notification(
                ctx.channel,
                announcement,
            )
            reused_notification = (
                announcement is not None
                and announcement.start_time == decided_start_time
                and old_notification is not None
            )
            if old_notification is not None and announcement is not None:
                if reused_notification:
                    content = (
                        f"{format_start_label(decided_start_time)} {role_mention}\n"
                        "✅ この時間で確定しました。"
                    )
                else:
                    content = (
                        f"~~{format_start_label(announcement.start_time)} "
                        f"{role_mention}~~\n"
                        "↪️ 手動確定により、"
                        f"{format_start_label(decided_start_time)}へ変更されました。"
                    )
                try:
                    await old_notification.edit(
                        content=content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    old_notification_changed = True
                except discord.NotFound:
                    old_notification = None
                    reused_notification = False

            if not reused_notification:
                allowed_mentions = (
                    discord.AllowedMentions.none()
                    if already_notified
                    else discord.AllowedMentions(
                        everyone=False,
                        users=False,
                        roles=[role] if role is not None else False,
                        replied_user=False,
                    )
                )
                new_notification = await ctx.channel.send(
                    content=(
                        f"{format_start_label(decided_start_time)} {role_mention}\n"
                        "✅ この時間で確定しました。"
                    ),
                    allowed_mentions=allowed_mentions,
                )
                if not already_notified:
                    sent_first_ping = True
                    self._start_notified_poll_ids.add(message_id)

            decided_embed = original_embed.copy()
            set_schedule_decision(decided_embed, decided_start_time)
            set_schedule_deadline(decided_embed, None)
            mark_schedule_closed(decided_embed)
            decision_notification = (
                old_notification if reused_notification else new_notification
            )
            if decision_notification is not None:
                set_schedule_related_notification_id(
                    decided_embed,
                    decision_notification.id,
                )
            if decided_embed.title and not decided_embed.title.endswith("（終了）"):
                decided_embed.title += "（終了）"
            await poll_message.edit(embed=decided_embed)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception("failed to decide schedule poll %s", message_id)
            if new_notification is not None:
                try:
                    await new_notification.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to remove incomplete decision notification for poll %s",
                        message_id,
                    )
            if old_notification_changed:
                await self._restore_start_notification(
                    old_notification,
                    announcement,
                    role_mention,
                )
            if sent_first_ping:
                history_embed = original_embed.copy()
                mark_start_notification_history(history_embed)
                try:
                    await poll_message.edit(embed=history_embed)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to persist notification history after decision failure for poll %s",
                        message_id,
                    )
            self._resume_auto_start_check(ctx, poll_message)
            await self._send_notice(ctx, "❌ 開始時間を確定できませんでした")
            return

        await self._store_lateness_event(
            poll_message,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            start_time=decided_start_time,
            current_eligible_user_ids=current_eligible_user_ids,
            finalized=True,
        )

        self._unregister_schedule_poll(message_id)
        await self._send_notice(
            ctx,
            f"✅ {format_start_label(decided_start_time)}で確定しました\n"
            f"{poll_message.jump_url}",
        )
        logger.info(
            "%s decided schedule poll %s at %s",
            ctx.author,
            message_id,
            decided_start_time,
        )

    @schedule.command(
        name="late",
        description="月・年ごとの遅刻・欠席集計を表示します",
    )
    @app_commands.describe(
        period="省略で今月。10、2026、202704、2027-04の形式で月または年を指定",
    )
    @commands.guild_only()
    async def schedule_late(
        self,
        ctx: commands.Context,
        period: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        try:
            target_period = parse_lateness_period(period, now=self._utc_now())
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return

        await ctx.defer(ephemeral=ctx.interaction is not None)
        try:
            stats = self._lateness_registry.stats_between(
                guild_id=ctx.guild.id,
                start_date=target_period.start_date,
                end_date=target_period.end_date,
            )
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to load lateness stats for guild %s period %s",
                ctx.guild.id,
                target_period.label,
            )
            await self._send_notice(ctx, "❌ 遅刻集計を取得できませんでした")
            return

        embeds = build_lateness_stats_embeds(
            stats,
            guild=ctx.guild,
            period=target_period,
        )
        chart_image = None
        if stats:
            try:
                from kazekoshi.lateness_chart import build_lateness_chart

                chart_image = build_lateness_chart(
                    stats,
                    guild=ctx.guild,
                    period_label=target_period.label,
                )
            except Exception:
                logger.exception(
                    "failed to render lateness chart for guild %s period %s",
                    ctx.guild.id,
                    target_period.label,
                )

        if chart_image is not None:
            filename = (
                f"lateness-{target_period.start_date.isoformat()}-"
                f"{target_period.end_date.isoformat()}.png"
            )
            embeds[0].set_image(url=f"attachment://{filename}")
            chart_file = discord.File(chart_image, filename=filename)
            try:
                kwargs = {"ephemeral": True} if ctx.interaction is not None else {}
                await ctx.send(embed=embeds[0], file=chart_file, **kwargs)
            finally:
                chart_file.close()
            remaining_embeds = embeds[1:]
        else:
            remaining_embeds = embeds
        for embed in remaining_embeds:
            await self._send_embed_notice(ctx, embed)
        logger.info(
            "%s viewed lateness stats for guild %s period %s",
            ctx.author,
            ctx.guild.id,
            target_period.label,
        )

    @schedule.command(
        name="lateoff",
        description="この募集の遅刻記録・集計・通知を停止します",
    )
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
    )
    @commands.guild_only()
    async def schedule_lateoff(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            poll_message = await self._fetch_editable_schedule_poll(
                ctx,
                message_id,
            )
            if poll_message is None:
                return
            try:
                changed = self._lateness_registry.disable_poll(
                    poll_message_id=message_id,
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                    disabled_at=self._utc_now(),
                )
            except (sqlite3.Error, OSError):
                logger.exception(
                    "failed to disable lateness tracking for schedule poll %s",
                    message_id,
                )
                await self._send_notice(ctx, "❌ 遅刻判定を停止できませんでした")
                return
            self._cancel_lateness_event(message_id)

        if changed:
            notice = (
                "✅ この募集の遅刻判定を停止しました。"
                "月次統計と今後の遅刻通知にも含めません"
            )
        else:
            notice = "ℹ️ この募集の遅刻判定はすでに停止しています"
        await self._send_notice(ctx, f"{notice}\n{poll_message.jump_url}")
        logger.info(
            "%s disabled lateness tracking for schedule poll %s",
            ctx.author,
            message_id,
        )

    @schedule.command(name="close", description="開始時間投票の自動判定を終了します")
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
    )
    @commands.guild_only()
    async def schedule_close(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        missing_permissions = [
            name
            for name, enabled in {
                "メッセージ履歴を読む": bot_permissions.read_message_history,
                "埋め込みリンク": bot_permissions.embed_links,
            }.items()
            if not enabled
        ]
        if missing_permissions:
            await self._send_notice(
                ctx,
                "❌ Botに次の権限が必要です: " + "、".join(missing_permissions),
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            poll_message = await self._fetch_editable_schedule_poll(ctx, message_id)
            if poll_message is None:
                return
            try:
                cancellation_warning = await self._close_schedule_poll_message(
                    ctx.channel,
                    poll_message,
                    cancellation_reason=(
                        "投票が終了したため、この開始通知は取り消されました。"
                    ),
                    fallback_reason="投票が終了しました",
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("failed to close schedule poll %s", message_id)
                self._resume_auto_start_check(ctx, poll_message)
                await self._send_notice(ctx, "❌ 開始時間投票を終了できませんでした")
                return
            await self._send_notice(
                ctx,
                "✅ 開始時間投票を終了しました"
                f"{cancellation_warning}\n{poll_message.jump_url}",
            )
            logger.info("%s closed schedule poll %s", ctx.author, message_id)

    @schedule.command(
        name="delete",
        description="開始時間投票の投稿と関連データを完全削除します",
    )
    @app_commands.describe(
        message="対象の投稿IDまたはリンク（省略時は同チャンネルの最新投票）",
    )
    @commands.guild_only()
    async def schedule_delete(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
    ):
        if not await self._validate_schedule_context(ctx):
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await self._send_notice(ctx, "❌ Botのサーバー権限を確認できませんでした")
            return
        bot_permissions = ctx.channel.permissions_for(bot_member)
        if not bot_permissions.read_message_history:
            await self._send_notice(
                ctx,
                "❌ Botに「メッセージ履歴を読む」権限が必要です",
            )
            return

        message_id = await self._resolve_schedule_message_id(ctx, message)
        if message_id is None:
            return
        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            poll_message = await self._fetch_editable_schedule_poll(
                ctx,
                message_id,
            )
            if poll_message is None:
                return

            embed = poll_message.embeds[0]
            announcement = start_announcement(embed)
            notification_id = (
                schedule_related_notification_id(embed)
                or (
                    announcement.message_id
                    if announcement is not None
                    else None
                )
                or self._poll_notification_ids.get(message_id)
            )
            notification = None
            notification_warning = ""
            if notification_id is not None:
                try:
                    notification = await self._fetch_start_notification(
                        ctx.channel,
                        StartAnnouncement("", notification_id),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "failed to fetch related notification while deleting poll %s",
                        message_id,
                    )
                    notification_warning = (
                        "\n⚠️ 関連する開始通知を取得できなかったため、"
                        "通知は削除できませんでした"
                    )

            try:
                await poll_message.delete()
            except discord.NotFound:
                # 取得後に別操作で削除された場合も、関連データの削除は続ける。
                pass
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("failed to delete schedule poll %s", message_id)
                await self._send_notice(
                    ctx,
                    "❌ 開始時間投票の投稿を削除できませんでした",
                )
                return

            self._invalidate_auto_start_check(message_id)
            self._delete_lateness_event(message_id)
            self._unregister_schedule_poll(message_id)

            if notification is not None:
                try:
                    await notification.delete()
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "failed to delete related notification for poll %s",
                        message_id,
                    )
                    notification_warning = (
                        "\n⚠️ 関連する開始通知は削除できませんでした"
                    )

            await self._send_notice(
                ctx,
                "✅ 開始時間投票の投稿と関連データを完全削除しました"
                f"{notification_warning}",
            )
            logger.info("%s deleted schedule poll %s", ctx.author, message_id)

    async def _close_schedule_poll_message(
        self,
        channel,
        poll_message: discord.Message,
        *,
        cancellation_reason: str,
        fallback_reason: str,
    ) -> str:
        message_id = poll_message.id
        self._invalidate_auto_start_check(message_id)
        original_embed = poll_message.embeds[0]
        active_announcement = start_announcement(original_embed)
        related_notification_id = (
            schedule_related_notification_id(original_embed)
            or (
                active_announcement.message_id
                if active_announcement is not None
                else None
            )
            or self._poll_notification_ids.get(message_id)
        )
        closed_embed = original_embed.copy()
        mark_schedule_closed(closed_embed)
        if related_notification_id is not None:
            set_schedule_related_notification_id(
                closed_embed,
                related_notification_id,
            )
        if closed_embed.title and not closed_embed.title.endswith("（終了）"):
            closed_embed.title += "（終了）"
        await poll_message.edit(embed=closed_embed)

        cancellation_warning = ""
        if active_announcement is not None:
            role = self._schedule_role(poll_message)
            role_mention = self._schedule_role_mention(poll_message, role)
            try:
                notification = await self._fetch_start_notification(
                    channel,
                    active_announcement,
                )
                if notification is not None:
                    await notification.edit(
                        content=(
                            f"~~{format_start_label(active_announcement.start_time)} "
                            f"{role_mention}~~\n↩️ {cancellation_reason}"
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "failed to cancel start notification while closing poll %s",
                    message_id,
                )
                if not await self._post_public_cancellation_fallback(
                    channel,
                    active_announcement,
                    role_mention,
                    fallback_reason,
                ):
                    cancellation_warning = (
                        "\n⚠️ 以前の開始通知を取消表示に更新できませんでした"
                    )

        self._cancel_lateness_event(message_id)
        self._unregister_schedule_poll(message_id)
        return cancellation_warning

    async def _fetch_schedule_poll(
        self,
        ctx: commands.Context,
        message_id: int,
    ) -> discord.Message | None:
        try:
            poll_message = await ctx.channel.fetch_message(message_id)
        except discord.NotFound:
            await self._send_notice(ctx, "❌ 指定された投稿が見つかりません")
            return None
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to fetch schedule poll %s", message_id)
            await self._send_notice(ctx, "❌ 指定された投稿を取得できませんでした")
            return None

        if self.bot.user is None or poll_message.author.id != self.bot.user.id:
            await self._send_notice(ctx, "❌ このBotが作成した開始時間投票を指定してください")
            return None
        if not poll_message.embeds:
            await self._send_notice(ctx, "❌ 指定された投稿は開始時間投票ではありません")
            return None
        creator_id = schedule_author_id(poll_message.embeds[0])
        if creator_id is None:
            await self._send_notice(ctx, "❌ 指定された投稿は開始時間投票ではありません")
            return None

        return poll_message

    async def _resolve_schedule_message_id(
        self,
        ctx: commands.Context,
        reference: str | None,
    ) -> int | None:
        if reference is not None:
            try:
                message_id, link_channel_id = parse_message_id(reference)
            except ScheduleInputError as error:
                await self._send_notice(ctx, f"❌ {error}")
                return None
            if link_channel_id is not None and link_channel_id != ctx.channel.id:
                await self._send_notice(
                    ctx,
                    "❌ 同じチャンネルの開始時間投票を指定してください",
                )
                return None
            return message_id

        bot_user = self.bot.user
        if bot_user is None:
            await self._send_notice(ctx, "❌ Botのユーザー情報を取得できませんでした")
            return None
        history = getattr(ctx.channel, "history", None)
        if history is None:
            await self._send_notice(ctx, "❌ このチャンネルの投稿履歴を取得できません")
            return None
        try:
            async for candidate in history(limit=LATEST_SCHEDULE_HISTORY_LIMIT):
                if (
                    candidate.author.id == bot_user.id
                    and candidate.embeds
                    and schedule_author_id(candidate.embeds[0]) is not None
                ):
                    return candidate.id
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to find latest schedule poll")
            await self._send_notice(ctx, "❌ このチャンネルの投稿履歴を取得できませんでした")
            return None

        await self._send_notice(
            ctx,
            f"❌ 直近{LATEST_SCHEDULE_HISTORY_LIMIT}件に開始時間投票が見つかりません",
        )
        return None

    async def _fetch_editable_schedule_poll(
        self,
        ctx: commands.Context,
        message_id: int,
    ) -> discord.Message | None:
        poll_message = await self._fetch_schedule_poll(ctx, message_id)
        if poll_message is None:
            return None
        creator_id = schedule_author_id(poll_message.embeds[0])

        author_can_manage = ctx.channel.permissions_for(ctx.author).manage_messages
        if ctx.author.id != creator_id and not author_can_manage:
            await self._send_notice(
                ctx,
                "❌ この投票を変更できるのは作成者か、メッセージ管理権限を持つ人だけです",
            )
            return None
        return poll_message

    def _register_schedule_poll(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        existing_poll = self._registered_schedule_polls.get(message_id)
        poll = RegisteredSchedulePoll(
            guild_id,
            channel_id,
            message_id,
            existing_poll.deadline_at if existing_poll is not None else None,
        )
        self._registered_schedule_ids.add(message_id)
        self._registered_schedule_polls[message_id] = poll
        if message_id in self._persisted_schedule_ids:
            return
        try:
            self._schedule_registry.register(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
            )
            self._persisted_schedule_ids.add(message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to register schedule poll %s for restart recovery",
                message_id,
            )

    def _set_schedule_poll_deadline(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        deadline_at: datetime,
    ) -> bool:
        poll = RegisteredSchedulePoll(
            guild_id,
            channel_id,
            message_id,
            deadline_at,
        )
        self._registered_schedule_ids.add(message_id)
        self._registered_schedule_polls[message_id] = poll
        persisted = True
        try:
            self._schedule_registry.set_deadline(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                deadline_at=deadline_at,
            )
            self._persisted_schedule_ids.add(message_id)
        except (sqlite3.Error, OSError):
            persisted = False
            logger.exception(
                "failed to persist schedule poll %s deadline",
                message_id,
            )
        self._queue_schedule_deadline(poll)
        return persisted

    def _clear_schedule_poll_deadline(self, message_id: int) -> bool:
        poll = self._registered_schedule_polls.get(message_id)
        if poll is not None:
            self._registered_schedule_polls[message_id] = RegisteredSchedulePoll(
                poll.guild_id,
                poll.channel_id,
                poll.message_id,
                None,
            )
        deadline_task = self._deadline_tasks.pop(message_id, None)
        if deadline_task is not None:
            deadline_task.cancel()
        try:
            self._schedule_registry.clear_deadline(message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to clear schedule poll %s deadline",
                message_id,
            )
            return False
        return True

    def _prune_expired_schedule_polls(
        self,
        *,
        now: datetime | None = None,
    ) -> list[RegisteredSchedulePoll]:
        cutoff_time = (now or datetime.now(timezone.utc)) - timedelta(
            days=SCHEDULE_RETENTION_DAYS
        )
        cutoff_message_id = discord.utils.time_snowflake(cutoff_time, high=False)
        try:
            persisted_expired_polls = self._schedule_registry.prune_before(
                cutoff_message_id
            )
        except (sqlite3.Error, OSError):
            logger.exception("failed to prune expired schedule polls")
            return []

        # DB書込みに失敗してメモリだけで監視していた投票も期限対象に含める。
        expired_by_id = {
            poll.message_id: poll for poll in persisted_expired_polls
        }
        expired_by_id.update(
            {
                poll.message_id: poll
                for poll in self._registered_schedule_polls.values()
                if poll.message_id < cutoff_message_id
                and poll.deadline_at is None
            }
        )
        expired_polls = [
            expired_by_id[message_id]
            for message_id in sorted(expired_by_id)
        ]

        # 先に登録対象外へ移し、進行中のshield済みtransitionにも失効を伝える。
        for poll in expired_polls:
            self._registered_schedule_ids.discard(poll.message_id)
        for poll in expired_polls:
            self._invalidate_auto_start_check(poll.message_id)
            self._persisted_schedule_ids.discard(poll.message_id)
            self._registered_schedule_polls.pop(poll.message_id, None)
            deadline_task = self._deadline_tasks.pop(poll.message_id, None)
            if deadline_task is not None:
                deadline_task.cancel()
            self._forget_start_notification(poll.message_id)
            self._last_cancelled_user_ids.pop(poll.message_id, None)
            self._start_notified_poll_ids.discard(poll.message_id)
        if expired_polls:
            logger.info(
                "expired %s schedule poll registry entries older than %s days",
                len(expired_polls),
                SCHEDULE_RETENTION_DAYS,
            )
        return expired_polls

    def _unregister_schedule_poll(self, message_id: int) -> None:
        self._registered_schedule_ids.discard(message_id)
        self._registered_schedule_polls.pop(message_id, None)
        deadline_task = self._deadline_tasks.pop(message_id, None)
        if deadline_task is not None:
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if deadline_task is not current_task:
                deadline_task.cancel()
        self._forget_start_notification(message_id)
        self._last_cancelled_user_ids.pop(message_id, None)
        self._start_notified_poll_ids.discard(message_id)
        if message_id not in self._persisted_schedule_ids:
            return
        try:
            self._schedule_registry.unregister(message_id)
            self._persisted_schedule_ids.discard(message_id)
        except (sqlite3.Error, OSError):
            logger.exception("failed to unregister schedule poll %s", message_id)

    def _queue_schedule_deadline(self, poll: RegisteredSchedulePoll) -> None:
        if poll.deadline_at is None:
            return
        previous_task = self._deadline_tasks.get(poll.message_id)
        if previous_task is not None:
            previous_task.cancel()
        task = asyncio.create_task(
            self._run_schedule_deadline(poll),
            name=f"schedule-deadline-{poll.message_id}",
        )
        self._deadline_tasks[poll.message_id] = task
        task.add_done_callback(
            lambda completed, message_id=poll.message_id: (
                self._deadline_tasks.pop(message_id, None)
                if self._deadline_tasks.get(message_id) is completed
                else None
            )
        )

    async def _run_schedule_deadline(
        self,
        poll: RegisteredSchedulePoll,
    ) -> None:
        deadline_at = poll.deadline_at
        if deadline_at is None:
            return
        delay = max(
            0.0,
            (deadline_at - self._utc_now()).total_seconds(),
        )
        try:
            await self._deadline_sleep(delay)
            for attempt in range(AUTO_START_MAX_RETRIES + 1):
                if self._registered_schedule_polls.get(poll.message_id) != poll:
                    return
                lock = self._auto_start_locks.setdefault(
                    poll.message_id,
                    asyncio.Lock(),
                )
                try:
                    async with lock:
                        if self._registered_schedule_polls.get(poll.message_id) != poll:
                            return
                        await self._close_schedule_poll_at_deadline(poll)
                    return
                except discord.NotFound:
                    self._unregister_schedule_poll(poll.message_id)
                    return
                except discord.Forbidden:
                    logger.exception(
                        "forbidden while closing schedule poll %s at deadline",
                        poll.message_id,
                    )
                    self._queue_auto_start_check_by_id(
                        guild_id=poll.guild_id,
                        channel_id=poll.channel_id,
                        message_id=poll.message_id,
                    )
                    return
                except discord.HTTPException:
                    if attempt >= AUTO_START_MAX_RETRIES:
                        logger.exception(
                            "failed to close schedule poll %s at deadline",
                            poll.message_id,
                        )
                        self._queue_auto_start_check_by_id(
                            guild_id=poll.guild_id,
                            channel_id=poll.channel_id,
                            message_id=poll.message_id,
                        )
                        return
                    await self._retry_sleep(
                        AUTO_START_NOTICE_RETRY_DELAY_SECONDS
                    )
        except asyncio.CancelledError:
            raise

    async def _close_schedule_poll_at_deadline(
        self,
        poll: RegisteredSchedulePoll,
    ) -> None:
        channel = self.bot.get_channel(poll.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(poll.channel_id)
        if not hasattr(channel, "fetch_message"):
            self._unregister_schedule_poll(poll.message_id)
            return
        poll_message = await channel.fetch_message(poll.message_id)
        bot_user = self.bot.user
        if (
            bot_user is None
            or poll_message.author.id != bot_user.id
            or not poll_message.embeds
            or schedule_author_id(poll_message.embeds[0]) is None
        ):
            self._unregister_schedule_poll(poll.message_id)
            return
        if is_schedule_closed(poll_message.embeds[0]):
            self._unregister_schedule_poll(poll.message_id)
            return
        await self._close_schedule_poll_message(
            channel,
            poll_message,
            cancellation_reason="締切時刻になったため、この開始通知は取り消されました。",
            fallback_reason="投票の締切時刻になりました",
        )
        logger.info("closed schedule poll %s at deadline", poll.message_id)

    def _remember_start_notification(
        self,
        poll_message_id: int,
        notification_message_id: int,
    ) -> None:
        poll = self._registered_schedule_polls.get(poll_message_id)
        if poll is None:
            return
        previous_id = self._poll_notification_ids.get(poll_message_id)
        if previous_id is not None:
            self._notification_poll_refs.pop(previous_id, None)
        self._poll_notification_ids[poll_message_id] = notification_message_id
        self._notification_poll_refs[notification_message_id] = poll

    def _forget_start_notification(self, poll_message_id: int) -> None:
        notification_id = self._poll_notification_ids.pop(
            poll_message_id,
            None,
        )
        if notification_id is not None:
            self._notification_poll_refs.pop(notification_id, None)

    async def _store_lateness_event(
        self,
        poll_message: discord.Message,
        *,
        guild_id: int,
        channel_id: int,
        start_time: str,
        current_eligible_user_ids: set[int] | None = None,
        finalized: bool = False,
        event_date: date | None = None,
    ) -> ScheduleLatenessEvent | None:
        try:
            if self._lateness_registry.is_disabled(poll_message.id):
                return None
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to inspect lateness setting for schedule poll %s",
                poll_message.id,
            )
            return None
        event_date = event_date or schedule_event_date(poll_message)
        start_at = schedule_start_datetime(event_date, start_time)
        minimum = schedule_minimum(poll_message.embeds[0]) or AUTO_START_THRESHOLD
        now = self._utc_now()
        if start_at + timedelta(hours=LATENESS_TRACKING_HOURS) < now:
            self._cancel_lateness_event(poll_message.id)
            return None
        try:
            event = self._lateness_registry.upsert_event(
                poll_message_id=poll_message.id,
                guild_id=guild_id,
                channel_id=channel_id,
                event_date=event_date,
                start_time=start_time,
                minimum=minimum,
                start_at=start_at,
                finalized=finalized,
            )
            if event.snapshotted_at is None and now >= event.snapshot_at:
                if current_eligible_user_ids is None:
                    options = schedule_options_from_embed(poll_message.embeds[0])
                    if options is None:
                        return event
                    voters_by_option = await self._collect_schedule_voters(
                        poll_message,
                        options,
                    )
                    current_eligible_user_ids = eligible_voters_for_start(
                        voters_by_option,
                        event.start_time,
                    )
                if self._lateness_registry.snapshot_participants(
                    event.poll_message_id,
                    current_eligible_user_ids,
                    snapshotted_at=now,
                ):
                    event = self._lateness_registry.get_event(event.poll_message_id)
                    if event is not None:
                        self._capture_current_voice_presence(
                            event,
                            captured_at=now,
                            unknown_join_at=event.start_at,
                        )
            elif (
                event.snapshotted_at is not None
                and current_eligible_user_ids is not None
            ):
                cancelled, restored = self._lateness_registry.sync_cancellations(
                    event.poll_message_id,
                    current_eligible_user_ids,
                    changed_at=now,
                )
                for user_id in cancelled:
                    self._lateness_registry.clear_presence(
                        event.poll_message_id,
                        user_id,
                    )
                self._capture_restored_voice_presence(
                    event,
                    restored,
                    captured_at=now,
                )
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to store lateness event for schedule poll %s",
                poll_message.id,
            )
            return None

        if event is not None and self._lateness_tracking_started:
            self._queue_lateness_event_task(event)
        return event

    def _stop_lateness_tasks(self, poll_message_id: int) -> None:
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        task = self._lateness_tasks.pop(poll_message_id, None)
        if task is not None and task is not current_task:
            task.cancel()
        reaction_task = self._lateness_reaction_tasks.pop(
            poll_message_id,
            None,
        )
        if reaction_task is not None and reaction_task is not current_task:
            reaction_task.cancel()

    def _cancel_lateness_event(self, poll_message_id: int) -> bool:
        self._stop_lateness_tasks(poll_message_id)
        try:
            return self._lateness_registry.cancel_pending(poll_message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to cancel lateness event for schedule poll %s",
                poll_message_id,
            )
            return False

    def _delete_lateness_event(self, poll_message_id: int) -> bool:
        self._stop_lateness_tasks(poll_message_id)
        try:
            return self._lateness_registry.delete_poll(poll_message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to delete lateness data for schedule poll %s",
                poll_message_id,
            )
            return False

    def _queue_lateness_event_task(self, event: ScheduleLatenessEvent) -> None:
        if (
            not self._lateness_tracking_started
            or event.tracking_completed_at is not None
        ):
            return
        previous_task = self._lateness_tasks.get(event.poll_message_id)
        if previous_task is not None:
            previous_task.cancel()
        task = asyncio.create_task(
            self._run_lateness_event_task(event.poll_message_id),
            name=f"schedule-lateness-{event.poll_message_id}",
        )
        self._lateness_tasks[event.poll_message_id] = task
        task.add_done_callback(
            lambda completed, message_id=event.poll_message_id: (
                self._lateness_tasks.pop(message_id, None)
                if self._lateness_tasks.get(message_id) is completed
                else None
            )
        )

    async def _run_lateness_event_task(self, poll_message_id: int) -> None:
        try:
            wait_until_ready = getattr(self.bot, "wait_until_ready", None)
            if wait_until_ready is not None:
                await wait_until_ready()
            event = self._lateness_registry.get_event(poll_message_id)
            if (
                event is None
                or event.tracking_completed_at is not None
                or self._lateness_registry.is_disabled(poll_message_id)
            ):
                return
            if event.snapshotted_at is None:
                await self._lateness_sleep(
                    max(
                        0.0,
                        (event.snapshot_at - self._utc_now()).total_seconds(),
                    )
                )
                lock = self._auto_start_locks.setdefault(
                    poll_message_id,
                    asyncio.Lock(),
                )
                async with lock:
                    event = self._lateness_registry.get_event(poll_message_id)
                    if event is None or event.snapshotted_at is not None:
                        pass
                    elif self._utc_now() <= event.tracking_until:
                        poll_message = await self._fetch_lateness_poll(event)
                        if poll_message is not None:
                            await self._snapshot_lateness_event(
                                event,
                                poll_message,
                            )

            event = self._lateness_registry.get_event(poll_message_id)
            if event is None or event.tracking_completed_at is not None:
                return
            await self._lateness_sleep(
                max(0.0, (event.start_at - self._utc_now()).total_seconds())
            )
            lock = self._auto_start_locks.setdefault(
                poll_message_id,
                asyncio.Lock(),
            )
            async with lock:
                event = self._lateness_registry.get_event(poll_message_id)
                if (
                    event is not None
                    and event.snapshotted_at is not None
                    and event.activated_at is None
                    and event.tracking_completed_at is None
                ):
                    await self._evaluate_lateness_event(poll_message_id)

            event = self._lateness_registry.get_event(poll_message_id)
            if event is None or event.tracking_completed_at is not None:
                return
            if (
                event.activated_at is not None
                and self._complete_lateness_if_minimum_present(
                    event,
                    completed_at=self._utc_now(),
                )
            ):
                return
            for reminder_minutes in LATENESS_REMINDER_MINUTES:
                reminder_at = event.start_at + timedelta(
                    minutes=reminder_minutes
                )
                if reminder_at >= event.tracking_until:
                    continue
                await self._lateness_sleep(
                    max(
                        0.0,
                        (reminder_at - self._utc_now()).total_seconds(),
                    )
                )
                async with lock:
                    event = self._lateness_registry.get_event(poll_message_id)
                    if (
                        event is None
                        or event.tracking_completed_at is not None
                        or self._lateness_registry.is_disabled(poll_message_id)
                    ):
                        return
                    if event.activated_at is None:
                        await self._evaluate_lateness_event(poll_message_id)
                        event = self._lateness_registry.get_event(
                            poll_message_id
                        )
                    if event is not None and event.activated_at is not None:
                        await self._process_due_lateness_reminders(event)
                        event = self._lateness_registry.get_event(
                            poll_message_id
                        )
                        if (
                            event is None
                            or event.tracking_completed_at is not None
                        ):
                            return

            event = self._lateness_registry.get_event(poll_message_id)
            if event is None or event.tracking_completed_at is not None:
                return
            await self._lateness_sleep(
                max(
                    0.0,
                    (event.tracking_until - self._utc_now()).total_seconds(),
                )
            )
            async with lock:
                recorded_absences = self._lateness_registry.complete_tracking(
                    poll_message_id,
                    completed_at=self._utc_now(),
                )
                logger.info(
                    "completed lateness tracking for schedule poll %s with %s absences",
                    poll_message_id,
                    recorded_absences,
                )
        except asyncio.CancelledError:
            raise
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to run lateness event for schedule poll %s",
                poll_message_id,
            )
        except Exception:
            logger.exception(
                "unexpected failure in lateness event for schedule poll %s",
                poll_message_id,
            )

    async def _fetch_lateness_notification_channel(
        self,
        event: ScheduleLatenessEvent,
    ):
        get_channel = getattr(self.bot, "get_channel", None)
        channel = get_channel(event.channel_id) if get_channel is not None else None
        if channel is None:
            fetch_channel = getattr(self.bot, "fetch_channel", None)
            if fetch_channel is None:
                return None
            try:
                channel = await fetch_channel(event.channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.exception(
                    "failed to fetch channel for lateness reminder %s",
                    event.poll_message_id,
                )
                return None
        if not hasattr(channel, "send"):
            return None
        return channel

    def _capture_current_activated_arrivals(
        self,
        event: ScheduleLatenessEvent,
    ) -> None:
        """再起動などで取り逃した、開催VCにいるメンバーを到着済みにする。"""
        if event.activated_at is None or event.voice_channel_id is None:
            return
        guild = self._get_lateness_guild(event.guild_id)
        if guild is None:
            return
        for user_id in self._lateness_registry.unarrived_participant_user_ids(
            event.poll_message_id
        ):
            member = guild.get_member(user_id)
            voice_channel = getattr(
                getattr(member, "voice", None),
                "channel",
                None,
            )
            if (
                voice_channel is None
                or voice_channel.id != event.voice_channel_id
            ):
                continue
            self._lateness_registry.record_arrival(
                event.poll_message_id,
                user_id,
                voice_channel_id=voice_channel.id,
                # 入室時刻を復元できない場合は、誤遅刻を避ける。
                joined_at=event.start_at,
            )

    def _complete_lateness_if_minimum_present(
        self,
        event: ScheduleLatenessEvent,
        *,
        completed_at: datetime,
    ) -> bool:
        if event.activated_at is None or event.voice_channel_id is None:
            return False
        self._capture_current_activated_arrivals(event)
        guild = self._get_lateness_guild(event.guild_id)
        if guild is None:
            return False
        present_count = 0
        for participant in self._lateness_registry.participants(
            event.poll_message_id
        ):
            member = guild.get_member(participant.user_id)
            voice_channel = getattr(
                getattr(member, "voice", None),
                "channel",
                None,
            )
            if (
                voice_channel is not None
                and voice_channel.id == event.voice_channel_id
            ):
                present_count += 1
        if present_count < event.minimum:
            return False
        if not self._lateness_registry.complete_tracking_without_absences(
            event.poll_message_id,
            completed_at=completed_at,
        ):
            return False
        self._stop_lateness_tasks(event.poll_message_id)
        logger.info(
            "completed lateness tracking for schedule poll %s with %s/%s present",
            event.poll_message_id,
            present_count,
            event.minimum,
        )
        return True

    async def _process_due_lateness_reminders(
        self,
        event: ScheduleLatenessEvent,
    ) -> bool:
        now = self._utc_now()
        if (
            event.activated_at is None
            or event.tracking_completed_at is not None
            or now >= event.tracking_until
            or self._lateness_registry.is_disabled(event.poll_message_id)
        ):
            return False
        history = self._lateness_registry.reminder_history(
            event.poll_message_id
        )
        pending_thresholds = {
            threshold
            for threshold in LATENESS_REMINDER_MINUTES
            if threshold not in history
            and now >= event.start_at + timedelta(minutes=threshold)
        }
        if not pending_thresholds:
            return False

        reminder_minutes = max(pending_thresholds)
        if self._complete_lateness_if_minimum_present(
            event,
            completed_at=now,
        ):
            return False
        late_user_ids = (
            self._lateness_registry.unarrived_participant_user_ids(
                event.poll_message_id
            )
        )
        if not late_user_ids:
            self._lateness_registry.record_processed_reminders(
                event.poll_message_id,
                pending_thresholds,
                processed_at=now,
            )
            return False

        channel = await self._fetch_lateness_notification_channel(event)
        if channel is None:
            return False
        duration = format_lateness_reminder_duration(reminder_minutes)
        try:
            for offset in range(
                0,
                len(late_user_ids),
                LATENESS_REMINDER_MAX_MENTIONS,
            ):
                user_ids = late_user_ids[
                    offset:offset + LATENESS_REMINDER_MAX_MENTIONS
                ]
                mentions = " ".join(f"<@{user_id}>" for user_id in user_ids)
                await channel.send(
                    content=f"{mentions} {duration}遅刻",
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=[discord.Object(id=user_id) for user_id in user_ids],
                        roles=False,
                        replied_user=False,
                    ),
                )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception(
                "failed to send %s-minute lateness reminder for poll %s",
                reminder_minutes,
                event.poll_message_id,
            )
            return False

        self._lateness_registry.record_processed_reminders(
            event.poll_message_id,
            pending_thresholds,
            processed_at=now,
            notified_threshold=reminder_minutes,
        )
        logger.info(
            "sent %s-minute lateness reminder for poll %s to %s users",
            reminder_minutes,
            event.poll_message_id,
            len(late_user_ids),
        )
        return True

    async def _fetch_lateness_poll(
        self,
        event: ScheduleLatenessEvent,
    ) -> discord.Message | None:
        get_channel = getattr(self.bot, "get_channel", None)
        channel = get_channel(event.channel_id) if get_channel is not None else None
        if channel is None:
            fetch_channel = getattr(self.bot, "fetch_channel", None)
            if fetch_channel is None:
                return None
            try:
                channel = await fetch_channel(event.channel_id)
            except discord.NotFound:
                self._cancel_lateness_event(event.poll_message_id)
                return None
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "failed to fetch channel for lateness event %s",
                    event.poll_message_id,
                )
                return None
        if not hasattr(channel, "fetch_message"):
            self._cancel_lateness_event(event.poll_message_id)
            return None
        try:
            poll_message = await channel.fetch_message(event.poll_message_id)
        except discord.NotFound:
            self._cancel_lateness_event(event.poll_message_id)
            return None
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "failed to fetch poll for lateness event %s",
                event.poll_message_id,
            )
            return None
        if not poll_message.embeds:
            self._cancel_lateness_event(event.poll_message_id)
            return None
        embed = poll_message.embeds[0]
        tracked_start_time = (
            schedule_decided_start_time(embed) or announced_start_time(embed)
        )
        if tracked_start_time != event.start_time:
            self._cancel_lateness_event(event.poll_message_id)
            return None
        return poll_message

    async def _snapshot_lateness_event(
        self,
        event: ScheduleLatenessEvent,
        poll_message: discord.Message,
    ) -> None:
        options = schedule_options_from_embed(poll_message.embeds[0])
        if options is None:
            return
        voters_by_option = await self._collect_schedule_voters(
            poll_message,
            options,
        )
        eligible_user_ids = eligible_voters_for_start(
            voters_by_option,
            event.start_time,
        )
        now = self._utc_now()
        if not self._lateness_registry.snapshot_participants(
            event.poll_message_id,
            eligible_user_ids,
            snapshotted_at=now,
        ):
            return
        refreshed_event = self._lateness_registry.get_event(event.poll_message_id)
        if refreshed_event is None:
            return
        self._capture_current_voice_presence(
            refreshed_event,
            captured_at=now,
            unknown_join_at=refreshed_event.start_at,
        )
        logger.info(
            "snapshotted %s lateness participants for schedule poll %s",
            len(eligible_user_ids),
            event.poll_message_id,
        )

    def _get_lateness_guild(self, guild_id: int):
        get_guild = getattr(self.bot, "get_guild", None)
        return get_guild(guild_id) if get_guild is not None else None

    def _capture_current_voice_presence(
        self,
        event: ScheduleLatenessEvent,
        *,
        captured_at: datetime,
        unknown_join_at: datetime,
    ) -> None:
        guild = self._get_lateness_guild(event.guild_id)
        if guild is None:
            return
        for participant in self._lateness_registry.participants(
            event.poll_message_id
        ):
            member = guild.get_member(participant.user_id)
            voice_channel = getattr(
                getattr(member, "voice", None),
                "channel",
                None,
            )
            if voice_channel is None:
                continue
            joined_at = (
                captured_at
                if captured_at <= event.start_at
                else unknown_join_at
            )
            self._lateness_registry.set_presence(
                event.poll_message_id,
                participant.user_id,
                voice_channel_id=voice_channel.id,
                joined_at=joined_at,
            )

    def _capture_restored_voice_presence(
        self,
        event: ScheduleLatenessEvent,
        user_ids: set[int],
        *,
        captured_at: datetime,
    ) -> None:
        if not user_ids:
            return
        guild = self._get_lateness_guild(event.guild_id)
        if guild is None:
            return
        for user_id in user_ids:
            member = guild.get_member(user_id)
            voice_channel = getattr(
                getattr(member, "voice", None),
                "channel",
                None,
            )
            if voice_channel is None:
                continue
            if event.activated_at is not None:
                if voice_channel.id == event.voice_channel_id:
                    self._lateness_registry.record_arrival(
                        event.poll_message_id,
                        user_id,
                        voice_channel_id=voice_channel.id,
                        joined_at=captured_at,
                    )
            else:
                self._lateness_registry.set_presence(
                    event.poll_message_id,
                    user_id,
                    voice_channel_id=voice_channel.id,
                    joined_at=captured_at,
                )

    def _queue_lateness_reaction_sync(
        self,
        payload,
        *,
        check_emoji: bool = True,
    ) -> None:
        if (
            payload.guild_id is None
            or (
                check_emoji
                and str(getattr(payload, "emoji", ""))
                not in SCHEDULE_REACTION_EMOJIS
            )
        ):
            return
        bot_user = self.bot.user
        if bot_user is not None and getattr(payload, "user_id", None) == bot_user.id:
            return
        try:
            event = self._lateness_registry.get_event(payload.message_id)
            disabled = self._lateness_registry.is_disabled(payload.message_id)
        except (sqlite3.Error, OSError):
            logger.exception("failed to inspect lateness reaction event")
            return
        if event is None or event.snapshotted_at is None or disabled:
            return
        previous_task = self._lateness_reaction_tasks.get(payload.message_id)
        if previous_task is not None:
            previous_task.cancel()
        task = asyncio.create_task(
            self._run_lateness_reaction_sync(payload.message_id),
            name=f"schedule-lateness-reaction-{payload.message_id}",
        )
        self._lateness_reaction_tasks[payload.message_id] = task
        task.add_done_callback(
            lambda completed, message_id=payload.message_id: (
                self._lateness_reaction_tasks.pop(message_id, None)
                if self._lateness_reaction_tasks.get(message_id) is completed
                else None
            )
        )

    async def _run_lateness_reaction_sync(self, poll_message_id: int) -> None:
        try:
            await self._lateness_reaction_sleep(
                LATENESS_REACTION_GRACE_SECONDS
            )
            lock = self._auto_start_locks.setdefault(
                poll_message_id,
                asyncio.Lock(),
            )
            async with lock:
                event = self._lateness_registry.get_event(poll_message_id)
                if (
                    event is None
                    or event.snapshotted_at is None
                    or self._lateness_registry.is_disabled(poll_message_id)
                ):
                    return
                poll_message = await self._fetch_lateness_poll(event)
                if poll_message is None:
                    return
                options = schedule_options_from_embed(poll_message.embeds[0])
                if options is None:
                    return
                voters_by_option = await self._collect_schedule_voters(
                    poll_message,
                    options,
                )
                current_eligible_user_ids = eligible_voters_for_start(
                    voters_by_option,
                    event.start_time,
                )
                now = self._utc_now()
                cancelled, restored = self._lateness_registry.sync_cancellations(
                    poll_message_id,
                    current_eligible_user_ids,
                    changed_at=now,
                )
                for user_id in cancelled:
                    self._lateness_registry.clear_presence(
                        poll_message_id,
                        user_id,
                    )
                refreshed_event = self._lateness_registry.get_event(
                    poll_message_id
                )
                if refreshed_event is None:
                    return
                self._capture_restored_voice_presence(
                    refreshed_event,
                    restored,
                    captured_at=now,
                )
                if (
                    refreshed_event.activated_at is not None
                    and self._complete_lateness_if_minimum_present(
                        refreshed_event,
                        completed_at=now,
                    )
                ):
                    return
                if now >= refreshed_event.start_at:
                    await self._evaluate_lateness_event(poll_message_id)
        except asyncio.CancelledError:
            raise
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to sync lateness reactions for schedule poll %s",
                poll_message_id,
            )
        except Exception:
            logger.exception(
                "unexpected failure while syncing lateness reactions for poll %s",
                poll_message_id,
            )

    async def _evaluate_lateness_event(self, poll_message_id: int) -> None:
        event = self._lateness_registry.get_event(poll_message_id)
        now = self._utc_now()
        if (
            event is None
            or event.snapshotted_at is None
            or event.activated_at is not None
            or now < event.start_at
            or now > event.tracking_until
        ):
            return
        guild = self._get_lateness_guild(event.guild_id)
        if guild is None:
            return
        members_by_channel: dict[int, list[int]] = {}
        for participant in self._lateness_registry.participants(
            event.poll_message_id
        ):
            member = guild.get_member(participant.user_id)
            voice_channel = getattr(
                getattr(member, "voice", None),
                "channel",
                None,
            )
            if voice_channel is None:
                continue
            members_by_channel.setdefault(voice_channel.id, []).append(
                participant.user_id
            )
        candidates = [
            (channel_id, user_ids)
            for channel_id, user_ids in members_by_channel.items()
            if len(user_ids) >= lateness_voice_quorum(event.minimum)
        ]
        if not candidates:
            return
        voice_channel_id, present_user_ids = min(
            candidates,
            key=lambda item: (-len(item[1]), item[0]),
        )
        observed_presence = self._lateness_registry.presence(
            event.poll_message_id
        )
        arrivals = {
            user_id: (
                observed_presence[user_id][1]
                if user_id in observed_presence
                and observed_presence[user_id][0] == voice_channel_id
                else event.start_at
            )
            for user_id in present_user_ids
        }
        if self._lateness_registry.activate(
            event.poll_message_id,
            voice_channel_id=voice_channel_id,
            activated_at=now,
            arrivals=arrivals,
        ):
            logger.info(
                "activated lateness tracking for schedule poll %s in VC %s with %s users",
                event.poll_message_id,
                voice_channel_id,
                len(present_user_ids),
            )
            activated_event = self._lateness_registry.get_event(
                event.poll_message_id
            )
            if activated_event is not None:
                if not self._complete_lateness_if_minimum_present(
                    activated_event,
                    completed_at=now,
                ):
                    await self._process_due_lateness_reminders(activated_event)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        now = self._utc_now()
        try:
            events = self._lateness_registry.tracking_events(
                now=now,
                guild_id=member.guild.id,
            )
        except (sqlite3.Error, OSError):
            logger.exception("failed to load lateness events for voice update")
            return
        for event in events:
            if now < event.snapshot_at:
                continue
            lock = self._auto_start_locks.setdefault(
                event.poll_message_id,
                asyncio.Lock(),
            )
            async with lock:
                event = self._lateness_registry.get_event(event.poll_message_id)
                if event is None:
                    continue
                if event.snapshotted_at is None:
                    poll_message = await self._fetch_lateness_poll(event)
                    if poll_message is None:
                        continue
                    await self._snapshot_lateness_event(event, poll_message)
                    event = self._lateness_registry.get_event(
                        event.poll_message_id
                    )
                    if event is None or event.snapshotted_at is None:
                        continue
                active_user_ids = {
                    participant.user_id
                    for participant in self._lateness_registry.participants(
                        event.poll_message_id
                    )
                }
                if member.id in active_user_ids:
                    if event.activated_at is not None:
                        if (
                            after.channel is not None
                            and after.channel.id == event.voice_channel_id
                        ):
                            late_seconds = self._lateness_registry.record_arrival(
                                event.poll_message_id,
                                member.id,
                                voice_channel_id=after.channel.id,
                                joined_at=now,
                            )
                            if late_seconds is not None and late_seconds > 0:
                                logger.info(
                                    "recorded %ss lateness for user %s in schedule poll %s",
                                    late_seconds,
                                    member.id,
                                    event.poll_message_id,
                                )
                            self._complete_lateness_if_minimum_present(
                                event,
                                completed_at=now,
                            )
                    else:
                        if before.channel is not None:
                            self._lateness_registry.clear_presence(
                                event.poll_message_id,
                                member.id,
                                voice_channel_id=before.channel.id,
                            )
                        if after.channel is not None:
                            self._lateness_registry.set_presence(
                                event.poll_message_id,
                                member.id,
                                voice_channel_id=after.channel.id,
                                joined_at=now,
                            )
                if event.activated_at is None and now >= event.start_at:
                    await self._evaluate_lateness_event(event.poll_message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        self._queue_auto_start_check(payload)
        self._queue_lateness_reaction_sync(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        self._queue_auto_start_check(
            payload,
            cancelled_user_id=payload.user_id,
        )
        self._queue_lateness_reaction_sync(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(
        self,
        payload: discord.RawReactionClearEmojiEvent,
    ):
        if str(payload.emoji) not in SCHEDULE_REACTION_EMOJIS:
            return
        # NG自体は集計しないが、消された投票UIを復元するため再評価する。
        self._queue_auto_start_check(
            payload,
            check_emoji=False,
            cancelled_user_id=None,
        )
        self._queue_lateness_reaction_sync(payload, check_emoji=False)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        self._queue_auto_start_check(
            payload,
            check_emoji=False,
            cancelled_user_id=None,
        )
        self._queue_lateness_reaction_sync(payload, check_emoji=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self._handle_schedule_message_delete(
            message_id=payload.message_id,
            channel_id=payload.channel_id,
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self,
        payload: discord.RawBulkMessageDeleteEvent,
    ):
        for message_id in payload.message_ids:
            await self._handle_schedule_message_delete(
                message_id=message_id,
                channel_id=payload.channel_id,
            )

    async def _handle_schedule_message_delete(
        self,
        *,
        message_id: int,
        channel_id: int,
    ) -> None:
        # Discord上で直接削除された場合も、遅刻統計を孤立させない。
        self._delete_lateness_event(message_id)
        poll = self._notification_poll_refs.pop(message_id, None)
        if poll is not None:
            self._poll_notification_ids.pop(poll.message_id, None)
            self._queue_auto_start_check_by_id(
                guild_id=poll.guild_id,
                channel_id=poll.channel_id,
                message_id=poll.message_id,
            )
            return

        if message_id not in self._registered_schedule_ids:
            return
        self._invalidate_auto_start_check(message_id)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            notification_id = self._poll_notification_ids.get(message_id)
            if notification_id is not None:
                await self._cancel_notification_for_deleted_poll(
                    channel_id=channel_id,
                    notification_id=notification_id,
                )
            self._unregister_schedule_poll(message_id)

    async def _cancel_notification_for_deleted_poll(
        self,
        *,
        channel_id: int,
        notification_id: int,
    ) -> None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.exception(
                    "failed to fetch channel for deleted schedule poll notification %s",
                    notification_id,
                )
                return
        if not hasattr(channel, "fetch_message"):
            return
        try:
            notification = await channel.fetch_message(notification_id)
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "failed to fetch start notification %s after poll deletion",
                notification_id,
            )
            return

        bot_user = self.bot.user
        if bot_user is None or notification.author.id != bot_user.id:
            return
        first_line = (notification.content or "開始時間通知").splitlines()[0]
        if not first_line.startswith("~~"):
            first_line = f"~~{first_line}~~"
        try:
            await notification.edit(
                content=(
                    f"{first_line}\n"
                    "↩️ 元の開始時間投票が削除されたため、この通知は取り消されました。"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception(
                "failed to cancel start notification %s after poll deletion",
                notification_id,
            )

    def _queue_auto_start_check(
        self,
        payload,
        *,
        check_emoji: bool = True,
        cancelled_user_id: int | None | object = _CANCELLED_USER_UNCHANGED,
    ) -> None:
        bot_user = self.bot.user
        if (
            bot_user is None
            or payload.guild_id is None
            or getattr(payload, "user_id", None) == bot_user.id
            or (
                check_emoji
                and str(getattr(payload, "emoji", "")) not in OPTION_EMOJIS
            )
        ):
            return

        self._queue_auto_start_check_by_id(
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            cancelled_user_id=cancelled_user_id,
        )

    def _queue_auto_start_check_by_id(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        retry_count: int = 0,
        cancelled_user_id: int | None | object = _CANCELLED_USER_UNCHANGED,
    ) -> None:
        if message_id not in self._registered_schedule_ids:
            return
        if cancelled_user_id is not _CANCELLED_USER_UNCHANGED:
            if cancelled_user_id is None:
                self._last_cancelled_user_ids.pop(message_id, None)
            else:
                self._last_cancelled_user_ids[message_id] = cancelled_user_id

        revision = self._auto_start_revisions.get(message_id, 0) + 1
        self._auto_start_revisions[message_id] = revision

        previous_task = self._auto_start_tasks.get(message_id)
        if previous_task is not None:
            previous_task.cancel()

        task = asyncio.create_task(
            self._run_debounced_auto_start_check(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                revision=revision,
                retry_count=retry_count,
            ),
            name=f"schedule-auto-start-{message_id}-{revision}",
        )
        self._auto_start_tasks[message_id] = task
        task.add_done_callback(
            lambda finished, poll_id=message_id: self._discard_auto_start_task(
                poll_id,
                finished,
            )
        )

    def _discard_auto_start_task(
        self,
        message_id: int,
        task: asyncio.Task,
    ) -> None:
        if self._auto_start_tasks.get(message_id) is task:
            self._auto_start_tasks.pop(message_id, None)
            self._auto_start_revisions.pop(message_id, None)
            self._last_cancelled_user_ids.pop(message_id, None)

    def _invalidate_auto_start_check(self, message_id: int) -> None:
        task = self._auto_start_tasks.get(message_id)
        if task is None:
            return
        self._auto_start_revisions[message_id] = (
            self._auto_start_revisions.get(message_id, 0) + 1
        )
        task.cancel()

    def _resume_auto_start_check(
        self,
        ctx: commands.Context,
        poll_message: discord.Message,
    ) -> None:
        if not poll_message.embeds or not is_auto_start_schedule(
            poll_message.embeds[0]
        ):
            return
        guild_id = getattr(ctx.guild, "id", None)
        if guild_id is None:
            return
        self._queue_auto_start_check_by_id(
            guild_id=guild_id,
            channel_id=ctx.channel.id,
            message_id=poll_message.id,
        )

    async def _run_debounced_auto_start_check(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        revision: int,
        retry_count: int,
    ) -> None:
        try:
            await self._sleep(self._auto_start_grace_seconds)
            if self._auto_start_revisions.get(message_id) != revision:
                return

            lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
            async with lock:
                if self._auto_start_revisions.get(message_id) != revision:
                    return
                await self._reconcile_start_time(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    revision=revision,
                    cancelled_user_id=self._last_cancelled_user_ids.get(
                        message_id
                    ),
                )
        except asyncio.CancelledError:
            raise
        except (discord.Forbidden, discord.NotFound):
            logger.exception(
                "failed to evaluate auto start for schedule poll %s",
                message_id,
            )
        except discord.HTTPException:
            logger.exception(
                "temporary failure while evaluating schedule poll %s",
                message_id,
            )
            if (
                retry_count < AUTO_START_MAX_RETRIES
                and message_id in self._registered_schedule_ids
            ):
                current_task = asyncio.current_task()
                if self._auto_start_tasks.get(message_id) is current_task:
                    self._auto_start_tasks.pop(message_id, None)
                    self._auto_start_revisions.pop(message_id, None)
                    self._queue_auto_start_check_by_id(
                        guild_id=guild_id,
                        channel_id=channel_id,
                        message_id=message_id,
                        retry_count=retry_count + 1,
                    )
        except Exception:
            logger.exception(
                "unexpected failure while evaluating schedule poll %s",
                message_id,
            )

    async def _reconcile_start_time(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        revision: int,
        cancelled_user_id: int | None,
    ) -> None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                self._unregister_schedule_poll(message_id)
                return
        if not hasattr(channel, "fetch_message") or not hasattr(channel, "send"):
            self._unregister_schedule_poll(message_id)
            return
        try:
            poll_message = await channel.fetch_message(message_id)
        except discord.NotFound:
            self._unregister_schedule_poll(message_id)
            return
        bot_user = self.bot.user
        if bot_user is None or poll_message.author.id != bot_user.id:
            self._unregister_schedule_poll(message_id)
            return
        if not poll_message.embeds or not is_auto_start_schedule(poll_message.embeds[0]):
            self._unregister_schedule_poll(message_id)
            return

        self._register_schedule_poll(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
        )

        options = schedule_options_from_embed(poll_message.embeds[0])
        minimum = auto_start_minimum(poll_message.embeds[0])
        if (
            options is None
            or normalize_auto_start_options(options) is None
            or minimum is None
        ):
            self._unregister_schedule_poll(message_id)
            return

        await self._ensure_schedule_reactions(poll_message, options)
        voters_by_option = await self._collect_schedule_voters(
            poll_message,
            options,
        )
        if self._auto_start_revisions.get(message_id) != revision:
            return
        try:
            lateness_event = self._lateness_registry.get_event(message_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "failed to inspect lateness event for schedule poll %s",
                message_id,
            )
            lateness_event = None
        if lateness_event is not None and lateness_event.activated_at is not None:
            return

        start_time = choose_start_time(voters_by_option, minimum)
        announcement = start_announcement(poll_message.embeds[0])
        current_start_time = (
            announcement.start_time if announcement is not None else None
        )
        missing_current_notification = False
        if current_start_time == start_time:
            if start_time is None:
                self._cancel_lateness_event(message_id)
            else:
                await self._store_lateness_event(
                    poll_message,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    start_time=start_time,
                    current_eligible_user_ids=eligible_voters_for_start(
                        voters_by_option,
                        start_time,
                    ),
                )
            if announcement is None or announcement.message_id is None:
                return
            existing_notification = await self._fetch_start_notification(
                channel,
                announcement,
            )
            if existing_notification is not None:
                self._remember_start_notification(
                    poll_message.id,
                    existing_notification.id,
                )
                return
            missing_current_notification = True

        role = self._schedule_role(poll_message)
        if (
            start_time is not None
            and role is None
            and not has_announced_start_before(poll_message.embeds[0])
        ):
            logger.warning(
                "schedule poll %s has no target role",
                poll_message.id,
            )
            return

        transition_task = asyncio.create_task(
            self._apply_start_time_transition(
                channel=channel,
                poll_message=poll_message,
                role=role,
                new_start_time=start_time,
                minimum=minimum,
                guild_id=guild_id,
                channel_id=channel_id,
                current_eligible_user_ids=(
                    eligible_voters_for_start(voters_by_option, start_time)
                    if start_time is not None
                    else None
                ),
                missing_current_notification=missing_current_notification,
                cancelled_user_id=cancelled_user_id,
            ),
            name=f"schedule-auto-start-transition-{message_id}-{revision}",
        )
        try:
            await asyncio.shield(transition_task)
        except asyncio.CancelledError:
            # Discordへの送信とfooter更新の途中で取消すと通知だけが残るため、
            # 開始済みの状態遷移は完了させる。新しい票は次の10秒判定で補正する。
            try:
                await transition_task
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.exception(
                    "failed to finish cancelled transition for schedule poll %s",
                    message_id,
                )
            raise

    async def _apply_start_time_transition(
        self,
        *,
        channel,
        poll_message: discord.Message,
        role: discord.Role | None,
        new_start_time: str | None,
        minimum: int,
        guild_id: int | None = None,
        channel_id: int | None = None,
        current_eligible_user_ids: set[int] | None = None,
        missing_current_notification: bool = False,
        cancelled_user_id: int | None = None,
    ) -> None:
        original_embed = poll_message.embeds[0]
        if poll_message.id not in self._registered_schedule_ids:
            return
        announcement = start_announcement(original_embed)
        old_notification = (
            None
            if missing_current_notification
            else await self._fetch_start_notification(channel, announcement)
        )
        role_mention = self._schedule_role_mention(poll_message, role)
        if poll_message.id not in self._registered_schedule_ids:
            return

        if announcement is not None and old_notification is not None:
            replacement = (
                f"\n↪️ 投票内容が変わり、{format_start_label(new_start_time)}へ変更されました。"
                if new_start_time is not None
                else "\n↩️ 投票内容が変わったため、この開始通知は取り消されました。"
            )
            await old_notification.edit(
                content=(
                    f"~~{format_start_label(announcement.start_time)} {role_mention}~~"
                    f"{replacement}"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if poll_message.id not in self._registered_schedule_ids:
                await self._restore_start_notification(
                    old_notification,
                    announcement,
                    role_mention,
                )
                return

        if new_start_time is None:
            cleared_embed = original_embed.copy()
            clear_start_announcement(cleared_embed)
            try:
                await poll_message.edit(embed=cleared_embed)
            except (discord.Forbidden, discord.HTTPException):
                await self._restore_start_notification(
                    old_notification,
                    announcement,
                    role_mention,
                )
                raise

            if poll_message.id not in self._registered_schedule_ids:
                try:
                    await poll_message.edit(embed=original_embed)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to restore expired schedule poll %s",
                        poll_message.id,
                    )
                await self._restore_start_notification(
                    old_notification,
                    announcement,
                    role_mention,
                )
                return

            self._forget_start_notification(poll_message.id)

            if announcement is not None:
                cancelled_user_name = self._schedule_user_display_name(
                    poll_message,
                    cancelled_user_id,
                )
                cancellation_detail = (
                    f"{cancelled_user_name}の参加がキャンセルされました。"
                    if cancelled_user_name is not None
                    else f"参加可能な投票者が{minimum}人未満になりました。"
                )
                cancellation_notice = await self._send_cancellation_notice(
                    channel,
                    content=(
                        f"↩️ {format_start_label(announcement.start_time)}の通知を取り消しました "
                        f"{role_mention}\n"
                        f"{cancellation_detail}"
                    ),
                    poll_message_id=poll_message.id,
                )
                if poll_message.id not in self._registered_schedule_ids:
                    if cancellation_notice is not None:
                        try:
                            await cancellation_notice.delete()
                        except (
                            discord.Forbidden,
                            discord.NotFound,
                            discord.HTTPException,
                        ):
                            logger.exception(
                                "failed to remove cancellation notice for expired poll %s",
                                poll_message.id,
                            )
                    try:
                        await poll_message.edit(embed=original_embed)
                    except (
                        discord.Forbidden,
                        discord.NotFound,
                        discord.HTTPException,
                    ):
                        logger.exception(
                            "failed to restore expired schedule poll %s",
                            poll_message.id,
                        )
                    await self._restore_start_notification(
                        old_notification,
                        announcement,
                        role_mention,
                    )
                    return
            self._cancel_lateness_event(poll_message.id)
            logger.info("cancelled start announcement for schedule poll %s", poll_message.id)
            return

        already_notified = (
            has_announced_start_before(original_embed)
            or poll_message.id in self._start_notified_poll_ids
        )
        if role is None and not already_notified:
            return

        content = f"{format_start_label(new_start_time)} {role_mention}"
        if announcement is not None and not missing_current_notification:
            content += f"\n🔄 {format_start_label(announcement.start_time)}から変更されました。"
        allowed_mentions = (
            discord.AllowedMentions.none()
            if already_notified
            else discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[role] if role is not None else False,
                replied_user=False,
            )
        )

        new_notification = None
        try:
            new_notification = await channel.send(
                content=content,
                allowed_mentions=allowed_mentions,
            )
            if poll_message.id not in self._registered_schedule_ids:
                try:
                    await new_notification.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to remove notification for expired poll %s",
                        poll_message.id,
                    )
                await self._restore_start_notification(
                    old_notification,
                    announcement,
                    role_mention,
                )
                return
            if not already_notified:
                # Discordへのrole pingは送信時点で成立する。footer確定に失敗しても
                # 同一プロセス内のretryでは再pingしない。
                self._start_notified_poll_ids.add(poll_message.id)
            # footer保存中の削除eventも拾えるよう、送信直後から一時追跡する。
            self._remember_start_notification(
                poll_message.id,
                new_notification.id,
            )
            announced_embed = original_embed.copy()
            mark_start_time_announced(
                announced_embed,
                new_start_time,
                new_notification.id,
            )
            await poll_message.edit(embed=announced_embed)
            if poll_message.id not in self._registered_schedule_ids:
                try:
                    await poll_message.edit(embed=original_embed)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to restore expired schedule poll %s",
                        poll_message.id,
                    )
                try:
                    await new_notification.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to remove notification for expired poll %s",
                        poll_message.id,
                    )
                await self._restore_start_notification(
                    old_notification,
                    announcement,
                    role_mention,
                )
                return
            if (
                self._poll_notification_ids.get(poll_message.id)
                != new_notification.id
            ):
                # 保存中に通知が削除された。footerを確定せず次の判定で再生成する。
                restored_embed = original_embed.copy()
                mark_start_notification_history(restored_embed)
                try:
                    await poll_message.edit(embed=restored_embed)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to restore schedule poll %s after notification deletion",
                        poll_message.id,
                    )
                await self._restore_start_notification(
                    old_notification,
                    announcement,
                    role_mention,
                )
                if old_notification is not None:
                    self._remember_start_notification(
                        poll_message.id,
                        old_notification.id,
                    )
                return
            self._remember_start_notification(
                poll_message.id,
                new_notification.id,
            )
        except (discord.Forbidden, discord.HTTPException):
            self._forget_start_notification(poll_message.id)
            if new_notification is not None:
                try:
                    await new_notification.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to remove untracked start notification for poll %s",
                        poll_message.id,
                    )
            await self._restore_start_notification(
                old_notification,
                announcement,
                role_mention,
            )
            if old_notification is not None:
                self._remember_start_notification(
                    poll_message.id,
                    old_notification.id,
                )
            if new_notification is not None and not already_notified:
                history_embed = original_embed.copy()
                mark_start_notification_history(history_embed)
                try:
                    await poll_message.edit(embed=history_embed)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "failed to persist notification history for poll %s",
                        poll_message.id,
                    )
            raise

        if guild_id is not None and channel_id is not None:
            await self._store_lateness_event(
                poll_message,
                guild_id=guild_id,
                channel_id=channel_id,
                start_time=new_start_time,
                current_eligible_user_ids=current_eligible_user_ids,
            )

        logger.info(
            "schedule poll %s reached %s unique voters; announced %s",
            poll_message.id,
            minimum,
            new_start_time,
        )

    async def _send_cancellation_notice(
        self,
        channel,
        *,
        content: str,
        poll_message_id: int,
    ):
        for attempt in range(AUTO_START_MAX_RETRIES + 1):
            if poll_message_id not in self._registered_schedule_ids:
                return None
            try:
                return await channel.send(
                    content=content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                logger.exception(
                    "forbidden while posting cancellation notice for poll %s",
                    poll_message_id,
                )
                return None
            except discord.HTTPException:
                if attempt >= AUTO_START_MAX_RETRIES:
                    logger.exception(
                        "failed to post cancellation notice for schedule poll %s",
                        poll_message_id,
                    )
                    return None
                await self._retry_sleep(AUTO_START_NOTICE_RETRY_DELAY_SECONDS)
        return None

    async def _fetch_start_notification(
        self,
        channel,
        announcement: StartAnnouncement | None,
    ):
        if announcement is None or announcement.message_id is None:
            return None
        try:
            message = await channel.fetch_message(announcement.message_id)
        except discord.NotFound:
            return None
        bot_user = self.bot.user
        if bot_user is None or message.author.id != bot_user.id:
            logger.warning(
                "start notification %s is not owned by this bot",
                announcement.message_id,
            )
            return None
        return message

    @staticmethod
    async def _post_public_cancellation_fallback(
        channel,
        announcement: StartAnnouncement,
        role_mention: str,
        reason: str,
    ) -> bool:
        try:
            await channel.send(
                content=(
                    f"↩️ ~~{format_start_label(announcement.start_time)} {role_mention}~~\n"
                    f"{reason}。以前の開始通知は無効です。"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "failed to post cancellation fallback for notification %s",
                announcement.message_id,
            )
            return False
        return True

    @staticmethod
    async def _restore_start_notification(
        notification,
        announcement: StartAnnouncement | None,
        role_mention: str,
    ) -> None:
        if notification is None or announcement is None:
            return
        try:
            await notification.edit(
                content=f"{format_start_label(announcement.start_time)} {role_mention}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception(
                "failed to restore start notification %s",
                announcement.message_id,
            )

    @staticmethod
    def _schedule_role_mention(
        poll_message: discord.Message,
        role: discord.Role | None,
    ) -> str:
        if role is not None:
            return role.mention
        match = ROLE_MENTION_PATTERN.search(poll_message.content)
        return match.group(0) if match is not None else "対象ロール"

    def _schedule_user_display_name(
        self,
        poll_message: discord.Message,
        user_id: int | None,
    ) -> str | None:
        if user_id is None:
            return None
        user = None
        if poll_message.guild is not None:
            get_member = getattr(poll_message.guild, "get_member", None)
            if get_member is not None:
                user = get_member(user_id)
        if user is None and hasattr(self.bot, "get_user"):
            user = self.bot.get_user(user_id)
        if user is None or getattr(user, "bot", False):
            return None
        display_name = getattr(user, "display_name", None) or getattr(
            user,
            "name",
            None,
        )
        if not display_name:
            return None
        display_name = " ".join(str(display_name).splitlines()).strip()
        if not display_name:
            return None
        return discord.utils.escape_markdown(
            discord.utils.escape_mentions(display_name)
        )

    async def _collect_schedule_voters(
        self,
        poll_message: discord.Message,
        options: list[str],
    ) -> dict[str, set[int]]:
        all_voters = await self._collect_all_schedule_voters(
            poll_message,
            options,
        )
        return {
            option: voters
            for option, voters in all_voters.items()
            if normalize_schedule_time(option) is not None
        }

    @staticmethod
    async def _collect_all_schedule_voters(
        poll_message: discord.Message,
        options: list[str],
    ) -> dict[str, set[int]]:
        reactions_by_emoji = {
            str(reaction.emoji): reaction
            for reaction in getattr(poll_message, "reactions", ())
        }
        voters_by_option: dict[str, set[int]] = {}
        for option, emoji in zip(options, schedule_option_emojis(options)):
            reaction = reactions_by_emoji.get(emoji)
            voters: set[int] = set()
            if reaction is not None:
                async for user in reaction.users(limit=None):
                    if not user.bot:
                        voters.add(user.id)
            voters_by_option[option] = voters
        return voters_by_option

    @staticmethod
    async def _ensure_schedule_reactions(
        poll_message: discord.Message,
        options: list[str],
    ) -> None:
        reactions_by_emoji = {
            str(reaction.emoji): reaction for reaction in poll_message.reactions
        }
        for emoji in schedule_option_emojis(options):
            reaction = reactions_by_emoji.get(emoji)
            if reaction is None or not reaction.me:
                await poll_message.add_reaction(emoji)

    @staticmethod
    def _schedule_role(poll_message: discord.Message) -> discord.Role | None:
        if poll_message.role_mentions:
            return poll_message.role_mentions[0]
        match = ROLE_MENTION_PATTERN.search(poll_message.content)
        if match is None or poll_message.guild is None:
            return None
        return poll_message.guild.get_role(int(match.group(1)))

    @schedule_add.error
    async def schedule_add_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule add @ロール [候補...]`\n"
                f"候補省略時: `{DEFAULT_SCHEDULE_OPTIONS}`\n"
                "最低人数を変える場合は末尾に `[人数]` を指定してください",
            )
            return
        if isinstance(error, commands.BadArgument):
            await self._send_notice(ctx, "❌ 対象ロールをメンションで指定してください")
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_status.error
    async def schedule_status_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule status [投稿IDまたはリンク]`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_clone.error
    async def schedule_clone_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule clone [投稿IDまたはリンク]`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_date.error
    async def schedule_date_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule date [投稿IDまたはリンク] <日付>`\n"
                "日付: `YYYY-MM-DD` / `YYYYMMDD` / `MM-DD` / `MMDD` / `DD`\n"
                "投稿日に戻す場合: `/schedule date [投稿IDまたはリンク] clear`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_update.error
    async def schedule_update_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule update [投稿IDまたはリンク] 21:00 22:00 24:00 NG`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_minimum.error
    async def schedule_minimum_error(self, ctx, error):
        if isinstance(
            error,
            (commands.MissingRequiredArgument, commands.BadArgument),
        ):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule minimum [投稿IDまたはリンク] <1〜999>`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_deadline.error
    async def schedule_deadline_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule deadline [投稿IDまたはリンク] YYYY-MM-DD HH:MM`\n"
                "解除する場合: `/schedule deadline [投稿IDまたはリンク] clear`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_decide.error
    async def schedule_decide_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule decide [投稿IDまたはリンク] <候補の時刻>`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_lateoff.error
    async def schedule_lateoff_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule lateoff [投稿IDまたはリンク]`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_late.error
    async def schedule_late_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_close.error
    async def schedule_close_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule close [投稿IDまたはリンク]`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_delete.error
    async def schedule_delete_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    async def _validate_schedule_context(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return False
        return True

    @staticmethod
    async def _add_schedule_reactions(
        message: discord.Message,
        options: list[str],
    ):
        for emoji in schedule_option_emojis(options):
            await message.add_reaction(emoji)

    @classmethod
    async def _reset_schedule_reactions(
        cls,
        message: discord.Message,
        options: list[str],
    ):
        schedule_emojis = [
            reaction.emoji
            for reaction in list(message.reactions)
            if str(reaction.emoji) in SCHEDULE_REACTION_EMOJIS
        ]
        for emoji in schedule_emojis:
            await message.clear_reaction(emoji)
        await cls._add_schedule_reactions(message, options)

    @staticmethod
    async def _send_notice(ctx: commands.Context, content: str):
        kwargs = {"ephemeral": True} if ctx.interaction is not None else {}
        await ctx.send(content, **kwargs)

    @staticmethod
    async def _send_embed_notice(ctx: commands.Context, embed: discord.Embed):
        kwargs = {"ephemeral": True} if ctx.interaction is not None else {}
        await ctx.send(embed=embed, **kwargs)


async def setup(bot):
    await bot.add_cog(PollCog(bot))
