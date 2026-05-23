import asyncio
from collections import defaultdict, deque
from logging import getLogger

import discord
from discord.ext import commands
import yt_dlp

logger = getLogger(__name__)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -ar 48000 -ac 2",
}


async def fetch_track(query: str) -> dict | None:
    loop = asyncio.get_event_loop()
    def _extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "url": info["url"],
                "title": info.get("title", "不明"),
                "duration": info.get("duration", 0),
                "webpage_url": info.get("webpage_url", ""),
            }
    try:
        return await loop.run_in_executor(None, _extract)
    except Exception:
        logger.exception(f"yt-dlp エラー: {query}")
        return None


def fmt_duration(sec: int) -> str:
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues: dict[int, deque] = defaultdict(deque)
        self.current: dict[int, dict] = {}
        self.text_channels: dict[int, discord.TextChannel] = {}

    # ─── コマンド ────────────────────────────────────────────────

    @commands.command(name="play", aliases=["p"])
    async def play(self, ctx, *, query: str):
        if ctx.author.voice is None:
            await ctx.send("❌ VCに接続してから使ってね")
            return

        if ctx.guild.voice_client is None:
            await ctx.author.voice.channel.connect(self_deaf=True)
        elif ctx.guild.voice_client.channel != ctx.author.voice.channel:
            await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
        self.text_channels[ctx.guild.id] = ctx.channel

        async with ctx.typing():
            track = await fetch_track(query)

        if track is None:
            await ctx.send("❌ 見つからなかった")
            return

        track["requester"] = ctx.author.display_name
        self.queues[ctx.guild.id].append(track)

        if ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused():
            embed = discord.Embed(title="📋 キューに追加", color=discord.Color.blue())
            embed.add_field(name="曲名", value=f"[{track['title']}]({track['webpage_url']})", inline=False)
            embed.add_field(name="長さ", value=fmt_duration(track["duration"]), inline=True)
            embed.set_footer(text=f"リクエスト: {track['requester']}")
            await ctx.send(embed=embed)
        else:
            await self._play_next(ctx.guild)

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
    async def on_voice_state_update(self, member, before, after):
        if member.id != self.bot.user.id:
            return
        # Bot unexpectedly left voice while something was playing
        if before.channel is None or after.channel is not None:
            return
        guild = before.channel.guild
        if self.current.get(guild.id) is None:
            return  # Queue was already cleared (e.g. !stop), intentional disconnect
        self.queues[guild.id].appendleft(self.current.pop(guild.id))
        await asyncio.sleep(5)
        try:
            await before.channel.connect(self_deaf=True)
            await self._play_next(guild)
        except Exception:
            logger.exception("音声再接続失敗")
            channel = self.text_channels.get(guild.id)
            if channel:
                await channel.send("❌ ボイスチャンネルへの再接続に失敗しました")

    # ─── 内部処理 ────────────────────────────────────────────────

    async def _play_next(self, guild: discord.Guild):
        vc = guild.voice_client
        if vc is None:
            return

        q = self.queues[guild.id]
        if not q:
            self.current.pop(guild.id, None)
            return

        track = q.popleft()
        self.current[guild.id] = track

        source = discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=0.5)

        def after(e):
            if e:
                logger.error(f"再生エラー: {e}")
            asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)

        vc.play(source, after=after)

        channel = self.text_channels.get(guild.id) or guild.text_channels[0]
        embed = discord.Embed(
            title="🎵 再生開始",
            description=f"[{track['title']}]({track['webpage_url']})",
            color=discord.Color.green(),
        )
        embed.add_field(name="長さ", value=fmt_duration(track["duration"]), inline=True)
        embed.add_field(name="リクエスト", value=track["requester"], inline=True)
        await channel.send(embed=embed)
        logger.info(f"playing: {track['title']}")


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
