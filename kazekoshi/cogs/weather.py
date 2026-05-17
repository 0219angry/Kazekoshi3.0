import sys
import configparser
from logging import getLogger

import requests
import discord
from discord import app_commands
from discord.ext import commands

logger = getLogger(__name__)

TEMP_TRIGGERS = {"あつい", "あつくない", "さむい", "さむくない"}


class WeatherCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        try:
            config = configparser.ConfigParser()
            config.read("config.ini", encoding="UTF-8")
            self.api_key = config["DEFAULT"]["OPEN_WEATHER_MAP_TOKEN"]
            # config で拠点を変更できるようにした
            self.default_lat = config["DEFAULT"].get("DEFAULT_LAT", "35.6895")
            self.default_lon = config["DEFAULT"].get("DEFAULT_LON", "139.6917")
            self.default_city_name = config["DEFAULT"].get("DEFAULT_CITY", "東京")
        except Exception:
            logger.exception("config.ini の読み込みに失敗しました")
            sys.exit(1)

    # ─── 内部: API呼び出し ───────────────────────────────────────

    def _fetch_by_latlon(self, lat: str, lon: str) -> dict:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric", "lang": "ja"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def _fetch_by_city(self, city: str) -> dict:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": self.api_key, "units": "metric", "lang": "ja"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def _build_embed(self, data: dict) -> discord.Embed:
        temp = data["main"]["temp"]
        feels_like = data["main"]["feels_like"]
        temp_min = data["main"]["temp_min"]
        temp_max = data["main"]["temp_max"]
        humidity = data["main"]["humidity"]
        description = data["weather"][0]["description"]
        city_name = data["name"]
        wind_speed = data.get("wind", {}).get("speed", "—")

        embed = discord.Embed(title=f"🌤️ {city_name} の天気", color=discord.Color.blue())
        embed.add_field(name="天気", value=description, inline=True)
        embed.add_field(name="気温", value=f"{temp:.1f}℃", inline=True)
        embed.add_field(name="体感温度", value=f"{feels_like:.1f}℃", inline=True)
        embed.add_field(name="最低 / 最高", value=f"{temp_min:.1f}℃ / {temp_max:.1f}℃", inline=True)
        embed.add_field(name="湿度", value=f"{humidity}%", inline=True)
        embed.add_field(name="風速", value=f"{wind_speed} m/s", inline=True)
        return embed

    # ─── スラッシュコマンド ───────────────────────────────────────

    @app_commands.command(name="weather", description="天気情報を表示します")
    @app_commands.describe(city="都市名（省略するとデフォルト拠点の天気を表示）")
    async def weather(self, interaction: discord.Interaction, city: str = None):
        await interaction.response.defer()
        try:
            data = self._fetch_by_city(city) if city else self._fetch_by_latlon(self.default_lat, self.default_lon)
            embed = self._build_embed(data)
            await interaction.followup.send(embed=embed)
        except requests.HTTPError:
            await interaction.followup.send(f"❌ 都市が見つかりませんでした: `{city}`", ephemeral=True)
        except Exception:
            logger.exception("天気取得エラー")
            await interaction.followup.send("❌ 天気情報の取得に失敗しました", ephemeral=True)

    # ─── on_message (あつい / さむい) ────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.content not in TEMP_TRIGGERS:
            return

        try:
            data = self._fetch_by_latlon(self.default_lat, self.default_lon)
            temp = data["main"]["temp"]
            reply = self._build_temp_reply(message, temp)
            await message.channel.send(reply)
        except Exception:
            logger.exception("天気応答エラー")

    def _build_temp_reply(self, message: discord.Message, temp: float) -> str:
        mention = message.author.mention
        t = f"（{temp:.1f}℃）"

        if message.content == "あつい":
            return (
                f"{mention}、あついね🥵🥵🥵{t}" if temp > 25
                else f"{mention}、あつくないね😅😅😅{t}"
            )
        if message.content == "あつくない":
            return (
                f"{mention}、あつくなくないね🥵🥵🥵{t}" if temp > 25
                else f"{mention}、あつくないね😅😅😅{t}"
            )
        if message.content == "さむい":
            return (
                f"{mention}、さむいね🥶🥶🥶{t}" if temp < 12
                else f"{mention}、さむくないね😅😅😅{t}"
            )
        if message.content == "さむくない":
            return (
                f"{mention}、さむくなくないね🥶🥶🥶{t}" if temp < 12
                else f"{mention}、さむくないね🌟🌟🌟{t}"
            )
        return ""


async def setup(bot: commands.Bot):
    await bot.add_cog(WeatherCog(bot))
