import re
import random
from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)
SHUKATSU_KANJI = re.compile(r".*就.*活.*")
SHUKATSU_KANA = re.compile(r".*し.*ゅ.*う.*か.*つ.*")

# ─── ヘルプUI ────────────────────────────────────────────────────

HELP_SECTIONS = {
    "home": {
        "label": "🏠 ホーム",
        "color": 0x5865F2,
        "description": None,
    },
    "voice": {
        "label": "🎤 読み上げ",
        "color": 0x57F287,
        "cmds": [
            ("{p}join",          "VCに接続して読み上げを開始"),
            ("{p}leave",         "VCから切断・読み上げ終了"),
            ("{p}voice",         "自分の読み上げボイスをプルダウンで変更"),
        ],
    },
    "music": {
        "label": "🎵 音楽再生",
        "color": 0xFEE75C,
        "cmds": [
            ("{p}play [曲名/URL]", "YouTube から再生（キューに追加）"),
            ("{p}skip",           "今の曲をスキップ"),
            ("{p}stop",           "停止してVC切断"),
            ("{p}pause",          "一時停止"),
            ("{p}resume",         "再開"),
            ("{p}queue",          "再生キューを表示"),
            ("{p}np",             "再生中の曲を表示"),
        ],
    },
    "games": {
        "label": "🎮 ゲーム",
        "color": 0xED4245,
        "cmds": [
            ("{p}janken グー",     "じゃんけん（グー / チョキ / パー）"),
            ("{p}omikuji",        "おみくじ（大吉〜大凶）"),
            ("{p}slot",           "スロット"),
            ("{p}8ball [質問]",   "8ボール占い"),
            ("{p}dice [表記]",    "ダイスロール（例: 2d6）"),
            ("{p}coin",           "コイントス"),
            ("{p}choose [選択肢]","カンマ区切りからランダムに1つ選ぶ"),
        ],
    },
    "notify": {
        "label": "🔔 通知・リマインダー",
        "color": 0xEB459E,
        "cmds": [
            ("{p}notify",         "接続中VCへの入室通知を設定"),
            ("{p}notify_remove",  "入室通知を解除"),
            ("{p}notify_list",    "通知設定一覧を表示"),
            ("{p}remind [時間] [内容]", "指定時間後にリマインド（例: 10m / 2h / 1d）"),
        ],
    },
    "weather": {
        "label": "🌤️ 天気",
        "color": 0x5DADE2,
        "cmds": [
            ("{p}weather [都市名]", "天気情報を表示（省略でデフォルト拠点）"),
            ("あつい / さむい",     "気温に合わせてBotがリアクション"),
        ],
    },
    "dict": {
        "label": "📖 辞書・AI",
        "color": 0x9B59B6,
        "cmds": [
            ("{p}dict_add [単語] [読み]", "読み上げ辞書に単語を追加"),
            ("{p}dict_del [単語]",        "辞書から削除"),
            ("{p}dict_list",              "辞書一覧を表示"),
            ("{p}ai [メッセージ]",        "AIと会話（履歴あり）"),
            ("{p}ai_reset",               "会話履歴をリセット"),
            ("@Botをメンション",           "メンションでAIに話しかける"),
        ],
    },
    "util": {
        "label": "🛠️ ユーティリティ",
        "color": 0x95A5A6,
        "cmds": [
            ("{p}poll [質問] [選択肢]", "投票を作成（最大10択）"),
            ("{p}quickpoll [質問]",     "👍 / 👎 の簡易投票"),
            ("{p}userinfo [メンバー]",  "ユーザー情報を表示"),
            ("{p}serverinfo",           "サーバー情報を表示"),
            ("{p}avatar [メンバー]",    "アバターを表示"),
            ("{p}ping",                 "Botのレイテンシを確認"),
        ],
    },
}


def build_home_embed(p: str, bot: discord.Client) -> discord.Embed:
    embed = discord.Embed(
        title="風越Bot — コマンド一覧",
        description=(
            "カテゴリをセレクトメニューから選ぶと詳細が見られるよ\n\n"
            f"**🎤 読み上げ** — VOICEVOX音声合成\n"
            f"**🎵 音楽再生** — YouTube再生\n"
            f"**🎮 ゲーム** — じゃんけん・スロットなど\n"
            f"**🔔 通知・リマインダー** — VC入室通知・時間指定通知\n"
            f"**🌤️ 天気** — 気温・天気情報\n"
            f"**📖 辞書・AI** — 読み上げ辞書・Gemini AI\n"
            f"**🛠️ ユーティリティ** — 投票・ユーザー情報など"
        ),
        color=0x5865F2,
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text=f"プレフィックス: {p}  |  就活の話はしないでください😡")
    return embed


def build_category_embed(key: str, p: str) -> discord.Embed:
    section = HELP_SECTIONS[key]
    cmds = section["cmds"]
    lines = "\n".join(
        f"`{cmd.replace('{p}', p)}` — {desc}" for cmd, desc in cmds
    )
    embed = discord.Embed(
        title=section["label"],
        description=lines,
        color=section["color"],
    )
    embed.set_footer(text=f"プレフィックス: {p}  |  就活の話はしないでください😡")
    return embed


def build_help_embed(p: str, bot: discord.Client):
    embed = build_home_embed(p, bot)
    view = HelpView(p, bot)
    return embed, view


class HelpSelect(discord.ui.Select):
    def __init__(self, p: str, bot: discord.Client):
        self.p = p
        self.bot = bot
        options = [discord.SelectOption(label="🏠 ホーム", value="home")] + [
            discord.SelectOption(label=v["label"], value=k)
            for k, v in HELP_SECTIONS.items() if k != "home"
        ]
        super().__init__(placeholder="カテゴリを選んでね", options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if key == "home":
            embed = build_home_embed(self.p, self.bot)
        else:
            embed = build_category_embed(key, self.p)
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self, p: str, bot: discord.Client):
        super().__init__(timeout=120)
        self.add_item(HelpSelect(p, bot))


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
        embed, view = build_help_embed(p, self.bot)
        await ctx.send(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        content = message.content
        if re.fullmatch(SHUKATSU_KANJI, content) or re.fullmatch(SHUKATSU_KANA, content):
            await message.channel.send("就活の話はしないでください😡")


async def setup(bot):
    await bot.add_cog(UtilityCog(bot))
