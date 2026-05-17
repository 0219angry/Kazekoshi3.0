import os
import json
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)


class NotifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.notify_dict = {}

    @commands.command(name="notify")
    async def notify(self, ctx):
        if ctx.author.voice is None:
            await ctx.send("❌ 通知対象のVCに接続した状態でコマンドを実行してください")
            return
        vc = ctx.author.voice.channel
        self._load(ctx.guild)
        self.notify_dict[str(vc.id)] = str(ctx.channel.id)
        self._save(ctx.guild)
        embed = discord.Embed(title="🔔 通知設定完了", color=discord.Color.blue())
        embed.add_field(name="監視VC", value=vc.name, inline=True)
        embed.add_field(name="通知先テキストチャンネル", value=ctx.channel.name, inline=True)
        await ctx.send(embed=embed)
        logger.info(f"notify set: {vc.name} -> {ctx.channel.name}")

    @commands.command(name="notify_remove")
    async def notify_remove(self, ctx):
        if ctx.author.voice is None:
            await ctx.send("❌ 解除対象のVCに接続した状態でコマンドを実行してください")
            return
        vc = ctx.author.voice.channel
        self._load(ctx.guild)
        if str(vc.id) not in self.notify_dict:
            await ctx.send(f"❌ 「{vc.name}」の通知設定はありません")
            return
        del self.notify_dict[str(vc.id)]
        self._save(ctx.guild)
        await ctx.send(f"🔕 「{vc.name}」の通知設定を解除しました")
        logger.info(f"notify removed: {vc.name}")

    @commands.command(name="notify_list")
    async def notify_list(self, ctx):
        self._load(ctx.guild)
        if not self.notify_dict:
            await ctx.send("通知設定はありません")
            return
        embed = discord.Embed(title="🔔 通知設定一覧", color=discord.Color.blue())
        for vc_id, tc_id in self.notify_dict.items():
            vc = ctx.guild.get_channel(int(vc_id))
            tc = ctx.guild.get_channel(int(tc_id))
            vc_name = vc.name if vc else f"不明チャンネル（{vc_id}）"
            tc_name = tc.name if tc else f"不明チャンネル（{tc_id}）"
            embed.add_field(name=vc_name, value=f"→ #{tc_name}", inline=False)
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel or after.channel is None:
            return
        self._load(member.guild)
        if len(after.channel.members) == 1 and str(after.channel.id) in self.notify_dict:
            name = member.nick or member.global_name or member.name
            tc = self.bot.get_channel(int(self.notify_dict[str(after.channel.id)]))
            if tc:
                embed = discord.Embed(
                    title="🔔 VC入室通知",
                    description=f"**{name}** が **{after.channel.name}** に入室しました",
                    color=discord.Color.green(),
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                await tc.send(embed=embed)

    def _load(self, guild):
        path = f"./json/{guild.id}_notify.json"
        self.notify_dict = json.load(open(path, "r", encoding="UTF-8")) if os.path.isfile(path) else {}

    def _save(self, guild):
        with open(f"./json/{guild.id}_notify.json", "w", encoding="UTF-8") as f:
            json.dump(self.notify_dict, f, indent=4)


async def setup(bot):
    await bot.add_cog(NotifyCog(bot))
