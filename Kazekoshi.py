import sys
import os
import glob
from datetime import datetime
from logging import (DEBUG, INFO, NOTSET, FileHandler, Formatter, StreamHandler, basicConfig, getLogger)
import configparser

import discord
from discord.ext import commands

MAX_LOG_FILE = 5

# ディレクトリ作成
for directory in ["log", "temp", "json"]:
    os.makedirs(directory, exist_ok=True)

# 古いログ削除
loglist = sorted(glob.glob("./log/*.log"))
if len(loglist) > MAX_LOG_FILE:
    for old_log in loglist[:-MAX_LOG_FILE]:
        os.remove(old_log)

# ロギング設定
log_format = "[%(asctime)s] %(name)s:%(lineno)s %(funcName)s [%(levelname)s]: %(message)s"
sh = StreamHandler()
sh.setLevel(INFO)
sh.setFormatter(Formatter(log_format))
fh = FileHandler(f"./log/{datetime.now():%Y-%m-%d_%H%M%S}.log")
fh.setLevel(DEBUG)
fh.setFormatter(Formatter(log_format))
basicConfig(level=NOTSET, handlers=[sh, fh])
logger = getLogger(__name__)

# config.ini 読み込み
try:
    config = configparser.ConfigParser()
    config.read("config.ini", encoding="UTF-8")
    DISCORD_TOKEN = config["DEFAULT"]["DISCORD_TOKEN"]
    COMMAND_PREFIX = config["DEFAULT"].get("COMMAND_PREFIX", "!")
except Exception:
    logger.exception("config.ini の読み込みに失敗しました")
    sys.exit(1)

COGS = [
    "kazekoshi.cogs.voice",
    "kazekoshi.cogs.dice",
    "kazekoshi.cogs.notify",
    "kazekoshi.cogs.weather",
    "kazekoshi.cogs.dictionary",
    "kazekoshi.cogs.ai_chat",
    "kazekoshi.cogs.poll",
    "kazekoshi.cogs.reminder",
    "kazekoshi.cogs.utility",
]


class KazekoshiBot(commands.Bot):
    async def setup_hook(self):
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info(f"Loaded cog: {cog}")
            except Exception:
                logger.exception(f"Cog の読み込みに失敗しました: {cog}")

        try:
            synced = await self.tree.sync()
            logger.info(f"スラッシュコマンドを {len(synced)} 件同期しました")
        except Exception:
            logger.exception("スラッシュコマンドの同期に失敗しました")

    async def on_ready(self):
        logger.info(f"Kazekoshi v3.0 on ready (discord.py v{discord.__version__})")
        await self.change_presence(activity=discord.Game(name="Kazekoshi v3.0 | /help"))


intents = discord.Intents.all()
bot = KazekoshiBot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

try:
    bot.run(DISCORD_TOKEN)
except Exception:
    logger.exception("Discord API キーエラー")
    sys.exit(1)
