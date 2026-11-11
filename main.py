"""
Menfess AI Bot: panel inline (aiogram) + userbot (Telethon) dalam satu file.

Alur utama:
  1. Postingan baru di channel terdeteksi otomatis (realtime + cek berkala sebagai cadangan).
  2. Setiap postingan dibalas di kolom komentar, berapa pun panjang teksnya.
  3. Kalau gagal membalas, bot mengirim alasannya ke owner (waktu WIB).
"""
import asyncio
import base64
import gzip
import html
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, errors, events, utils
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.types import Channel, PeerChannel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
esc = html.escape
