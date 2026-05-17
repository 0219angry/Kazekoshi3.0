import os
import json
from logging import getLogger

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)


class NotifyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.notify_dict: dict[str, str] = {}

    # ─── スラッシュコマンド ───────────────────────────────────────

    @app_commands.command(name="notify", description="現在接続中のVCへの入室を通知します")
    async def notify(self, interaction: discord.Interaction):
        if interaction.user.voice is None:
            await interaction.response.send_message(
                "❌ 通知対象のVCに接続した状態でコマンドを実行してください", ephemeral=True
            )
            return

        vc = interaction.user.voice.channel
        tc = interaction.channel
        self._load(interaction.guild)
        self.notify_dict[str(vc.id)] = str(tc.id)
        self._save(interaction.guild)

        embed = discord.Embed(title="🔔 通知設定完了", color=discord.Color.blue())
        embed.add_field(name="監視VC", value=vc.name, inline=True)
        embed.add_field(name="通知先テキストチャンネル", value=tc.name, inline=True)
        await interaction.response.send_message(embed=embed)
        logger.info(f"notify set: {vc.name} -> {tc.name}")

    @app_commands.command(name="notify_remove", description="現在接続中のVCの入室通知を解除します")
    async def notify_remove(self, interaction: discord.Interaction):
        if interaction.user.voice is None:
            await interaction.response.send_message(
                "❌ 解除対象のVCに接続した状態でコマンドを実行してください", ephemeral=True
            )
            return

        vc = interaction.user.voice.channel
        self._load(interaction.guild)

        if str(vc.id) not in self.notify_dict:
            await interaction.response.send_message(
                f"❌ 「{vc.name}」の通知設定はありません", ephemeral=True
            )
            return

        del self.notify_dict[str(vc.id)]
        self._save(interaction.guild)
        await interaction.response.send_message(f"🔕 「{vc.name}」の通知設定を解除しました")
        logger.info(f"notify removed: {vc.name}")

    @app_commands.command(name="notify_list", description="VC入室通知の設定一覧を表示します")
    async def notify_list(self, interaction: discord.Interaction):
        self._load(interaction.guild)

        if not self.notify_dict:
            await interaction.response.send_message("通知設定はありません", ephemeral=True)
            return

        embed = discord.Embed(title="🔔 通知設定一覧", color=discord.Color.blue())
        for vc_id, tc_id in self.notify_dict.items():
            vc = interaction.guild.get_channel(int(vc_id))
            tc = interaction.guild.get_channel(int(tc_id))
            vc_name = vc.name if vc else f"不明チャンネル（{vc_id}）"
            tc_name = tc.name if tc else f"不明チャンネル（{tc_id}）"
            embed.add_field(name=vc_name, value=f"→ #{tc_name}", inline=False)

        await interaction.response.send_message(embed=embed)

    # ─── イベント ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return
        if before.channel == after.channel:
            return
        if after.channel is None:
            return

        self._load(member.guild)

        if len(after.channel.members) == 1 and str(after.channel.id) in self.notify_dict:
            name = member.nick or member.global_name or member.name
            tc_id = int(self.notify_dict[str(after.channel.id)])
            tc = self.bot.get_channel(tc_id)
            if tc:
                embed = discord.Embed(
                    title="🔔 VC入室通知",
                    description=f"**{name}** が **{after.channel.name}** に入室しました",
                    color=discord.Color.green(),
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                await tc.send(embed=embed)
                logger.info(f"{name} joined {after.channel.name}, notified to #{tc.name}")

    # ─── ユーティリティ ──────────────────────────────────────────

    def _load(self, guild: discord.Guild):
        path = f"./json/{guild.id}_notify.json"
        if os.path.isfile(path):
            with open(path, "r", encoding="UTF-8") as f:
                self.notify_dict = json.load(f)
        else:
            self.notify_dict = {}

    def _save(self, guild: discord.Guild):
        path = f"./json/{guild.id}_notify.json"
        with open(path, "w", encoding="UTF-8") as f:
            json.dump(self.notify_dict, f, indent=4)


async def setup(bot: commands.Bot):
    await bot.add_cog(NotifyCog(bot))
