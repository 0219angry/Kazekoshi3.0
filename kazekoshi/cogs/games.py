import random
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)

OMIKUJI = [
    ("大吉", discord.Color.gold(),     "最高の運気！何でもうまくいく日だよ🌟"),
    ("中吉", discord.Color.green(),    "まあまあいい感じ。積極的にいこう👍"),
    ("小吉", discord.Color.blue(),     "悪くはないけど油断は禁物ね"),
    ("末吉", discord.Color.greyple(),  "ちょっと地味な日かも。地道にいこ"),
    ("凶",   discord.Color.orange(),   "うーん微妙な日。慎重にね⚠️"),
    ("大凶", discord.Color.red(),      "今日はおとなしくしてたほうがいいかも😇"),
]
OMIKUJI_WEIGHTS = [30, 25, 20, 15, 7, 3]

SLOT_EMOJIS = ["🍒", "🍋", "🍊", "🍇", "⭐", "💎", "7️⃣"]

EIGHTBALL_REPLIES = [
    "まあそうだろうね", "絶対そう", "たぶんね", "うん、そう思う",
    "なんとも言えない", "どっちでもいいんじゃない", "うーん微妙",
    "ないと思う", "たぶん違う", "絶対違う", "やめといたほうがいい",
]

JANKEN_MAP = {
    "グー": "✊", "ぐー": "✊",
    "チョキ": "✌️", "ちょき": "✌️",
    "パー": "🖐️", "ぱー": "🖐️",
}
JANKEN_WIN = {("グー", "チョキ"), ("チョキ", "パー"), ("パー", "グー")}


class GamesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="janken")
    async def janken(self, ctx, choice: str):
        normalized = {"ぐー": "グー", "ちょき": "チョキ", "ぱー": "パー"}.get(choice, choice)
        if normalized not in ("グー", "チョキ", "パー"):
            await ctx.send("グー / チョキ / パー のどれかで打ってね")
            return

        bot_choice = random.choice(["グー", "チョキ", "パー"])
        user_em = JANKEN_MAP[normalized]
        bot_em  = JANKEN_MAP[bot_choice]

        if normalized == bot_choice:
            result, color = "あいこ！", discord.Color.greyple()
        elif (normalized, bot_choice) in JANKEN_WIN:
            result, color = "あなたの勝ち！", discord.Color.green()
        else:
            result, color = "私の勝ち😏", discord.Color.red()

        embed = discord.Embed(title=f"✊ じゃんけん → **{result}**", color=color)
        embed.add_field(name=ctx.author.display_name, value=f"{user_em} {normalized}", inline=True)
        embed.add_field(name="風越Bot", value=f"{bot_em} {bot_choice}", inline=True)
        await ctx.send(embed=embed)
        logger.info(f"{ctx.author} janken: {normalized} vs {bot_choice} → {result}")

    @commands.command(name="omikuji")
    async def omikuji(self, ctx):
        fortune, color, msg = random.choices(OMIKUJI, weights=OMIKUJI_WEIGHTS, k=1)[0]
        embed = discord.Embed(title=f"🎋 おみくじ — **{fortune}**", description=msg, color=color)
        embed.set_footer(text=ctx.author.display_name)
        await ctx.send(embed=embed)
        logger.info(f"{ctx.author} omikuji: {fortune}")

    @commands.command(name="slot")
    async def slot(self, ctx):
        reels = [random.choice(SLOT_EMOJIS) for _ in range(3)]
        display = " | ".join(reels)

        if reels[0] == reels[1] == reels[2]:
            if reels[0] == "💎":
                result, color = "💎 ダイヤ三つ揃い！！神引き！！", discord.Color.gold()
            elif reels[0] == "7️⃣":
                result, color = "777！！ジャックポット！！", discord.Color.gold()
            else:
                result, color = "三つ揃い！やるじゃん！", discord.Color.green()
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            result, color = "二つ揃い。惜しい", discord.Color.blue()
        else:
            result, color = "はずれ〜", discord.Color.greyple()

        embed = discord.Embed(title="🎰 スロット", color=color)
        embed.add_field(name="結果", value=f"**{display}**", inline=False)
        embed.add_field(name="​", value=result, inline=False)
        embed.set_footer(text=ctx.author.display_name)
        await ctx.send(embed=embed)
        logger.info(f"{ctx.author} slot: {reels} → {result}")

    @commands.command(name="8ball", aliases=["8b", "はちぼ"])
    async def eightball(self, ctx, *, question: str):
        reply = random.choice(EIGHTBALL_REPLIES)
        embed = discord.Embed(color=discord.Color.purple())
        embed.add_field(name="🎱 質問", value=question, inline=False)
        embed.add_field(name="答え", value=f"**{reply}**", inline=False)
        embed.set_footer(text=ctx.author.display_name)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(GamesCog(bot))
