from logging import getLogger

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)

EMOJI_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


class PollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="poll", description="投票を作成します（最大10選択肢）")
    @app_commands.describe(
        question="投票の質問文",
        options="選択肢をカンマ区切りで入力（例: 選択肢A,選択肢B,選択肢C）",
    )
    async def poll(self, interaction: discord.Interaction, question: str, options: str):
        option_list = [o.strip() for o in options.split(",") if o.strip()]

        if len(option_list) < 2:
            await interaction.response.send_message(
                "❌ 選択肢を2つ以上カンマ区切りで入力してください（例: `A,B,C`）", ephemeral=True
            )
            return
        if len(option_list) > 10:
            await interaction.response.send_message("❌ 選択肢は最大10個です", ephemeral=True)
            return

        description = "\n".join(
            f"{EMOJI_NUMBERS[i]}　{opt}" for i, opt in enumerate(option_list)
        )
        embed = discord.Embed(
            title=f"📊 {question}",
            description=description,
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"作成者: {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)
        poll_msg = await interaction.original_response()

        for i in range(len(option_list)):
            await poll_msg.add_reaction(EMOJI_NUMBERS[i])

        logger.info(f"{interaction.user} created poll: {question}")

    @app_commands.command(name="quickpoll", description="👍 / 👎 の簡易投票を作成します")
    @app_commands.describe(question="投票の質問文")
    async def quickpoll(self, interaction: discord.Interaction, question: str):
        embed = discord.Embed(
            title=f"📊 {question}",
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"作成者: {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)
        poll_msg = await interaction.original_response()
        await poll_msg.add_reaction("👍")
        await poll_msg.add_reaction("👎")

        logger.info(f"{interaction.user} created quickpoll: {question}")


async def setup(bot: commands.Bot):
    await bot.add_cog(PollCog(bot))
