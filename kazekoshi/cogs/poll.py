import asyncio
import re
import shlex
from logging import getLogger
from typing import Optional
from weakref import WeakValueDictionary

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)
EMOJI_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
SCHEDULE_CHANNEL_NAME = "valorant"
SCHEDULE_TITLE_PREFIX = "📅 "
SCHEDULE_FOOTER_PATTERN = re.compile(r"\|\s*作成者ID:\s*(\d+)\s*$")
MESSAGE_LINK_PATTERN = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)"
)
ROLE_MENTION_PATTERN = re.compile(r"<@&(\d+)>")
MIN_SCHEDULE_OPTIONS = 2
MAX_SCHEDULE_OPTIONS = len(EMOJI_NUMBERS)
MAX_SCHEDULE_OPTION_LENGTH = 100
DEFAULT_SCHEDULE_OPTION_LIST = ("20", "21", "22", "23", "24", "ng")
DEFAULT_SCHEDULE_OPTIONS = " ".join(DEFAULT_SCHEDULE_OPTION_LIST)
DEFAULT_TIME_OPTIONS = DEFAULT_SCHEDULE_OPTION_LIST[:-1]
DEFAULT_TIME_EMOJIS = tuple(EMOJI_NUMBERS[:len(DEFAULT_TIME_OPTIONS)])
AUTO_START_THRESHOLD = 5
AUTO_START_FOOTER_MARKER = "5人で自動開始判定"
AUTO_START_ANNOUNCED_LABEL = "開始通知済み"
AUTO_START_FOOTER_PATTERN = re.compile(
    rf"\|\s*複数選択可\s*\|\s*{AUTO_START_FOOTER_MARKER}\s*"
    rf"(?:\|\s*{AUTO_START_ANNOUNCED_LABEL}:\s*\d+\s*)?"
    r"\|\s*作成者ID:\s*\d+\s*$"
)
AUTO_START_MARKER_REMOVAL_PATTERN = re.compile(
    rf"\s*\|\s*{AUTO_START_FOOTER_MARKER}"
    rf"(?:\s*\|\s*{AUTO_START_ANNOUNCED_LABEL}:\s*\d+)?"
    r"(?=\s*\|\s*作成者ID:\s*\d+\s*$)"
)
AUTO_START_ANNOUNCED_PATTERN = re.compile(
    rf"\|\s*{AUTO_START_ANNOUNCED_LABEL}:\s*(\d+)\s*"
    r"(?=\|\s*作成者ID:\s*\d+\s*$)"
)
CREATOR_ID_SUFFIX_PATTERN = re.compile(r"\s*\|\s*作成者ID:\s*\d+\s*$")


class ScheduleInputError(ValueError):
    """日程投票の入力値が不正な場合に送出する例外。"""


def parse_schedule_options(value: str) -> list[str]:
    """空白区切りの候補を解析する。引用符で空白を含む候補も指定できる。"""
    try:
        options = [option.strip() for option in shlex.split(value) if option.strip()]
    except ValueError as error:
        raise ScheduleInputError("引用符が閉じられていません") from error

    if len(options) < MIN_SCHEDULE_OPTIONS:
        raise ScheduleInputError("候補を2つ以上入力してください")
    if len(options) > MAX_SCHEDULE_OPTIONS:
        raise ScheduleInputError("候補は最大10個です")
    if any(len(option) > MAX_SCHEDULE_OPTION_LENGTH for option in options):
        raise ScheduleInputError(
            f"候補は1つにつき{MAX_SCHEDULE_OPTION_LENGTH}文字以内にしてください"
        )
    return options


def format_schedule_options(options: list[str]) -> str:
    return "\n".join(
        f"{EMOJI_NUMBERS[index]}：{option}" for index, option in enumerate(options)
    )


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


def is_auto_start_schedule(embed: discord.Embed) -> bool:
    expected_description = format_schedule_options(list(DEFAULT_SCHEDULE_OPTION_LIST))
    footer_text = embed.footer.text or ""
    return (
        embed.description == expected_description
        and AUTO_START_FOOTER_PATTERN.search(footer_text) is not None
    )


def remove_auto_start_marker(embed: discord.Embed) -> None:
    footer_text = embed.footer.text or ""
    updated_footer = AUTO_START_MARKER_REMOVAL_PATTERN.sub("", footer_text)
    if updated_footer != footer_text:
        embed.set_footer(text=updated_footer)


def announced_start_time(embed: discord.Embed) -> str | None:
    footer_text = embed.footer.text or ""
    match = AUTO_START_ANNOUNCED_PATTERN.search(footer_text)
    return match.group(1) if match else None


def mark_start_time_announced(embed: discord.Embed, start_time: str) -> None:
    if announced_start_time(embed) is not None:
        return
    footer_text = embed.footer.text or ""
    suffix_match = CREATOR_ID_SUFFIX_PATTERN.search(footer_text)
    if suffix_match is None:
        return
    updated_footer = (
        footer_text[:suffix_match.start()]
        + f" | {AUTO_START_ANNOUNCED_LABEL}: {start_time}"
        + suffix_match.group(0)
    )
    embed.set_footer(text=updated_footer)


def choose_start_time(voters_by_option: dict[str, set[int]]) -> str | None:
    """20時側から重複を除いて集計し、5人目が加わる開始時刻を返す。"""
    distinct_voters: set[int] = set()
    for option in DEFAULT_TIME_OPTIONS:
        distinct_voters.update(voters_by_option.get(option, set()))
        if len(distinct_voters) >= AUTO_START_THRESHOLD:
            return option
    return None


def is_schedule_channel(ctx: commands.Context) -> bool:
    """設定されたチャンネル、または #valorant とそのスレッドだけを許可する。"""
    channel = ctx.channel
    # TextChannel.parent はカテゴリなので、スレッドの場合だけ親チャンネルを許可する。
    target_channel = channel.parent if isinstance(channel, discord.Thread) else channel
    if target_channel is None:
        return False
    configured_id = getattr(ctx.bot, "valorant_channel_id", 0)

    if configured_id:
        return target_channel.id == configured_id
    return getattr(target_channel, "name", "").casefold() == SCHEDULE_CHANNEL_NAME


def build_schedule_embed(
    role: discord.Role,
    options: list[str],
    author,
    *,
    auto_start: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{SCHEDULE_TITLE_PREFIX}{role.name} 開始時間",
        description=format_schedule_options(options),
        color=discord.Color.blue(),
    )
    footer_parts = [f"作成者: {author.display_name}", "複数選択可"]
    if auto_start:
        footer_parts.append(AUTO_START_FOOTER_MARKER)
    footer_parts.append(f"作成者ID: {author.id}")
    embed.set_footer(text=" | ".join(footer_parts))
    return embed


class PollCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._auto_start_locks: WeakValueDictionary[int, asyncio.Lock] = (
            WeakValueDictionary()
        )

    @commands.command(name="poll")
    async def poll(self, ctx, question: str, *, options: str):
        option_list = [o.strip() for o in options.split(",") if o.strip()]
        if len(option_list) < 2:
            await ctx.send("❌ 選択肢を2つ以上カンマ区切りで入力してください（例: `A,B,C`）")
            return
        if len(option_list) > 10:
            await ctx.send("❌ 選択肢は最大10個です")
            return
        description = "\n".join(f"{EMOJI_NUMBERS[i]}　{opt}" for i, opt in enumerate(option_list))
        embed = discord.Embed(title=f"📊 {question}", description=description, color=discord.Color.blue())
        embed.set_footer(text=f"作成者: {ctx.author.display_name}")
        poll_msg = await ctx.send(embed=embed)
        for i in range(len(option_list)):
            await poll_msg.add_reaction(EMOJI_NUMBERS[i])
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
        description="VALORANTの日程投票を管理します",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def schedule(self, ctx):
        prefix = ctx.clean_prefix or "/"
        await ctx.send(
            "📅 日程投票コマンド\n"
            f"作成: `{prefix}schedule add @VALORANT [候補...]`\n"
            f"候補省略時: `{DEFAULT_SCHEDULE_OPTIONS}`\n"
            f"更新: `{prefix}schedule update <投稿IDまたはリンク> 21 22 24 ng`"
        )

    @schedule.error
    async def schedule_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await self._send_notice(ctx, "❌ このコマンドはサーバー内でのみ使えます")
            return
        raise error

    @schedule.command(name="add", description="新しい日程投票を作成します")
    @app_commands.describe(
        role="日程調整の対象ロール",
        options="空白区切りの候補（省略時: 20 21 22 23 24 ng）",
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

        uses_default_options = options is None
        try:
            option_list = parse_schedule_options(
                DEFAULT_SCHEDULE_OPTIONS if options is None else options
            )
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
            if ctx.interaction is not None or uses_default_options:
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
            auto_start=uses_default_options,
        )
        # Prefixコマンドでは元の投稿が既にロールへ通知するため、二重通知を避ける。
        allowed_roles = [role] if ctx.interaction is not None else False
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=allowed_roles,
            replied_user=False,
        )

        try:
            poll_message = await ctx.send(
                content=role.mention,
                embed=embed,
                allowed_mentions=allowed_mentions,
            )
            await self._add_number_reactions(poll_message, len(option_list))
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to create schedule poll")
            await self._send_notice(
                ctx,
                "❌ 投票の作成に失敗しました。Botの送信・埋め込み・リアクション権限を確認してください",
            )
            return

        logger.info(
            "%s created schedule poll %s for role %s",
            ctx.author,
            poll_message.id,
            role.id,
        )

    @schedule.command(name="update", description="既存の日程投票を更新します")
    @app_commands.describe(
        message="更新する日程投票の投稿IDまたはリンク",
        options="新しい候補（例: 21 22 24 ng）。更新時に投票はリセットされます",
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
        try:
            poll_message = await ctx.channel.fetch_message(message_id)
        except discord.NotFound:
            await self._send_notice(ctx, "❌ 指定された投稿が見つかりません")
            return
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to fetch schedule poll %s", message_id)
            await self._send_notice(ctx, "❌ 指定された投稿を取得できませんでした")
            return

        if self.bot.user is None or poll_message.author.id != self.bot.user.id:
            await self._send_notice(ctx, "❌ このBotが作成した日程投票を指定してください")
            return
        if not poll_message.embeds:
            await self._send_notice(ctx, "❌ 指定された投稿は日程投票ではありません")
            return

        creator_id = schedule_author_id(poll_message.embeds[0])
        if creator_id is None:
            await self._send_notice(ctx, "❌ 指定された投稿は日程投票ではありません")
            return

        author_can_manage = ctx.channel.permissions_for(ctx.author).manage_messages
        if ctx.author.id != creator_id and not author_can_manage:
            await self._send_notice(
                ctx,
                "❌ この投票を更新できるのは作成者か、メッセージ管理権限を持つ人だけです",
            )
            return

        updated_embed = poll_message.embeds[0].copy()
        updated_embed.description = format_schedule_options(option_list)
        # update で指定した候補はカスタム扱いにし、自動開始判定を解除する。
        remove_auto_start_marker(updated_embed)
        try:
            await poll_message.edit(embed=updated_embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to edit schedule poll %s", message_id)
            await self._send_notice(
                ctx,
                "❌ 投票の候補を更新できませんでした",
            )
            return

        try:
            await self._reset_number_reactions(poll_message, len(option_list))
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to reset schedule poll reactions %s", message_id)
            await self._send_notice(
                ctx,
                "⚠️ 候補は更新しましたが、番号リアクションの再設定に失敗しました。"
                "権限を確認して同じ内容でもう一度 update してください\n"
                f"{poll_message.jump_url}",
            )
            return

        await self._send_notice(
            ctx,
            f"✅ 日程投票を更新し、投票をリセットしました\n{poll_message.jump_url}",
        )
        logger.info("%s updated schedule poll %s", ctx.author, message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        bot_user = self.bot.user
        if (
            bot_user is None
            or payload.guild_id is None
            or payload.user_id == bot_user.id
            or str(payload.emoji) not in DEFAULT_TIME_EMOJIS
        ):
            return

        lock = self._auto_start_locks.setdefault(payload.message_id, asyncio.Lock())
        async with lock:
            try:
                await self._maybe_announce_start_time(payload)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.exception(
                    "failed to evaluate auto start for schedule poll %s",
                    payload.message_id,
                )

    async def _maybe_announce_start_time(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(payload.channel_id)
        if not hasattr(channel, "fetch_message") or not hasattr(channel, "send"):
            return

        poll_message = await channel.fetch_message(payload.message_id)
        bot_user = self.bot.user
        if bot_user is None or poll_message.author.id != bot_user.id:
            return
        if not poll_message.embeds or not is_auto_start_schedule(poll_message.embeds[0]):
            return
        if announced_start_time(poll_message.embeds[0]) is not None:
            return

        voters_by_option = await self._collect_default_schedule_voters(poll_message)
        start_time = choose_start_time(voters_by_option)
        if start_time is None:
            return

        role = self._schedule_role(poll_message)
        if role is None:
            logger.warning(
                "schedule poll %s has no target role",
                poll_message.id,
            )
            return

        original_embed = poll_message.embeds[0]
        announced_embed = original_embed.copy()
        mark_start_time_announced(announced_embed, start_time)
        marker_saved = False
        try:
            await poll_message.edit(embed=announced_embed)
            marker_saved = True
            await channel.send(
                content=f"{start_time}時開始 {role.mention}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[role],
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            if marker_saved:
                try:
                    await poll_message.edit(embed=original_embed)
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "failed to roll back auto-start marker on schedule poll %s",
                        poll_message.id,
                    )
            raise

        logger.info(
            "schedule poll %s reached %s unique voters; announced %s:00",
            poll_message.id,
            AUTO_START_THRESHOLD,
            start_time,
        )

    async def _collect_default_schedule_voters(
        self,
        poll_message: discord.Message,
    ) -> dict[str, set[int]]:
        reactions_by_emoji = {
            str(reaction.emoji): reaction for reaction in poll_message.reactions
        }
        voters_by_option: dict[str, set[int]] = {}
        for option, emoji in zip(DEFAULT_TIME_OPTIONS, DEFAULT_TIME_EMOJIS):
            reaction = reactions_by_emoji.get(emoji)
            voters: set[int] = set()
            if reaction is not None:
                async for user in reaction.users(limit=None):
                    if not user.bot:
                        voters.add(user.id)
            voters_by_option[option] = voters
        return voters_by_option

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
                "❌ 使い方: `/schedule add @VALORANT [候補...]`\n"
                f"候補省略時: `{DEFAULT_SCHEDULE_OPTIONS}`",
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
                "❌ 使い方: `/schedule update <投稿IDまたはリンク> 21 22 24 ng`",
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
        if not is_schedule_channel(ctx):
            configured_id = getattr(ctx.bot, "valorant_channel_id", 0)
            channel_label = f"<#{configured_id}>" if configured_id else "#valorant"
            await self._send_notice(
                ctx,
                f"❌ このコマンドは {channel_label} でのみ使えます",
            )
            return False
        return True

    @staticmethod
    async def _add_number_reactions(message: discord.Message, count: int):
        for emoji in EMOJI_NUMBERS[:count]:
            await message.add_reaction(emoji)

    @classmethod
    async def _reset_number_reactions(cls, message: discord.Message, count: int):
        number_emojis = [
            reaction.emoji
            for reaction in list(message.reactions)
            if str(reaction.emoji) in EMOJI_NUMBERS
        ]
        for emoji in number_emojis:
            await message.clear_reaction(emoji)
        await cls._add_number_reactions(message, count)

    @staticmethod
    async def _send_notice(ctx: commands.Context, content: str):
        kwargs = {"ephemeral": True} if ctx.interaction is not None else {}
        await ctx.send(content, **kwargs)


async def setup(bot):
    await bot.add_cog(PollCog(bot))
