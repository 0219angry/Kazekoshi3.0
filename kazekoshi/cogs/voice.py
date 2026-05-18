import os
import glob
import json
import re
import sys
import configparser
from collections import defaultdict, deque
from datetime import datetime
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)
MAX_WAV_FILE = 10


def _init_synthesizer(dict_dir):
    from voicevox_core.blocking import Synthesizer, OpenJtalk, VoiceModelFile, Onnxruntime
    open_jtalk = OpenJtalk(dict_dir)
    onnxruntime = Onnxruntime.load_once()
    synth = Synthesizer(onnxruntime, open_jtalk)
    model_files = sorted(glob.glob("models/*.vvm"))
    if not model_files:
        logger.warning("models/*.vvm が見つかりません。読み上げ機能は無効です。")
        return None, []
    for path in model_files:
        model = VoiceModelFile.open(path)
        synth.load_voice_model(model)
        logger.info(f"loaded voice model: {path}")
    return synth, synth.metas


class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.connected_channels = {}
        self.queue_text = defaultdict(deque)
        self.queue_speaker = defaultdict(deque)
        self.user_speaker_dict = {}
        self.word_dict = {}
        self.synthesizer = None
        self.metas = []

        try:
            config = configparser.ConfigParser()
            config.read("config.ini", encoding="UTF-8")
            self.default_speaker_id = int(config["DEFAULT"]["SPEAKER_ID"])
            dict_dir = config["DEFAULT"]["OPEN_JTALK_DICT_DIR"]
        except Exception:
            logger.exception("config.ini の読み込みに失敗しました")
            sys.exit(1)

        try:
            self.synthesizer, self.metas = _init_synthesizer(dict_dir)
        except Exception:
            logger.exception("VOICEVOX 初期化エラー（読み上げ機能は無効）")

    # ─── コマンド ────────────────────────────────────────────────

    @commands.command(name="join")
    async def join(self, ctx):
        if self.synthesizer is None:
            await ctx.send("❌ 読み上げ機能が無効です（models/*.vvm が見つかりません）")
            return
        if ctx.author.voice is None:
            await ctx.send("❌ ボイスチャンネルに接続してからコマンドを実行してください")
            return
        if ctx.guild.voice_client is not None:
            await ctx.guild.voice_client.disconnect()
        await ctx.author.voice.channel.connect()
        self.connected_channels[ctx.guild] = ctx.channel
        self._load_user_speaker_id(ctx.guild)
        embed = discord.Embed(title="📢 読み上げ開始", color=discord.Color.green())
        embed.add_field(name="テキストチャンネル", value=ctx.channel.name, inline=True)
        embed.add_field(name="ボイスチャンネル", value=ctx.author.voice.channel.name, inline=True)
        await ctx.send(embed=embed)
        logger.info(f"joined VC: {ctx.author.voice.channel.name}")

    @commands.command(name="leave")
    async def leave(self, ctx):
        if ctx.guild.voice_client is None:
            await ctx.send("❌ VCに接続していません")
            return
        await ctx.guild.voice_client.disconnect()
        self.connected_channels.pop(ctx.guild, None)
        await ctx.send(embed=discord.Embed(title="🔇 読み上げ終了", color=discord.Color.red()))
        logger.info("disconnected from VC")

    @commands.command(name="voice")
    async def voice(self, ctx):
        if not self.metas:
            await ctx.send("❌ 読み上げ機能が無効です")
            return
        await ctx.send("🎙️ 読み上げボイスを選んでください", view=DropdownView(self, ctx.author))

    # ─── イベント ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or self.synthesizer is None:
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
    async def on_voice_state_update(self, member, before, after):
        vc = member.guild.voice_client
        if vc and member.id != self.bot.user.id and vc.channel == before.channel and len(vc.channel.members) == 1:
            await vc.disconnect()
            channel = self.connected_channels.pop(member.guild, None)
            if channel:
                await channel.send(embed=discord.Embed(
                    title="🔇 読み上げ終了",
                    description="誰もいなくなったため読み上げを終了しました",
                    color=discord.Color.red(),
                ))
            logger.info(f"auto disconnected from {before.channel}")

    # ─── 内部処理 ────────────────────────────────────────────────

    async def _process_message(self, message):
        from voicevox_core import StyleId
        msg_text = message.content
        msg_text = self._replace_url(msg_text)
        msg_text = self._replace_mention(msg_text, message)
        msg_text = self._replace_channel(msg_text, message)
        msg_text = self._replace_dictionary(msg_text, message.guild)
        if not msg_text.strip():
            return
        if len(msg_text) > 100:
            msg_text = msg_text[:100] + "、以下省略"
        if str(message.author.id) not in self.user_speaker_dict:
            self._save_user_speaker_id(message.author, self.default_speaker_id)
        speaker_id = int(self.user_speaker_dict[str(message.author.id)])
        self._enqueue(message.guild.voice_client, message.guild, msg_text, StyleId(speaker_id))

    def _enqueue(self, voice_client, guild, text, style_id):
        self.queue_text[guild.id].append(text)
        self.queue_speaker[guild.id].append(style_id)
        if not voice_client.is_playing():
            self._synthesize_play(voice_client, guild.id)

    def _synthesize_play(self, voice_client, guild_id):
        qt = self.queue_text[guild_id]
        qs = self.queue_speaker[guild_id]
        if not qt or voice_client.is_playing():
            return
        text = qt.popleft()
        style_id = qs.popleft()
        try:
            wave_bytes = self.synthesizer.tts(text, style_id)
            wavfilename = f"./temp/{datetime.now():%Y-%m-%d_%H%M%S_%f}.wav"
            with open(wavfilename, "wb") as f:
                f.write(wave_bytes)
            voice_client.play(
                discord.FFmpegPCMAudio(wavfilename),
                after=lambda e: self._synthesize_play(voice_client, guild_id),
            )
            wavlist = sorted(glob.glob("./temp/*.wav"))
            if len(wavlist) > MAX_WAV_FILE:
                for old in wavlist[:-MAX_WAV_FILE]:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
        except Exception:
            logger.exception(f"音声合成エラー: [{text}]")

    def _replace_url(self, text):
        return re.sub(r"https?://\S+", "URL", text)

    def _replace_mention(self, text, message):
        for match in re.finditer(r"<@!?(\d+)>", text):
            member = message.guild.get_member(int(match.group(1)))
            if member:
                text = text.replace(match.group(0), f"アット{member.display_name}")
        return text

    def _replace_channel(self, text, message):
        for match in re.finditer(r"<#(\d+)>", text):
            channel = message.guild.get_channel(int(match.group(1)))
            if channel:
                text = text.replace(match.group(0), f"チャンネル{channel.name}")
        return text

    def _replace_dictionary(self, text, guild):
        self._load_dictionary(guild)
        read_list = []
        for i, (from_word, to_word) in enumerate(self.word_dict.items()):
            text = text.replace(from_word, "{" + str(i) + "}")
            read_list.append(to_word)
        return text.format(*read_list)

    def _load_dictionary(self, guild):
        path = f"./json/{guild.id}_dictionary.json"
        self.word_dict = json.load(open(path, "r", encoding="UTF-8")) if os.path.isfile(path) else {}

    def _load_user_speaker_id(self, guild):
        path = f"./json/{guild.id}_speakerid.json"
        if os.path.isfile(path):
            with open(path, "r", encoding="UTF-8") as f:
                self.user_speaker_dict = json.load(f)

    def _save_user_speaker_id(self, member, speaker_id):
        self.user_speaker_dict[str(member.id)] = str(speaker_id)
        with open(f"./json/{member.guild.id}_speakerid.json", "w", encoding="UTF-8") as f:
            json.dump(self.user_speaker_dict, f, indent=4)


class Dropdown(discord.ui.Select):
    def __init__(self, cog, member):
        self.cog = cog
        self.member = member
        options = [
            discord.SelectOption(
                label=f"{meta.name}（{style.name}）",
                value=str(style.id),
                description=f"Style ID: {style.id}",
            )
            for meta in cog.metas for style in meta.styles
        ]
        super().__init__(placeholder="ボイスを選んでください", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction):
        speaker_id = int(self.values[0])
        self.cog._save_user_speaker_id(self.member, speaker_id)
        self.disabled = True
        label = next((o.label for o in self.options if o.value == self.values[0]), self.values[0])
        await interaction.response.edit_message(
            content=f"✅ {self.member.display_name} の読み上げを **{label}** に変更しました",
            view=self.view,
        )


class DropdownView(discord.ui.View):
    def __init__(self, cog, member):
        super().__init__(timeout=60)
        self.add_item(Dropdown(cog, member))


async def setup(bot):
    await bot.add_cog(VoiceCog(bot))
