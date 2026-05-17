import os
import json
from logging import getLogger

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)


class DictionaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── スラッシュコマンド ───────────────────────────────────────

    @app_commands.command(name="dict_add", description="読み上げ辞書に単語を登録します")
    @app_commands.describe(word="登録する単語", reading="読み方")
    async def dict_add(self, interaction: discord.Interaction, word: str, reading: str):
        d = self._load(interaction.guild)
        d[word] = reading
        self._save(interaction.guild, d)
        await interaction.response.send_message(f"📖 「{word}」→「{reading}」を登録しました")
        logger.info(f"dict add: {word} -> {reading}")

    @app_commands.command(name="dict_del", description="読み上げ辞書から単語を削除します")
    @app_commands.describe(word="削除する単語")
    async def dict_del(self, interaction: discord.Interaction, word: str):
        d = self._load(interaction.guild)
        if word not in d:
            await interaction.response.send_message(f"❌ 「{word}」は辞書に登録されていません", ephemeral=True)
            return
        del d[word]
        self._save(interaction.guild, d)
        await interaction.response.send_message(f"🗑️ 「{word}」を削除しました")
        logger.info(f"dict del: {word}")

    @app_commands.command(name="dict_list", description="読み上げ辞書の内容を一覧表示します")
    async def dict_list(self, interaction: discord.Interaction):
        d = self._load(interaction.guild)
        if not d:
            await interaction.response.send_message("辞書は空です", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📖 {interaction.guild.name} の辞書",
            color=discord.Color.blue(),
        )
        items = list(d.items())
        for word, reading in items[:25]:
            embed.add_field(name=word, value=reading, inline=True)
        if len(items) > 25:
            embed.set_footer(text=f"他 {len(items) - 25} 件（/dict_list では最大25件表示）")

        await interaction.response.send_message(embed=embed)

    # ─── ユーティリティ ──────────────────────────────────────────

    def _load(self, guild: discord.Guild) -> dict:
        path = f"./json/{guild.id}_dictionary.json"
        if os.path.isfile(path):
            with open(path, "r", encoding="UTF-8") as f:
                return json.load(f)
        return {}

    def _save(self, guild: discord.Guild, d: dict):
        path = f"./json/{guild.id}_dictionary.json"
        with open(path, "w", encoding="UTF-8") as f:
            json.dump(d, f, indent=4, ensure_ascii=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(DictionaryCog(bot))
