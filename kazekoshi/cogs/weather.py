import sys
import configparser
from logging import getLogger

import requests
import discord
from discord.ext import commands

logger = getLogger(__name__)
TEMP_TRIGGERS = {"あつい", "あつくない", "さむい", "さむくない"}


class WeatherCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        try:
            config = configparser.ConfigParser()
            config.read("config.ini", encoding="UTF-8")
            self.api_key = config["DEFAULT"]["OPEN_WEATHER_MAP_TOKEN"]
            self.default_lat = config["DEFAULT"].get("DEFAULT_LAT", "35.6895")
            self.default_lon = config["DEFAULT"].get("DEFAULT_LON", "139.6917")
            self.default_city_name = config["DEFAULT"].get("DEFAULT_CITY", "東京")
        except Exception:
            logger.exception("config.ini の読み込みに失敗しました")
            sys.exit(1)

    def _fetch_by_latlon(self, lat, lon):
        r = requests.get("https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric", "lang": "ja"}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _fetch_by_city(self, city):
        r = requests.get("https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": self.api_key, "units": "metric", "lang": "ja"}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _build_embed(self, data):
        m = data["main"]
        embed = discord.Embed(title=f"🌤️ {data['name']} の天気", color=discord.Color.blue())
        embed.add_field(name="天気", value=data["weather"][0]["description"], inline=True)
        embed.add_field(name="気温", value=f"{m['temp']:.1f}℃", inline=True)
        embed.add_field(name="体感温度", value=f"{m['feels_like']:.1f}℃", inline=True)
        embed.add_field(name="最低 / 最高", value=f"{m['temp_min']:.1f}℃ / {m['temp_max']:.1f}℃", inline=True)
        embed.add_field(name="湿度", value=f"{m['humidity']}%", inline=True)
        embed.add_field(name="風速", value=f"{data.get('wind', {}).get('speed', '—')} m/s", inline=True)
        return embed

    @commands.command(name="weather")
    async def weather(self, ctx, *, city: str = None):
        async with ctx.typing():
            try:
                data = self._fetch_by_city(city) if city else self._fetch_by_latlon(self.default_lat, self.default_lon)
                await ctx.send(embed=self._build_embed(data))
            except requests.HTTPError:
                await ctx.send(f"❌ 都市が見つかりませんでした: `{city}`")
            except Exception:
                logger.exception("天気取得エラー")
                await ctx.send("❌ 天気情報の取得に失敗しました")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.content not in TEMP_TRIGGERS:
            return
        try:
            data = self._fetch_by_latlon(self.default_lat, self.default_lon)
            temp = data["main"]["temp"]
            mention = message.author.mention
            t = f"（{temp:.1f}℃）"
            c = message.content
            if c == "あつい":
                reply = f"{mention}、あついね🥵🥵🥵{t}" if temp > 25 else f"{mention}、あつくないね😅😅😅{t}"
            elif c == "あつくない":
                reply = f"{mention}、あつくなくないね🥵🥵🥵{t}" if temp > 25 else f"{mention}、あつくないね😅😅😅{t}"
            elif c == "さむい":
                reply = f"{mention}、さむいね🥶🥶🥶{t}" if temp < 12 else f"{mention}、さむくないね😅😅😅{t}"
            else:
                reply = f"{mention}、さむくなくないね🥶🥶🥶{t}" if temp < 12 else f"{mention}、さむくないね🌟🌟🌟{t}"
            await message.channel.send(reply)
        except Exception:
            logger.exception("天気応答エラー")


async def setup(bot):
    await bot.add_cog(WeatherCog(bot))
