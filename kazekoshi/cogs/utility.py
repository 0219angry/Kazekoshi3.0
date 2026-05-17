import re
import random
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)
SHUKATSU_KANJI = re.compile(r".*就.*活.*")
SHUKATSU_KANA = re.compile(r".*し.*ゅ.*う.*か.*つ.*")


class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx):
        latency = round(self.bot.latency * 1000)
        color = discord.Color.green() if latency < 100 else discord.Color.orange()
        await ctx.send(embed=discord.Embed(title="🏓 Pong!", description=f"レイテンシ: **{latency}ms**", color=color))

    @commands.command(name="userinfo")
    async def userinfo(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        embed = discord.Embed(title=f"👤 {member.display_name}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ユーザー名", value=str(member), inline=True)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Bot", value="✅" if member.bot else "❌", inline=True)
        embed.add_field(name="アカウント作成日", value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="サーバー参加日", value=f"<t:{int(member.joined_at.timestamp())}:D>" if member.joined_at else "不明", inline=True)
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        embed.add_field(name=f"ロール ({len(roles)})", value=" ".join(roles) if roles else "なし", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="serverinfo")
    async def serverinfo(self, ctx):
        guild = ctx.guild
        embed = discord.Embed(title=f"🏠 {guild.name}", color=discord.Color.blue())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="ID", value=str(guild.id), inline=True)
        embed.add_field(name="オーナー", value=guild.owner.mention if guild.owner else "不明", inline=True)
        embed.add_field(name="作成日", value=f"<t:{int(guild.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="メンバー数", value=str(guild.member_count), inline=True)
        embed.add_field(name="テキストch数", value=str(len(guild.text_channels)), inline=True)
        embed.add_field(name="ボイスch数", value=str(len(guild.voice_channels)), inline=True)
        embed.add_field(name="ロール数", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="絵文字数", value=str(len(guild.emojis)), inline=True)
        embed.add_field(name="Boostレベル", value=f"Lv.{guild.premium_tier}", inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="avatar")
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        embed = discord.Embed(title=f"🖼️ {member.display_name} のアバター", color=discord.Color.blue())
        embed.set_image(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="choose")
    async def choose(self, ctx, *, options: str):
        choices = [o.strip() for o in options.split(",") if o.strip()]
        if len(choices) < 2:
            await ctx.send("❌ 選択肢を2つ以上カンマ区切りで入力してください")
            return
        picked = random.choice(choices)
        embed = discord.Embed(title="🎯 選択結果", description=f"**{picked}**", color=discord.Color.gold())
        embed.set_footer(text=f"選択肢: {', '.join(choices)}")
        await ctx.send(embed=embed)

    @commands.command(name="help")
    async def help(self, ctx):
        p = self.bot.command_prefix
        embed = discord.Embed(title="📋 Kazekoshi v3.0 コマンド一覧", color=discord.Color.blue())
        sections = {
            "🎤 読み上げ": [
                (f"{p}join", "VCに接続して読み上げ開始"),
                (f"{p}leave", "VCから切断・終了"),
                (f"{p}voice", "自分の読み上げボイスを変更"),
            ],
            "🎲 ダイス・ゲーム": [
                (f"{p}dice [表記]", "ダイスを振る（例: `2d6`）"),
                (f"{p}coin", "コインを投げる"),
                (f"{p}choose [選択肢]", "選択肢からランダムに1つ選ぶ"),
            ],
            "🔔 通知": [
                (f"{p}notify", "現在接続中VCの入室通知を設定"),
                (f"{p}notify_remove", "入室通知を解除"),
                (f"{p}notify_list", "通知設定一覧を表示"),
            ],
            "🌤️ 天気": [
                (f"{p}weather [都市名]", "天気情報を表示"),
                ("あつい / さむい など", "気温に合わせてリアクション"),
            ],
            "📖 辞書": [
                (f"{p}dict_add [単語] [読み]", "辞書に単語を追加"),
                (f"{p}dict_del [単語]", "辞書から単語を削除"),
                (f"{p}dict_list", "辞書の内容を一覧表示"),
            ],
            "🤖 AI": [
                (f"{p}ai [メッセージ]", "AIと会話（会話履歴あり）"),
                (f"{p}ai_reset", "AIの会話履歴をリセット"),
                ("@Botをメンション", "メンションでAIに直接話しかける"),
            ],
            "📊 投票": [
                (f"{p}poll [質問] [選択肢]", "投票を作成（最大10択）"),
                (f"{p}quickpoll [質問]", "👍 / 👎 の簡易投票"),
            ],
            "⏰ リマインダー": [
                (f"{p}remind [時間] [内容]", "指定時間後にリマインド"),
            ],
            "🛠️ ユーティリティ": [
                (f"{p}userinfo [メンバー]", "ユーザー情報を表示"),
                (f"{p}serverinfo", "サーバー情報を表示"),
                (f"{p}avatar [メンバー]", "アバターを表示"),
                (f"{p}ping", "Botのレイテンシを確認"),
            ],
        }
        for section, cmds in sections.items():
            value = "\n".join(f"`{cmd}` — {desc}" for cmd, desc in cmds)
            embed.add_field(name=section, value=value, inline=False)
        embed.set_footer(text="Kazekoshi v3.0 | 就活の話はしないでください😡")
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        content = message.content
        if re.fullmatch(SHUKATSU_KANJI, content) or re.fullmatch(SHUKATSU_KANA, content):
            await message.channel.send("就活の話はしないでください😡")


async def setup(bot):
    await bot.add_cog(UtilityCog(bot))
