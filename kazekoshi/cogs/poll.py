import asyncio
import re
import shlex
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from typing import Optional
from weakref import WeakValueDictionary

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)
EMOJI_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
EMOJI_LETTERS = [chr(0x1F1E6 + index) for index in range(26)]
MAX_MESSAGE_REACTIONS = 20
OPTION_EMOJIS = EMOJI_NUMBERS + EMOJI_LETTERS[
    :MAX_MESSAGE_REACTIONS - len(EMOJI_NUMBERS)
]
EMOJI_NG = "🆖"
SCHEDULE_TITLE_PREFIX = "📅 "
SCHEDULE_TITLE_MINIMUM_PATTERN = re.compile(r"\s+\[[1-9]\d{0,2}人\]$")
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
                channel_id INTEGER NOT NULL
            )
            """
        )
        return connection

    def register(self, *, guild_id: int, channel_id: int, message_id: int) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO schedule_polls
                        (message_id, guild_id, channel_id)
                    VALUES (?, ?, ?)
                    """,
                    (message_id, guild_id, channel_id),
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
                "SELECT guild_id, channel_id, message_id FROM schedule_polls"
            ).fetchall()
        return [RegisteredSchedulePoll(*row) for row in rows]

    def prune_before(self, cutoff_message_id: int) -> list[RegisteredSchedulePoll]:
        """指定したDiscord Snowflakeより古い登録を削除して返す。"""
        with closing(self._connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    SELECT guild_id, channel_id, message_id
                    FROM schedule_polls
                    WHERE message_id < ?
                    """,
                    (cutoff_message_id,),
                ).fetchall()
                connection.execute(
                    "DELETE FROM schedule_polls WHERE message_id < ?",
                    (cutoff_message_id,),
                )
        return [RegisteredSchedulePoll(*row) for row in rows]


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


class PollCog(commands.Cog):
    def __init__(
        self,
        bot,
        *,
        auto_start_grace_seconds: float = AUTO_START_GRACE_SECONDS,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        registry: SchedulePollRegistry | None = None,
    ):
        self.bot = bot
        self._auto_start_grace_seconds = auto_start_grace_seconds
        self._sleep = sleeper
        self._retry_sleep = retry_sleeper
        self._schedule_registry = registry or SchedulePollRegistry()
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
        self._registry_recovery_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._prune_expired_schedule_polls()
        try:
            polls = self._schedule_registry.all()
        except (sqlite3.Error, OSError):
            logger.exception("failed to load the schedule poll registry")
            return
        loaded_ids = {poll.message_id for poll in polls}
        self._registered_schedule_ids.update(loaded_ids)
        self._persisted_schedule_ids.update(loaded_ids)
        self._registered_schedule_polls.update(
            {poll.message_id: poll for poll in polls}
        )
        if not polls or not hasattr(self.bot, "wait_until_ready"):
            return
        self._registry_recovery_task = asyncio.create_task(
            self._recover_registered_schedule_polls(polls),
            name="schedule-poll-recovery",
        )

    async def cog_unload(self) -> None:
        if self._registry_recovery_task is not None:
            self._registry_recovery_task.cancel()
        tasks = list(self._auto_start_tasks.values())
        self._auto_start_tasks.clear()
        self._auto_start_revisions.clear()
        self._last_cancelled_user_ids.clear()
        self._start_notified_poll_ids.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
            f"最低人数変更: `{prefix}schedule minimum <投稿IDまたはリンク> 3`\n"
            f"更新: `{prefix}schedule update <投稿IDまたはリンク> 21:00 22:00 24:00 NG`\n"
            f"終了: `{prefix}schedule close <投稿IDまたはリンク>`"
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
        auto_start_enabled = normalize_auto_start_options(option_list) is not None

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
            return

        if not role.mentionable:
            author_permissions = ctx.channel.permissions_for(ctx.author)
            if not author_permissions.mention_everyone:
                await self._send_notice(
                    ctx,
                    "❌ メンション不可のロールを指定する権限がありません",
                )
                return
            if ctx.interaction is not None or auto_start_enabled:
                if not bot_permissions.mention_everyone:
                    await self._send_notice(
                        ctx,
                        "❌ Botに「@everyone、@here、すべてのロールにメンション」の権限が必要です",
                    )
                    return

        embed = build_schedule_embed(
            role,
            option_list,
            ctx.author,
            auto_start=auto_start_enabled,
            minimum=minimum,
        )
        # Prefixコマンドでは元の投稿が既にロールへ通知するため、二重通知を避ける。
        allowed_roles = [role] if ctx.interaction is not None else False
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
            return

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

    @schedule.command(
        name="minimum",
        description="開始時間投票の自動判定人数を変更します",
    )
    @app_commands.describe(
        message="変更する開始時間投票の投稿IDまたはリンク",
        minimum="自動開始と判定する最低人数（1〜999人）",
    )
    @commands.guild_only()
    async def schedule_minimum(
        self,
        ctx: commands.Context,
        message: str,
        minimum: int,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        if not AUTO_START_MINIMUM_MIN <= minimum <= AUTO_START_MINIMUM_MAX:
            await self._send_notice(
                ctx,
                f"❌ 最低人数は{AUTO_START_MINIMUM_MIN}〜{AUTO_START_MINIMUM_MAX}人で指定してください",
            )
            return
        try:
            message_id, link_channel_id = parse_message_id(message)
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return
        if link_channel_id is not None and link_channel_id != ctx.channel.id:
            await self._send_notice(ctx, "❌ 同じチャンネルの開始時間投票を指定してください")
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

    @schedule.command(name="update", description="既存の開始時間投票を更新します")
    @app_commands.describe(
        message="更新する開始時間投票の投稿IDまたはリンク",
        options="新しい候補（例: 21:00 22:00 24:00 NG）。更新時に投票はリセットされます",
    )
    @commands.guild_only()
    async def schedule_update(
        self,
        ctx: commands.Context,
        message: str,
        *,
        options: str,
    ):
        if not await self._validate_schedule_context(ctx):
            return

        try:
            option_list = parse_schedule_options(options)
            message_id, link_channel_id = parse_message_id(message)
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return

        if link_channel_id is not None and link_channel_id != ctx.channel.id:
            await self._send_notice(ctx, "❌ 同じチャンネルの日程投票を指定してください")
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

        self._invalidate_auto_start_check(message_id)
        original_embed = poll_message.embeds[0]
        active_announcement = start_announcement(original_embed)
        auto_start_enabled = normalize_auto_start_options(option_list) is not None
        minimum = auto_start_minimum(original_embed) or AUTO_START_THRESHOLD
        updated_embed = original_embed.copy()
        updated_embed.description = format_schedule_options(option_list)
        set_auto_start_marker(
            updated_embed,
            enabled=auto_start_enabled,
            minimum=minimum,
        )
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

    @schedule.command(name="close", description="開始時間投票の自動判定を終了します")
    @app_commands.describe(
        message="終了する開始時間投票の投稿IDまたはリンク",
    )
    @commands.guild_only()
    async def schedule_close(
        self,
        ctx: commands.Context,
        message: str,
    ):
        if not await self._validate_schedule_context(ctx):
            return
        try:
            message_id, link_channel_id = parse_message_id(message)
        except ScheduleInputError as error:
            await self._send_notice(ctx, f"❌ {error}")
            return
        if link_channel_id is not None and link_channel_id != ctx.channel.id:
            await self._send_notice(ctx, "❌ 同じチャンネルの開始時間投票を指定してください")
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

        await ctx.defer(ephemeral=ctx.interaction is not None)
        lock = self._auto_start_locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            poll_message = await self._fetch_editable_schedule_poll(ctx, message_id)
            if poll_message is None:
                return

            self._invalidate_auto_start_check(message_id)
            original_embed = poll_message.embeds[0]
            active_announcement = start_announcement(original_embed)
            closed_embed = original_embed.copy()
            mark_schedule_closed(closed_embed)
            if closed_embed.title and not closed_embed.title.endswith("（終了）"):
                closed_embed.title += "（終了）"
            try:
                await poll_message.edit(embed=closed_embed)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("failed to close schedule poll %s", message_id)
                self._resume_auto_start_check(ctx, poll_message)
                await self._send_notice(ctx, "❌ 開始時間投票を終了できませんでした")
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
                                f"~~{format_start_label(active_announcement.start_time)} "
                                f"{role_mention}~~\n"
                                "↩️ 投票が終了したため、この開始通知は取り消されました。"
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "failed to cancel start notification while closing poll %s",
                        message_id,
                    )
                    if not await self._post_public_cancellation_fallback(
                        ctx.channel,
                        active_announcement,
                        role_mention,
                        "投票が終了しました",
                    ):
                        cancellation_warning = (
                            "\n⚠️ 以前の開始通知を取消表示に更新できませんでした"
                        )

            self._unregister_schedule_poll(message_id)
            await self._send_notice(
                ctx,
                "✅ 開始時間投票を終了しました"
                f"{cancellation_warning}\n{poll_message.jump_url}",
            )
            logger.info("%s closed schedule poll %s", ctx.author, message_id)

    async def _fetch_editable_schedule_poll(
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
        poll = RegisteredSchedulePoll(guild_id, channel_id, message_id)
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

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        self._queue_auto_start_check(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        self._queue_auto_start_check(
            payload,
            cancelled_user_id=payload.user_id,
        )

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

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        self._queue_auto_start_check(
            payload,
            check_emoji=False,
            cancelled_user_id=None,
        )

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

        start_time = choose_start_time(voters_by_option, minimum)
        announcement = start_announcement(poll_message.embeds[0])
        current_start_time = (
            announcement.start_time if announcement is not None else None
        )
        missing_current_notification = False
        if current_start_time == start_time:
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
        reactions_by_emoji = {
            str(reaction.emoji): reaction for reaction in poll_message.reactions
        }
        voters_by_option: dict[str, set[int]] = {}
        for option, emoji in zip(options, schedule_option_emojis(options)):
            if normalize_schedule_time(option) is None:
                continue
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

    @schedule_update.error
    async def schedule_update_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule update <投稿IDまたはリンク> 21:00 22:00 24:00 NG`",
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
                "❌ 使い方: `/schedule minimum <投稿IDまたはリンク> <1〜999>`",
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule_close.error
    async def schedule_close_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await self._send_notice(
                ctx,
                "❌ 使い方: `/schedule close <投稿IDまたはリンク>`",
            )
            return
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


async def setup(bot):
    await bot.add_cog(PollCog(bot))
