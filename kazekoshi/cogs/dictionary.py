import os
import json
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)


class DictionaryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dict_add")
    async def dict_add(self, ctx, word: str, reading: str):
        d = self._load(ctx.guild)
        d[word] = reading
        self._save(ctx.guild, d)
        await ctx.send(f"📖 「{word}」→「{reading}」を登録しました")
        logger.info(f"dict add: {word} -> {reading}")

    @commands.command(name="dict_del")
    async def dict_del(self, ctx, word: str):
        d = self._load(ctx.guild)
        if word not in d:
            await ctx.send(f"❌ 「{word}」は辞書に登録されていません")
            return
        del d[word]
        self._save(ctx.guild, d)
        await ctx.send(f"🗑️ 「{word}」を削除しました")
        logger.info(f"dict del: {word}")

    @commands.command(name="dict_list")
    async def dict_list(self, ctx):
        d = self._load(ctx.guild)
        if not d:
            await ctx.send("辞書は空です")
            return
        embed = discord.Embed(title=f"📖 {ctx.guild.name} の辞書", color=discord.Color.blue())
        items = list(d.items())
        for word, reading in items[:25]:
            embed.add_field(name=word, value=reading, inline=True)
        if len(items) > 25:
            embed.set_footer(text=f"他 {len(items) - 25} 件")
        await ctx.send(embed=embed)

    def _load(self, guild):
        path = f"./json/{guild.id}_dictionary.json"
        return json.load(open(path, "r", encoding="UTF-8")) if os.path.isfile(path) else {}

    def _save(self, guild, d):
        with open(f"./json/{guild.id}_dictionary.json", "w", encoding="UTF-8") as f:
            json.dump(d, f, indent=4, ensure_ascii=False)


async def setup(bot):
    await bot.add_cog(DictionaryCog(bot))
