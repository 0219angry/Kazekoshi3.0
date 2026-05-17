import re
import random
from logging import getLogger

import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)

SHUKATSU_KANJI = re.compile(r".*就.*活.*")
SHUKATSU_KANA = re.compile(r".*し.*ゅ.*う.*か.*つ.*")


class UtilityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── スラッシュコマンド ───────────────────────────────────────

    @app_commands.command(name="ping", description="Botのレイテンシを確認します")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        color = discord.Color.green() if latency < 100 else discord.Color.orange()
        embed = discord.Embed(title="🏓 Pong!", description=f"レイテンシ: **{latency}ms**", color=color)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="ユーザー情報を表示します")
    @app_commands.describe(member="情報を表示するメンバー（省略すると自分）")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        embed = discord.Embed(title=f"👤 {member.display_name}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ユーザー名", value=str(member), inline=True)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Bot", value="✅" if member.bot else "❌", inline=True)
        embed.add_field(
            name="アカウント作成日",
            value=f"<t:{int(member.created_at.timestamp())}:D>",
            inline=True,
        )
        embed.add_field(
            name="サーバー参加日",
            value=f"<t:{int(member.joined_at.timestamp())}:D>" if member.joined_at else "不明",
            inline=True,
        )
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        embed.add_field(
            name=f"ロール ({len(roles)})",
            value=" ".join(roles) if roles else "なし",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="サーバー情報を表示します")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        embed = discord.Embed(title=f"🏠 {guild.name}", color=discord.Color.blue())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="ID", value=str(guild.id), inline=True)
        embed.add_field(
            name="オーナー",
            value=guild.owner.mention if guild.owner else "不明",
            inline=True,
        )
        embed.add_field(
            name="作成日",
            value=f"<t:{int(guild.created_at.timestamp())}:D>",
            inline=True,
        )
        embed.add_field(name="メンバー数", value=str(guild.member_count), inline=True)
        embed.add_field(name="テキストch数", value=str(len(guild.text_channels)), inline=True)
        embed.add_field(name="ボイスch数", value=str(len(guild.voice_channels)), inline=True)
        embed.add_field(name="ロール数", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="絵文字数", value=str(len(guild.emojis)), inline=True)
        embed.add_field(name="Boostレベル", value=f"Lv.{guild.premium_tier}", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="アバターを表示します")
    @app_commands.describe(member="アバターを表示するメンバー（省略すると自分）")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        embed = discord.Embed(title=f"🖼️ {member.display_name} のアバター", color=discord.Color.blue())
        embed.set_image(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="choose", description="複数の選択肢からランダムに1つ選びます")
    @app_commands.describe(options="選択肢をカンマ区切りで入力（例: ラーメン,寿司,焼肉）")
    async def choose(self, interaction: discord.Interaction, options: str):
        choices = [o.strip() for o in options.split(",") if o.strip()]
        if len(choices) < 2:
            await interaction.response.send_message("❌ 選択肢を2つ以上カンマ区切りで入力してください", ephemeral=True)
            return

        picked = random.choice(choices)
        embed = discord.Embed(
            title="🎯 選択結果",
            description=f"**{picked}**",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"選択肢: {', '.join(choices)}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="コマンド一覧を表示します")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📋 Kazekoshi v3.0 コマンド一覧",
            color=discord.Color.blue(),
        )

        sections = {
            "🎤 読み上げ": [
                ("/join", "VCに接続して読み上げ開始"),
                ("/leave", "VCから切断・終了"),
                ("/voice", "自分の読み上げボイスを変更"),
            ],
            "🎲 ダイス・ゲーム": [
                ("/dice [表記]", "ダイスを振る（例: `2d6`）"),
                ("/coin", "コインを投げる"),
                ("/choose [選択肢]", "選択肢からランダムに1つ選ぶ"),
            ],
            "🔔 通知": [
                ("/notify", "現在接続中VCの入室通知を設定"),
                ("/notify_remove", "入室通知を解除"),
                ("/notify_list", "通知設定一覧を表示"),
            ],
            "🌤️ 天気": [
                ("/weather [都市名]", "天気情報を表示（省略でデフォルト拠点）"),
                ("あつい / さむい など", "気温に合わせてリアクション"),
            ],
            "📖 辞書": [
                ("/dict_add [単語] [読み]", "辞書に単語を追加"),
                ("/dict_del [単語]", "辞書から単語を削除"),
                ("/dict_list", "辞書の内容を一覧表示"),
            ],
            "🤖 AI": [
                ("/ai [メッセージ]", "AIと会話（会話履歴あり）"),
                ("/ai_reset", "AIの会話履歴をリセット"),
                ("@Botをメンション", "メンションでAIに直接話しかける"),
            ],
            "📊 投票": [
                ("/poll [質問] [選択肢]", "投票を作成（最大10択）"),
                ("/quickpoll [質問]", "👍 / 👎 の簡易投票"),
            ],
            "⏰ リマインダー": [
                ("/remind [時間] [内容]", "指定時間後にリマインド\n例: `1h30m` `10m` `2d`"),
            ],
            "🛠️ ユーティリティ": [
                ("/userinfo [メンバー]", "ユーザー情報を表示"),
                ("/serverinfo", "サーバー情報を表示"),
                ("/avatar [メンバー]", "アバターを表示"),
                ("/ping", "Botのレイテンシを確認"),
            ],
        }

        for section, cmds in sections.items():
            value = "\n".join(f"`{cmd}` — {desc}" for cmd, desc in cmds)
            embed.add_field(name=section, value=value, inline=False)

        embed.set_footer(text="Kazekoshi v3.0 | 就活の話はしないでください😡")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── on_message（就活検出） ──────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        content = message.content
        if re.fullmatch(SHUKATSU_KANJI, content) or re.fullmatch(SHUKATSU_KANA, content):
            logger.info(f"shukatsu detected from {message.author}")
            await message.channel.send("就活の話はしないでください😡")


async def setup(bot: commands.Bot):
    await bot.add_cog(UtilityCog(bot))
