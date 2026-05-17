from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)
EMOJI_NUMBERS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]


class PollCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="poll")
    async def poll(self, ctx, question: str, *, options: str):
        option_list = [o.strip() for o in options.split(",") if o.strip()]
        if len(option_list) < 2:
            await ctx.send("❌ 選択肢を2つ以上カンマ区切りで入力してください（例: `A,B,C`）")
            return
        if len(option_list) > 10:
            await ctx.send("❌ 選択肢は最大10個です")
            return
        description = "\n".join(f"{EMOJI_NUMBERS[i]}　{opt}" for i, opt in enumerate(option_list))
        embed = discord.Embed(title=f"📊 {question}", description=description, color=discord.Color.blue())
        embed.set_footer(text=f"作成者: {ctx.author.display_name}")
        poll_msg = await ctx.send(embed=embed)
        for i in range(len(option_list)):
            await poll_msg.add_reaction(EMOJI_NUMBERS[i])
        logger.info(f"{ctx.author} created poll: {question}")

    @commands.command(name="quickpoll")
    async def quickpoll(self, ctx, *, question: str):
        embed = discord.Embed(title=f"📊 {question}", color=discord.Color.blue())
        embed.set_footer(text=f"作成者: {ctx.author.display_name}")
        poll_msg = await ctx.send(embed=embed)
        await poll_msg.add_reaction("👍")
        await poll_msg.add_reaction("👎")
        logger.info(f"{ctx.author} created quickpoll: {question}")


async def setup(bot):
    await bot.add_cog(PollCog(bot))
