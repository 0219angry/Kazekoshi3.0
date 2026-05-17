import sys
import configparser
from logging import getLogger

import discord
from discord import app_commands
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
    "あなたはDiscordサーバーのフレンドリーなアシスタントです。"
    "ユーザーの質問に対して、簡潔で分かりやすい日本語で答えてください。"
    "Markdownの装飾は最低限に留め、Discord上で読みやすい形式にしてください。"
)

MAX_HISTORY = 20


class AIChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = None
        self.history: dict[int, list] = {}

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

    # ─── スラッシュコマンド ───────────────────────────────────────

    @app_commands.command(name="ai", description="AIと会話します")
    @app_commands.describe(message="AIへのメッセージ")
    async def ai(self, interaction: discord.Interaction, message: str):
        if not self._available():
            await interaction.response.send_message("❌ AI機能は現在設定されていません", ephemeral=True)
            return

        await interaction.response.defer()
        reply = await self._chat(interaction.user.id, message)

        embed = discord.Embed(description=reply, color=discord.Color.purple())
        embed.set_author(
            name="🤖 Kazekoshi AI",
            icon_url=self.bot.user.display_avatar.url,
        )
        embed.set_footer(text=f"質問: {message[:60]}{'…' if len(message) > 60 else ''}")
        await interaction.followup.send(embed=embed)
        logger.info(f"{interaction.user} /ai: {message[:50]}")

    @app_commands.command(name="ai_reset", description="AIとの会話履歴をリセットします")
    async def ai_reset(self, interaction: discord.Interaction):
        self.history.pop(interaction.user.id, None)
        await interaction.response.send_message("🔄 会話履歴をリセットしました", ephemeral=True)

    # ─── メンションで会話 ────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not self._available():
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

    # ─── 内部処理 ────────────────────────────────────────────────

    def _available(self) -> bool:
        return self.client is not None

    async def _chat(self, user_id: int, message: str) -> str:
        if user_id not in self.history:
            self.history[user_id] = []

        self.history[user_id].append(
            types.Content(role="user", parts=[types.Part(text=message)])
        )

        if len(self.history[user_id]) > MAX_HISTORY * 2:
            self.history[user_id] = self.history[user_id][-(MAX_HISTORY * 2):]

        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.0-flash",
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
                contents=self.history[user_id],
            )
            reply = response.text
            self.history[user_id].append(
                types.Content(role="model", parts=[types.Part(text=reply)])
            )
            return reply
        except Exception:
            logger.exception("Gemini API 呼び出しエラー")
            return "AI応答の取得に失敗しました"


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))
