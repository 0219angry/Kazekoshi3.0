import re
from random import randint
from logging import getLogger

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)


class DiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="dice", description="ダイスを振ります（例: 2d6）")
    @app_commands.describe(notation="ダイスの表記（例: 2d6, 1d20, 3d100）")
    async def dice(self, interaction: discord.Interaction, notation: str):
        match = re.fullmatch(r"(\d+)[dD](\d+)", notation.strip())
        if not match:
            await interaction.response.send_message(
                "❌ `[個数]d[面数]` の形式で入力してください（例: `2d6`）", ephemeral=True
            )
            return

        quantity = int(match.group(1))
        faces = int(match.group(2))

        if not (1 <= quantity <= 100):
            await interaction.response.send_message("❌ ダイスの個数は 1〜100 にしてください", ephemeral=True)
            return
        if not (2 <= faces <= 10000):
            await interaction.response.send_message("❌ ダイスの面数は 2〜10000 にしてください", ephemeral=True)
            return

        results = [randint(1, faces) for _ in range(quantity)]
        total = sum(results)

        embed = discord.Embed(title="🎲 ダイスロール", color=discord.Color.gold())
        embed.add_field(name="表記", value=f"`{notation.upper()}`", inline=True)
        embed.add_field(name="合計", value=f"**{total}**", inline=True)
        if quantity > 1:
            # 表示が長すぎる場合は省略
            result_str = str(results) if len(str(results)) <= 1000 else str(results[:20]) + " ..."
            embed.add_field(name="内訳", value=result_str, inline=False)
        embed.set_footer(text=interaction.user.display_name)

        await interaction.response.send_message(embed=embed)
        logger.info(f"{interaction.user} rolled {notation}: total={total}")

    @app_commands.command(name="coin", description="コインを投げます")
    async def coin(self, interaction: discord.Interaction):
        result = randint(0, 1)
        label = "表（ヘッズ）🪙" if result == 0 else "裏（テイルズ）🔄"
        embed = discord.Embed(title="🪙 コイントス", description=f"**{label}**", color=discord.Color.gold())
        embed.set_footer(text=interaction.user.display_name)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DiceCog(bot))
