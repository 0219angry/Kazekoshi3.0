import asyncio
import re
from datetime import datetime, timedelta
from logging import getLogger

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)

TIME_PATTERN = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
MAX_SECONDS = 86400 * 7  # 7日


def parse_duration(text: str) -> int | None:
    """
    時間文字列を秒に変換する。
    例: "10m" -> 600, "1h30m" -> 5400, "2d" -> 172800
    """
    matches = TIME_PATTERN.findall(text.lower())
    if not matches:
        return None

    total = 0
    unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for value, unit in matches:
        total += int(value) * unit_map[unit]
    return total


class ReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="remind", description="指定時間後にリマインドします")
    @app_commands.describe(
        time="時間（例: 10m / 2h / 1h30m / 1d）",
        message="リマインドする内容",
    )
    async def remind(self, interaction: discord.Interaction, time: str, message: str):
        seconds = parse_duration(time)

        if seconds is None:
            await interaction.response.send_message(
                "❌ 時間の形式が正しくありません\n"
                "例: `30s`（30秒） `10m`（10分） `2h`（2時間） `1d`（1日） `1h30m`（1時間30分）",
                ephemeral=True,
            )
            return
        if seconds < 10:
            await interaction.response.send_message("❌ リマインダーは10秒以上に設定してください", ephemeral=True)
            return
        if seconds > MAX_SECONDS:
            await interaction.response.send_message("❌ リマインダーは7日以内に設定してください", ephemeral=True)
            return

        remind_at = datetime.now() + timedelta(seconds=seconds)

        embed = discord.Embed(title="⏰ リマインダー設定完了", color=discord.Color.green())
        embed.add_field(name="内容", value=message, inline=False)
        embed.add_field(name="時間指定", value=time, inline=True)
        embed.add_field(name="通知予定", value=f"<t:{int(remind_at.timestamp())}:R>", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        asyncio.create_task(
            self._fire(
                channel=interaction.channel,
                user=interaction.user,
                message=message,
                seconds=seconds,
            )
        )
        logger.info(f"{interaction.user} set reminder: '{message}' in {seconds}s")

    async def _fire(
        self,
        channel: discord.TextChannel,
        user: discord.Member,
        message: str,
        seconds: int,
    ):
        await asyncio.sleep(seconds)
        embed = discord.Embed(
            title="⏰ リマインダー",
            description=message,
            color=discord.Color.orange(),
        )
        embed.set_footer(text="お知らせしましたよ！")
        await channel.send(content=user.mention, embed=embed)
        logger.info(f"reminder fired for {user}: {message}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ReminderCog(bot))
