import asyncio
import os
from collections import defaultdict, deque
from logging import getLogger

import discord
from discord.ext import commands
import yt_dlp

logger = getLogger(__name__)

_NODE_PATH = os.path.expanduser("~/.nvm/versions/node/v20.20.2/bin/node")
_JS_RUNTIMES = {"node": {"path": _NODE_PATH}} if os.path.exists(_NODE_PATH) else {"deno": {}}

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "js_runtimes": _JS_RUNTIMES,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -ar 48000 -ac 2",
}


def fmt_duration(sec: int) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def fetch_track(query: str) -> dict | None:
    loop = asyncio.get_running_loop()
    def _extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "stream_url": info["url"],
                "title": info.get("title", "不明"),
                "duration": info.get("duration", 0),
                "webpage_url": info.get("webpage_url", query),
            }
    try:
        return await loop.run_in_executor(None, _extract)
    except Exception:
        logger.exception(f"yt-dlp エラー: {query}")
        return None


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # キューにはメタデータのみ保存（stream_url は再生時に都度取得）
        self.queues: dict[int, deque] = defaultdict(deque)
        self.current: dict[int, dict] = {}
        self.text_channels: dict[int, discord.TextChannel] = {}

    # ─── play ────────────────────────────────────────────────────

    @commands.command(name="play", aliases=["p"])
    async def play(self, ctx, *, query: str):
        if ctx.author.voice is None:
            await ctx.send("❌ VCに接続してから使ってね")
            return
        if ctx.guild.voice_client is None:
            await ctx.author.voice.channel.connect()
        elif ctx.guild.voice_client.channel != ctx.author.voice.channel:
            await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
        self.text_channels[ctx.guild.id] = ctx.channel

        async with ctx.typing():
            track = await fetch_track(query)
        if track is None:
            await ctx.send("❌ 見つからなかった")
            return

        item = {
            "title": track["title"],
            "duration": track["duration"],
            "webpage_url": track["webpage_url"],
            "requester": ctx.author.display_name,
        }
        self.queues[ctx.guild.id].append(item)

        vc = ctx.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            embed = discord.Embed(title="📋 キューに追加", color=discord.Color.blue())
            embed.add_field(name="曲名", value=f"[{item['title']}]({item['webpage_url']})", inline=False)
            embed.add_field(name="長さ", value=fmt_duration(item["duration"]), inline=True)
            embed.set_footer(text=f"リクエスト: {item['requester']}")
            await ctx.send(embed=embed)
        else:
            await self._play_next(ctx.guild)

    # ─── 基本操作 ────────────────────────────────────────────────

    @commands.command(name="skip", aliases=["s"])
    async def skip(self, ctx):
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_playing():
            await ctx.send("❌ 再生中じゃないよ")
            return
        vc.stop()
        await ctx.send("⏭️ スキップ")

    @commands.command(name="stop")
    async def stop(self, ctx):
        vc = ctx.guild.voice_client
        if vc is None:
            await ctx.send("❌ VCに入ってないよ")
            return
        self.queues[ctx.guild.id].clear()
        self.current.pop(ctx.guild.id, None)
        await vc.disconnect()
        await ctx.send("⏹️ 停止して切断したよ")

    @commands.command(name="pause")
    async def pause(self, ctx):
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await ctx.send("⏸️ 一時停止")
        else:
            await ctx.send("❌ 再生中じゃないよ")

    @commands.command(name="resume")
    async def resume(self, ctx):
        vc = ctx.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await ctx.send("▶️ 再開")
        else:
            await ctx.send("❌ 一時停止中じゃないよ")

    # ─── キュー表示 ──────────────────────────────────────────────

    @commands.command(name="queue", aliases=["q"])
    async def queue_cmd(self, ctx):
        current = self.current.get(ctx.guild.id)
        q = self.queues[ctx.guild.id]
        if not current and not q:
            await ctx.send("キューは空だよ")
            return
        embed = discord.Embed(title="🎵 再生キュー", color=discord.Color.green())
        if current:
            embed.add_field(
                name="▶️ 再生中",
                value=f"[{current['title']}]({current['webpage_url']}) `{fmt_duration(current['duration'])}`",
                inline=False,
            )
        lines = [
            f"`{i+1}.` [{t['title']}]({t['webpage_url']}) `{fmt_duration(t['duration'])}` — {t['requester']}"
            for i, t in enumerate(q)
        ]
        if lines:
            embed.add_field(name="次の曲", value="\n".join(lines[:10]), inline=False)
            if len(lines) > 10:
                embed.set_footer(text=f"他 {len(lines)-10} 曲")
        await ctx.send(embed=embed)

    @commands.command(name="nowplaying", aliases=["np"])
    async def nowplaying(self, ctx):
        current = self.current.get(ctx.guild.id)
        if not current:
            await ctx.send("今は何も流れてないよ")
            return
        embed = discord.Embed(
            title="▶️ 再生中",
            description=f"[{current['title']}]({current['webpage_url']})",
            color=discord.Color.green(),
        )
        embed.add_field(name="長さ", value=fmt_duration(current["duration"]), inline=True)
        embed.add_field(name="リクエスト", value=current["requester"], inline=True)
        await ctx.send(embed=embed)

    # ─── エラーハンドラ ──────────────────────────────────────────

    @play.error
    async def play_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("曲名かURLを入力してね。例: `!play 夜に駆ける`")

    # ─── イベント ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        vc = member.guild.voice_client
        if vc is None or before.channel != vc.channel:
            return
        if any(not m.bot for m in vc.channel.members):
            return
        self.queues[member.guild.id].clear()
        self.current.pop(member.guild.id, None)
        await vc.disconnect()
        channel = self.text_channels.get(member.guild.id)
        if channel:
            await channel.send("👋 VCに誰もいなくなったので切断したよ")

    # ─── 内部処理 ────────────────────────────────────────────────

    async def _play_next(self, guild: discord.Guild):
        vc = guild.voice_client
        if vc is None:
            return

        q = self.queues[guild.id]
        if not q:
            self.current.pop(guild.id, None)
            return

        item = q.popleft()
        self.current[guild.id] = item

        # 再生時に毎回ストリームURLを新鮮に取得
        track = await fetch_track(item["webpage_url"])
        if track is None:
            channel = self.text_channels.get(guild.id) or guild.text_channels[0]
            await channel.send(f"⚠️ **{item['title']}** の取得に失敗したのでスキップ")
            self.current.pop(guild.id, None)
            asyncio.create_task(self._play_next(guild))
            return

        source = discord.FFmpegPCMAudio(track["stream_url"], **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=0.5)

        def after_play(e):
            if e:
                logger.error(f"再生エラー: {e}")
            asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)

        vc.play(source, after=after_play)

        channel = self.text_channels.get(guild.id) or guild.text_channels[0]
        embed = discord.Embed(
            title="🎵 再生開始",
            description=f"[{item['title']}]({item['webpage_url']})",
            color=discord.Color.green(),
        )
        embed.add_field(name="長さ", value=fmt_duration(item["duration"]), inline=True)
        embed.add_field(name="リクエスト", value=item["requester"], inline=True)
        await channel.send(embed=embed)
        logger.info(f"playing: {item['title']}")


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
