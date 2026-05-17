import re
from random import randint
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)


class DiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dice")
    async def dice(self, ctx, notation: str):
        match = re.fullmatch(r"(\d+)[dD](\d+)", notation.strip())
        if not match:
            await ctx.send("❌ `[個数]d[面数]` の形式で入力してください（例: `2d6`）")
            return
        quantity = int(match.group(1))
        faces = int(match.group(2))
        if not (1 <= quantity <= 100):
            await ctx.send("❌ ダイスの個数は 1〜100 にしてください")
            return
        if not (2 <= faces <= 10000):
            await ctx.send("❌ ダイスの面数は 2〜10000 にしてください")
            return
        results = [randint(1, faces) for _ in range(quantity)]
        total = sum(results)
        embed = discord.Embed(title="🎲 ダイスロール", color=discord.Color.gold())
        embed.add_field(name="表記", value=f"`{notation.upper()}`", inline=True)
        embed.add_field(name="合計", value=f"**{total}**", inline=True)
        if quantity > 1:
            result_str = str(results) if len(str(results)) <= 1000 else str(results[:20]) + " ..."
            embed.add_field(name="内訳", value=result_str, inline=False)
        embed.set_footer(text=ctx.author.display_name)
        await ctx.send(embed=embed)
        logger.info(f"{ctx.author} rolled {notation}: total={total}")

    @commands.command(name="coin")
    async def coin(self, ctx):
        result = randint(0, 1)
        label = "表（ヘッズ）🪙" if result == 0 else "裏（テイルズ）🔄"
        embed = discord.Embed(title="🪙 コイントス", description=f"**{label}**", color=discord.Color.gold())
        embed.set_footer(text=ctx.author.display_name)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DiceCog(bot))
