import os
import glob
import json
import re
import sys
import configparser
import asyncio
from collections import defaultdict, deque
from datetime import datetime
from logging import getLogger
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from voicevox_core import VoicevoxCore, METAS

logger = getLogger(__name__)

MAX_WAV_FILE = 10


class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.connected_channels: dict[discord.Guild, discord.TextChannel] = {}
        self.queue_text: dict[int, deque] = defaultdict(deque)
        self.queue_speaker: dict[int, deque] = defaultdict(deque)
        self.user_speaker_dict: dict[str, str] = {}
        self.word_dict: dict[str, str] = {}

        try:
            config = configparser.ConfigParser()
            config.read("config.ini", encoding="UTF-8")
            self.default_speaker_id = int(config["DEFAULT"]["SPEAKER_ID"])
            self.open_jtalk_dict_dir = config["DEFAULT"]["OPEN_JTALK_DICT_DIR"]
        except Exception:
            logger.exception("config.ini の読み込みに失敗しました")
            sys.exit(1)

        self.core = VoicevoxCore(open_jtalk_dict_dir=Path(self.open_jtalk_dict_dir))

    # ─── スラッシュコマンド ───────────────────────────────────────

    @app_commands.command(name="join", description="VCに接続して読み上げを開始します")
    async def join(self, interaction: discord.Interaction):
        if interaction.user.voice is None:
            await interaction.response.send_message(
                "❌ ボイスチャンネルに接続してからコマンドを実行してください", ephemeral=True
            )
            return

        if interaction.guild.voice_client is not None:
            await interaction.guild.voice_client.disconnect()

        await interaction.user.voice.channel.connect()
        self.connected_channels[interaction.guild] = interaction.channel
        self._load_user_speaker_id(interaction.guild)

        embed = discord.Embed(title="📢 読み上げ開始", color=discord.Color.green())
        embed.add_field(name="テキストチャンネル", value=interaction.channel.name, inline=True)
        embed.add_field(name="ボイスチャンネル", value=interaction.user.voice.channel.name, inline=True)
        await interaction.response.send_message(embed=embed)
        logger.info(f"joined VC: {interaction.user.voice.channel.name}")

    @app_commands.command(name="leave", description="VCから切断して読み上げを終了します")
    async def leave(self, interaction: discord.Interaction):
        if interaction.guild.voice_client is None:
            await interaction.response.send_message("❌ VCに接続していません", ephemeral=True)
            return

        await interaction.guild.voice_client.disconnect()
        self.connected_channels.pop(interaction.guild, None)

        embed = discord.Embed(title="🔇 読み上げ終了", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        logger.info("disconnected from VC")

    @app_commands.command(name="voice", description="自分の読み上げボイスを変更します")
    async def voice(self, interaction: discord.Interaction):
        view = DropdownView(self)
        await interaction.response.send_message("🎙️ 読み上げボイスを選んでください", view=view, ephemeral=True)

    # ─── イベント ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.content.startswith(self.bot.command_prefix):
            return
        if message.guild is None:
            return
        if message.channel not in self.connected_channels.values():
            return
        if message.guild.voice_client is None:
            return

        await self._process_message(message)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        vc = member.guild.voice_client
        # 全員退出したら自動切断
        if (
            vc is not None
            and member.id != self.bot.user.id
            and vc.channel == before.channel
            and len(vc.channel.members) == 1
        ):
            await vc.disconnect()
            channel = self.connected_channels.pop(member.guild, None)
            if channel:
                embed = discord.Embed(
                    title="🔇 読み上げ終了",
                    description="誰もいなくなったため読み上げを終了しました",
                    color=discord.Color.red(),
                )
                await channel.send(embed=embed)
            logger.info(f"auto disconnected from {before.channel}")

    # ─── 内部処理 ────────────────────────────────────────────────

    async def _process_message(self, message: discord.Message):
        """メッセージを読み上げ用テキストに変換してキューに積む"""
        msg_text = message.content
        msg_text = self._replace_url(msg_text)
        msg_text = self._replace_mention(msg_text, message)
        msg_text = self._replace_channel(msg_text, message)
        msg_text = self._replace_dictionary(msg_text, message.guild)

        if not msg_text.strip():
            return

        # 長文カット
        if len(msg_text) > 100:
            msg_text = msg_text[:100] + "、以下省略"

        if str(message.author.id) not in self.user_speaker_dict:
            self._save_user_speaker_id(message.author, 3)

        speaker_id = int(self.user_speaker_dict[str(message.author.id)])
        self._enqueue(message.guild.voice_client, message.guild, msg_text, speaker_id)
        logger.debug(f"enqueued: [{msg_text}]")

    def _enqueue(
        self,
        voice_client: discord.VoiceClient,
        guild: discord.Guild,
        text: str,
        speaker_id: int,
    ):
        self.queue_text[guild.id].append(text)
        self.queue_speaker[guild.id].append(speaker_id)
        if not voice_client.is_playing():
            self._synthesize_play(voice_client, guild.id)

    def _synthesize_play(self, voice_client: discord.VoiceClient, guild_id: int):
        """キューから取り出して再生。after コールバックで連鎖させる"""
        qt = self.queue_text[guild_id]
        qs = self.queue_speaker[guild_id]

        if not qt or voice_client.is_playing():
            return

        # キューからローカル変数に取り出す（スレッドセーフ）
        text = qt.popleft()
        speaker = qs.popleft()

        try:
            if not self.core.is_model_loaded(speaker):
                self.core.load_model(speaker)

            wave_bytes = self.core.tts(text, speaker)
            wavfilename = f"./temp/{datetime.now():%Y-%m-%d_%H%M%S_%f}.wav"

            with open(wavfilename, "wb") as f:
                f.write(wave_bytes)

            source = discord.FFmpegPCMAudio(wavfilename)
            voice_client.play(
                source,
                after=lambda e: self._synthesize_play(voice_client, guild_id),
            )

            # 古いWAVを削除
            wavlist = sorted(glob.glob("./temp/*.wav"))
            if len(wavlist) > MAX_WAV_FILE:
                for old in wavlist[:-MAX_WAV_FILE]:
                    try:
                        os.remove(old)
                    except OSError:
                        pass

        except Exception:
            logger.exception(f"音声合成エラー: [{text}]")

    # ─── 辞書・設定ユーティリティ ────────────────────────────────

    def _replace_url(self, text: str) -> str:
        return re.sub(r"https?://\S+", "URL", text)

    def _replace_mention(self, text: str, message: discord.Message) -> str:
        for match in re.finditer(r"<@!?(\d+)>", text):
            member = message.guild.get_member(int(match.group(1)))
            if member:
                text = text.replace(match.group(0), f"アット{member.display_name}")
        return text

    def _replace_channel(self, text: str, message: discord.Message) -> str:
        for match in re.finditer(r"<#(\d+)>", text):
            channel = message.guild.get_channel(int(match.group(1)))
            if channel:
                text = text.replace(match.group(0), f"チャンネル{channel.name}")
        return text

    def _replace_dictionary(self, text: str, guild: discord.Guild) -> str:
        self._load_dictionary(guild)
        read_list = []
        for i, (from_word, to_word) in enumerate(self.word_dict.items()):
            text = text.replace(from_word, "{" + str(i) + "}")
            read_list.append(to_word)
        return text.format(*read_list)

    def _load_dictionary(self, guild: discord.Guild):
        path = f"./json/{guild.id}_dictionary.json"
        if os.path.isfile(path):
            with open(path, "r", encoding="UTF-8") as f:
                self.word_dict = json.load(f)
        else:
            self.word_dict = {}

    def _load_user_speaker_id(self, guild: discord.Guild):
        path = f"./json/{guild.id}_speakerid.json"
        if os.path.isfile(path):
            with open(path, "r", encoding="UTF-8") as f:
                self.user_speaker_dict = json.load(f)
        logger.debug(f"loaded speaker ids: {self.user_speaker_dict}")

    def _save_user_speaker_id(self, member: discord.Member, speaker_id: int):
        self.user_speaker_dict[str(member.id)] = str(speaker_id)
        path = f"./json/{member.guild.id}_speakerid.json"
        with open(path, "w", encoding="UTF-8") as f:
            json.dump(self.user_speaker_dict, f, indent=4)


# ─── ボイス選択UI ───────────────────────────────────────────────

class Dropdown(discord.ui.Select):
    def __init__(self, cog: VoiceCog):
        self.cog = cog
        options = []
        for meta in METAS:
            for style in meta.styles:
                if style.name == "ノーマル":
                    options.append(
                        discord.SelectOption(
                            label=meta.name,
                            value=str(style.id),
                            description=f"Speaker ID: {style.id}",
                        )
                    )
        super().__init__(
            placeholder="ボイスを選んでください",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        speaker_id = int(self.values[0])
        self.cog._save_user_speaker_id(interaction.user, speaker_id)
        self.disabled = True
        label = next((o.label for o in self.options if o.value == self.values[0]), self.values[0])
        await interaction.response.edit_message(
            content=f"✅ {interaction.user.display_name} の読み上げを **{label}**（ID: {speaker_id}）に変更しました",
            view=self.view,
        )
        logger.info(f"{interaction.user} changed speaker to {label}({speaker_id})")


class DropdownView(discord.ui.View):
    def __init__(self, cog: VoiceCog):
        super().__init__(timeout=60)
        self.add_item(Dropdown(cog))


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceCog(bot))
