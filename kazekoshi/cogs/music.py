import asyncio
import json
import os
import random
from collections import defaultdict, deque
from logging import getLogger

import discord
from discord.ext import commands
import yt_dlp

logger = getLogger(__name__)

YTDL_SINGLE = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

YTDL_PLAYLIST = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -ar 48000 -ac 2",
}

MAX_HISTORY = 20


def _playlist_path(guild_id: int) -> str:
    return os.path.join("json", f"playlists_{guild_id}.json")


def _load_playlists(guild_id: int) -> dict:
    path = _playlist_path(guild_id)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_playlists(guild_id: int, data: dict):
    with open(_playlist_path(guild_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def fetch_track(query: str) -> dict | None:
    loop = asyncio.get_event_loop()
    def _extract():
        with yt_dlp.YoutubeDL(YTDL_SINGLE) as ydl:
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


async def fetch_playlist_tracks(url: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    def _extract():
        with yt_dlp.YoutubeDL(YTDL_PLAYLIST) as ydl:
            info = ydl.extract_info(url, download=False)
            tracks = []
            for e in (info.get("entries") or []):
                if not e:
                    continue
                vid_id = e.get("id", "")
                webpage = e.get("url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "")
                tracks.append({
                    "url": webpage,
                    "title": e.get("title", "不明"),
                    "duration": e.get("duration", 0),
                    "webpage_url": webpage,
                })
            return tracks
    try:
        return await loop.run_in_executor(None, _extract)
    except Exception:
        logger.exception(f"プレイリスト取得エラー: {url}")
        return []


def fmt_duration(sec: int) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _is_playlist_url(query: str) -> bool:
    # 動画URLに list= が付いている場合は単曲として扱う（プレイリストから共有した動画など）
    if "list=" not in query:
        return False
    if "watch?v=" in query or "youtu.be/" in query:
        return False
    return True


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues: dict[int, deque] = defaultdict(deque)
        self.current: dict[int, dict] = {}
        self.text_channels: dict[int, discord.TextChannel] = {}
        self.loop_mode: dict[int, str] = defaultdict(lambda: "off")  # "off" / "one" / "queue"
        self.history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
        self.volumes: dict[int, float] = defaultdict(lambda: 0.5)
        self._skip_flags: set[int] = set()  # ループ中でも強制スキップするギルドID

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

        if _is_playlist_url(query):
            await self._handle_playlist(ctx, query)
        else:
            await self._handle_single(ctx, query)

    async def _handle_single(self, ctx, query: str):
        async with ctx.typing():
            track = await fetch_track(query)
        if track is None:
            await ctx.send("❌ 見つからなかった")
            return
        track["requester"] = ctx.author.display_name
        self.queues[ctx.guild.id].append(track)
        vc = ctx.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            embed = discord.Embed(title="📋 キューに追加", color=discord.Color.blue())
            embed.add_field(name="曲名", value=f"[{track['title']}]({track['webpage_url']})", inline=False)
            embed.add_field(name="長さ", value=fmt_duration(track["duration"]), inline=True)
            embed.set_footer(text=f"リクエスト: {track['requester']}")
            await ctx.send(embed=embed)
        else:
            await self._play_next(ctx.guild)

    async def _handle_playlist(self, ctx, url: str):
        msg = await ctx.send("⏳ プレイリストを読み込み中...")
        async with ctx.typing():
            tracks = await fetch_playlist_tracks(url)
        if not tracks:
            await msg.edit(content="❌ プレイリストの読み込みに失敗した")
            return
        for t in tracks:
            t["requester"] = ctx.author.display_name
            self.queues[ctx.guild.id].append(t)
        await msg.edit(content=f"📋 **{len(tracks)}曲** をキューに追加したよ")
        vc = ctx.guild.voice_client
        if not (vc.is_playing() or vc.is_paused()):
            await self._play_next(ctx.guild)

    # ─── 基本操作 ────────────────────────────────────────────────

    @commands.command(name="skip", aliases=["s"])
    async def skip(self, ctx):
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_playing():
            await ctx.send("❌ 再生中じゃないよ")
            return
        self._skip_flags.add(ctx.guild.id)
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
        self.loop_mode[ctx.guild.id] = "off"
        self._skip_flags.discard(ctx.guild.id)
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

    # ─── キュー操作 ──────────────────────────────────────────────

    @commands.command(name="queue", aliases=["q"])
    async def queue_cmd(self, ctx):
        current = self.current.get(ctx.guild.id)
        q = self.queues[ctx.guild.id]
        if not current and not q:
            await ctx.send("キューは空だよ")
            return
        loop_label = {"one": " 🔂", "queue": " 🔁"}.get(self.loop_mode[ctx.guild.id], "")
        embed = discord.Embed(title=f"🎵 再生キュー{loop_label}", color=discord.Color.green())
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

    @commands.command(name="remove", aliases=["rm"])
    async def remove(self, ctx, index: int):
        q = self.queues[ctx.guild.id]
        if not 1 <= index <= len(q):
            await ctx.send(f"❌ 1〜{len(q)} の番号で指定してね")
            return
        lst = list(q)
        removed = lst.pop(index - 1)
        self.queues[ctx.guild.id] = deque(lst)
        await ctx.send(f"🗑️ **{removed['title']}** をキューから削除したよ")

    @commands.command(name="shuffle")
    async def shuffle(self, ctx):
        q = self.queues[ctx.guild.id]
        if len(q) < 2:
            await ctx.send("❌ シャッフルするには2曲以上必要だよ")
            return
        lst = list(q)
        random.shuffle(lst)
        self.queues[ctx.guild.id] = deque(lst)
        await ctx.send(f"🔀 **{len(lst)}曲** をシャッフルしたよ")

    # ─── ループ・音量 ────────────────────────────────────────────

    @commands.command(name="loop", aliases=["l"])
    async def loop_cmd(self, ctx, mode: str = ""):
        gid = ctx.guild.id
        current = self.loop_mode[gid]
        if mode.lower() in ("queue", "q", "all"):
            if current == "queue":
                self.loop_mode[gid] = "off"
                await ctx.send("🔁 キューループ: **オフ**")
            else:
                self.loop_mode[gid] = "queue"
                await ctx.send("🔁 キューループ: **オン**")
        else:
            if current == "one":
                self.loop_mode[gid] = "off"
                await ctx.send("🔂 1曲ループ: **オフ**")
            else:
                self.loop_mode[gid] = "one"
                await ctx.send("🔂 1曲ループ: **オン**")

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx, vol: int):
        if not 0 <= vol <= 100:
            await ctx.send("❌ 音量は 0〜100 で指定してね")
            return
        self.volumes[ctx.guild.id] = vol / 100
        vc = ctx.guild.voice_client
        if vc and vc.source:
            vc.source.volume = vol / 100
        await ctx.send(f"🔊 音量を **{vol}%** にしたよ")

    # ─── 表示 ────────────────────────────────────────────────────

    @commands.command(name="nowplaying", aliases=["np"])
    async def nowplaying(self, ctx):
        current = self.current.get(ctx.guild.id)
        if not current:
            await ctx.send("今は何も流れてないよ")
            return
        loop_label = {"one": " 🔂", "queue": " 🔁"}.get(self.loop_mode[ctx.guild.id], "")
        embed = discord.Embed(
            title=f"▶️ 再生中{loop_label}",
            description=f"[{current['title']}]({current['webpage_url']})",
            color=discord.Color.green(),
        )
        embed.add_field(name="長さ", value=fmt_duration(current["duration"]), inline=True)
        embed.add_field(name="リクエスト", value=current["requester"], inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="history", aliases=["hist"])
    async def history_cmd(self, ctx):
        hist = self.history[ctx.guild.id]
        if not hist:
            await ctx.send("再生履歴がないよ")
            return
        embed = discord.Embed(title="📜 再生履歴（新しい順）", color=discord.Color.blurple())
        lines = [
            f"`{i+1}.` [{t['title']}]({t['webpage_url']}) `{fmt_duration(t['duration'])}` — {t['requester']}"
            for i, t in enumerate(reversed(list(hist)))
        ]
        embed.description = "\n".join(lines[:15])
        if len(hist) > 15:
            embed.set_footer(text="最新15件を表示")
        await ctx.send(embed=embed)

    # ─── プレイリスト ─────────────────────────────────────────────

    @commands.group(name="playlist", aliases=["pl"], invoke_without_command=True)
    async def playlist_group(self, ctx):
        await ctx.send(
            "使い方:\n"
            "`!playlist save <名前>` — 現在のキューを保存\n"
            "`!playlist load <名前>` — プレイリストをキューに追加\n"
            "`!playlist list` — 一覧表示\n"
            "`!playlist delete <名前>` — 削除"
        )

    @playlist_group.command(name="save")
    async def playlist_save(self, ctx, *, name: str):
        current = self.current.get(ctx.guild.id)
        tracks = ([current] if current else []) + list(self.queues[ctx.guild.id])
        if not tracks:
            await ctx.send("❌ 保存する曲がないよ")
            return
        playlists = _load_playlists(ctx.guild.id)
        playlists[name] = [
            {"title": t["title"], "webpage_url": t["webpage_url"], "duration": t["duration"]}
            for t in tracks
        ]
        _save_playlists(ctx.guild.id, playlists)
        await ctx.send(f"💾 **{name}** に **{len(tracks)}曲** を保存したよ")

    @playlist_group.command(name="load")
    async def playlist_load(self, ctx, *, name: str):
        if ctx.author.voice is None:
            await ctx.send("❌ VCに接続してから使ってね")
            return
        playlists = _load_playlists(ctx.guild.id)
        if name not in playlists:
            await ctx.send(f"❌ **{name}** というプレイリストは見つからないよ")
            return
        if ctx.guild.voice_client is None:
            await ctx.author.voice.channel.connect()
        self.text_channels[ctx.guild.id] = ctx.channel
        tracks = playlists[name]
        for t in tracks:
            self.queues[ctx.guild.id].append({
                "url": t["webpage_url"],  # _play_next で再取得する
                "title": t["title"],
                "duration": t["duration"],
                "webpage_url": t["webpage_url"],
                "requester": ctx.author.display_name,
            })
        await ctx.send(f"📂 **{name}** から **{len(tracks)}曲** をキューに追加したよ")
        vc = ctx.guild.voice_client
        if not (vc.is_playing() or vc.is_paused()):
            await self._play_next(ctx.guild)

    @playlist_group.command(name="list")
    async def playlist_list(self, ctx):
        playlists = _load_playlists(ctx.guild.id)
        if not playlists:
            await ctx.send("保存済みプレイリストはないよ")
            return
        embed = discord.Embed(title="💿 プレイリスト一覧", color=discord.Color.purple())
        embed.description = "\n".join(f"**{n}** — {len(ts)}曲" for n, ts in playlists.items())
        await ctx.send(embed=embed)

    @playlist_group.command(name="delete", aliases=["del"])
    async def playlist_delete(self, ctx, *, name: str):
        playlists = _load_playlists(ctx.guild.id)
        if name not in playlists:
            await ctx.send(f"❌ **{name}** というプレイリストは見つからないよ")
            return
        del playlists[name]
        _save_playlists(ctx.guild.id, playlists)
        await ctx.send(f"🗑️ **{name}** を削除したよ")

    # ─── エラーハンドラ ──────────────────────────────────────────

    @play.error
    async def play_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("曲名かURLを入力してね。例: `!play 夜に駆ける`")

    @volume.error
    async def volume_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ 音量は数字で指定してね。例: `!volume 80`")

    @remove.error
    async def remove_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ 番号を数字で指定してね。例: `!remove 3`")

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
        self.loop_mode[member.guild.id] = "off"
        self._skip_flags.discard(member.guild.id)
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
        skip_once = guild.id in self._skip_flags
        if skip_once:
            self._skip_flags.discard(guild.id)
        effective_loop = "off" if skip_once else self.loop_mode[guild.id]
        current = self.current.get(guild.id)

        if effective_loop == "one" and current:
            # 同じ曲を再生し直す（ストリームURLは期限切れなので再取得）
            refreshed = await fetch_track(current["webpage_url"])
            track = {**current, "url": refreshed["url"]} if refreshed else current
        elif effective_loop == "queue" and current:
            # 現在の曲をキュー末尾に移してから次を取り出す
            self.queues[guild.id].append(current)
            if not q:
                self.current.pop(guild.id, None)
                return
            track = q.popleft()
            self.current[guild.id] = track
        else:
            if not q:
                self.current.pop(guild.id, None)
                return
            track = q.popleft()
            self.current[guild.id] = track

        # ストリームURLを確保（プレイリスト由来は再取得が必要）
        stream_url = track.get("url", "")
        if not stream_url or stream_url == track.get("webpage_url", ""):
            refreshed = await fetch_track(track["webpage_url"])
            if refreshed:
                stream_url = refreshed["url"]
                track["url"] = stream_url

        # 取得失敗時はスキップして次の曲へ
        if not stream_url or stream_url == track.get("webpage_url", ""):
            channel = self.text_channels.get(guild.id) or guild.text_channels[0]
            await channel.send(f"⚠️ **{track['title']}** の再生情報を取得できなかったのでスキップ")
            if self.current.get(guild.id) is track:
                self.current.pop(guild.id, None)
            asyncio.create_task(self._play_next(guild))
            return

        # 履歴に追加（1曲ループ中は重複させない）
        if effective_loop != "one":
            self.history[guild.id].append(track)

        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=self.volumes[guild.id])

        def after(e):
            if e:
                logger.error(f"再生エラー: {e}")
            asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)

        vc.play(source, after=after)

        channel = self.text_channels.get(guild.id) or guild.text_channels[0]
        loop_label = {"one": " 🔂", "queue": " 🔁"}.get(self.loop_mode[guild.id], "")
        embed = discord.Embed(
            title=f"🎵 再生開始{loop_label}",
            description=f"[{track['title']}]({track['webpage_url']})",
            color=discord.Color.green(),
        )
        embed.add_field(name="長さ", value=fmt_duration(track["duration"]), inline=True)
        embed.add_field(name="リクエスト", value=track["requester"], inline=True)
        await channel.send(embed=embed)
        logger.info(f"playing: {track['title']}")


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
