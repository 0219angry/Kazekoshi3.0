import asyncio
import re
from datetime import datetime, timedelta
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)
TIME_PATTERN = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
MAX_SECONDS = 86400 * 7


def parse_duration(text):
    matches = TIME_PATTERN.findall(text.lower())
    if not matches:
        return None
    unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(int(v) * unit_map[u] for v, u in matches)


class ReminderCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="remind")
    async def remind(self, ctx, time: str, *, message: str):
        seconds = parse_duration(time)
        if seconds is None:
            await ctx.send("❌ 時間の形式が正しくありません\n例: `30s` `10m` `2h` `1d` `1h30m`")
            return
        if seconds < 10:
            await ctx.send("❌ リマインダーは10秒以上に設定してください")
            return
        if seconds > MAX_SECONDS:
            await ctx.send("❌ リマインダーは7日以内に設定してください")
            return
        remind_at = datetime.now() + timedelta(seconds=seconds)
        embed = discord.Embed(title="⏰ リマインダー設定完了", color=discord.Color.green())
        embed.add_field(name="内容", value=message, inline=False)
        embed.add_field(name="時間指定", value=time, inline=True)
        embed.add_field(name="通知予定", value=f"<t:{int(remind_at.timestamp())}:R>", inline=True)
        await ctx.send(embed=embed)
        asyncio.create_task(self._fire(ctx.channel, ctx.author, message, seconds))
        logger.info(f"{ctx.author} set reminder: '{message}' in {seconds}s")

    async def _fire(self, channel, user, message, seconds):
        await asyncio.sleep(seconds)
        embed = discord.Embed(title="⏰ リマインダー", description=message, color=discord.Color.orange())
        embed.set_footer(text="お知らせしましたよ！")
        await channel.send(content=user.mention, embed=embed)
        logger.info(f"reminder fired for {user}: {message}")


async def setup(bot):
    await bot.add_cog(ReminderCog(bot))
