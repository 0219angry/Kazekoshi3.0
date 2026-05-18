import os
import glob
import json
import re
import asyncio
import configparser
from collections import defaultdict, deque
from datetime import datetime
from logging import getLogger

import discord
from discord.ext import commands
import edge_tts

logger = getLogger(__name__)
MAX_WAV_FILE = 10

VOICES = [
    ("ja-JP-NanamiNeural",  "七海（女性）"),
    ("ja-JP-AoiNeural",     "葵（女性）"),
    ("ja-JP-MayuNeural",    "繭（女性）"),
    ("ja-JP-ShioriNeural",  "栞（女性）"),
    ("ja-JP-KeitaNeural",   "慶太（男性）"),
    ("ja-JP-DaichiNeural",  "大地（男性）"),
    ("ja-JP-NaokiNeural",   "直樹（男性）"),
]
DEFAULT_VOICE = VOICES[0][0]


async def synthesize(text: str, voice: str, out_path: str):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.connected_channels: dict = {}
        self.queue: dict[int, deque] = defaultdict(deque)
        self.user_voice: dict[str, str] = {}
        self.word_dict: dict = {}
        self.playing: dict[int, bool] = defaultdict(bool)

        try:
            config = configparser.ConfigParser()
            config.read("config.ini", encoding="UTF-8")
            self.default_voice = config["DEFAULT"].get("DEFAULT_VOICE", DEFAULT_VOICE)
        except Exception:
            logger.exception("config.ini の読み込みに失敗しました")
            self.default_voice = DEFAULT_VOICE

    # ─── コマンド ────────────────────────────────────────────────

    @commands.command(name="join")
    async def join(self, ctx):
        if ctx.author.voice is None:
            await ctx.send("❌ ボイスチャンネルに接続してからコマンドを実行してください")
            return
        if ctx.guild.voice_client is not None:
            await ctx.guild.voice_client.disconnect()
        await ctx.author.voice.channel.connect()
        self.connected_channels[ctx.guild] = ctx.channel
        self._load_user_voice(ctx.guild)
        embed = discord.Embed(title="📢 読み上げ開始", color=discord.Color.green())
        embed.add_field(name="テキストチャンネル", value=ctx.channel.name, inline=True)
        embed.add_field(name="ボイスチャンネル", value=ctx.author.voice.channel.name, inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="leave")
    async def leave(self, ctx):
        if ctx.guild.voice_client is None:
            await ctx.send("❌ VCに接続していません")
            return
        await ctx.guild.voice_client.disconnect()
        self.connected_channels.pop(ctx.guild, None)
        await ctx.send(embed=discord.Embed(title="🔇 読み上げ終了", color=discord.Color.red()))

    @commands.command(name="voice")
    async def voice(self, ctx):
        await ctx.send("🎙️ 読み上げボイスを選んでください", view=VoiceDropdownView(self, ctx.author))

    # ─── イベント ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message):
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

    # ─── 内部処理 ────────────────────────────────────────────────

    async def _process_message(self, message):
        text = message.content
        text = re.sub(r"https?://\S+", "URL", text)
        for m in re.finditer(r"<@!?(\d+)>", text):
            member = message.guild.get_member(int(m.group(1)))
            if member:
                text = text.replace(m.group(0), f"アット{member.display_name}")
        for m in re.finditer(r"<#(\d+)>", text):
            ch = message.guild.get_channel(int(m.group(1)))
            if ch:
                text = text.replace(m.group(0), f"チャンネル{ch.name}")
        text = self._apply_dictionary(text, message.guild)
        if not text.strip():
            return
        if len(text) > 100:
            text = text[:100] + "、以下省略"
        voice = self.user_voice.get(str(message.author.id), self.default_voice)
        self.queue[message.guild.id].append((text, voice, message.guild.voice_client))
        if not self.playing[message.guild.id]:
            asyncio.create_task(self._drain_queue(message.guild.id))

    async def _drain_queue(self, guild_id: int):
        self.playing[guild_id] = True
        try:
            while self.queue[guild_id]:
                text, voice, vc = self.queue[guild_id].popleft()
                if vc is None or not vc.is_connected():
                    continue
                out = f"./temp/{datetime.now():%Y-%m-%d_%H%M%S_%f}.mp3"
                try:
                    await synthesize(text, voice, out)
                except Exception:
                    logger.exception(f"音声合成エラー: [{text}]")
                    continue
                fut = asyncio.get_event_loop().create_future()
                vc.play(discord.FFmpegPCMAudio(out), after=lambda e, f=fut: f.get_loop().call_soon_threadsafe(f.set_result, e))
                await fut
                self._cleanup_temp()
        finally:
            self.playing[guild_id] = False

    def _cleanup_temp(self):
        files = sorted(glob.glob("./temp/*.mp3"))
        for old in files[:-MAX_WAV_FILE]:
            try:
                os.remove(old)
            except OSError:
                pass

    def _apply_dictionary(self, text: str, guild) -> str:
        path = f"./json/{guild.id}_dictionary.json"
        word_dict = json.load(open(path, "r", encoding="UTF-8")) if os.path.isfile(path) else {}
        read_list = []
        for i, (from_word, to_word) in enumerate(word_dict.items()):
            text = text.replace(from_word, "{" + str(i) + "}")
            read_list.append(to_word)
        return text.format(*read_list)

    def _load_user_voice(self, guild):
        path = f"./json/{guild.id}_voice.json"
        self.user_voice = json.load(open(path, "r", encoding="UTF-8")) if os.path.isfile(path) else {}

    def _save_user_voice(self, member, voice: str):
        self.user_voice[str(member.id)] = voice
        with open(f"./json/{member.guild.id}_voice.json", "w", encoding="UTF-8") as f:
            json.dump(self.user_voice, f, indent=4)


class VoiceDropdown(discord.ui.Select):
    def __init__(self, cog, member):
        self.cog = cog
        self.member = member
        options = [
            discord.SelectOption(label=label, value=voice_id)
            for voice_id, label in VOICES
        ]
        super().__init__(placeholder="ボイスを選んでください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        voice_id = self.values[0]
        self.cog._save_user_voice(self.member, voice_id)
        label = next(label for vid, label in VOICES if vid == voice_id)
        self.disabled = True
        await interaction.response.edit_message(
            content=f"✅ {self.member.display_name} の読み上げを **{label}** に変更しました",
            view=self.view,
        )


class VoiceDropdownView(discord.ui.View):
    def __init__(self, cog, member):
        super().__init__(timeout=60)
        self.add_item(VoiceDropdown(cog, member))


async def setup(bot):
    await bot.add_cog(VoiceCog(bot))
