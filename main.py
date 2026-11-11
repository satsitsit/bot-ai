"""
Menfess AI Bot: panel inline (aiogram) + userbot (Telethon) dalam satu file.

Alur utama:
  1. Postingan baru di channel terdeteksi otomatis (realtime + cek berkala sebagai cadangan).
  2. Setiap postingan dibalas di kolom komentar, berapa pun panjang teksnya.
  3. Kalau gagal membalas, bot mengirim alasannya ke owner (waktu WIB).
"""
import asyncio
import base64
from collections import Counter
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

# ---------- konfigurasi (dari GitHub Secrets) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
SESSION = os.getenv("TG_STRING_SESSION", "")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
KEYS = {
    "gemini": os.getenv("GEMINI_API_KEY", ""),
    "groq": os.getenv("GROQ_API_KEY", ""),
    "openrouter": os.getenv("OPENROUTER_API_KEY", ""),
}
NAMES = {"gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter"}
URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
# Nama model bisa berubah. Kalau Tes AI menampilkan 404, ganti di sini atau pilih model lain di panel.
# Update 11/11/2026: llama-3.x (Groq) pensiun 16 Agu 2026, gemini-2.5-* pensiun 16 Okt 2026.
MODELS = {
    "gemini": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest"],
    "groq": ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.6-27b"],
    "openrouter": ["meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free"],
}
# panjang jawaban: key -> (label, aturan untuk AI, batas token, batas karakter)
LENGTHS = {
    "auto": ("Sesuai Konteks",
             "Sesuaikan panjang balasan dengan konteks. Sapaan, candaan, atau menfess ringan: 1 kalimat pendek. "
             "Curhat, pertanyaan, atau cerita panjang: 3-5 kalimat yang menanggapi poin pentingnya. "
             "Jangan bertele-tele dan jangan mengulang isi pesan.", 500, 1000),
    "pendek": ("Pendek", "Balas sangat singkat: 1 kalimat, maksimal 20 kata.", 150, 300),
    "sedang": ("Sedang", "Balas dengan 2-3 kalimat.", 300, 700),
    "panjang": ("Panjang", "Balas lebih lengkap, 4-6 kalimat, tetap natural dan tidak bertele-tele.", 700, 1500),
}
PERSONAS = {
    "teman": ("Teman Ngobrol", "Kamu adalah teman ngobrol yang hangat, suportif, dan sedikit humoris."),
    "netral": ("Netral", "Kamu adalah pendengar yang tenang, sopan, dan objektif."),
    "ceria": ("Ceria", "Kamu orang yang ceria dan suka bercanda ringan, tetapi tetap sopan."),
}
STYLES = {
    "baku": ("Baku", "Gunakan bahasa Indonesia baku dan sopan."),
    "santai": ("Santai", "Gunakan bahasa Indonesia santai sehari-hari."),
    "gaul": ("Gaul", "Gunakan bahasa Indonesia gaul ringan tanpa kata kasar."),
    "x": (
        "X Santai",
        "Gunakan gaya bahasa Indonesia santai seperti pengguna X/Twitter. "
        "Huruf kecil semua. Natural dan tidak kaku. Minim tanda baca. "
        "Emoji maksimal satu dan tidak selalu digunakan. "
        "Sesekali menggunakan bahasa sehari-hari seperti wkwk, relate, atau semangat ya. "
        "Jangan berlebihan atau terdengar seperti robot.",
    ),
}
# key: (label, min, max, langkah, preset)
NUMS = {
    "reply_prob": ("Persen menfess yang dibalas", 0, 100, 5, [25, 50, 75, 100]),
    "thread_prob": ("Persen reply yang dibalas", 0, 100, 5, [30, 60, 80, 100]),
    "min_len": ("Abaikan menfess lebih pendek dari (karakter)", 0, 500, 1, [0, 1, 5, 20]),
    "delay_min": ("Jeda balas paling cepat (detik)", 0, 600, 5, [0, 3, 10, 30]),
    "delay_max": ("Jeda balas paling lama (detik)", 0, 900, 5, [3, 10, 30, 60]),
    "max_comment_hour": ("Maks komentar menfess per jam (0 = bebas)", 0, 1000, 10, [0, 30, 100, 500]),
    "max_age_min": ("Abaikan postingan lebih tua dari (menit)", 5, 1440, 5, [15, 30, 60, 180]),
    "max_thread": ("Maks balasan per thread", 1, 20, 1, [1, 2, 3, 5]),
    "max_user_day": ("Maks balasan per orang per hari", 1, 100, 1, [3, 5, 10, 20]),
    "dm_hour": ("Maks balasan DM per jam per orang (0 = bebas)", 0, 200, 5, [0, 10, 20, 50]),
    "nimbrung_prob": ("Persen nimbrung di grup tanpa dipanggil", 0, 100, 5, [0, 10, 20, 40]),
    "group_hour": ("Maks balasan asisten per jam per grup (0 = bebas)", 0, 200, 5, [0, 10, 30, 60]),
}
# profil cepat: sekali tekan mengatur banyak angka sekaligus
PROFILES = {
    "hemat": ("🐢 Hemat Kuota", "Aman untuk AI gratis. Tidak semua dibalas, ada jeda, dan ada batas per jam.",
              dict(reply_prob=60, thread_prob=50, delay_min=3, delay_max=15, max_comment_hour=30,
                   max_thread=2, max_user_day=5, dm_hour=0)),
    "normal": ("⚖️ Seimbang", "Hampir semua dibalas dengan jeda pendek. Batas 40 komentar per jam.",
               dict(reply_prob=100, thread_prob=70, delay_min=0, delay_max=3, max_comment_hour=40,
                    max_thread=3, max_user_day=10, dm_hour=0)),
    "aktif": ("🚀 Aktif Penuh", "Semua dibalas secepat mungkin, tanpa batas per jam. Kuota gratis cepat habis.",
              dict(reply_prob=100, thread_prob=100, delay_min=0, delay_max=3, max_comment_hour=0,
                   max_thread=5, max_user_day=20, dm_hour=0)),
}
HELP = {
    "reply_prob": "Dari 100 menfess yang masuk, berapa yang dibalas. 100 berarti semua dibalas.",
    "thread_prob": "Kalau ada yang membalas komentar bot, berapa persen kemungkinan bot membalas lagi.",
    "min_len": "Menfess yang lebih pendek dari angka ini dilewati. 0 berarti semua dibalas, termasuk foto tanpa teks.",
    "delay_min": "Bot menunggu dulu sebelum membalas supaya terlihat manusiawi. Ini waktu tunggu paling cepat.",
    "delay_max": "Waktu tunggu paling lama. Bot memilih acak antara jeda cepat dan jeda lama.",
    "max_comment_hour": "Batas komentar menfess per jam. Berguna supaya kuota AI gratis tidak cepat habis. 0 berarti tanpa batas.",
    "max_age_min": "Kalau bot sempat mati, postingan yang lebih tua dari ini tidak dibalas supaya tidak terlihat aneh.",
    "max_thread": "Berapa kali bot boleh membalas dalam satu menfess (satu thread).",
    "max_user_day": "Berapa kali bot boleh membalas satu orang yang sama dalam sehari.",
    "dm_hour": "Batas balasan DM per jam untuk setiap orang. 0 berarti tanpa batas, AI membalas terus.",
    "nimbrung_prob": "Di grup yang Nimbrung-nya dinyalakan, berapa persen pesan yang ditanggapi bot walau tidak dipanggil. Kalau tidak ada yang nyambung, bot tetap diam.",
    "group_hour": "Batas balasan asisten di satu grup per jam. 0 berarti tanpa batas.",
}
DEFAULTS = {
    "comment_on": True, "reply_on": True, "dm_on": True, "dm_whitelist": False,
    "notify": True, "notify_ok": False,
    "reply_prob": 100, "thread_prob": 70, "min_len": 0, "delay_min": 0, "delay_max": 3,
    "max_comment_hour": 40, "max_age_min": 60, "max_thread": 3, "max_user_day": 10, "dm_hour": 0,
    "keywords": [], "order": ["gemini", "groq", "openrouter"], "disabled": ["openrouter"],
    "model_gemini": MODELS["gemini"][0],
    "model_groq": MODELS["groq"][0],
    "model_openrouter": MODELS["openrouter"][0],
    "persona": "teman", "style": "x", "reply_len": "auto", "prompt": "",
    "learn_on": True, "vision_on": True, "ignore_names": ["rose"], "learn_ts": 0,
    "banned": ["@"],
    "takeover_on": False, "free_mode": False,
    "group_on": True, "group_names": ["newt"], "nimbrung_prob": 15, "group_hour": 0,
}
SETTINGS_VERSION = 13   # naikkan kalau DEFAULTS diubah, supaya pengaturan lama tidak menimpa
POLL_SEC = 10          # jeda cek berkala channel (cadangan kalau update realtime tidak masuk)
SEND_GAP = 2.5         # jeda minimal antar komentar, mencegah FloodWait
LEARN_EVERY = 12 * 3600  # jeda belajar otomatis dari channel
MAX_EXAMPLES = 60000     # contoh tersimpan per channel (yang paling lama dibuang kalau penuh)
LEARN_MAX_COMMENTS = 1000   # komentar yang dibaca per menfess (praktis: semuanya)
LEARN_VERSION = 2        # naikkan kalau aturan belajar berubah: semua channel dipelajari ulang sekali
BACKUP_TAG = "#menfess_backup"   # penanda file backup di Pesan Tersimpan akun userbot
BACKUP_EVERY = 900       # detik: selama belajar lama, backup tiap 15 menit
CATCHUP_MIN = 90         # menit: DM belum dibaca yang masuk selama bot mati dibalas saat bot menyala lagi
NIMBRUNG_GAP = 45        # detik: jeda minimal antar nimbrung di satu grup
FALLBACK_DM = "Maaf, aku lagi error sebentar. Coba chat lagi beberapa menit lagi ya 🙏"
FALLBACK_CONFUSED = "hm aku kurang nangkep maksudnya, bisa dijelasin lagi?"
LEARN_PARALLEL = 4       # berapa postingan dipelajari bersamaan (makin besar makin cepat, tapi rawan FloodWait)
LEARN_PAGE = 100         # postingan diambil per halaman
LEARN_GAP = 0.3          # jeda singkat antar kelompok postingan
SETCNT = "INSERT INTO counters(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
DISCLOSURE = (
    "Halo! 👋 Aku asisten AI-nya Newt (@sticksc), dibuat dan dikelola langsung oleh Newt.\n"
    "Akun ini membalas pesan secara otomatis pakai AI.\n"
    "Ketik /stop kapan saja untuk menghentikan balasan otomatis."
)
CLOSING = re.compile(r"^\s*(ok|oke|okay|sip|makasih|mksh|thanks|thx|noted|siap|yoi)[\s!.]*$", re.I)
MODE_TEXT = {
    "menfess": (
        "Kamu sedang memberi komentar pada sebuah menfess (pesan anonim) di channel. "
        "Menfess bisa singkat, tidak jelas, bercanda, atau memakai istilah khas komunitas: pahami konteks dan "
        "nadanya (bercanda dibalas bercanda), lalu tanggapi dengan nyambung. "
        "Jika menfess berisi curhat berat atau keinginan menyakiti diri, balas dengan empati singkat "
        "dan sarankan bicara dengan orang terpercaya atau profesional."
    ),
    "reply": "Kamu sedang membalas seseorang di thread komentar sebuah menfess. Lanjutkan percakapan dengan relevan.",
    "dm": "Kamu sedang membalas pesan pribadi dari seseorang. Tanggapi sesuai isi pesannya.",
    "group": (
        "Kamu sedang berada di grup Telegram sebagai asisten AI. Baca percakapan terakhir, lalu tanggapi "
        "pesan yang ditujukan kepadamu sesuai konteks obrolan, dengan gaya yang pas untuk grup."
    ),
    "nimbrung": (
        "Kamu sedang membaca obrolan di grup Telegram dan ikut nimbrung tanpa dipanggil. "
        "Balas hanya kalau kamu bisa menambah sesuatu yang nyambung dengan obrolan terakhir."
    ),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels (username TEXT PRIMARY KEY, chat_id INTEGER, disc_id INTEGER,
    title TEXT, paused INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, lst TEXT, ai_on INTEGER DEFAULT 1,
    stopped INTEGER DEFAULT 0, greeted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS hist (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, role TEXT, content TEXT);
CREATE INDEX IF NOT EXISTS hist_key ON hist(key, id);
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS seen (chat_id INTEGER, post_id INTEGER, PRIMARY KEY(chat_id, post_id));
CREATE TABLE IF NOT EXISTS cursor (chat_id INTEGER PRIMARY KEY, last_id INTEGER);
CREATE TABLE IF NOT EXISTS examples (id INTEGER PRIMARY KEY AUTOINCREMENT, ch TEXT, kind TEXT, src TEXT,
    reply TEXT, ts INTEGER, UNIQUE(ch, kind, src, reply));
CREATE TABLE IF NOT EXISTS tg_groups (chat_id INTEGER PRIMARY KEY, title TEXT, enabled INTEGER DEFAULT 0,
    free INTEGER DEFAULT 0);
"""

WIB = timezone(timedelta(hours=7))
STARTED = time.time()
bot = None
ub = None
db = None
LOGIN = {}      # client login OTP yang sedang berjalan
PENDING = {}    # session string yang menunggu diaktifkan
LAST = {}       # info postingan terakhir yang terdeteksi
REPORT_AT = {}  # throttle laporan ke owner
BG = set()      # referensi task latar belakang
AI_SEM = asyncio.Semaphore(3)  # maksimal 3 permintaan AI bersamaan
SEND_LOCK = asyncio.Lock()     # komentar dikirim satu per satu


class AIError(Exception):
    pass


# ---------- waktu WIB ----------

def now_wib():
    return datetime.now(WIB)


def fmt(ts=None):
    d = datetime.fromtimestamp(ts, WIB) if ts else now_wib()
    return d.strftime("%d-%m-%Y %H:%M:%S") + " WIB"


def fmt_short(ts):
    return datetime.fromtimestamp(ts, WIB).strftime("%d/%m %H:%M:%S")


def short(e):
    return (str(e) or type(e).__name__)[:150]


# ---------- database ----------

async def db_init():
    global db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    rows = await q("SELECT value FROM settings WHERE key='settings_version'")
    old = json.loads(rows[0]["value"]) if rows else 0
    if old != SETTINGS_VERSION:
        # Versi baru: pengaturan lama dibuang dan default terbaru dipakai.
        # Hanya session dan prompt kustom yang dipertahankan.
        await db.execute("DELETE FROM settings WHERE key NOT IN ('session','prompt')")
        await db.execute("UPDATE users SET ai_on=1")   # AI yang dulu mati karena takeover dinyalakan lagi
        await db.execute(
            "INSERT OR REPLACE INTO settings VALUES('settings_version', ?)",
            (json.dumps(SETTINGS_VERSION),),
        )
    for k, v in DEFAULTS.items():
        await db.execute("INSERT OR IGNORE INTO settings VALUES(?, ?)", (k, json.dumps(v)))
    await db.execute("DELETE FROM seen WHERE rowid NOT IN (SELECT rowid FROM seen ORDER BY rowid DESC LIMIT 5000)")
    await db.commit()


async def q(sql, args=()):
    cur = await db.execute(sql, args)
    return [dict(r) for r in await cur.fetchall()]


async def x(sql, args=()):
    await db.execute(sql, args)
    await db.commit()


async def settings():
    rows = await q("SELECT key, value FROM settings")
    return {**DEFAULTS, **{r["key"]: json.loads(r["value"]) for r in rows}}


async def setv(key, value):
    await x(
        "INSERT INTO settings VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


async def count(key):
    rows = await q("SELECT value FROM counters WHERE key=?", (key,))
    return rows[0]["value"] if rows else 0


async def incr(key):
    await x("INSERT INTO counters(key, value) VALUES(?, 1) ON CONFLICT(key) DO UPDATE SET value=value+1", (key,))


async def stat(name):
    await incr(f"stat:{name}")


async def stats():
    rows = await q("SELECT key, value FROM counters WHERE key LIKE 'stat:%'")
    return {r["key"][5:]: r["value"] for r in rows}


async def log_(level, text):
    await x("INSERT INTO logs(ts, level, text) VALUES(?, ?, ?)", (int(time.time()), level, text[:300]))
    await x("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 200)")


async def skip(reason):
    await stat("skipped")
    await log_("INFO", f"Dilewati: {reason}")


async def user(uid):
    await x("INSERT OR IGNORE INTO users(uid) VALUES(?)", (uid,))
    return (await q("SELECT * FROM users WHERE uid=?", (uid,)))[0]


async def user_set(uid, **fields):
    await user(uid)
    cols = ", ".join(f"{k}=?" for k in fields)
    await x(f"UPDATE users SET {cols} WHERE uid=?", (*fields.values(), uid))


async def hist_add(key, role, content):
    await x("INSERT INTO hist(key, role, content) VALUES(?, ?, ?)", (key, role, content[:2000]))


async def hist_get(key, n=12):
    return await q(
        "SELECT role, content FROM (SELECT id, role, content FROM hist WHERE key=? "
        "ORDER BY id DESC LIMIT ?) ORDER BY id",
        (key, n),
    )


# ---------- belajar dari balasan asli ----------

URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+|@\w+")


def clean_ex(t, limit=200):
    """Bersihkan teks contoh: buang link dan @mention supaya tidak ada identitas orang yang tersimpan."""
    t = URL_RE.sub("", t or "")
    return re.sub(r"\s+", " ", t).strip()[:limit]


STOP = set(
    "yang dan atau dengan untuk dari pada itu ini aku kamu dia kita kami mereka ada apa kok sih deh dong nih tuh "
    "aja saja juga lagi udah sudah belum bisa mau gak nggak tidak bukan banget sama gue gua lu lo kan yah ya yaa "
    "oke ok wkwk haha hehe kalo kalau jadi biar karena tapi tp aja nya gitu gini".split()
)


def media_label(post):
    """Penanda untuk menfess tanpa teks, supaya komentarnya tetap dipelajari."""
    for attr, label in (("photo", "[foto]"), ("video", "[video]"), ("gif", "[video]"), ("sticker", "[stiker]"),
                        ("voice", "[suara]"), ("audio", "[audio]"), ("document", "[file]")):
        if getattr(post, attr, None):
            return label
    return "[media]" if getattr(post, "media", None) else ""


def tokens(t):
    return {w for w in re.findall(r"\w+", (t or "").lower()) if len(w) >= 3 and w not in STOP}


def is_ignored(sender, s):
    """Akun bot selalu diabaikan, begitu juga nama di daftar abaikan (contoh: Rose)."""
    if sender is None:
        return False
    if getattr(sender, "bot", False) or getattr(sender, "id", None) == 777000:
        return True
    ident = " ".join(str(getattr(sender, k, None) or "") for k in ("first_name", "last_name", "username", "title")).lower()
    return any(
        re.search(rf"(?<!\w){re.escape(n.strip().lower())}(?!\w)", ident)
        for n in s.get("ignore_names", []) if n.strip()
    )


async def add_example(ch, kind, src, reply, ts=None):
    cur = await db.execute(
        "INSERT OR IGNORE INTO examples(ch, kind, src, reply, ts) VALUES(?, ?, ?, ?, ?)",
        (ch, kind, src, reply, ts or int(time.time())),
    )
    return max(cur.rowcount, 0)


async def term_examples(ch_username, text, max_lines=8, per_term=2):
    """Cari pemakaian kata-kata di pesan ini (istilah, slang, singkatan) di SEMUA contoh yang sudah dipelajari,
    supaya AI paham artinya dari konteks asli channel, bukan menebak."""
    words = sorted(tokens(text), key=lambda w: -len(w))[:6]
    if not words:
        return ""
    chf, cha = ("ch=? AND", (ch_username,)) if ch_username else ("", ())
    out, seen = [], set()
    for w in words:
        if len(out) >= max_lines:
            break
        rows = await q(f"SELECT src, reply FROM examples WHERE {chf} (src LIKE ? OR reply LIKE ?) "
                       "ORDER BY ts DESC LIMIT 40", (*cha, f"%{w}%", f"%{w}%"))
        if len(rows) >= 40:
            continue   # kata terlalu umum, bukan istilah khas
        pat = re.compile(rf"(?<!\w){re.escape(w)}(?!\w)", re.I)
        got = 0
        for r in rows:
            key = (r["src"], r["reply"])
            if got >= per_term or key in seen or not pat.search(f"{r['src']} {r['reply']}"):
                continue
            seen.add(key)
            got += 1
            out.append(f'- kata "{w}" dipakai di: "{r["src"][:110]}" → "{r["reply"][:110]}"')
    if not out:
        return ""
    return ("Kata/istilah di pesan ini juga dipakai orang di channel. Pakai contoh berikut hanya untuk memahami "
            "artinya dari konteks (jangan disalin):\n" + "\n".join(out[:max_lines]))


async def examples_block(ch_username, kind, text, s, n=3, per=6):
    """Beberapa menfess yang topiknya paling mirip, masing-masing dengan banyak komentar aslinya
    (bukan cuma satu), ditambah pemakaian istilah khas channel, untuk ditempel ke prompt AI.
    ch_username=None berarti semua channel."""
    if not s["learn_on"]:
        return ""
    chf, cha = ("ch=? AND", (ch_username,)) if ch_username else ("", ())
    parts = []
    rows = await q(f"SELECT src, reply FROM examples WHERE {chf} kind=? ORDER BY ts DESC LIMIT 8000", (*cha, kind))
    if rows:
        groups = {}
        for r in rows:
            groups.setdefault(r["src"], []).append(r["reply"])
        want = tokens(text)
        ranked = sorted(groups.items(), key=lambda kv: (-len(want & tokens(kv[0])), random.random()))
        a, b = ("Menfess", "Komentar") if kind == "menfess" else ("Komentar", "Balasan")
        blocks = []
        for src, replies in ranked[:n]:
            if len(replies) > per:
                replies = random.sample(replies, per)
            lines = "\n".join(f'- "{r[:140]}"' for r in replies)
            blocks.append(f'{a}: "{src[:160]}"\n{b} dari berbagai orang:\n{lines}')
        parts.append(
            "Contoh ASLI dari pengunjung channel ini (satu pesan beserta tanggapan beberapa orang di bawahnya), "
            "hanya sebagai pedoman nada, panjang, dan variasi. Tiru gayanya, jangan menyalin kalimatnya, "
            "dan abaikan contoh yang kasar atau menghina:\n\n" + "\n\n".join(blocks)
        )
    terms = await term_examples(ch_username, text)
    if terms:
        parts.append(terms)
    return "\n\n".join(parts)


async def claim(chat_id, post_id):
    """True hanya untuk pemanggil pertama, sehingga satu postingan tidak dibalas dua kali."""
    cur = await db.execute("INSERT OR IGNORE INTO seen(chat_id, post_id) VALUES(?, ?)", (chat_id, post_id))
    await db.commit()
    return cur.rowcount == 1


async def cursor_get(chat_id):
    rows = await q("SELECT last_id FROM cursor WHERE chat_id=?", (chat_id,))
    return rows[0]["last_id"] if rows else None


async def cursor_bump(chat_id, post_id):
    await x(
        "INSERT INTO cursor(chat_id, last_id) VALUES(?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
        (chat_id, post_id),
    )


# ---------- notifikasi & laporan ke owner ----------

async def notify(text):
    if bot and (await settings())["notify"]:
        try:
            await bot.send_message(OWNER_ID, text)
        except Exception:
            pass


async def report(title, body, key=None, every=20):
    """Kirim penjelasan ke owner. Laporan sejenis digabung supaya tidak membanjiri chat."""
    k = key or title
    now = time.time()
    last, n = REPORT_AT.get(k, (0.0, 0))
    if now - last < every:
        REPORT_AT[k] = (last, n + 1)
        return
    REPORT_AT[k] = (now, 0)
    extra = f"\n➕ {n} kejadian serupa sejak laporan terakhir." if n else ""
    await notify(f"<b>{title}</b>\n🕒 {fmt()}\n{body}{extra}")


def explain(e):
    """Terjemahkan error Telegram menjadi penjelasan yang mudah dipahami."""
    name = type(e).__name__
    secs = getattr(e, "seconds", None)
    table = {
        "FloodWaitError": f"Telegram membatasi kecepatan kirim (FloodWait {secs} detik).",
        "SlowModeWaitError": f"Grup diskusi memakai slow mode ({secs} detik).",
        "PeerFloodError": "Akun ditandai spam oleh Telegram. Kurangi frekuensi balasan.",
        "UserNotParticipantError": "Userbot belum join grup diskusi.",
        "ChannelPrivateError": "Grup diskusi privat dan userbot tidak punya akses (belum join atau dikeluarkan).",
        "ChatWriteForbiddenError": "Userbot tidak boleh mengirim pesan di grup diskusi (dibatasi atau grup hanya-baca).",
        "UserBannedInChannelError": "Akun userbot dibatasi untuk mengirim pesan di grup tersebut.",
        "ChatGuestSendForbiddenError": "Userbot harus join grup diskusi dulu sebelum bisa komentar.",
        "MsgIdInvalidError": "Postingan tidak punya kolom komentar (komentar mati atau belum tersambung ke grup diskusi).",
    }
    return table.get(name, f"{name}: {short(e)}")


# ---------- AI ----------

def is_skip(text):
    return text.strip().strip("\"'.").upper() == "SKIP"


BUBBLE_SPLIT = re.compile(r"\s*\|{2,}\s*")


def split_bubbles(text, limit=4):
    """Pecah balasan AI jadi beberapa bubble chat (dipisah |||). Kalau AI lupa memakai pemisah
    tapi menulis beberapa baris, tiap baris jadi satu bubble. Maksimal `limit` bubble."""
    parts = [p.strip() for p in BUBBLE_SPLIT.split(text or "") if p.strip()]
    if len(parts) == 1 and "\n" in parts[0]:
        parts = [ln.strip() for ln in parts[0].split("\n") if ln.strip()]
    if len(parts) > limit:
        parts = parts[:limit - 1] + [" ".join(parts[limit - 1:])]
    return parts


def system_prompt(s, mode):
    persona = s["prompt"].strip() or PERSONAS[s["persona"]][1]
    rules = [
        f"Balas dalam Bahasa Indonesia seperti chat sungguhan. {LENGTHS[s['reply_len']][1]}",
        "Jika ditanya langsung, jujur bahwa kamu asisten AI. Jangan mengaku manusia.",
        "Identitasmu: asisten AI milik Newt (@sticksc), diproduksi dan dikelola oleh Newt. "
        "Kalau ada yang bertanya kamu siapa, asisten siapa, atau siapa yang membuatmu, jawab sesuai itu "
        "dengan gaya bahasamu. Jangan menyebut identitas ini kalau tidak ditanya.",
        "Jangan menghina, memprovokasi, membahas SARA, konten seksual, atau kekerasan.",
        "Jangan meminta atau membagikan data pribadi.",
        "Jangan memaksakan percakapan.",
        "Keluarkan hanya teks balasan, tanpa label atau tanda kutip.",
    ]
    extra = [
        "Pahami konteks: pesan bisa singkat, tidak jelas, bercanda, atau memakai singkatan dan istilah khas komunitas. "
        "Gunakan contoh yang diberikan untuk memahami istilah itu, tanggapi sesuai topik dan nadanya, dan jangan melenceng.",
    ]
    # Boleh SKIP? nimbrung selalu boleh (tidak dipanggil). Komentar menfess dan reply boleh kalau Mode Bebas mati.
    can_skip = mode == "nimbrung" or (mode in ("menfess", "reply") and not s["free_mode"])
    if can_skip:
        extra.append(
            "Jika pesan spam atau provokasi, atau kamu benar-benar tidak paham maksud atau istilahnya dan tidak bisa "
            "menebaknya dari konteks maupun contoh, jawab persis: SKIP, daripada membalas ngawur atau melenceng."
        )
    else:
        extra.append(
            "Kalau kurang paham, tetap balas: tanggapi bagian yang kamu pahami atau tanya balik singkat dengan santai. "
            "Jangan ngawur, jangan melenceng dari topik, dan jangan jawab SKIP."
        )
    if mode == "dm":
        extra.append(
            "Ini chat pribadi, jadi balas seperti orang mengetik di WhatsApp: pecah balasan jadi 2 sampai 4 "
            "pesan pendek, satu ide per pesan, dan pisahkan antar pesan dengan tanda ||| "
            "(jangan taruh ||| di awal atau akhir). Sapaan pun dibalas 2 pesan: sapaan balik lalu satu kalimat lanjutan "
            "yang nyambung. Hanya pesan penutup seperti ok atau makasih yang cukup 1 pesan. "
            "Langsung ke inti dan jangan menjawab setengah-setengah atau menggantung, "
            "supaya lawan bicara tidak perlu bertanya ulang."
        )
    rules = rules[:-1] + extra + rules[-1:]
    body = "\n".join(f"- {r}" for r in rules)
    return f"{persona}\n{STYLES[s['style']][1]}\n{MODE_TEXT[mode]}\n\nAturan wajib:\n{body}"


def merge_roles(msgs):
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n" + m["content"]}
        else:
            out.append({"role": m["role"], "content": m["content"]})
    while out and out[0]["role"] != "user":
        out.pop(0)   # Gemini dan beberapa provider mewajibkan percakapan diawali pesan user
    return out


def http_err(code):
    hint = {
        400: "permintaan ditolak (cek nama model)",
        401: "API key salah",
        403: "API key ditolak atau wilayah tidak didukung",
        404: "nama model tidak ditemukan",
        429: "kuota habis atau terlalu banyak permintaan",
    }.get(code, "server provider bermasalah" if code >= 500 else "error tidak dikenal")
    return f"HTTP {code}: {hint}"


NO_IMAGE_NOTE = ("\n(Catatan: menfess ini menyertakan foto yang tidak bisa kamu lihat. "
                 "Jangan mengarang isi foto; tanggapi dari teksnya saja.)")
VISION_NOTE = ("Menfess ini menyertakan foto yang bisa kamu lihat. Tanggapi isi fotonya secara natural "
               "seperti orang yang benar-benar melihatnya, tanpa menulis \"dalam foto ini\".")


def can_see(name, model):
    """Provider/model yang bisa membaca gambar."""
    return name == "gemini" or (name == "openrouter" and "gemma-3" in model)


async def call(http, name, model, system, msgs, max_out=300, image=None):
    if image:
        if can_see(name, model):
            system = f"{system}\n\n{VISION_NOTE}"
        else:
            # Model ini tidak bisa melihat gambar: beri tahu AI supaya tidak mengarang isi foto.
            last = msgs[-1]
            msgs = [*msgs[:-1], {"role": last["role"], "content": last["content"] + NO_IMAGE_NOTE}]
            image = None
    b64 = base64.b64encode(image).decode() if image else None
    if name == "gemini":
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in msgs
        ]
        if b64:
            contents[-1]["parts"].append({"inlineData": {"mimeType": "image/jpeg", "data": b64}})
        r = await http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": KEYS["gemini"]},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {"temperature": 0.9, "maxOutputTokens": 2048},
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(http_err(r.status_code))
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise RuntimeError(f"diblokir filter keamanan ({block})" if block else "respons kosong")
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)

    if b64:
        last = msgs[-1]
        msgs = [*msgs[:-1], {"role": last["role"], "content": [
            {"type": "text", "text": last["content"]},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]}]
    body = {"model": model, "messages": [{"role": "system", "content": system}, *msgs],
            "temperature": 0.9, "max_tokens": max_out}
    if name == "groq" and model.startswith(("openai/gpt-oss", "qwen/")):
        # model reasoning: token "berpikir" ikut dihitung, jadi budget dilonggarkan dan effort dibuat rendah
        body["max_tokens"] = max_out + 1200
        body["reasoning_effort"] = "low" if model.startswith("openai/") else "none"
    r = await http.post(
        URLS[name],
        headers={"Authorization": f"Bearer {KEYS[name]}"},
        json=body,
    )
    if r.status_code >= 400:
        raise RuntimeError(http_err(r.status_code))
    return r.json()["choices"][0]["message"]["content"]


async def ask_ai(mode, msgs, s, image=None, extra=""):
    """Coba provider sesuai urutan. Kalau semua gagal, raise AIError berisi alasan tiap provider."""
    system = system_prompt(s, mode) + (f"\n\n{extra}" if extra else "")
    msgs = merge_roles(msgs)
    errs = []
    async with httpx.AsyncClient(timeout=20) as http:
        for name in s["order"]:
            if name in s["disabled"]:
                errs.append(f"{NAMES[name]}: dimatikan di panel")
                continue
            if not KEYS.get(name):
                errs.append(f"{NAMES[name]}: API key belum diisi")
                continue
            chosen = s[f"model_{name}"]
            for model in [chosen] + [m for m in MODELS[name] if m != chosen]:
                try:
                    text = (await call(http, name, model, system, msgs, LENGTHS[s["reply_len"]][2], image) or "").strip()
                    text = text.strip("\"'").strip()
                    if text:
                        if s["style"] == "x":
                            text = text.lower()
                        return text[:LENGTHS[s["reply_len"]][3]]
                    errs.append(f"{NAMES[name]}: respons kosong")
                    break
                except Exception as e:
                    errs.append(f"{NAMES[name]}: {short(e)}")
                    if isinstance(e, httpx.TimeoutException) or any(
                        c in str(e) for c in ("HTTP 404", "HTTP 400", "HTTP 429", "HTTP 5")
                    ):
                        continue   # kuota/overload/pensiun: tiap model punya kuota sendiri, coba model lain
                    break          # 401/403 = masalah API key, model lain juga pasti gagal
    raise AIError(" | ".join(errs) or "tidak ada provider aktif")


async def test_providers(s):
    """Tes SEMUA model tiap provider. Kalau model yang sedang dipakai gagal tapi ada yang lolos,
    model pertama yang lolos langsung dipakai (disimpan ke pengaturan)."""
    lines = []
    async with httpx.AsyncClient(timeout=25) as http:
        async def probe(name, model):
            t0 = time.time()
            try:
                await call(http, name, model, "Balas singkat.",
                           [{"role": "user", "content": "Balas satu kata: halo"}])
                return True, f"{time.time() - t0:.1f} dtk"
            except Exception as e:
                return False, short(e)

        for name in s["order"]:
            if name in s["disabled"]:
                lines.append(f"⚪ {NAMES[name]}: dimatikan")
                continue
            if not KEYS.get(name):
                lines.append(f"⚪ {NAMES[name]}: API key belum diisi")
                continue
            models = MODELS[name]
            results = await asyncio.gather(*(probe(name, m) for m in models))   # semua model dites bersamaan
            current = s[f"model_{name}"]
            ok = [m for m, (good, _) in zip(models, results) if good]
            if current in ok:
                pick = current
            else:
                pick = ok[0] if ok else None
            for m, (good, info) in zip(models, results):
                mark = "  ← dipakai" if m == pick else ""
                lines.append(f"{'✅' if good else '❌'} {NAMES[name]} · {m}: "
                             f"{'bisa dipakai (' + info + ')' if good else info}{mark}")
            if pick and pick != current:
                await setv(f"model_{name}", pick)
                lines.append(f"🔁 {NAMES[name]}: model otomatis diganti ke {pick}")
            elif not pick:
                lines.append(f"⚠️ {NAMES[name]}: tidak ada model yang lolos, model lama tetap dipakai")
    return lines


async def humanize(s):
    lo, hi = sorted((s["delay_min"], s["delay_max"]))
    if hi > 0:
        await asyncio.sleep(random.uniform(lo, hi))


# ---------- userbot ----------

async def find_root(m):
    """Telusuri rantai reply sampai ke postingan menfess asli (auto-forward)."""
    for _ in range(8):
        if m.fwd_from and m.fwd_from.channel_post:
            return m
        if not m.reply_to_msg_id:
            return None
        m = await m.get_reply_message()
        if m is None:
            return None
    return None


class UB:
    def __init__(self, string):
        self.string = string or ""
        self.c = TelegramClient(StringSession(self.string), API_ID, API_HASH)
        self.me = None
        self.ready = False
        self.chans = {}    # chat_id channel -> baris database
        self.by_disc = {}  # chat_id grup diskusi -> baris database
        self.ents = {}     # chat_id channel -> entity
        self.groups = {}   # chat_id grup diskusi -> entity
        self.albums = set()
        self.joined = set()
        self.ai_at = {}
        self.dm_locks = {}   # uid -> lock, supaya bubble satu percakapan tidak tercampur
        self.fail_at = {}    # uid -> kapan terakhir dikirimi pesan 'lagi error' (supaya tidak spam)
        self.nimbrung_at = {}   # chat_id grup -> kapan terakhir nimbrung
        self.tasks = set()
        self.poller = None
        self.learning = False
        self.next_learn = 0.0   # kapan terakhir dicek perlu belajar lagi atau belum
        self.next_resume = 0.0  # kapan terakhir dicek ada belajar yang harus dilanjutkan
        self.learn_stop = False  # tombol Hentikan di panel
        self.prog = ""          # ringkasan progres belajar untuk panel
        self.lstat = Counter()  # hitungan komentar: dibaca, baru, sudah ada, dilewati

    async def start(self):
        await self.c.connect()
        if not await self.c.is_user_authorized():
            raise RuntimeError("session kosong, tidak valid, atau sudah logout")
        self.me = await self.c.get_me()
        dialogs = []
        try:
            # Memuat dialog supaya Telethon mengenal grup diskusi dan menerima update-nya.
            dialogs = await self.c.get_dialogs(limit=200)
        except Exception as e:
            log.warning("get_dialogs gagal: %s", e)
        self.c.add_event_handler(self.on_in, events.NewMessage(incoming=True))
        self.c.add_event_handler(self.on_out, events.NewMessage(outgoing=True))
        await self.refresh()
        await self.migrate_learning()
        await self.register_groups(dialogs)
        self.ready = True
        self.poller = asyncio.create_task(self.poll_loop())
        self.spawn(self.auto_restore())
        self.spawn(self.catch_up(dialogs))
        log.info("Userbot aktif sebagai %s", self.me.first_name)

    async def stop(self):
        self.ready = False
        if self.poller:
            self.poller.cancel()
            try:
                await self.poller
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self.tasks):
            t.cancel()
        try:
            self.c.remove_event_handler(self.on_in)
            self.c.remove_event_handler(self.on_out)
        except Exception:
            pass
        await self.c.disconnect()

    async def load(self, row):
        ent = await self.c.get_entity(row["username"])
        full = await self.c(GetFullChannelRequest(ent))
        linked = full.full_chat.linked_chat_id
        self.ents[row["chat_id"]] = ent
        for chat in full.chats:
            if linked and chat.id == linked:
                self.groups[row["disc_id"]] = chat
        return ent, full

    async def refresh(self):
        rows = await q("SELECT * FROM channels")
        self.chans = {r["chat_id"]: r for r in rows}
        self.by_disc = {r["disc_id"]: r for r in rows if r["disc_id"]}
        for r in rows:
            if r["disc_id"]:   # grup diskusi channel otomatis dapat asisten (bisa dimatikan di panel)
                await x("INSERT OR IGNORE INTO tg_groups(chat_id, title, enabled) VALUES(?, ?, 1)",
                        (r["disc_id"], f"{r['title'] or r['username']} (diskusi)"))
        for r in rows:
            if r["chat_id"] not in self.ents:
                try:
                    await self.load(r)
                except Exception as e:
                    await log_("ERROR", f"Gagal memuat @{r['username']}: {short(e)}")

    async def resolve(self, name):
        ent = await self.c.get_entity(name)
        if not isinstance(ent, Channel) or not ent.broadcast:
            raise ValueError("bukan channel broadcast")
        full = await self.c(GetFullChannelRequest(ent))
        linked = full.full_chat.linked_chat_id
        if not linked:
            raise ValueError("channel belum punya grup diskusi (kolom komentar mati)")
        chat_id = utils.get_peer_id(ent)
        disc_id = utils.get_peer_id(PeerChannel(linked))
        self.ents[chat_id] = ent
        for chat in full.chats:
            if chat.id == linked:
                self.groups[disc_id] = chat
        return {"username": name, "chat_id": chat_id, "disc_id": disc_id, "title": ent.title}

    # ----- diagnostik -----
    async def diagnose(self, ch):
        out = []
        try:
            ent = self.ents.get(ch["chat_id"]) or (await self.load(ch))[0]
            out.append("✅ Channel terbaca")
        except Exception as e:
            return [f"❌ Channel tidak terbaca: {esc(short(e))}"]
        try:
            await self.c.get_permissions(ent, self.me)
            out.append("✅ Userbot subscriber channel (deteksi realtime)")
        except errors.UserNotParticipantError:
            out.append(f"⚠️ Userbot belum subscribe channel. Deteksi lewat cek berkala tiap {POLL_SEC} detik, tetap jalan.")
        except Exception as e:
            out.append(f"⚠️ Status subscribe tidak diketahui: {esc(short(e))}")
        try:
            full = await self.c(GetFullChannelRequest(ent))
            linked = full.full_chat.linked_chat_id
        except Exception as e:
            return out + [f"❌ Gagal membaca info channel: {esc(short(e))}"]
        if not linked:
            return out + ["❌ Channel belum terhubung ke grup diskusi (kolom komentar mati)"]
        grp = next((c for c in full.chats if c.id == linked), None)
        out.append(f"✅ Grup diskusi: {esc(grp.title) if grp else linked}")
        if grp is not None:
            try:
                await self.c.get_permissions(grp, self.me)
                out.append("✅ Userbot sudah join grup diskusi")
            except errors.UserNotParticipantError:
                out.append("❌ Userbot BELUM join grup diskusi. Join dulu supaya bisa komentar.")
            except Exception as e:
                out.append(f"⚠️ Status join grup tidak diketahui: {esc(short(e))}")
            rights = getattr(grp, "default_banned_rights", None)
            if rights is not None and getattr(rights, "send_messages", False):
                out.append("❌ Grup melarang anggota mengirim pesan")
        try:
            last = await self.c.get_messages(ent, limit=1)
            if last:
                await self.c(GetDiscussionMessageRequest(peer=ent, msg_id=last[0].id))
                out.append(f"✅ Komentar aktif di postingan terakhir (#{last[0].id})")
        except errors.MsgIdInvalidError:
            out.append("❌ Postingan terakhir tidak punya kolom komentar")
        except Exception as e:
            out.append(f"⚠️ Cek komentar gagal: {esc(short(e))}")
        return out

    # ----- penerimaan event -----
    def spawn(self, coro):
        t = asyncio.create_task(self.guard(coro))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def guard(self, coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except AIError as e:
            await stat("error")
            await log_("ERROR", f"AI gagal: {e}")
            await report("❌ AI gagal membalas", f"❓ {esc(str(e))}", key="ai", every=60)
        except Exception as e:
            log.exception("handler")
            await stat("error")
            await log_("ERROR", repr(e)[:250])
            await report("❌ Error tak terduga", f"❓ {esc(explain(e))}", key="exc", every=60)

    async def on_in(self, ev):
        if ev.is_private:
            self.spawn(self.dm(ev))
        elif ev.chat_id in self.chans:
            if not ev.message.action:
                self.spawn(self.on_post(ev.message, self.chans[ev.chat_id], "realtime"))
        elif ev.chat_id in self.by_disc:
            self.spawn(self.group(ev))
        elif ev.is_group:
            self.spawn(self.assistant(ev))

    async def on_out(self, ev):
        if ev.is_private and ev.chat_id != self.me.id:
            self.spawn(self.takeover(ev))

    # ----- cek berkala (cadangan kalau update realtime tidak masuk) -----
    async def poll_loop(self):
        await asyncio.sleep(3)
        while self.ready:
            if not self.learning and time.time() >= self.next_resume:
                # Belajar semua isi yang belum selesai (bot restart, error) dilanjutkan sendiri.
                self.next_resume = time.time() + 60
                try:
                    for c in list(self.chans.values()):
                        if await count(f"learnfull:{c['chat_id']}") == 1:
                            self.spawn(self.learn_all(None, who=True, resume=True))
                            break
                except Exception as e:
                    log.warning("lanjut belajar gagal: %s", e)
            if time.time() >= self.next_learn:
                # Belajar otomatis dari channel: cek tiap jam, jalan kalau sudah lewat 12 jam.
                self.next_learn = time.time() + 3600
                try:
                    s0 = await settings()
                    if s0["learn_on"] and time.time() - s0["learn_ts"] > LEARN_EVERY:
                        self.spawn(self.learn_all(30))
                except Exception as e:
                    log.warning("jadwal belajar gagal: %s", e)
            for ch in list(self.chans.values()):
                if not self.ready:
                    break
                try:
                    await self.poll(ch)
                except errors.FloodWaitError as e:
                    await asyncio.sleep(min(e.seconds + 1, 120))
                except Exception as e:
                    await log_("ERROR", f"Cek @{ch['username']} gagal: {short(e)}")
                    await report(
                        "⚠️ Gagal mengecek channel",
                        f"📡 @{esc(ch['username'])}\n❓ {esc(explain(e))}",
                        key=f"poll:{ch['username']}", every=300,
                    )
            await asyncio.sleep(POLL_SEC)

    async def poll(self, ch):
        ent = self.ents.get(ch["chat_id"])
        if ent is None:
            ent = (await self.load(ch))[0]
        cur = await cursor_get(ch["chat_id"])
        if cur is None:
            last = await self.c.get_messages(ent, limit=1)
            await cursor_bump(ch["chat_id"], last[0].id if last else 0)
            return
        msgs = await self.c.get_messages(ent, limit=30, min_id=cur)
        msgs = [m for m in msgs if m]
        for m in sorted((m for m in msgs if not m.action), key=lambda m: m.id):
            await self.on_post(m, ch, "cek berkala")
        if msgs:
            await cursor_bump(ch["chat_id"], max(m.id for m in msgs))

    async def migrate_learning(self):
        """Aturan belajar berubah (sekarang SEMUA komentar dipelajari): semua channel dipelajari ulang dari awal,
        sekali saja. Contoh yang sudah ada tidak digandakan. Berjalan sendiri di latar belakang."""
        if await count("learnver") == LEARN_VERSION:
            return
        await x("DELETE FROM counters WHERE key LIKE 'learnpos:%'")
        for c in self.chans.values():
            await x(SETCNT, (f"learnfull:{c['chat_id']}", 1))
        await x(SETCNT, ("learnver", LEARN_VERSION))
        await log_("INFO", "Aturan belajar diperbarui: semua channel dipelajari ulang (semua komentar)")

    # ----- grup: daftar, konteks, asisten -----
    async def register_groups(self, dialogs):
        for d in dialogs:
            try:
                if d.is_group:
                    await x("INSERT INTO tg_groups(chat_id, title, enabled) VALUES(?, ?, ?) "
                            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                            (d.id, d.name or str(d.id), 1 if d.id in self.by_disc else 0))
            except Exception as e:
                log.warning("daftar grup gagal: %s", e)

    async def group_row(self, chat_id, title, enabled=0):
        rows = await q("SELECT * FROM tg_groups WHERE chat_id=?", (chat_id,))
        if rows:
            return rows[0]
        await x("INSERT OR IGNORE INTO tg_groups(chat_id, title, enabled) VALUES(?, ?, ?)", (chat_id, title, enabled))
        return (await q("SELECT * FROM tg_groups WHERE chat_id=?", (chat_id,)))[0]

    def fmt_lines(self, msgs):
        lines = []
        for m in msgs:
            who = "Aku (AI)" if m.sender_id == self.me.id else (getattr(m.sender, "first_name", None) or "Seseorang")
            lines.append(f"{who}: {clean_ex(m.message, 160)}")
        return "\n".join(lines)

    async def thread_context(self, chat_id, root_id, skip_id=None, n=10):
        """Komentar terakhir di satu thread (termasuk dari orang lain) supaya AI paham arah obrolannya."""
        try:
            msgs = await self.c.get_messages(chat_id, limit=n, reply_to=root_id)
        except Exception:
            return ""
        keep = [m for m in msgs if m and (m.message or "").strip() and m.id != skip_id]
        return self.fmt_lines(list(reversed(keep)))

    async def group_context(self, chat_id, msg, discussion, n=10):
        try:
            kw = {}
            if discussion and msg.reply_to:
                top = getattr(msg.reply_to, "reply_to_top_id", None) or getattr(msg.reply_to, "reply_to_msg_id", None)
                if top:
                    kw["reply_to"] = top
            msgs = await self.c.get_messages(chat_id, limit=n, **kw)
        except Exception:
            return ""
        keep = [m for m in msgs if m and (m.message or "").strip() and m.id != msg.id]
        return self.fmt_lines(list(reversed(keep)))

    async def called(self, msg, text, s):
        """Apakah pesan ini memanggil userbot: mention, reply ke pesan userbot, atau menyebut nama panggilan."""
        if getattr(msg, "mentioned", False):
            return True
        low = text.lower()
        uname = (getattr(self.me, "username", None) or "").lower()
        if uname and f"@{uname}" in low:
            return True
        for n in s["group_names"]:
            n = n.strip().lower()
            if n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", low):
                return True
        if msg.reply_to_msg_id:
            try:
                tgt = await msg.get_reply_message()
                return tgt is not None and tgt.sender_id == self.me.id
            except Exception:
                return False
        return False

    async def assistant(self, ev, discussion=False, s=None):
        """Asisten di grup: menjawab kalau dipanggil (mention, reply, nama panggilan) dan, kalau Nimbrung
        dinyalakan untuk grup itu, sesekali ikut ngobrol. Hanya di grup yang diaktifkan di panel."""
        s = s or await settings()
        if not s["group_on"]:
            return
        msg = ev.message
        if msg.action or msg.out or msg.sender_id == self.me.id:
            return
        text = (msg.message or "").strip()
        if not text:
            return
        chat = await ev.get_chat()
        g = await self.group_row(ev.chat_id, getattr(chat, "title", "") or str(ev.chat_id), 1 if discussion else 0)
        if not g["enabled"]:
            return
        sender = await msg.get_sender()
        if is_ignored(sender, s):
            return
        if msg.sender_id and (await user(msg.sender_id))["lst"] == "black":
            return
        mode = "group"
        if not await self.called(msg, text, s):
            if discussion or not g["free"] or s["nimbrung_prob"] <= 0:
                return
            if time.time() - self.nimbrung_at.get(ev.chat_id, 0) < NIMBRUNG_GAP:
                return
            if random.uniform(0, 100) > s["nimbrung_prob"]:
                return
            mode = "nimbrung"
        hk = f"grp:{ev.chat_id}:{now_wib():%Y%m%d%H}"
        if s["group_hour"] > 0 and await count(hk) >= s["group_hour"]:
            return await skip("batas balasan grup per jam tercapai")
        if mode == "nimbrung":
            self.nimbrung_at[ev.chat_id] = time.time()
        name = getattr(sender, "first_name", None) or "Seseorang"
        await humanize(s)
        ctx = await self.group_context(ev.chat_id, msg, discussion)
        quoted = ""
        if msg.reply_to_msg_id:
            try:
                tgt = await msg.get_reply_message()
                if tgt is not None and (tgt.message or "").strip():
                    who = "Aku (AI)" if tgt.sender_id == self.me.id else (getattr(tgt.sender, "first_name", None) or "Seseorang")
                    quoted = f'Pesan ini membalas {who}: "{clean_ex(tgt.message, 200)}"\n'
            except Exception:
                pass
        chrow = self.by_disc.get(ev.chat_id)
        extra = await examples_block(chrow["username"] if chrow else None, "reply", text, s)
        prompt = (f"Percakapan terakhir di grup:\n{ctx}\n\n" if ctx else "")
        prompt += f'Pesan dari {name} yang perlu kamu tanggapi:\n"""\n{text[:800]}\n"""\n{quoted}'
        prompt += ("Tulis satu balasan singkat yang nyambung dengan obrolan." if mode == "group" else
                   "Kamu nimbrung tanpa dipanggil. Balas hanya kalau kamu bisa menambah sesuatu yang nyambung, "
                   "kalau tidak jawab persis: SKIP.")
        async with AI_SEM:
            reply = await ask_ai(mode, [{"role": "user", "content": prompt}], s, extra=extra)
        if is_skip(reply):
            return await skip(f"asisten grup {g['title'] or ev.chat_id}: AI memilih SKIP")
        async with SEND_LOCK:
            await self.typing(ev.chat_id, min(1 + len(reply) * 0.04, 5))
            await self.c.send_message(ev.chat_id, reply, reply_to=msg.id)
            await asyncio.sleep(SEND_GAP)
        await incr(hk)
        await stat("group")
        await log_("INFO", f"Asisten grup ({mode}) membalas di {g['title'] or ev.chat_id}: {reply[:60]}")

    async def catch_up(self, dialogs):
        """DM belum dibaca yang masuk saat bot mati (jeda antar run Actions) dibalas begitu bot menyala lagi."""
        await asyncio.sleep(5)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=CATCHUP_MIN)
        n = 0
        for d in dialogs:
            try:
                m = d.message
                if (d.is_user and not getattr(d.entity, "bot", False) and d.unread_count > 0 and m is not None
                        and not m.out and m.date and m.date > cutoff and (m.raw_text or "").strip()):
                    n += 1
                    self.spawn(self.dm(m))
            except Exception:
                continue
        if n:
            await log_("INFO", f"Menyusul {n} DM yang masuk saat bot mati")

    # ----- A. menfess masuk channel -----
    async def group(self, ev):
        ch = self.by_disc[ev.chat_id]
        msg = ev.message
        fwd = msg.fwd_from
        from_channel = bool(
            fwd and fwd.channel_post and (
                (fwd.from_id and utils.get_peer_id(fwd.from_id) == ch["chat_id"])
                or (not fwd.from_id and msg.sender_id == 777000)
            )
        )
        if from_channel:
            return await self.on_post(None, ch, "grup diskusi", group_msg=msg)
        s = await settings()
        if msg.reply_to_msg_id and msg.sender_id != self.me.id:
            tgt = await msg.get_reply_message()
            if tgt is not None and tgt.sender_id == self.me.id:
                return await self.reply(msg, s)   # balasan ke komentar bot: jalur thread
        await self.assistant(ev, True, s)

    async def on_post(self, msg, ch, src, group_msg=None):
        base = msg if msg is not None else group_msg
        post_id = msg.id if msg is not None else group_msg.fwd_from.channel_post
        if not await claim(ch["chat_id"], post_id):
            return  # sudah ditangani jalur lain
        await cursor_bump(ch["chat_id"], post_id)

        gid = getattr(base, "grouped_id", None)
        if gid:
            key = (ch["chat_id"], gid)
            if key in self.albums:
                return  # bagian lain dari album yang sama
            if len(self.albums) > 500:
                self.albums.clear()
            self.albums.add(key)

        s = await settings()
        text = (base.message or "").strip()
        LAST.update(id=post_id, ts=time.time(), ch=ch["username"])
        await log_("INFO", f"Terdeteksi menfess #{post_id} di @{ch['username']} ({len(text)} karakter) via {src}")

        age = (datetime.now(timezone.utc) - base.date).total_seconds()
        reason = None
        if ch["paused"]:
            reason = "Channel sedang dijeda di panel."
        elif not s["comment_on"]:
            reason = "Fitur Komentar Menfess dimatikan di Mode Chat."
        elif age > s["max_age_min"] * 60:
            reason = (f"Postingan berumur {int(age // 60)} menit (batas {s['max_age_min']} menit). "
                      "Kemungkinan bot sempat mati.")
        elif len(text) < s["min_len"]:
            reason = f"Teks {len(text)} karakter, di bawah batas minimal {s['min_len']}."
        elif (hit := next((w for w in s["banned"] if w and w.lower() in text.lower()), None)) is not None:
            reason = f"Mengandung kata/karakter terlarang: {hit}"
        elif s["reply_prob"] < 100 and not s["free_mode"] and random.uniform(0, 100) > s["reply_prob"]:
            reason = f"Terkena peluang balas {s['reply_prob']}%."
        elif s["keywords"] and not any(k.lower() in text.lower() for k in s["keywords"]):
            reason = "Tidak ada keyword filter yang cocok."
        if reason:
            await skip(f"#{post_id}: {reason}")
            return await report(
                "⏭ Menfess tidak dibalas", f"{self.ctx(ch, post_id, text)}\n❓ {esc(reason)}",
                key="skip:" + reason[:18], every=30,
            )
        self.spawn(self.reply_post(ch, post_id, text, group_msg, s, base))

    @staticmethod
    def ctx(ch, post_id, text):
        return f"📡 @{esc(ch['username'])} · menfess #{post_id}\n📝 {esc(text[:80]) or '[tanpa teks]'}"

    async def reply_post(self, ch, post_id, text, group_msg, s, base=None):
        ctx = self.ctx(ch, post_id, text)
        hk = f"hour:{now_wib():%Y%m%d%H}:c"
        if s["max_comment_hour"] > 0 and await count(hk) >= s["max_comment_hour"]:
            await skip(f"#{post_id}: batas komentar per jam")
            return await report("⏭ Menfess tidak dibalas",
                                f"{ctx}\n❓ Batas komentar per jam ({s['max_comment_hour']}) tercapai.", key="hourlimit")

        await humanize(s)
        media, image = "", None
        if base is not None:
            if getattr(base, "photo", None):
                media = "photo"
                if s["vision_on"]:
                    try:
                        data = await self.c.download_media(base, file=bytes)
                        if data and len(data) <= 4_000_000:
                            image = data
                    except Exception as e:
                        log.warning("Foto gagal diunduh: %s", short(e))
            elif getattr(base, "video", None) or getattr(base, "gif", None):
                media = "video"
            elif getattr(base, "sticker", None):
                media = "sticker"
        note = {"photo": "Menfess ini menyertakan foto.",
                "video": "Menfess ini menyertakan video (isinya tidak terlihat).",
                "sticker": "Menfess ini berupa stiker."}.get(media, "")
        content = text[:800] or f"(menfess ini tanpa teks{', hanya media' if media else ''})"
        ending = ("Tulis satu komentar untuk menfess ini sesuai aturan panjang balasan. Jangan jawab SKIP."
                  if s["free_mode"] else
                  "Tulis satu komentar untuk menfess ini sesuai aturan panjang balasan. "
                  "Kalau kamu benar-benar tidak paham maksudnya, jawab persis: SKIP.")
        prompt = (
            f'Menfess:\n"""\n{content}\n"""\n{note}\n'
            + ending
        )
        extra = await examples_block(ch["username"], "menfess", text, s)
        reply, last_err = None, ""
        for attempt in range(1, 4):
            try:
                async with AI_SEM:
                    reply = await ask_ai("menfess", [{"role": "user", "content": prompt}], s, image, extra)
                break
            except AIError as e:
                last_err = str(e)
                await log_("ERROR", f"AI gagal #{post_id} (percobaan {attempt}/3): {last_err}")
                if attempt < 3:
                    await asyncio.sleep(8 * attempt)
        if reply is None:
            await stat("error")
            return await report(
                "❌ Menfess tidak dibalas: AI gagal",
                f"{ctx}\n❓ {esc(last_err)}\n💡 Buka Pengaturan AI → Tes AI untuk memeriksa tiap provider.",
                key="ai",
            )
        if is_skip(reply):
            await skip(f"#{post_id}: AI menolak membalas")
            return await report(
                "⏭ Menfess tidak dibalas",
                f"{ctx}\n❓ AI memilih tidak membalas (tidak paham konteksnya atau isinya sensitif). "
                "Nyalakan Mode Bebas di Mode Chat kalau mau tetap dibalas.", key="aiskip",
            )

        try:
            await self.deliver(ch, post_id, reply, group_msg)
        except Exception as e:
            await stat("error")
            await log_("ERROR", f"Gagal kirim #{post_id}: {type(e).__name__}")
            return await report(
                "❌ Menfess tidak dibalas: gagal kirim komentar",
                f"{ctx}\n❓ {esc(explain(e))}", key=f"send:{type(e).__name__}",
            )
        await incr(hk)
        await stat("comment")
        await log_("INFO", f"Komentar terkirim ke #{post_id}: {reply[:60]}")
        if s["notify_ok"]:
            await notify(f"✅ <b>Komentar terkirim</b>\n🕒 {fmt()}\n{ctx}\n💬 {esc(reply[:120])}")

    async def typing(self, chat, secs):
        try:
            async with self.c.action(chat, "typing"):
                await asyncio.sleep(secs)
        except Exception:
            pass

    async def deliver(self, ch, post_id, reply, group_msg):
        disc_id = ch["disc_id"]
        async with SEND_LOCK:
            for attempt in range(6):
                try:
                    secs = min(1 + len(reply) * 0.04, 5)
                    if group_msg is not None:
                        await self.typing(group_msg.chat_id, secs)
                        await self.c.send_message(group_msg.chat_id, reply, reply_to=group_msg.id)
                    else:
                        ent = self.ents.get(ch["chat_id"])
                        if ent is None:
                            ent = (await self.load(ch))[0]
                        await self.typing(self.groups.get(disc_id) or disc_id, secs)
                        await self.c.send_message(ent, reply, comment_to=post_id)
                    await asyncio.sleep(SEND_GAP)
                    return
                except errors.MsgIdInvalidError:
                    # Salinan postingan di grup diskusi kadang belum muncul. Tunggu sebentar lalu coba lagi.
                    if attempt >= 4:
                        raise
                    await asyncio.sleep(3)
                except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
                    wait = getattr(e, "seconds", 30)
                    if wait > 120 or attempt >= 5:
                        raise
                    await asyncio.sleep(wait + 1)
                except (errors.UserNotParticipantError, errors.ChannelPrivateError):
                    grp = self.groups.get(disc_id)
                    if attempt >= 1 or disc_id in self.joined or grp is None:
                        raise
                    self.joined.add(disc_id)
                    await self.c(JoinChannelRequest(grp))  # coba join otomatis
                    await asyncio.sleep(2)

    # ----- belajar dari channel -----
    async def learn_post(self, ent, post, s, user_ch):
        """Pelajari satu menfess beserta SEMUA komentarnya (termasuk balasan antar komentar).
        None = tidak bisa dibaca (tanpa kolom komentar), angka = contoh baru yang tersimpan."""
        if post.action:
            return None
        src = clean_ex(post.message, 160) or media_label(post)
        if not src:
            return None
        for _ in range(2):
            comments = []
            try:
                async for m in self.c.iter_messages(ent, reply_to=post.id, limit=LEARN_MAX_COMMENTS):
                    comments.append(m)
                break
            except errors.FloodWaitError as e:
                await asyncio.sleep(min(e.seconds + 1, 90))
            except Exception:
                return None   # postingan tanpa kolom komentar
        else:
            return None
        by_id = {m.id: m for m in comments}
        added = 0
        for m in comments:
            try:
                added += await self.learn_comment(m, by_id, src, s, user_ch)
            except Exception as e:   # satu komentar bermasalah tidak boleh membuang komentar lainnya
                self.lstat["gagal"] += 1
                log.warning("komentar gagal dipelajari: %s", short(e))
        await db.commit()
        return added

    async def learn_comment(self, m, by_id, src, s, user_ch):
        """Simpan satu komentar sebagai contoh untuk menfess-nya. Return jumlah contoh baru."""
        st = self.lstat
        st["dibaca"] += 1
        if m.action or m.sender_id == self.me.id:
            st["akun sendiri"] += 1
            return 0
        if is_ignored(m.sender, s):
            st["bot/diabaikan"] += 1
            return 0
        txt = clean_ex(m.message, 200)   # link dan @mention dibuang dari teks, komentarnya tetap dipelajari
        if not txt:
            st["tanpa teks"] += 1
            return 0
        ts = int(m.date.timestamp()) if m.date else None
        # Semua komentar jadi contoh untuk menfess ini, termasuk yang membalas komentar orang lain.
        new = await add_example(user_ch, "menfess", src, txt, ts)
        st["baru" if new else "sudah ada"] += 1
        rt = m.reply_to
        top = getattr(rt, "reply_to_top_id", None) if rt else None
        nested = bool(rt and top and rt.reply_to_msg_id != top)   # balasan ke komentar lain
        parent = by_id.get(m.reply_to_msg_id) if nested else None
        if parent is not None:
            ptxt = clean_ex(parent.message, 160)
            if ptxt and parent.sender_id != self.me.id and not is_ignored(parent.sender, s):
                new += await add_example(user_ch, "reply", ptxt, txt, ts)   # pasangan komentar -> balasan
        return new

    async def fetch_page(self, ent, limit, offset):
        """Ambil satu halaman postingan (terbaru ke terlama). Kena FloodWait atau koneksi putus:
        tunggu lalu coba lagi, jangan langsung menyerah."""
        for attempt in range(8):
            try:
                return [m for m in await self.c.get_messages(ent, limit=limit, offset_id=offset) if m]
            except errors.FloodWaitError as e:
                await asyncio.sleep(min(e.seconds + 1, 300))
            except (ConnectionError, OSError, asyncio.TimeoutError):
                await asyncio.sleep(min(5 * (attempt + 1), 60))
        raise RuntimeError("gagal mengambil postingan (koneksi putus atau FloodWait berulang)")

    async def learn_batch(self, ent, batch, s, user_ch):
        """Pelajari beberapa postingan sekaligus (paralel). Satu postingan gagal tidak menghentikan yang lain."""
        res = await asyncio.gather(*(self.learn_post(ent, p, s, user_ch) for p in batch), return_exceptions=True)
        scanned = added = 0
        for r in res:
            if isinstance(r, BaseException) or r is None:
                continue
            scanned += 1
            added += r
        return scanned, added

    async def learn(self, ch, posts=40):
        """Baca postingan dan komentar aslinya. Yang disimpan hanya teks (tanpa nama atau ID).
        posts=None berarti SEMUA isi channel, dari yang terbaru ke terlama. Posisinya disimpan tiap
        kelompok postingan, jadi kalau terhenti (restart, error) bisa dilanjutkan dari posisi terakhir.
        Return None kalau semuanya sudah selesai."""
        s = await settings()
        user_ch = ch["username"]
        ent = self.ents.get(ch["chat_id"]) or await self.c.get_entity(user_ch)
        pk = f"learnpos:{ch['chat_id']}"
        fk = f"learnfull:{ch['chat_id']}"
        full = posts is None
        offset = 0
        if full:
            pos = await count(pk)
            if pos == -1:
                return None
            offset = max(pos, 0)
        scanned = added = seen_posts = since_bk = 0
        last_bk = time.time()
        completed = True
        while full or seen_posts < posts:
            want = LEARN_PAGE if full else min(LEARN_PAGE, posts - seen_posts)
            page = await self.fetch_page(ent, want, offset)
            if not page:
                break
            for i in range(0, len(page), LEARN_PARALLEL):
                if self.learn_stop:
                    completed = False
                    break
                batch = page[i:i + LEARN_PARALLEL]
                sc, ad = await self.learn_batch(ent, batch, s, user_ch)
                seen_posts += len(batch)
                scanned += sc
                added += ad
                since_bk += ad
                offset = batch[-1].id
                if full:
                    await x(SETCNT, (pk, offset))
                self.prog = f"@{user_ch}: {seen_posts} post, {self.lstat['dibaca']} komentar dibaca, {added} contoh baru"
                if since_bk and time.time() - last_bk > BACKUP_EVERY:
                    await self.backup_safe()
                    since_bk, last_bk = 0, time.time()
                if sc:
                    await asyncio.sleep(LEARN_GAP)   # jeda singkat supaya tidak kena FloodWait
            if not completed:
                break
        if full and completed:
            await x(SETCNT, (pk, -1))
            await x(SETCNT, (fk, 0))
        await x("DELETE FROM examples WHERE ch=? AND id NOT IN "
                "(SELECT id FROM examples WHERE ch=? ORDER BY ts DESC LIMIT ?)", (user_ch, user_ch, MAX_EXAMPLES))
        return scanned, added

    async def learn_all(self, posts=40, who=False, only=None, resume=False):
        if self.learning:
            return
        self.learning = True
        self.learn_stop = False
        self.prog = ""
        lines, total, had_err = [], 0, False
        try:
            targets = []
            for c in self.chans.values():
                if only is not None and c["username"] != only:
                    continue
                if resume and await count(f"learnfull:{c['chat_id']}") != 1:
                    continue
                targets.append(c)
            if posts is None:
                for c in targets:   # tandai supaya belajar lanjut sendiri kalau terhenti (restart, error)
                    await x(SETCNT, (f"learnfull:{c['chat_id']}", 1))
            for ch in targets:
                if self.learn_stop:
                    break
                try:
                    self.lstat = Counter()
                    res = await self.learn(ch, posts)
                    if res is None:
                        await x(SETCNT, (f"learnfull:{ch['chat_id']}", 0))
                        lines.append(f"@{ch['username']}: semua isi sudah pernah dipelajari "
                                     "(pakai Ulang dari awal kalau mau mengulang)")
                        continue
                    sc, ad = res
                    total += ad
                    tail = " (dihentikan, bisa dilanjutkan)" if self.learn_stop else ""
                    st = self.lstat
                    skipped = st["akun sendiri"] + st["bot/diabaikan"] + st["tanpa teks"]
                    lines.append(
                        f"@{ch['username']}: {sc} postingan dibaca, {st['dibaca']} komentar dibaca "
                        f"({st['baru']} baru disimpan, {st['sudah ada']} sudah ada, {skipped} dilewati: "
                        f"bot {st['bot/diabaikan']}, tanpa teks {st['tanpa teks']}, akun sendiri {st['akun sendiri']}"
                        f"{', error ' + str(st['gagal']) if st['gagal'] else ''})"
                        f"{tail}")
                except Exception as e:
                    had_err = True
                    more = "lanjut otomatis sebentar lagi" if posts is None else "tekan lagi untuk melanjutkan"
                    lines.append(f"@{ch['username']}: terhenti ({short(e)}), {more}")
            if total:
                await self.backup_safe()
            if only is None and not resume:   # belajar satu channel tidak menunda jadwal belajar otomatis channel lain
                await setv("learn_ts", int(time.time()))
            if had_err:
                self.next_resume = time.time() + 300   # kalau error, coba lanjut lagi 5 menit kemudian
            await log_("INFO", "Belajar channel selesai: " + "; ".join(lines))
            if who and bot and not (resume and had_err):
                try:
                    await bot.send_message(OWNER_ID, "📚 <b>Selesai belajar</b>\n\n" + "\n".join(esc(l) for l in lines or ["Belum ada channel."]))
                except Exception:
                    pass
        finally:
            self.learning = False
            self.prog = ""

    # ----- backup hasil belajar ke Pesan Tersimpan (akun userbot) -----
    async def backup(self):
        """Simpan semua contoh dan progres belajar sebagai satu file di Pesan Tersimpan, sisakan yang terbaru."""
        rows = await q("SELECT ch, kind, src, reply, ts FROM examples")
        if not rows:
            return 0
        prog = await q("SELECT key, value FROM counters WHERE key LIKE 'learnpos:%' OR key LIKE 'learnfull:%'")
        payload = {"v": 1, "examples": rows, "progress": prog}
        f = io.BytesIO(gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        f.name = "menfess_belajar.json.gz"
        sent = await self.c.send_file("me", f, force_document=True,
                                      caption=f"{BACKUP_TAG} · {len(rows)} contoh · {fmt()}")
        await self.drop_backups(keep=sent.id)
        return len(rows)

    async def backup_safe(self):
        try:
            n = await self.backup()
            if n:
                await log_("INFO", f"Backup hasil belajar: {n} contoh")
        except Exception as e:
            await log_("ERROR", f"Backup gagal: {short(e)}")

    async def drop_backups(self, keep=None):
        old = []
        async for m in self.c.iter_messages("me", search=BACKUP_TAG, limit=50):
            if m.id != keep and m.document and (m.message or "").startswith(BACKUP_TAG):
                old.append(m.id)
        if old:
            await self.c.delete_messages("me", old)

    async def restore(self):
        """Gabungkan contoh dari backup terbaru ke database (yang sudah ada tidak ditimpa)."""
        target = None
        async for m in self.c.iter_messages("me", search=BACKUP_TAG, limit=50):
            if m.document and (m.message or "").startswith(BACKUP_TAG):
                target = m
                break
        if target is None:
            return 0
        if target.file and target.file.size and target.file.size > 30_000_000:
            raise ValueError("file backup terlalu besar")
        data = await self.c.download_media(target, file=bytes)
        payload = json.loads(gzip.decompress(data).decode("utf-8"))
        added = 0
        for r in payload.get("examples", []):
            try:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO examples(ch, kind, src, reply, ts) VALUES(?, ?, ?, ?, ?)",
                    (str(r["ch"]), str(r["kind"]), str(r["src"]), str(r["reply"]), int(r.get("ts") or 0)),
                )
                added += max(cur.rowcount, 0)
            except (KeyError, TypeError, ValueError):
                continue
        for p in payload.get("progress", []):
            try:
                await db.execute("INSERT OR IGNORE INTO counters(key, value) VALUES(?, ?)",
                                 (str(p["key"]), int(p["value"])))
            except (KeyError, TypeError, ValueError):
                continue
        await db.commit()
        return added

    async def auto_restore(self):
        """Kalau database baru/kosong (misalnya server berganti), pulihkan hasil belajar dari backup."""
        try:
            if (await q("SELECT COUNT(*) AS n FROM examples"))[0]["n"] == 0:
                n = await self.restore()
                if n:
                    await log_("INFO", f"Hasil belajar dipulihkan dari backup: {n} contoh")
                    await notify(f"♻️ Hasil belajar dipulihkan dari backup: {n} contoh.\n🕒 {fmt()}")
        except Exception as e:
            await log_("ERROR", f"Pulihkan backup gagal: {short(e)}")

    # ----- B. balas reply ke komentar userbot -----
    async def reply(self, msg, s):
        if not s["reply_on"]:
            return
        target = await msg.get_reply_message()
        if not target or target.sender_id != self.me.id:
            return
        root = await find_root(target)
        if not root:
            return await skip("reply bukan ke thread menfess")
        sender = await msg.get_sender()
        if is_ignored(sender, s):
            return await skip("pengirim adalah bot atau ada di daftar abaikan")
        uid = msg.sender_id
        if (await user(uid))["lst"] == "black":
            return await skip("pengirim ada di blacklist")
        text = (msg.message or "").strip()
        if len(text) < 2:
            return await skip("reply terlalu pendek")
        thread = f"t:{msg.chat_id}:{root.id}"
        closed = f"closed:{thread}:{uid}"
        if await count(closed):
            return await skip("percakapan sudah ditutup")
        if CLOSING.match(text):
            await incr(closed)
            return await skip("pesan penutup")
        day = f"day:{uid}:{now_wib():%Y%m%d}"
        if await count(f"n:{thread}") >= s["max_thread"]:
            return await skip("batas balasan per thread tercapai")
        if await count(day) >= s["max_user_day"]:
            return await skip("batas balasan per user tercapai")
        if not s["free_mode"] and random.uniform(0, 100) > s["thread_prob"]:
            return await skip("reply kena peluang balas")

        name = getattr(sender, "first_name", None) or "Seseorang"
        await hist_add(thread, "user", f"{name}: {text}")
        await humanize(s)
        post = (root.message or "")[:500]
        around = await self.thread_context(msg.chat_id, root.id, skip_id=msg.id)
        head = f"Menfess asli:\n{post}" + (f"\n\nKomentar terakhir di thread ini:\n{around}" if around else "")
        msgs = [{"role": "user", "content": head}] + await hist_get(thread, 10)
        chrow = self.by_disc.get(msg.chat_id)
        extra = await examples_block(chrow["username"], "reply", text, s) if chrow else ""
        async with AI_SEM:
            reply = await ask_ai("reply", msgs, s, extra=extra)
        if is_skip(reply):
            return await skip("AI memilih SKIP")
        async with SEND_LOCK:
            await self.typing(msg.chat_id, min(1 + len(reply) * 0.04, 5))
            self.ai_at[msg.chat_id] = time.time()
            await self.c.send_message(msg.chat_id, reply, reply_to=msg.id)
            await asyncio.sleep(SEND_GAP)
        await hist_add(thread, "assistant", reply)
        await incr(f"n:{thread}")
        await incr(day)
        await stat("reply")

    # ----- C. DM -----
    async def peer_of(self, ev):
        """Tujuan kirim yang pasti valid. Id angka saja bisa gagal ("Could not find the input entity")
        kalau access_hash user belum tersimpan di session; entity dari event selalu lengkap."""
        for getter in (ev.get_input_chat, ev.get_input_sender):
            try:
                peer = await getter()
                if peer:
                    return peer
            except Exception:
                pass
        try:
            return await self.c.get_input_entity(ev.sender_id)
        except Exception:
            return ev.sender_id

    def dm_lock(self, uid):
        lock = self.dm_locks.get(uid)
        if lock is None:
            lock = self.dm_locks[uid] = asyncio.Lock()
        return lock

    async def say(self, chat, text, peer=None):
        self.ai_at[chat] = time.time()  # dicatat sebelum kirim supaya tidak dianggap takeover
        await self.c.send_message(peer or chat, text)

    async def dm(self, ev):
        uid = ev.sender_id
        if not uid or uid == self.me.id:
            return
        text = (ev.raw_text or "").strip()
        peer = await self.peer_of(ev)
        if text.lower() == "/stop":
            await user_set(uid, stopped=1)
            return await self.say(uid, "Balasan otomatis dihentikan. Ketik /start untuk mengaktifkan lagi.", peer)
        if text.lower() == "/start":
            await user_set(uid, stopped=0, greeted=1, ai_on=1)
            return await self.say(uid, DISCLOSURE, peer)

        s = await settings()
        try:
            if is_ignored(await ev.get_sender(), s):
                return
        except Exception:
            pass
        u = await user(uid)
        if not s["dm_on"] or not text:
            return
        if u["lst"] == "black" or u["stopped"] or not u["ai_on"]:
            return
        if s["dm_whitelist"] and u["lst"] != "white":
            return
        hk = f"dm:{uid}:{now_wib():%Y%m%d%H}"
        if s["dm_hour"] > 0 and await count(hk) >= s["dm_hour"]:
            return await skip("batas DM per jam tercapai")
        if not u["greeted"]:
            await self.say(uid, DISCLOSURE, peer)
            await user_set(uid, greeted=1)

        key = f"dm:{uid}"
        async with self.dm_lock(uid):   # satu percakapan diproses berurutan
            await hist_add(key, "user", text)
            await humanize(s)
            extra = await examples_block(None, "reply", text, s)
            reply, last_err = None, ""
            for attempt in range(1, 4):
                try:
                    async with AI_SEM:
                        reply = await ask_ai("dm", await hist_get(key, 12), s, extra=extra)
                    break
                except AIError as e:
                    last_err = str(e)
                    await log_("ERROR", f"AI gagal DM {uid} (percobaan {attempt}/3): {last_err}")
                    if attempt < 3:
                        await asyncio.sleep(4 * attempt)
            if reply is None:   # DM tidak boleh didiamkan: beri tahu lawan bicara dan laporkan ke owner
                await stat("error")
                await report("❌ DM tidak dibalas: AI gagal",
                             f"👤 {uid}\n❓ {esc(last_err)}\n💡 Buka Pengaturan AI → Tes AI.", key="dmai", every=60)
                if time.time() - self.fail_at.get(uid, 0) > 600:
                    self.fail_at[uid] = time.time()
                    await self.say(uid, FALLBACK_DM, peer)
                return
            if is_skip(reply):
                reply = FALLBACK_CONFUSED   # DM selalu dibalas
            bubbles = split_bubbles(reply)
            if not bubbles:
                return await skip("balasan AI kosong")
            for i, part in enumerate(bubbles):
                if i:
                    await asyncio.sleep(random.uniform(0.4, 1.2))   # jeda antar bubble seperti orang mengetik
                await self.typing(peer, min(0.8 + len(part) * 0.03, 3))
                await self.say(uid, part, peer)
            await hist_add(key, "assistant", "\n".join(bubbles))
            await incr(hk)
            await stat("dm")

    async def takeover(self, ev):
        """Kalau kamu mengetik manual di DM yang ditangani AI, AI dimatikan untuk user itu."""
        uid = ev.chat_id
        if time.time() - self.ai_at.get(uid, 0) < 10:
            return
        if not (await settings())["takeover_on"]:
            return
        u = await user(uid)
        if u["greeted"] and u["ai_on"]:
            await user_set(uid, ai_on=0)
            await log_("INFO", f"Takeover DM {uid}")
            await notify(f"🙋 Kamu mengambil alih chat {uid}. AI dimatikan untuk user ini.")


# ---------- session: konversi, verifikasi, aktivasi ----------

def session_to_string(path_no_ext):
    """Ubah file .session (SQLite Telethon) menjadi StringSession."""
    old = SQLiteSession(path_no_ext)
    try:
        if old.auth_key is None:
            raise ValueError("file session kosong atau tidak valid")
        new = StringSession()
        new.set_dc(old.dc_id, old.server_address, old.port)
        new.auth_key = old.auth_key
        return new.save()
    finally:
        old.close()


async def verify_string(string):
    c = TelegramClient(StringSession(string), API_ID, API_HASH)
    await c.connect()
    try:
        if not await c.is_user_authorized():
            raise ValueError("session tidak aktif atau sudah logout")
        return await c.get_me()
    finally:
        await c.disconnect()


async def activate(string):
    """Mulai userbot dengan session ini, lalu simpan ke database."""
    global ub
    old_string = ub.string if ub else None
    if ub:
        await ub.stop()
    new = UB(string)
    try:
        await new.start()
    except Exception as e:
        try:
            await new.stop()
        except Exception:
            pass
        if old_string:  # kembalikan userbot lama
            back_ub = UB(old_string)
            try:
                await back_ub.start()
            except Exception:
                pass
            ub = back_ub
        return False, short(e)
    ub = new
    await setv("session", string)
    return True, ""


async def login_close():
    c = LOGIN.get("client")
    LOGIN.clear()
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass


# ---------- panel: helper ----------

class CB(CallbackData, prefix="c"):
    a: str
    b: str = ""


def B(text, a, b=""):
    # Pemisah di dalam b adalah "|" (bukan ":"), karena ":" dipakai aiogram.
    return InlineKeyboardButton(text=text, callback_data=CB(a=a, b=b).pack())


def chunk(items, n=2):
    return [items[i:i + n] for i in range(0, len(items), n)]


def back(to="home", arg=""):
    return [[B("🏠 Beranda", "home"), B("⬅️ Kembali", to, arg)]]


def on(flag):
    return "🟢" if flag else "⚪"


def active_ai(s):
    for p in s["order"]:
        if p not in s["disabled"] and KEYS.get(p):
            return f"{NAMES[p]} · {s['model_' + p]}"
    return "tidak ada (cek API key)"


def user_tag(u):
    if u["lst"] == "white":
        return "✅ Whitelist"
    if u["lst"] == "black":
        return "🚫 Blacklist"
    if not u["ai_on"]:
        return "⛔ AI dimatikan"
    if u["stopped"]:
        return "⏹ /stop"
    return "🤖 AI aktif"


# ---------- screens ----------
# Catatan: nama tombol yang berarti "buka layar" sama dengan kunci di SCREENS.
# Tombol yang berarti "lakukan sesuatu" memakai nama aksi lain (tog, set, pset, dolearn, ...).

async def scr_home(_=""):
    s = await settings()
    chans = await q("SELECT paused FROM channels")
    st = await stats()
    total = st.get("comment", 0) + st.get("reply", 0) + st.get("dm", 0) + st.get("group", 0)
    online = bool(ub and ub.ready)
    active = sum(1 for c in chans if not c["paused"])
    if LAST:
        last = f"#{LAST['id']} di @{esc(LAST['ch'])} · {fmt_short(LAST['ts'])}"
    else:
        last = "belum ada sejak bot menyala"
    text = (
        "<b>🏠 Beranda</b>\n━━━━━━━━━━━━━━━━\n"
        f"🤖 Userbot : {'🟢 Online' if online else '🔴 Offline'}\n"
        f"📡 Channel : {len(chans)} ({active} aktif)\n"
        f"💬 Total balasan : {total}\n"
        f"📨 Postingan terakhir : {last}\n"
        f"🧠 AI : {esc(active_ai(s))}\n"
        f"🕒 {fmt()}"
    )
    rows = [
        [B("📡 Channel", "chan"), B("🤖 Pengaturan AI", "ai")],
        [B("💬 Mode Chat", "chat"), B("📊 Statistik", "stats")],
        [B("⚙️ Sistem", "sys"), B("🔑 Session", "sess")],
        [B("❓ Bantuan", "help")],
    ]
    return text, rows


async def scr_help(_=""):
    text = (
        "<b>❓ Bantuan</b>\n\n"
        "📡 <b>Channel</b>: channel menfess yang dipantau bot.\n\n"
        "🤖 <b>Pengaturan AI</b>: gaya, panjang jawaban, provider AI, dan <b>Belajar dari Channel</b> "
        "(bot meniru balasan asli di channel dan membaca foto).\n\n"
        "💬 <b>Mode Chat</b>: bot boleh membalas apa saja (komentar, reply, DM) dan seberapa sering. "
        "Paling gampang: pilih <b>Profil Cepat</b>.\n\n"
        "🧑‍🤝‍🧑 <b>Asisten Grup</b>: bot ikut menjawab di grup yang kamu aktifkan (Mode Chat → Asisten Grup), "
        "kalau dipanggil atau kalau Nimbrung dinyalakan.\n\n"
        "📊 <b>Statistik</b>: jumlah balasan dan log terakhir.\n\n"
        "⚙️ <b>Sistem</b>: notifikasi, hapus log/riwayat, dan restart.\n\n"
        "🔑 <b>Session</b>: akun Telegram yang dipakai userbot untuk membalas.\n\n"
        "<i>Kalau bot tidak membalas: buka Pengaturan AI → Tes AI, lalu Channel → Diagnostik.</i>"
    )
    return text, back()


async def scr_chan(_=""):
    chans = await q("SELECT * FROM channels ORDER BY username")
    ncount = {r["ch"]: r["n"] for r in await q("SELECT ch, COUNT(*) AS n FROM examples GROUP BY ch")}
    body = "\n".join(
        f"{'⏸' if c['paused'] else '🟢'} @{esc(c['username'])} · {esc(c['title'] or '-')} · 📚 {ncount.get(c['username'], 0)}"
        for c in chans
    ) or "<i>Belum ada channel.</i>"
    rows = [[B(f"{'⏸' if c['paused'] else '🟢'} @{c['username']}", "chanv", c["username"])] for c in chans]
    rows.append([B("➕ Tambah Channel", "in", "channel"), B("🩺 Diagnostik", "diag")])
    note = "\n\n<i>📚 = jumlah contoh gaya yang sudah dipelajari dari channel itu. Tiap channel belajar sendiri-sendiri.</i>" if chans else ""
    return f"<b>📡 Channel Menfess</b>\n\n{body}{note}", rows + back()


async def scr_chanv(username=""):
    rows_ = await q("SELECT * FROM channels WHERE username=?", (username,))
    if not rows_:
        return await scr_chan()
    c = rows_[0]
    n = (await q("SELECT COUNT(*) AS n FROM examples WHERE ch=?", (username,)))[0]["n"]
    pos = await count(f"learnpos:{c['chat_id']}")
    state = "✅ semua isi sudah dipelajari" if pos == -1 else f"lanjut dari post #{pos}" if pos > 0 else "belum dimulai"
    busy = f"\n⏳ Sedang belajar... {esc(ub.prog)}" if ub and ub.learning else ""
    text = (
        f"<b>📡 @{esc(c['username'])}</b>\n\n"
        f"Judul : {esc(c['title'] or '-')}\n"
        f"Status : {'⏸ Dijeda' if c['paused'] else '🟢 Aktif'}\n"
        f"Diskusi ID : <code>{c['disc_id']}</code>\n"
        f"📚 Contoh belajar : {n}\n"
        f"📖 Belajar semua isi : {state}{busy}"
    )
    rows = [
        [B("▶️ Lanjutkan" if c["paused"] else "⏸ Jeda", "pause", username),
         B("🗑 Hapus", "ask", f"delchan|{username}")],
        [B("🩺 Diagnostik channel ini", "diag", username)],
        [B("📚 Pelajari 100 post", "dolearn", f"100|{username}"),
         B("📖 Pelajari semua isi", "dolearn", f"all|{username}")],
    ]
    if ub and ub.learning:
        rows.append([B("⏹ Hentikan belajar", "learnstop")])
    return text, rows + back("chan")


async def scr_diag(arg=""):
    if not ub or not ub.ready:
        return "<b>🩺 Diagnostik</b>\n\n🔴 Userbot offline. Buat atau aktifkan session di menu 🔑 Session.", back("chan")
    if arg:
        chans = await q("SELECT * FROM channels WHERE username=?", (arg,))
    else:
        chans = await q("SELECT * FROM channels ORDER BY username")
    if not chans:
        return "<b>🩺 Diagnostik</b>\n\nBelum ada channel. Tambahkan dulu di menu Channel.", back("chan")
    parts = []
    for ch in chans:
        lines = await ub.diagnose(ch)
        parts.append(f"<b>@{esc(ch['username'])}</b>\n" + "\n".join(lines))
    text = "<b>🩺 Diagnostik</b>\n🕒 " + fmt() + "\n\n" + "\n\n".join(parts)
    rows = [[B("🔄 Periksa ulang", "diag", arg)]]
    return text, rows + back("chan")


async def scr_ai(_=""):
    s = await settings()
    text = (
        "<b>🤖 Pengaturan AI</b>\n\n"
        f"✍️ Gaya jawaban : {PERSONAS[s['persona']][0]} · {STYLES[s['style']][0]} · "
        f"{LENGTHS[s['reply_len']][0]}\n"
        f"🧠 AI yang dipakai : {esc(active_ai(s))}\n"
        f"📝 Instruksi khusus : {'aktif' if s['prompt'] else 'tidak ada'}"
    )
    rows = [
        [B("✍️ Gaya Jawaban", "aistyle")],
        [B("🔀 Provider", "aiord"), B("🧩 Model", "aimdl")],
        [B("📝 Instruksi Khusus", "in", "prompt"), B("♻️ Hapus Instruksi", "rprompt")],
        [B("📚 Belajar dari Channel", "learn"), B("🧪 Tes AI", "aitest")],
    ]
    return text, rows + back()


async def scr_aistyle(_=""):
    s = await settings()
    btns = (
        [B(f"{'✅' if s['persona'] == k else '🎭'} {v[0]}", "set", f"persona|{k}") for k, v in PERSONAS.items()]
        + [B(f"{'✅' if s['style'] == k else '✍️'} {v[0]}", "set", f"style|{k}") for k, v in STYLES.items()]
        + [B(f"{'✅' if s['reply_len'] == k else '📏'} {v[0]}", "set", f"reply_len|{k}") for k, v in LENGTHS.items()]
    )
    text = (
        "<b>✍️ Gaya Jawaban</b>\n\n"
        "🎭 <b>Kepribadian</b>: sifat bot (teman, netral, ceria).\n"
        "✍️ <b>Bahasa</b>: baku, santai, gaul, atau ala X/Twitter.\n"
        "📏 <b>Panjang</b>: <b>Sesuai Konteks</b> = bot menilai sendiri. Sapaan atau menfess ringan dibalas "
        "pendek, curhat atau pertanyaan dibalas lebih panjang.\n\n"
        f"Sekarang: {PERSONAS[s['persona']][0]} · {STYLES[s['style']][0]} · {LENGTHS[s['reply_len']][0]}\n\n"
        "<i>Instruksi Khusus (jika diisi) menggantikan kepribadian.</i>"
    )
    rows = chunk(btns[:3], 2) + chunk(btns[3:7], 2) + chunk(btns[7:], 2)
    return text, rows + back("ai")


async def scr_aitest(_=""):
    s = await settings()
    lines = await test_providers(s)
    text = "<b>🧪 Tes AI</b>\n🕒 " + fmt() + "\n\n" + "\n".join(esc(line) for line in lines)
    text += "\n\n<i>✅ model bisa dipakai, ❌ menjelaskan penyebabnya. Model yang lolos otomatis dipakai.</i>"
    return text, [[B("🔄 Tes ulang", "aitest")]] + back("ai")


async def scr_learn(_=""):
    s = await settings()
    cnt = {r["kind"]: r["n"] for r in await q("SELECT kind, COUNT(*) AS n FROM examples GROUP BY kind")}
    nsrc = (await q("SELECT COUNT(DISTINCT ch || src) AS n FROM examples WHERE kind='menfess'"))[0]["n"]
    avg = f"{cnt.get('menfess', 0) / nsrc:.1f}" if nsrc else "0"
    last = fmt_short(s["learn_ts"]) if s["learn_ts"] else "belum pernah"
    busy = f"\n⏳ Sedang belajar... {esc(ub.prog)}" if ub and ub.learning else ""
    pl = []
    for c in await q("SELECT username, chat_id FROM channels ORDER BY username"):
        pos = await count(f"learnpos:{c['chat_id']}")
        state = "✅ semua isi sudah dipelajari" if pos == -1 else f"lanjut dari post #{pos}" if pos > 0 else "belum dimulai"
        pl.append(f"• @{esc(c['username'])}: {state}")
    all_block = ("\n📖 <b>Belajar semua isi channel</b>\n" + "\n".join(pl) + "\n") if pl else ""
    text = (
        "<b>📚 Belajar dari Channel</b>\n\n"
        "Bot membaca tiap postingan beserta SEMUA komentar aslinya (termasuk balasan antar komentar), lalu memakainya "
        "sebagai contoh supaya balasannya mirip gaya orang di channel itu (pendek/panjang, bahasa, candaan, istilah).\n\n"
        f"🧾 Menfess dipelajari : <b>{nsrc}</b>\n"
        f"💬 Komentar menfess : <b>{cnt.get('menfess', 0)}</b> (rata-rata {avg} per menfess)\n"
        f"↩️ Contoh balasan reply : <b>{cnt.get('reply', 0)}</b>\n"
        f"🕒 Terakhir belajar : {last}{busy}\n"
        f"🚫 Diabaikan : semua akun bot + {esc(', '.join(s['ignore_names']) or '-')}\n"
        f"{all_block}\n"
        "<i>Yang disimpan hanya teks. Nama, ID, link, dan @mention dibuang. Bot juga belajar otomatis "
        "tiap 12 jam. Foto dibaca lewat Gemini.\n"
        "💾 Hasil belajar di-backup ke Pesan Tersimpan akun userbot dan dipulihkan sendiri kalau data hilang. "
        "Belajar semua isi lanjut otomatis kalau bot restart atau error, dan baru berhenti kalau sudah selesai "
        "atau kamu tekan Hentikan.</i>"
    )
    rows = [
        [B(f"{on(s['learn_on'])} Pakai contoh asli", "tog", "learn_on"),
         B(f"{on(s['vision_on'])} Baca foto", "tog", "vision_on")],
        [B("📖 Pelajari SEMUA isi channel", "dolearn", "all")],
        [B("⏹ Hentikan", "learnstop"), B("🔁 Ulang dari awal", "ask", "lreset")],
        [B("📚 100 post", "dolearn", "100"), B("📚 500 post", "dolearn", "500")],
        [B("💾 Backup sekarang", "dobackup"), B("♻️ Pulihkan", "dorestore")],
        [B("🚫 Abaikan Nama", "in", "ignore"), B("🗑 Hapus Hasil Belajar", "ask", "cex")],
    ]
    return text, rows + back("ai")


async def scr_aiord(_=""):
    s = await settings()
    lines = []
    for p in s["order"]:
        state = "⛔ dimatikan" if p in s["disabled"] else ("✅ siap" if KEYS.get(p) else "⚪ API key belum diisi")
        lines.append(f"{len(lines) + 1}. {NAMES[p]} · {state}")
    prefer = [B(f"⭐ Utamakan {NAMES[p]}", "set", f"order|{','.join([p] + [o for o in s['order'] if o != p])}")
              for p in NAMES]
    toggles = [B(f"{'⛔ Nyalakan' if p in s['disabled'] else '✅ Matikan'} {NAMES[p]}", "prov", p) for p in NAMES]
    text = (
        "<b>🔀 Provider AI</b>\n\n" + "\n".join(lines) +
        "\n\nBot mencoba dari nomor 1. Kalau gagal atau kuota habis, otomatis pindah ke berikutnya."
    )
    return text, chunk(prefer) + chunk(toggles) + back("ai")


async def scr_aimdl(_=""):
    s = await settings()
    btns = [
        B(f"{'✅ ' if s['model_' + p] == m else ''}{NAMES[p]} · {m.split('/')[-1][:22]}", "mdl", f"{p}|{i}")
        for p in NAMES for i, m in enumerate(MODELS[p])
    ]
    lines = "\n".join(f"• {NAMES[p]}: <code>{esc(s['model_' + p])}</code>" for p in NAMES)
    text = f"<b>🧩 Model AI</b>\n\n{lines}\n\nTidak perlu diatur manual: tombol <b>Tes AI</b> otomatis memilih model yang jalan."
    return text, chunk(btns) + back("ai")


def current_profile(s):
    for key, (_, _, vals) in PROFILES.items():
        if all(s[k] == v for k, v in vals.items()):
            return key
    return None


async def scr_chat(_=""):
    s = await settings()
    prof = current_profile(s)
    text = (
        "<b>💬 Mode Chat</b>\n\n"
        "🟢 = aktif, ⚪ = mati. Ketuk untuk mengubah.\n"
        f"🎛 Profil sekarang : <b>{PROFILES[prof][0] if prof else 'Kustom'}</b>\n\n"
        "🆓 <b>Mode Bebas</b>: bot selalu membalas walau kurang paham (tanya balik), tanpa peluang acak. "
        "Kalau mati, bot memilih diam (SKIP) saat benar-benar tidak paham menfess atau reply.\n"
        "🔁 <b>Ambil alih otomatis</b>: kalau kamu membalas manual di DM seseorang, AI mati untuk orang itu."
    )
    rows = [
        [B(f"{on(s['comment_on'])} Komentar menfess", "tog", "comment_on"),
         B(f"{on(s['reply_on'])} Balas reply", "tog", "reply_on")],
        [B(f"{on(s['dm_on'])} Balas DM", "tog", "dm_on"),
         B(f"{on(s['dm_whitelist'])} DM khusus whitelist", "tog", "dm_whitelist")],
        [B(f"{on(s['free_mode'])} Mode Bebas", "tog", "free_mode"),
         B(f"{on(s['takeover_on'])} Ambil alih otomatis", "tog", "takeover_on")],
        [B("🎛 Profil Cepat", "prof"), B("🔧 Pengaturan Detail", "detail")],
        [B("🔑 Filter Kata", "kw"), B("🚫 Kata Terlarang", "ban")],
        [B("👥 Kelola DM", "dm"), B("🧑‍🤝‍🧑 Asisten Grup", "groups")],
    ]
    return text, rows + back()


async def scr_prof(_=""):
    s = await settings()
    cur = current_profile(s)
    desc = "\n\n".join(f"{'✅ ' if k == cur else ''}<b>{v[0]}</b>\n{v[1]}" for k, v in PROFILES.items())
    text = f"<b>🎛 Profil Cepat</b>\n\nSekali tekan, semua angka diatur otomatis.\n\n{desc}"
    rows = [[B(f"{'✅ ' if k == cur else ''}{v[0]}", "pset", k)] for k, v in PROFILES.items()]
    return text, rows + back("chat")


async def scr_detail(_=""):
    text = "<b>🔧 Pengaturan Detail</b>\n\nUbah angka satu per satu. Ketuk tombol untuk melihat penjelasannya."
    rows = [
        [B("🎯 % Menfess Dibalas", "num", "reply_prob"), B("🎯 % Reply Dibalas", "num", "thread_prob")],
        [B("⏱ Jeda Tercepat", "num", "delay_min"), B("⏱ Jeda Terlama", "num", "delay_max")],
        [B("🔢 Komentar/Jam", "num", "max_comment_hour"), B("🔢 Balasan/Thread", "num", "max_thread")],
        [B("🔢 Balasan/Orang/Hari", "num", "max_user_day"), B("📩 DM/Jam", "num", "dm_hour")],
        [B("📏 Abaikan Menfess Pendek", "num", "min_len"), B("🕰 Abaikan Post Lama", "num", "max_age_min")],
        [B("💬 % Nimbrung Grup", "num", "nimbrung_prob"), B("🔢 Balasan Grup/Jam", "num", "group_hour")],
    ]
    return text, rows + back("chat")


async def scr_num(key=""):
    label, lo, hi, step, presets = NUMS[key]
    s = await settings()
    text = (
        f"<b>🎚 {label}</b>\n\nNilai sekarang : <b>{s[key]}</b>\nRentang : {lo} – {hi}\n\n"
        f"<i>{esc(HELP.get(key, ''))}</i>"
    )
    rows = [
        [B(f"➖ {step * 5}", "adj", f"{key}|{-step * 5}"), B(f"➕ {step * 5}", "adj", f"{key}|{step * 5}")],
        [B(f"➖ {step}", "adj", f"{key}|{-step}"), B(f"➕ {step}", "adj", f"{key}|{step}")],
    ]
    rows += chunk([B(f"⭐ {p}", "set", f"{key}|{p}") for p in presets])
    return text, rows + back("detail")


async def scr_kw(_=""):
    s = await settings()
    words = ", ".join(s["keywords"]) or "(kosong: semua menfess dibalas)"
    text = (
        "<b>🔑 Filter Kata</b>\n\n"
        "Kalau diisi, bot hanya membalas menfess yang memuat salah satu kata ini:\n"
        f"<code>{esc(words)}</code>"
    )
    rows = [[B("✏️ Ubah Daftar", "in", "keyword"), B("🧹 Kosongkan", "kwclr")]]
    return text, rows + back("chat")


async def scr_ban(_=""):
    s = await settings()
    words = ", ".join(s["banned"]) or "(kosong)"
    text = (
        "<b>🚫 Kata / Karakter Terlarang</b>\n\n"
        "Kalau sebuah menfess memuat salah satunya, bot tidak membalas menfess itu. "
        "Bisa satu karakter (contoh <code>@</code>) atau kata. Huruf besar/kecil tidak berpengaruh.\n\n"
        f"Daftar sekarang:\n<code>{esc(words)}</code>"
    )
    rows = [
        [B("➕ @", "banadd", "@"), B("➕ http", "banadd", "http"), B("➕ #", "banadd", "#")],
        [B("✏️ Ubah Daftar", "in", "banned"), B("🧹 Kosongkan", "banclr")],
    ]
    return text, rows + back("chat")


async def scr_dm(_=""):
    users = await q("SELECT * FROM users WHERE lst IS NOT NULL OR ai_on=0 OR stopped=1 ORDER BY uid DESC LIMIT 20")
    rows = [[B("✅ Tambah Whitelist", "in", "white"), B("🚫 Tambah Blacklist", "in", "black")]]
    rows += [[B(f"👤 {u['uid']} · {user_tag(u)}", "duser", str(u["uid"]))] for u in users]
    return "<b>👥 Kelola DM</b>\n\nWhitelist, blacklist, dan AI yang dimatikan.", rows + back("chat")


async def scr_duser(uid=""):
    u = await user(int(uid))
    ai_state = "🟢 aktif" if u["ai_on"] else "⛔ nonaktif"
    stop_state = "dihentikan (/stop)" if u["stopped"] else "berjalan"
    text = f"<b>👤 Pengguna {uid}</b>\n\nStatus : {user_tag(u)}\nAI : {ai_state}\nBalasan : {stop_state}"
    rows = [
        [B("🔁 Aktifkan AI" if not u["ai_on"] else "⛔ Matikan AI", "aitog", uid)],
        [B("✅ Whitelist", "list", f"{uid}|white"), B("🚫 Blacklist", "list", f"{uid}|black")],
        [B("♻️ Hapus dari list", "list", f"{uid}|none")],
    ]
    return text, rows + back("dm")


async def scr_groups(_=""):
    s = await settings()
    gs = await q("SELECT * FROM tg_groups ORDER BY enabled DESC, title LIMIT 20")
    total = (await q("SELECT COUNT(*) AS n FROM tg_groups"))[0]["n"]
    body = "\n".join(
        f"{'🟢' if g['enabled'] else '⚪'} {esc((g['title'] or str(g['chat_id']))[:40])}{' · 💬 nimbrung' if g['free'] else ''}"
        for g in gs
    ) or "<i>Belum ada grup. Grup muncul di sini setelah bot menyala dan melihat daftar chat akun userbot.</i>"
    text = (
        "<b>🧑‍🤝‍🧑 Asisten Grup</b>\n\n"
        f"Bot menjawab di grup yang diaktifkan (🟢) kalau ada yang <b>memanggil</b>: mention, reply ke pesan bot, "
        f"atau menyebut nama panggilan (<code>{esc(', '.join(s['group_names']) or '-')}</code>).\n"
        "Kalau <b>Nimbrung</b> (💬) dinyalakan untuk sebuah grup, bot juga sesekali ikut ngobrol tanpa dipanggil "
        f"(peluang {s['nimbrung_prob']}%, diatur di Pengaturan Detail). Kalau tidak ada yang nyambung, bot diam.\n\n"
        "Grup diskusi channel otomatis aktif. Grup lain mati sampai kamu nyalakan.\n\n"
        f"{body}"
        + (f"\n<i>... dan {total - len(gs)} grup lain</i>" if total > len(gs) else "")
    )
    rows = [[B(f"{on(s['group_on'])} Asisten grup (utama)", "tog", "group_on"), B("✏️ Nama Panggilan", "in", "gnames")]]
    for g in gs:
        rows.append([
            B(f"{'🟢' if g['enabled'] else '⚪'} {(g['title'] or str(g['chat_id']))[:20]}", "gtog", str(g["chat_id"])),
            B(f"{'💬' if g['free'] else '🔇'} Nimbrung", "gfree", str(g["chat_id"])),
        ])
    return text, rows + back("chat")


async def scr_stats(_=""):
    st = await stats()
    logs = await q("SELECT ts, level, text FROM logs ORDER BY id DESC LIMIT 8")
    lines = [f"[{fmt_short(x_['ts'])}] {x_['level']}: {esc(x_['text'][:110])}" for x_ in logs]
    log_text = "\n".join(f"<code>{line}</code>" for line in lines) or "<i>Belum ada log.</i>"
    text = (
        "<b>📊 Statistik</b>\n\n"
        f"💬 Komentar menfess : <b>{st.get('comment', 0)}</b>\n"
        f"↩️ Balasan reply : <b>{st.get('reply', 0)}</b>\n"
        f"📩 Balasan DM : <b>{st.get('dm', 0)}</b>\n"
        f"🧑‍🤝‍🧑 Balasan grup : <b>{st.get('group', 0)}</b>\n"
        f"⏭ Dilewati : <b>{st.get('skipped', 0)}</b>\n"
        f"❌ Error : <b>{st.get('error', 0)}</b>\n\n"
        f"<b>Log terakhir (WIB)</b>\n{log_text}"
    )
    rows = [[B("🔄 Refresh", "stats"), B("🗑 Reset Statistik", "ask", "rstats")]]
    return text, rows + back()


async def scr_sys(_=""):
    s = await settings()
    up = int(time.time() - STARTED)
    hours, minutes = divmod(up // 60, 60)
    size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
    ub_line = f"🟢 Online ({esc(ub.me.first_name or '')})" if ub and ub.ready else "🔴 Offline"
    text = (
        "<b>⚙️ Sistem</b>\n\n"
        f"🤖 Userbot : {ub_line}\n"
        f"🗄 Database : {size:.1f} KB\n"
        f"⏱ Uptime : {hours} jam {minutes} menit\n"
        f"🕒 {fmt()}\n"
        f"🔔 Laporan gagal : {'aktif' if s['notify'] else 'nonaktif'}\n"
        f"✅ Notif tiap balasan : {'aktif' if s['notify_ok'] else 'nonaktif'}"
    )
    rows = [
        [B(f"{on(s['notify'])} Laporan gagal", "tog", "notify"),
         B(f"{on(s['notify_ok'])} Notif balasan", "tog", "notify_ok")],
        [B("🩺 Diagnostik", "diag"), B("🧹 Hapus Log", "ask", "clogs")],
        [B("🗑 Hapus Riwayat AI", "ask", "chist"), B("🔄 Refresh", "sys")],
        [B("🔁 Restart", "ask", "restart")],
    ]
    return text, rows + back()


async def scr_sess(_=""):
    online = bool(ub and ub.ready)
    text = (
        "<b>🔑 Session Userbot</b>\n\n"
        f"Status : {'🟢 Userbot login' if online else '🔴 Userbot belum login'}\n\n"
        "• <b>Upload</b>: file <code>.session</code> atau <code>.zip</code> dari PC.\n"
        "• <b>Login OTP</b>: masuk langsung lewat kode dari Telegram."
    )
    rows = [
        [B("📤 Upload .session / .zip", "in", "sessfile")],
        [B("🔐 Login pakai OTP", "in", "phone")],
    ]
    return text, rows + back()


ASK = {
    "delchan": ("Hapus channel ini dari daftar?", "chan"),
    "rstats": ("Reset semua statistik?", "stats"),
    "clogs": ("Hapus semua log?", "sys"),
    "chist": ("Hapus riwayat percakapan AI? Konteks balasan akan hilang.", "sys"),
    "restart": ("Restart bot sekarang?", "sys"),
    "cex": ("Hapus semua contoh balasan yang sudah dipelajari, termasuk backup-nya?", "learn"),
    "lreset": ("Mulai ulang belajar semua isi channel dari postingan terbaru? Contoh yang sudah ada tetap disimpan.", "learn"),
}


async def scr_ask(spec=""):
    action = spec.split("|", 1)[0]
    question, back_to = ASK[action]
    rows = [[B("✅ Ya, lanjutkan", "ok", spec)]] + back(back_to)
    return f"<b>⚠️ Konfirmasi</b>\n\n{question}", rows


INPUT_TEXT = {
    "channel": ("➕ Tambah Channel",
                "Kirim username channel, contoh <code>@namachannel</code>.\n"
                "Userbot sebaiknya sudah join grup diskusinya."),
    "prompt": ("📝 Instruksi Khusus",
               "Tulis sifat atau aturan khusus untuk bot, contoh: <i>Kamu cewek ramah yang suka kasih semangat.</i>\n"
               "Ini menggantikan Kepribadian bawaan. Gaya bahasa dan panjang jawaban tetap mengikuti pengaturan."),
    "keyword": ("🔑 Filter Kata", "Kirim kata dipisahkan koma, contoh <code>galau, capek, sedih</code>. Kirim <code>-</code> untuk mengosongkan."),
    "ignore": ("🚫 Abaikan Nama",
               "Semua akun bot sudah otomatis diabaikan. Kirim nama tambahan dipisahkan koma, "
               "contoh <code>rose, admin</code>. Kirim <code>-</code> untuk mengosongkan."),
    "banned": ("🚫 Kata Terlarang",
               "Kirim kata atau karakter dipisahkan koma, contoh <code>@, http, wa.me</code>. "
               "Ini mengganti seluruh daftar yang ada. Kirim <code>-</code> untuk mengosongkan."),
    "gnames": ("✏️ Nama Panggilan Asisten",
               "Kirim nama panggilan dipisahkan koma. Kalau seseorang menyebut salah satunya di grup yang aktif, "
               "bot menjawab. Contoh <code>newt, ai</code>. Kirim <code>-</code> untuk mengosongkan "
               "(mention dan reply ke bot tetap dijawab)."),
    "white": ("✅ Whitelist DM", "Kirim user ID Telegram (angka)."),
    "black": ("🚫 Blacklist DM", "Kirim user ID Telegram (angka)."),
    "sessfile": ("📤 Upload Session",
                 "Kirim file <code>.session</code> atau <code>.zip</code> yang berisi .session. "
                 "Hanya gunakan file milik akunmu sendiri."),
    "phone": ("📱 Login OTP", "Kirim nomor HP akun userbot dengan kode negara, contoh <code>+628123456789</code>."),
    "code": ("🔢 Kode OTP", "Kirim kode yang dikirim Telegram ke akun tersebut."),
    "pwd": ("🔒 Password 2FA", "Kirim password verifikasi dua langkah akun tersebut."),
}
INPUT_BACK = {
    "channel": "chan", "prompt": "ai", "keyword": "kw", "ignore": "learn", "banned": "ban", "white": "dm", "black": "dm", "gnames": "groups",
    "sessfile": "sess", "phone": "sess", "code": "sess", "pwd": "sess",
}
LOGIN_KINDS = {"phone", "code", "pwd"}


def input_screen(kind):
    title, desc = INPUT_TEXT[kind]
    rows = back(INPUT_BACK[kind])
    if kind in LOGIN_KINDS:
        rows = [[B("🛑 Batalkan login", "loginx")]] + rows
    return f"<b>{title}</b>\n\n{desc}", rows


SCREENS = {
    "home": scr_home, "chan": scr_chan, "chanv": scr_chanv, "diag": scr_diag,
    "ai": scr_ai, "aitest": scr_aitest, "aiord": scr_aiord, "aimdl": scr_aimdl,
    "aistyle": scr_aistyle, "chat": scr_chat, "prof": scr_prof, "detail": scr_detail, "num": scr_num,
    "help": scr_help, "learn": scr_learn,
    "kw": scr_kw, "ban": scr_ban, "dm": scr_dm, "groups": scr_groups, "duser": scr_duser, "stats": scr_stats,
    "sys": scr_sys, "sess": scr_sess,
}


async def render(to, arg=""):
    return await SCREENS.get(to, scr_home)(arg)


# ---------- panel: aiogram ----------

class In(StatesGroup):
    channel = State()
    prompt = State()
    keyword = State()
    ignore = State()
    banned = State()
    gnames = State()
    white = State()
    black = State()
    sessfile = State()
    phone = State()
    code = State()
    pwd = State()


INPUTS = {
    "channel": In.channel, "prompt": In.prompt, "keyword": In.keyword, "ignore": In.ignore, "banned": In.banned,
    "gnames": In.gnames, "white": In.white, "black": In.black, "sessfile": In.sessfile, "phone": In.phone,
}
PANEL = {"chat": None, "mid": None}

panel = Router(name="panel")
panel.message.filter(F.from_user.id == OWNER_ID)
panel.callback_query.filter(F.from_user.id == OWNER_ID)


async def show(text, rows, notice=None):
    """Edit satu pesan panel. Kalau belum ada atau sudah hilang, kirim yang baru."""
    if notice:
        text = f"<i>{esc(notice)}</i>\n\n{text}"
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if PANEL["mid"]:
        try:
            await bot.edit_message_text(text, chat_id=PANEL["chat"], message_id=PANEL["mid"], reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        except TelegramAPIError:
            pass
    m = await bot.send_message(OWNER_ID, text, reply_markup=markup)
    PANEL["chat"], PANEL["mid"] = m.chat.id, m.message_id


async def show_cb(cq: CallbackQuery, text, rows, notice=None):
    await cq.answer()
    await show(text, rows, notice)


async def show_result(notice):
    text, rows = await render("sess")
    await show(text, rows, notice)


async def show_string(string, me, source):
    PENDING["string"] = string
    text = (
        "<b>🔑 Session siap</b>\n\n"
        f"Akun : {esc(me.first_name or '-')} (lewat {source})\n\n"
        "1. Tekan <b>⚡ Aktifkan sekarang</b> supaya bot langsung memakai session ini.\n"
        "2. Salin string di bawah ke secret <code>TG_STRING_SESSION</code> di GitHub.\n\n"
        f"<code>{string}</code>"
    )
    rows = [[B("⚡ Aktifkan sekarang", "act")]] + back("sess")
    await show(text, rows)


async def delete_msg(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass


async def drop_panel():
    if PANEL["mid"]:
        try:
            await bot.delete_message(PANEL["chat"], PANEL["mid"])
        except Exception:
            pass
    PANEL["chat"] = PANEL["mid"] = None


def restart():
    os.execv(sys.executable, [sys.executable] + sys.argv)


@panel.message(CommandStart())
async def on_start(msg: Message, state: FSMContext):
    # /start TIDAK dihapus. Panel lama dihapus, panel baru dikirim tepat di bawahnya.
    await state.clear()
    await drop_panel()
    text, rows = await render("home")
    await show(text, rows)


async def handle_cb(cq: CallbackQuery, cd: CB, state: FSMContext):
    await state.clear()
    a, b = cd.a, cd.b
    to, arg, notice = a, b, None
    if cq.message:
        PANEL["chat"], PANEL["mid"] = cq.message.chat.id, cq.message.message_id

    if a == "in":
        await state.set_state(INPUTS[b])
        text, rows = input_screen(b)
        return await show_cb(cq, text, rows)
    if a == "ask":
        text, rows = await scr_ask(b)
        return await show_cb(cq, text, rows)
    if a in ("diag", "aitest"):
        await cq.answer("Memeriksa, tunggu sebentar...")
        text, rows = await render(a, b)
        return await show(text, rows)
    if a == "act":
        string = PENDING.get("string")
        if not string:
            text, rows = await render("sess")
            return await show_cb(cq, text, rows, "Session sudah tidak tersedia. Buat ulang.")
        await cq.answer("Mengaktifkan...")
        ok, err = await activate(string)
        if ok:
            PENDING.clear()
            text, rows = await render("home")
            return await show(text, rows, "✅ Userbot aktif dengan session baru.")
        text, rows = await render("sess")
        return await show(text, rows, f"Gagal mengaktifkan: {err}")
    if a == "loginx":
        await login_close()
        text, rows = await render("sess")
        return await show_cb(cq, text, rows, "Login dibatalkan.")
    if a == "dolearn":
        amount, _, only = b.partition("|")
        if not ub or not ub.ready:
            notice = "Userbot belum online."
        elif ub.learning:
            notice = "Bot masih belajar. Tunggu selesai atau tekan Hentikan."
        else:
            n = None if amount == "all" else int(amount)
            ub.spawn(ub.learn_all(n, who=True, only=only or None))
            target = f"@{only}" if only else "semua channel"
            if n is None:
                notice = (f"Mulai belajar SEMUA isi {target}, dari yang terbaru. Bot tetap membalas seperti biasa. "
                          "Kalau terhenti (restart atau error) lanjut sendiri. Tekan Hentikan untuk berhenti.")
            else:
                notice = f"Mulai belajar {n} post dari {target}. Hasilnya dikirim ke chat ini kalau selesai."
        text, rows = await render("chanv", only) if only else await render("learn")
        return await show_cb(cq, text, rows, notice)
    if a == "learnstop":
        await x("UPDATE counters SET value=0 WHERE key LIKE 'learnfull:%'")   # matikan lanjut otomatis
        if ub and ub.learning:
            ub.learn_stop = True
            notice = "Menghentikan... progres tersimpan. Belajar tidak lanjut otomatis sampai kamu mulai lagi."
        else:
            notice = "Bot sedang tidak belajar. Lanjut otomatis dimatikan."
        text, rows = await render("learn")
        return await show_cb(cq, text, rows, notice)
    if a in ("dobackup", "dorestore"):
        await cq.answer("Memproses, tunggu sebentar...")
        if not ub or not ub.ready:
            notice = "Userbot belum online."
        else:
            try:
                if a == "dobackup":
                    n = await ub.backup()
                    notice = f"Backup selesai: {n} contoh disimpan di Pesan Tersimpan." if n else "Belum ada contoh untuk di-backup."
                else:
                    n = await ub.restore()
                    notice = f"Dipulihkan: {n} contoh baru dari backup." if n else "Tidak ada contoh baru di backup (atau backup belum ada)."
            except Exception as e:
                notice = f"Gagal: {explain(e)}"
        text, rows = await render("learn")
        return await show(text, rows, notice)
    if a == "tog":
        s = await settings()
        await setv(b, not s[b])
        to, arg = ("sys" if b in ("notify", "notify_ok") else "learn" if b in ("learn_on", "vision_on")
                   else "groups" if b == "group_on" else "chat"), ""
    elif a == "set":
        key, val = b.split("|", 1)
        if key == "order":
            await setv("order", val.split(","))
            to, arg = "aiord", ""
        elif key in ("persona", "style", "reply_len"):
            await setv(key, val)
            to, arg = "aistyle", ""
        else:
            await setv(key, int(val))
            to, arg = "num", key
    elif a == "gtog":
        await x("UPDATE tg_groups SET enabled = 1 - enabled WHERE chat_id=?", (int(b),))
        to, arg = "groups", ""
    elif a == "gfree":
        await x("UPDATE tg_groups SET free = 1 - free WHERE chat_id=?", (int(b),))
        to, arg = "groups", ""
    elif a == "pset":
        for k, v in PROFILES[b][2].items():
            await setv(k, v)
        to, arg, notice = "prof", "", f"Profil {PROFILES[b][0]} dipakai."
    elif a == "mdl":
        prov, idx = b.split("|")
        await setv(f"model_{prov}", MODELS[prov][int(idx)])
        to, arg = "aimdl", ""
    elif a == "adj":
        key, delta = b.split("|")
        _, lo, hi, _, _ = NUMS[key]
        s = await settings()
        await setv(key, max(lo, min(hi, s[key] + int(delta))))
        to, arg = "num", key
    elif a == "prov":
        s = await settings()
        await setv("disabled", sorted(set(s["disabled"]) ^ {b}))
        to, arg = "aiord", ""
    elif a == "pause":
        await x("UPDATE channels SET paused = 1 - paused WHERE username=?", (b,))
        if ub:
            await ub.refresh()
        to, arg = "chanv", b
    elif a == "rprompt":
        await setv("prompt", "")
        to, arg = "ai", ""
    elif a == "kwclr":
        await setv("keywords", [])
        to, arg = "kw", ""
    elif a == "banadd":
        s = await settings()
        if b not in s["banned"]:
            await setv("banned", [*s["banned"], b])
        to, arg = "ban", ""
    elif a == "banclr":
        await setv("banned", [])
        to, arg = "ban", ""
    elif a == "aitog":
        u = await user(int(b))
        await user_set(int(b), ai_on=0 if u["ai_on"] else 1)
        to, arg = "duser", b
    elif a == "list":
        uid, lst = b.split("|")
        await user_set(int(uid), lst=None if lst == "none" else lst)
        to, arg = "duser", uid
    elif a == "ok":
        action, _, target = b.partition("|")
        to, arg = "home", ""
        if action == "delchan":
            await x("DELETE FROM channels WHERE username=?", (target,))
            if ub:
                await ub.refresh()
            to = "chan"
        elif action == "rstats":
            await x("DELETE FROM counters WHERE key LIKE 'stat:%'")
            to = "stats"
        elif action == "clogs":
            await x("DELETE FROM logs")
            to = "sys"
        elif action == "cex":
            await x("DELETE FROM examples")
            await x("DELETE FROM counters WHERE key LIKE 'learnpos:%' OR key LIKE 'learnfull:%'")
            if ub and ub.ready:
                try:
                    await ub.drop_backups()
                except Exception as e:
                    log.warning("hapus backup gagal: %s", e)
            to, notice = "learn", "Hasil belajar dan backup dihapus."
        elif action == "lreset":
            await x("DELETE FROM counters WHERE key LIKE 'learnpos:%' OR key LIKE 'learnfull:%'")
            to, notice = "learn", "Belajar semua isi channel akan mulai lagi dari postingan terbaru."
        elif action == "chist":
            await x("DELETE FROM hist")
            to, notice = "sys", "Riwayat AI dihapus."
        elif action == "restart":
            asyncio.get_running_loop().call_later(2, restart)
            to, notice = "sys", "Bot restart dalam 2 detik..."

    text, rows = await render(to, arg)
    await show_cb(cq, text, rows, notice)


@panel.callback_query(CB.filter())
async def on_cb(cq: CallbackQuery, callback_data: CB, state: FSMContext):
    try:
        await handle_cb(cq, callback_data, state)
    except Exception as e:
        log.exception("callback")
        try:
            await cq.answer(f"Error: {short(e)}"[:190], show_alert=True)
        except Exception:
            pass


@panel.message(In.channel)
async def in_channel(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    raw = (msg.text or "").strip()
    name = re.sub(r"^(https?://)?(t\.me/)?", "", raw).lstrip("@").split("/")[0].split("?")[0].lower()
    if not ub or not ub.ready:
        notice = "Userbot belum online. Buat session dulu di menu 🔑 Session."
    elif not name:
        notice = "Username tidak valid."
    else:
        try:
            info = await ub.resolve(name)
            await x(
                "INSERT OR REPLACE INTO channels(username, chat_id, disc_id, title, paused) VALUES(?, ?, ?, ?, 0)",
                (info["username"], info["chat_id"], info["disc_id"], info["title"]),
            )
            await ub.refresh()
            notice = f"@{name} berhasil ditambahkan. Postingan baru akan dibalas otomatis."
            if ub.learning:
                notice += " Bot sedang belajar channel lain, belajar channel ini bisa dimulai dari menu Channel setelah selesai."
            else:
                ub.spawn(ub.learn_all(100, who=True, only=info["username"]))
                notice += " Bot juga mulai belajar gaya komentar di channel ini (100 post terakhir)."
        except Exception as e:
            notice = f"Gagal: {explain(e)}"
    text, rows = await render("chan")
    await show(text, rows, notice)


@panel.message(In.prompt)
async def in_prompt(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    raw = (msg.text or "").strip()
    if raw:
        await setv("prompt", raw[:1500])
    text, rows = await render("ai")
    await show(text, rows, "Prompt kustom disimpan." if raw else "Kirim teks untuk prompt.")


@panel.message(In.keyword)
async def in_keyword(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    raw = (msg.text or "").strip()
    words = [] if raw == "-" else [w.strip() for w in raw.split(",") if w.strip()]
    await setv("keywords", words[:30])
    text, rows = await render("kw")
    await show(text, rows, "Filter keyword diperbarui.")


@panel.message(In.banned)
async def in_banned(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    raw = (msg.text or "").strip()
    words = [] if raw == "-" else [w.strip() for w in re.split(r"[,\n]", raw) if w.strip()]
    await setv("banned", words[:50])
    text, rows = await render("ban")
    await show(text, rows, "Daftar terlarang diperbarui.")


@panel.message(In.gnames)
async def in_gnames(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    raw = (msg.text or "").strip()
    names = [] if raw == "-" else [w.strip() for w in raw.split(",") if w.strip()]
    await setv("group_names", names[:20])
    text, rows = await render("groups")
    await show(text, rows, "Nama panggilan diperbarui.")


@panel.message(In.ignore)
async def in_ignore(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    raw = (msg.text or "").strip()
    names = [] if raw == "-" else [w.strip() for w in raw.split(",") if w.strip()]
    await setv("ignore_names", names[:20])
    text, rows = await render("learn")
    await show(text, rows, "Daftar abaikan diperbarui.")


async def list_input(msg: Message, state: FSMContext, lst: str):
    await state.clear()
    await delete_msg(msg)
    raw = (msg.text or "").strip()
    if raw.isdigit():
        await user_set(int(raw), lst=lst)
        notice = f"User {raw} masuk {lst}list."
    else:
        notice = "Kirim user ID berupa angka."
    text, rows = await render("dm")
    await show(text, rows, notice)


@panel.message(In.white)
async def in_white(msg: Message, state: FSMContext):
    await list_input(msg, state, "white")


@panel.message(In.black)
async def in_black(msg: Message, state: FSMContext):
    await list_input(msg, state, "black")


@panel.message(In.sessfile)
async def in_sessfile(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    doc = msg.document
    name = (doc.file_name or "").lower() if doc else ""
    if not name.endswith((".session", ".zip")):
        return await show_result("Kirim file berekstensi .session atau .zip.")
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        return await show_result("File terlalu besar (maks 5 MB).")

    tmp = tempfile.mkdtemp()
    try:
        raw = os.path.join(tmp, "upload")
        await bot.download(doc, destination=raw)
        if name.endswith(".zip"):
            with zipfile.ZipFile(raw) as zf:
                member = next((n for n in zf.namelist() if n.lower().endswith(".session")), None)
                if not member:
                    raise ValueError("zip tidak berisi file .session")
                data = zf.read(member)
        else:
            with open(raw, "rb") as f:
                data = f.read()
        base = os.path.join(tmp, "account")
        with open(base + ".session", "wb") as f:
            f.write(data)
        string = session_to_string(base)
        me = await verify_string(string)
    except Exception as e:
        return await show_result(f"Gagal memproses session: {short(e)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    await show_string(string, me, "upload")


@panel.message(In.phone)
async def in_phone(msg: Message, state: FSMContext):
    await delete_msg(msg)
    phone = re.sub(r"[\s-]", "", msg.text or "")
    if not re.fullmatch(r"\+\d{8,15}", phone):
        text, rows = input_screen("phone")
        return await show(text, rows, "Format salah. Contoh: +628123456789")
    await login_close()
    try:
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        sent = await c.send_code_request(phone)
    except Exception as e:
        await state.clear()
        return await show_result(f"Gagal mengirim kode: {short(e)}")
    LOGIN.update(client=c, phone=phone, hash=sent.phone_code_hash)
    await state.set_state(In.code)
    text, rows = input_screen("code")
    await show(text, rows, "Kode OTP sudah dikirim ke akun Telegram tersebut.")


@panel.message(In.code)
async def in_code(msg: Message, state: FSMContext):
    await delete_msg(msg)
    code = re.sub(r"\D", "", msg.text or "")
    c = LOGIN.get("client")
    if not c or not code:
        await state.clear()
        await login_close()
        return await show_result("Sesi login tidak ditemukan. Mulai lagi dari menu Session.")
    try:
        await c.sign_in(phone=LOGIN["phone"], code=code, phone_code_hash=LOGIN["hash"])
    except errors.SessionPasswordNeededError:
        await state.set_state(In.pwd)
        text, rows = input_screen("pwd")
        return await show(text, rows, "Akun ini memakai 2FA. Kirim password-nya.")
    except errors.PhoneCodeInvalidError:
        text, rows = input_screen("code")
        return await show(text, rows, "Kode salah. Coba lagi.")
    except Exception as e:
        await state.clear()
        await login_close()
        return await show_result(f"Login gagal: {short(e)}")
    await state.clear()
    string = c.session.save()
    me = await c.get_me()
    await login_close()
    await show_string(string, me, "login OTP")


@panel.message(In.pwd)
async def in_pwd(msg: Message, state: FSMContext):
    await delete_msg(msg)
    pwd = (msg.text or "").strip()
    c = LOGIN.get("client")
    if not c or not pwd:
        await state.clear()
        await login_close()
        return await show_result("Sesi login tidak ditemukan. Mulai lagi dari menu Session.")
    try:
        await c.sign_in(password=pwd)
    except Exception:
        text, rows = input_screen("pwd")
        return await show(text, rows, "Password salah. Coba lagi.")
    await state.clear()
    string = c.session.save()
    me = await c.get_me()
    await login_close()
    await show_string(string, me, "login OTP")


@panel.message()
async def on_any(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    text, rows = await render("home")
    await show(text, rows)


# ---------- entry point ----------

async def startup_check():
    """Periksa semua channel saat bot menyala dan laporkan masalahnya ke owner."""
    try:
        chans = await q("SELECT * FROM channels")
        problems = []
        for ch in chans:
            lines = await ub.diagnose(ch)
            bad = [ln for ln in lines if ln.startswith(("❌", "⚠️"))]
            if bad:
                problems.append(f"<b>@{esc(ch['username'])}</b>\n" + "\n".join(bad))
        if problems:
            await notify("🩺 <b>Cek awal menemukan masalah</b>\n🕒 " + fmt() + "\n\n" + "\n\n".join(problems))
        elif chans:
            await notify(f"🩺 Cek awal: {len(chans)} channel siap, semua pemeriksaan lolos.\n🕒 {fmt()}")
        else:
            await notify("ℹ️ Belum ada channel. Tambahkan lewat panel: Channel → Tambah Channel.")
    except Exception as e:
        log.warning("startup_check gagal: %s", e)


async def run_bot():
    global bot, ub
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    s = await settings()
    ub = UB(s.get("session") or SESSION)
    err = ""
    try:
        await ub.start()
    except Exception as e:
        err = short(e)
        log.error("Userbot gagal: %s", err)
        await log_("ERROR", f"Userbot gagal: {err}")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(panel)
    if ub.ready:
        await notify(f"✅ <b>Bot dan userbot aktif</b>\n🕒 {fmt()}\n👤 {esc(ub.me.first_name or '-')}")
        task = asyncio.create_task(startup_check())
        BG.add(task)
        task.add_done_callback(BG.discard)
    else:
        await notify(
            f"⚠️ <b>Panel aktif, userbot OFFLINE</b>\n🕒 {fmt()}\n❓ {esc(err)}\n"
            "💡 Buat session di menu 🔑 Session, atau periksa secret TG_STRING_SESSION."
        )
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        try:
            await ub.stop()
        except Exception:
            pass
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    if not BOT_TOKEN or not OWNER_ID:
        raise SystemExit("BOT_TOKEN dan OWNER_ID wajib diisi")
    asyncio.run(run_bot())
