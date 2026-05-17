import sys
import configparser
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    logger.warning("google-genai パッケージが見つかりません。AI機能は無効です。")

SYSTEM_PROMPT = (
    "あなたは「風越」というDiscordサーバーに住みついたBot、風越（かぜこし）です。"
    "メンバーとは友達感覚でフランクに話してください。敬語は使わなくていいです。"
    "返答は短めにまとめ、くどくど説明しない。Discord上で読みやすい形式で。"
    "Markdownの装飾（**太字**や```コードブロック```）は必要なときだけ使う。"
    "就活の話題が出たら嫌そうにしてください。"
    "ユーモアがあって、ちょっとツッコミ気質。でも困ってる人には親身になる。"
)

MAX_HISTORY = 20


class AIChatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.client = None
        self.history = {}

        if not GENAI_AVAILABLE:
            return

        try:
            config = configparser.ConfigParser()
            config.read("config.ini", encoding="UTF-8")
            api_key = config["DEFAULT"].get("GEMINI_API_KEY", "")
            if not api_key or api_key == "your_gemini_api_key_here":
                logger.warning("GEMINI_API_KEY が未設定です（AI機能は無効）")
                return
            self.client = genai.Client(api_key=api_key)
            logger.info("Gemini APIクライアントを初期化しました")
        except Exception:
            logger.exception("GEMINI_API_KEY の読み込みに失敗しました（AI機能は無効）")

    @commands.command(name="ai")
    async def ai(self, ctx, *, message: str):
        if not self._available():
            await ctx.send("❌ AI機能は現在設定されていません")
            return
        async with ctx.typing():
            reply = await self._chat(ctx.author.id, message)
        embed = discord.Embed(description=reply, color=discord.Color.purple())
        embed.set_author(name="🤖 Kazekoshi AI", icon_url=self.bot.user.display_avatar.url)
        embed.set_footer(text=f"質問: {message[:60]}{'…' if len(message) > 60 else ''}")
        await ctx.send(embed=embed)
        logger.info(f"{ctx.author} !ai: {message[:50]}")

    @commands.command(name="ai_reset")
    async def ai_reset(self, ctx):
        self.history.pop(ctx.author.id, None)
        await ctx.send("🔄 会話履歴をリセットしました")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not self._available():
            return
        if self.bot.user not in message.mentions:
            return
        content = message.content.replace(f"<@{self.bot.user.id}>", "").strip()
        if not content:
            return
        async with message.channel.typing():
            reply = await self._chat(message.author.id, content)
            if len(reply) > 1900:
                reply = reply[:1900] + "\n…（省略）"
            await message.reply(reply)
        logger.info(f"{message.author} mention AI: {content[:50]}")

    def _available(self):
        return self.client is not None

    async def _chat(self, user_id, message):
        if user_id not in self.history:
            self.history[user_id] = []
        self.history[user_id].append(types.Content(role="user", parts=[types.Part(text=message)]))
        if len(self.history[user_id]) > MAX_HISTORY * 2:
            self.history[user_id] = self.history[user_id][-(MAX_HISTORY * 2):]
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
                contents=self.history[user_id],
            )
            reply = response.text
            self.history[user_id].append(types.Content(role="model", parts=[types.Part(text=reply)]))
            return reply
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                logger.warning("Gemini API レート制限")
                return "❌ AIのレート制限に達しました。しばらく待ってから再試行してください。"
            logger.exception("Gemini API 呼び出しエラー")
            return "❌ AI応答の取得に失敗しました"


async def setup(bot):
    await bot.add_cog(AIChatCog(bot))
