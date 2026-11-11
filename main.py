"""
Menfess AI Bot: panel inline (aiogram) + userbot (Telethon) dalam satu file.
Jalankan bot: python main.py
"""
import asyncio
import html
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
from datetime import datetime
from itertools import permutations

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
from telethon import TelegramClient, events, utils
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, PeerChannel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

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
# Nama model bisa berubah. Kalau ada error HTTP 404, ganti di sini.
MODELS = {
    "gemini": ["gemini-flash-latest", "gemini-2.5-flash-lite"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "openrouter": ["meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free"],
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
        "Huruf kecil semua. Singkat, natural, dan tidak kaku. Minim tanda baca. "
        "Emoji maksimal satu dan tidak selalu digunakan. "
        "Sesekali menggunakan bahasa sehari-hari seperti wkwk, relate, atau semangat ya. "
        "Jangan berlebihan atau terdengar seperti robot.",
    ),
}
# key: (label, min, max, langkah, preset)
NUMS = {
    "reply_prob": ("Peluang balas menfess (%)", 0, 100, 5, [25, 50, 75, 100]),
    "thread_prob": ("Peluang balas reply (%)", 0, 100, 5, [30, 60, 80, 100]),
    "min_len": ("Panjang minimal menfess (karakter)", 0, 200, 1, [0, 1, 3, 10]),
    "delay_min": ("Jeda minimal (detik)", 0, 600, 5, [5, 15, 30]),
    "delay_max": ("Jeda maksimal (detik)", 0, 900, 5, [15, 30, 60, 120]),
    "max_comment_hour": ("Batas komentar per jam", 1, 1000, 10, [6, 30, 100, 1000]),
    "max_thread": ("Balasan per thread", 1, 20, 1, [1, 2, 3, 5]),
    "max_user_day": ("Balasan per user per hari", 1, 100, 1, [3, 5, 10, 20]),
    "dm_hour": ("Balasan DM per jam", 1, 200, 5, [10, 20, 50, 100]),
}
HELP = {
    "reply_prob": "Dari 100 menfess yang masuk, berapa yang dibalas. 100 berarti semua menfess dibalas.",
    "thread_prob": "Peluang bot membalas orang yang membalas komentarnya. 70 berarti sekitar 7 dari 10.",
    "min_len": "Menfess yang teksnya lebih pendek dari angka ini dilewati. 0 berarti semua dibalas.",
    "delay_min": "Waktu tunggu minimal sebelum bot membalas, supaya terlihat manusiawi.",
    "delay_max": "Waktu tunggu maksimal. Bot memilih acak antara jeda minimal dan maksimal.",
    "max_comment_hour": "Batas komentar menfess per jam. Setelah tercapai, sisanya dilewati sampai jam berikutnya.",
    "max_thread": "Berapa kali bot boleh membalas dalam satu thread (satu menfess).",
    "max_user_day": "Berapa kali bot boleh membalas satu orang yang sama dalam sehari.",
    "dm_hour": "Batas balasan DM per jam untuk setiap pengguna.",
}
DEFAULTS = {
    "comment_on": True, "reply_on": True, "dm_on": True, "dm_whitelist": False, "notify": True,
    "reply_prob": 100, "thread_prob": 70, "min_len": 0, "delay_min": 5, "delay_max": 25,
    "max_comment_hour": 1000, "max_thread": 3, "max_user_day": 10, "dm_hour": 20,
    "keywords": [], "order": ["gemini", "groq", "openrouter"], "disabled": [],
    "model_gemini": MODELS["gemini"][0],
    "model_groq": MODELS["groq"][0],
    "model_openrouter": MODELS["openrouter"][0],
    "persona": "teman", "style": "x", "prompt": "",
}
DISCLOSURE = (
    "Halo! 👋 Akun ini menggunakan asisten AI untuk membalas pesan secara otomatis.\n"
    "Ketik /stop kapan saja untuk menghentikan balasan otomatis."
)
CLOSING = re.compile(r"^\s*(ok|oke|okay|sip|makasih|mksh|thanks|thx|noted|siap|yoi)[\s!.]*$", re.I)
MODE_TEXT = {
    "menfess": "Kamu sedang memberi komentar pada menfess (pesan anonim) di channel. Tanggapi dengan hangat dan relevan.",
    "reply": "Kamu sedang membalas seseorang di thread komentar sebuah menfess. Lanjutkan percakapan dengan relevan dan singkat.",
    "dm": "Kamu sedang membalas pesan pribadi dari seseorang. Tanggapi sesuai isi pesannya.",
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
"""
STARTED = time.time()
bot = None
ub = None
db = None
LOGIN = {}    # client login OTP yang sedang berjalan
PENDING = {}  # session string yang menunggu diaktifkan


class AIError(Exception):
    pass


# ---------- database ----------

async def db_init():
    global db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    for k, v in DEFAULTS.items():
        await db.execute("INSERT OR IGNORE INTO settings VALUES(?, ?)", (k, json.dumps(v)))
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


async def skip(reason):
    await stat("skipped")
    await log_("INFO", f"Dilewati: {reason}")


async def stats():
    rows = await q("SELECT key, value FROM counters WHERE key LIKE 'stat:%'")
    return {r["key"][5:]: r["value"] for r in rows}


async def log_(level, text):
    await x("INSERT INTO logs(ts, level, text) VALUES(?, ?, ?)", (int(time.time()), level, text[:300]))
    await x("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 200)")


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


# ---------- AI ----------

def is_skip(text):
    return text.strip().strip("\"'.").upper() == "SKIP"


def system_prompt(s, mode):
    persona = s["prompt"].strip() or PERSONAS[s["persona"]][1]
    return (
        f"{persona}\n{STYLES[s['style']][1]}\n{MODE_TEXT[mode]}\n\n"
        "Aturan wajib:\n"
        "- Balas dalam Bahasa Indonesia, singkat, seperti chat sungguhan.\n"
        "- Jika ditanya, jujur bahwa kamu asisten AI. Jangan mengaku manusia.\n"
        "- Jangan menghina, memprovokasi, membahas SARA, pornografi, atau kekerasan.\n"
        "- Jangan meminta atau membagikan data pribadi.\n"
        "- Jangan memaksakan percakapan. Tutup dengan sopan jika sudah cukup.\n"
        "- Jika pesan spam, provokasi, atau tidak bermakna, jawab persis: SKIP\n"
        "- Keluarkan hanya teks balasan."
    )


async def call(http, name, model, system, msgs):
    if name == "gemini":
        r = await http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": KEYS["gemini"]},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [
                    {"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]}
                    for m in msgs
                ],
                "generationConfig": {"temperature": 0.9, "maxOutputTokens": 600},
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    r = await http.post(
        URLS[name],
        headers={"Authorization": f"Bearer {KEYS[name]}"},
        json={"model": model, "messages": [{"role": "system", "content": system}, *msgs],
              "temperature": 0.9, "max_tokens": 300},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()["choices"][0]["message"]["content"]


async def ask_ai(mode, msgs, s):
    """Coba provider sesuai urutan. Kalau semua gagal, raise AIError."""
    system = system_prompt(s, mode)
    errs = []
    async with httpx.AsyncClient(timeout=30) as http:
        for name in s["order"]:
            if name in s["disabled"] or not KEYS.get(name):
                continue
            try:
                text = (await call(http, name, s[f"model_{name}"], system, msgs) or "").strip()
                if text:
                    return text
                errs.append(f"{name}: kosong")
            except Exception as e:
                errs.append(f"{name}: {str(e)[:80]}")
    raise AIError("; ".join(errs) or "tidak ada provider aktif")


async def humanize(s):
    lo, hi = sorted((s["delay_min"], s["delay_max"]))
    await asyncio.sleep(random.uniform(lo, hi))


async def notify(text):
    if bot and (await settings())["notify"]:
        try:
            await bot.send_message(OWNER_ID, text)
        except Exception:
            pass


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
        self.c = TelegramClient(StringSession(string or ""), API_ID, API_HASH)
        self.me = None
        self.ready = False
        self.by_disc = {}
        self.ai_at = {}
        self.tasks = set()
        self.alert_at = 0.0

    async def start(self):
        await self.c.connect()
        if not await self.c.is_user_authorized():
            raise RuntimeError("session kosong atau tidak valid")
        self.me = await self.c.get_me()
        self.c.add_event_handler(self.on_in, events.NewMessage(incoming=True))
        self.c.add_event_handler(self.on_out, events.NewMessage(outgoing=True))
        await self.refresh()
        self.ready = True
        log.info("Userbot aktif sebagai %s", self.me.first_name)

    async def stop(self):
        self.ready = False
        await self.c.disconnect()

    async def refresh(self):
        rows = await q("SELECT * FROM channels")
        self.by_disc = {r["disc_id"]: r for r in rows if r["disc_id"]}

    async def resolve(self, name):
        ent = await self.c.get_entity(name)
        if not isinstance(ent, Channel) or not ent.broadcast:
            raise ValueError("bukan channel broadcast")
        full = await self.c(GetFullChannelRequest(ent))
        linked = full.full_chat.linked_chat_id
        if not linked:
            raise ValueError("channel belum punya discussion group")
        return {
            "username": name,
            "chat_id": utils.get_peer_id(ent),
            "disc_id": utils.get_peer_id(PeerChannel(linked)),
            "title": ent.title,
        }

    # setiap event diproses di task sendiri agar delay tidak memblokir update lain
    def spawn(self, coro):
        t = asyncio.create_task(self.guard(coro))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def guard(self, coro):
        try:
            await coro
        except AIError as e:
            await stat("error")
            await log_("ERROR", f"AI gagal: {e}")
            if time.time() - self.alert_at > 900:
                self.alert_at = time.time()
                await notify(f"⚠️ Semua provider AI gagal:\n<code>{html.escape(str(e)[:250])}</code>")
        except Exception as e:
            log.exception("handler")
            await stat("error")
            await log_("ERROR", repr(e))

    async def on_in(self, ev):
        if ev.is_private:
            self.spawn(self.dm(ev))
        elif ev.chat_id in self.by_disc:
            self.spawn(self.group(ev))

    async def on_out(self, ev):
        if ev.is_private and ev.chat_id != self.me.id:
            self.spawn(self.takeover(ev))

    async def say(self, chat, text, reply_to=None):
        # Catat waktu sebelum kirim, supaya pesan ini tidak dianggap takeover.
        self.ai_at[chat] = time.time()
        await self.c.send_message(chat, text, reply_to=reply_to)

    async def send(self, chat, text, reply_to=None):
        async with self.c.action(chat, "typing"):
            await asyncio.sleep(min(1.5 + len(text) * 0.05, 8))
        await self.say(chat, text, reply_to)

    # A. komentar menfess, B. balas reply
    async def group(self, ev):
        ch = self.by_disc[ev.chat_id]
        msg = ev.message
        s = await settings()
        fwd = msg.fwd_from
        from_channel = bool(
            fwd and fwd.channel_post and fwd.from_id and utils.get_peer_id(fwd.from_id) == ch["chat_id"]
        )
        if from_channel:
            return await self.menfess(msg, ch, s)
        if msg.reply_to_msg_id and msg.sender_id != self.me.id:
            return await self.reply(msg, s)

    async def menfess(self, msg, ch, s):
        if ch["paused"] or not s["comment_on"]:
            return await skip("channel dijeda atau komentar nonaktif")
        text = (msg.message or "").strip()
        if len(text) < s["min_len"]:
            return await skip(f"menfess terlalu pendek ({len(text)} karakter)")
        if s["reply_prob"] < 100 and random.uniform(0, 100) > s["reply_prob"]:
            return await skip("kena peluang balas")
        if s["keywords"] and not any(k.lower() in text.lower() for k in s["keywords"]):
            return await skip("tidak ada keyword yang cocok")
        hk = f"hour:{datetime.now():%Y%m%d%H}:c"
        if await count(hk) >= s["max_comment_hour"]:
            return await skip("batas komentar per jam tercapai")

        await log_("INFO", f"Menfess #{msg.id} diproses")
        await humanize(s)
        # Menfess foto/video tanpa teks tetap dibalas, dengan konteks umum.
        content = text[:800] or "(menfess ini berupa foto atau video tanpa teks)"
        prompt = f'Menfess:\n"""\n{content}\n"""\nTulis satu komentar singkat.'
        reply = await ask_ai("menfess", [{"role": "user", "content": prompt}], s)
        if is_skip(reply):
            return await skip("AI memilih SKIP")
        await self.send(msg.chat_id, reply, msg.id)
        await incr(hk)
        await stat("comment")
        await log_("INFO", f"Komentar terkirim ke menfess #{msg.id}")

    async def reply(self, msg, s):
        if not s["reply_on"]:
            return
        target = await msg.get_reply_message()
        if not target or target.sender_id != self.me.id:
            return
        root = await find_root(target)
        if not root:
            return await skip("reply bukan ke thread menfess")
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
        day = f"day:{uid}:{datetime.now():%Y%m%d}"
        if await count(f"n:{thread}") >= s["max_thread"]:
            return await skip("batas balasan per thread tercapai")
        if await count(day) >= s["max_user_day"]:
            return await skip("batas balasan per user tercapai")
        if random.uniform(0, 100) > s["thread_prob"]:
            return await skip("kena peluang balas reply")

        sender = await msg.get_sender()
        name = getattr(sender, "first_name", None) or "Seseorang"
        await hist_add(thread, "user", f"{name}: {text}")
        await humanize(s)
        post = (root.message or "")[:500]
        msgs = [{"role": "user", "content": f"Menfess asli:\n{post}"}] + await hist_get(thread, 10)
        reply = await ask_ai("reply", msgs, s)
        if is_skip(reply):
            return await skip("AI memilih SKIP")
        await self.send(msg.chat_id, reply, msg.id)
        await hist_add(thread, "assistant", reply)
        await incr(f"n:{thread}")
        await incr(day)
        await stat("reply")

    # C. DM
    async def dm(self, ev):
        uid = ev.sender_id
        if not uid or uid == self.me.id:
            return
        text = (ev.raw_text or "").strip()
        if text.lower() == "/stop":
            await user_set(uid, stopped=1)
            return await self.say(uid, "Balasan otomatis dihentikan. Ketik /start untuk mengaktifkan lagi.")
        if text.lower() == "/start":
            await user_set(uid, stopped=0, greeted=1)
            return await self.say(uid, DISCLOSURE)

        s = await settings()
        u = await user(uid)
        if not s["dm_on"] or not text:
            return
        if u["lst"] == "black" or u["stopped"] or not u["ai_on"]:
            return
        if s["dm_whitelist"] and u["lst"] != "white":
            return
        hk = f"dm:{uid}:{datetime.now():%Y%m%d%H}"
        if await count(hk) >= s["dm_hour"]:
            return await skip("batas DM per jam tercapai")
        if not u["greeted"]:
            await self.say(uid, DISCLOSURE)
            await user_set(uid, greeted=1)

        key = f"dm:{uid}"
        await hist_add(key, "user", text)
        await humanize(s)
        reply = await ask_ai("dm", await hist_get(key, 12), s)
        if is_skip(reply):
            return await skip("AI memilih SKIP")
        await self.send(uid, reply)
        await hist_add(key, "assistant", reply)
        await incr(hk)
        await stat("dm")

    async def takeover(self, ev):
        """Jika kamu mengetik manual di DM yang ditangani AI, AI dimatikan untuk user itu."""
        uid = ev.chat_id
        if time.time() - self.ai_at.get(uid, 0) < 10:
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
    """Mulai userbot baru dengan session ini, lalu simpan ke database."""
    global ub
    new = UB(string)
    try:
        await new.start()
    except Exception as e:
        try:
            await new.stop()
        except Exception:
            pass
        return False, str(e)
    old, ub = ub, new
    if old:
        try:
            await old.stop()
        except Exception:
            pass
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
    # Data tombol memakai "|" sebagai pemisah di dalam b, bukan ":".
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

async def scr_home(_=""):
    s = await settings()
    chans = await q("SELECT paused FROM channels")
    st = await stats()
    total = st.get("comment", 0) + st.get("reply", 0) + st.get("dm", 0)
    status = "🟢 Online" if ub and ub.ready else "🔴 Offline"
    active = sum(1 for c in chans if not c["paused"])
    text = (
        "<b>🏠 Beranda</b>\n━━━━━━━━━━━━━━━━\n"
        f"🤖 Userbot : {status}\n"
        f"📡 Channel : {len(chans)} ({active} aktif)\n"
        f"💬 Total balasan : {total}\n"
        f"🧠 AI : {html.escape(active_ai(s))}"
    )
    rows = [
        [B("📡 Channel", "chan"), B("🤖 Pengaturan AI", "ai")],
        [B("💬 Mode Chat", "chat"), B("📊 Statistik", "stats")],
        [B("⚙️ Sistem", "sys"), B("🔑 Session", "sess")],
    ]
    return text, rows


async def scr_chan(_=""):
    chans = await q("SELECT * FROM channels ORDER BY username")
    body = "\n".join(
        f"{'⏸' if c['paused'] else '🟢'} @{html.escape(c['username'])} · {html.escape(c['title'] or '-')}"
        for c in chans
    ) or "<i>Belum ada channel.</i>"
    rows = [[B(f"{'⏸' if c['paused'] else '🟢'} @{c['username']}", "chanv", c["username"])] for c in chans]
    rows.append([B("➕ Tambah Channel", "in", "channel")])
    return f"<b>📡 Channel Menfess</b>\n\n{body}", rows + back()


async def scr_chanv(username=""):
    rows_ = await q("SELECT * FROM channels WHERE username=?", (username,))
    if not rows_:
        return await scr_chan()
    c = rows_[0]
    text = (
        f"<b>📡 @{html.escape(c['username'])}</b>\n\n"
        f"Judul : {html.escape(c['title'] or '-')}\n"
        f"Status : {'⏸ Dijeda' if c['paused'] else '🟢 Aktif'}\n"
        f"Diskusi ID : <code>{c['disc_id']}</code>"
    )
    rows = [[
        B("▶️ Lanjutkan" if c["paused"] else "⏸ Jeda", "pause", username),
        B("🗑 Hapus", "ask", f"delchan|{username}"),
    ]]
    return text, rows + back("chan")


async def scr_ai(_=""):
    s = await settings()
    text = (
        "<b>🤖 Pengaturan AI</b>\n\n"
        f"Urutan : {' → '.join(NAMES[p] for p in s['order'])}\n"
        f"Aktif : {html.escape(active_ai(s))}\n"
        f"Persona : {PERSONAS[s['persona']][0]}\n"
        f"Gaya : {STYLES[s['style']][0]}\n"
        f"Prompt kustom : {'aktif' if s['prompt'] else 'nonaktif'}"
    )
    rows = [
        [B("🔀 Urutan", "aiord"), B("🧩 Model", "aimdl")],
        [B("🎭 Persona", "aipers"), B("✍️ Gaya", "aisty")],
        [B("📝 Prompt Kustom", "in", "prompt"), B("♻️ Reset Prompt", "rprompt")],
    ]
    return text, rows + back()


async def scr_aiord(_=""):
    s = await settings()
    presets = [[B(" → ".join(NAMES[p] for p in perm), "set", f"order|{','.join(perm)}")]
               for perm in permutations(NAMES)]
    toggles = [B(f"{'⛔' if p in s['disabled'] else '✅'} {NAMES[p]}", "prov", p) for p in NAMES]
    text = "<b>🔀 Urutan Provider</b>\n\nJika satu provider gagal, sistem mencoba provider berikutnya."
    return text, presets + chunk(toggles) + back("ai")


async def scr_aimdl(_=""):
    s = await settings()
    btns = [
        B(f"{'✅ ' if s['model_' + p] == m else ''}{NAMES[p]} · {m.split('/')[-1][:22]}", "mdl", f"{p}|{i}")
        for p in NAMES for i, m in enumerate(MODELS[p])
    ]
    lines = "\n".join(f"• {NAMES[p]}: <code>{html.escape(s['model_' + p])}</code>" for p in NAMES)
    text = f"<b>🧩 Model AI</b>\n\n{lines}\n\nModel gratis OpenRouter bisa berubah sewaktu-waktu."
    return text, chunk(btns) + back("ai")


async def scr_aipers(_=""):
    s = await settings()
    btns = [B(f"{'✅ ' if s['persona'] == k else ''}{v[0]}", "set", f"persona|{k}") for k, v in PERSONAS.items()]
    desc = "\n".join(f"• <b>{v[0]}</b>: {html.escape(v[1])}" for v in PERSONAS.values())
    text = f"<b>🎭 Persona</b>\n\n{desc}\n\n<i>Prompt kustom (jika diisi) menggantikan persona.</i>"
    return text, chunk(btns) + back("ai")


async def scr_aisty(_=""):
    s = await settings()
    btns = [B(f"{'✅ ' if s['style'] == k else ''}{v[0]}", "set", f"style|{k}") for k, v in STYLES.items()]
    desc = "\n".join(f"• <b>{v[0]}</b>: {html.escape(v[1])}" for v in STYLES.values())
    return f"<b>✍️ Gaya Bahasa</b>\n\n{desc}", chunk(btns) + back("ai")


async def scr_chat(_=""):
    s = await settings()
    text = "<b>💬 Mode Chat</b>\n\n🟢 = aktif. Angka diubah dengan ➕ / ➖ atau preset."
    rows = [
        [B(f"{on(s['comment_on'])} Komentar", "tog", "comment_on"),
         B(f"{on(s['reply_on'])} Reply", "tog", "reply_on")],
        [B(f"{on(s['dm_on'])} DM", "tog", "dm_on"),
         B(f"{on(s['dm_whitelist'])} DM Whitelist saja", "tog", "dm_whitelist")],
        [B("🎚 Peluang Menfess", "num", "reply_prob"), B("🎚 Peluang Reply", "num", "thread_prob")],
        [B("⏱ Jeda Min", "num", "delay_min"), B("⏱ Jeda Max", "num", "delay_max")],
        [B("🔢 Batas/Jam", "num", "max_comment_hour"), B("🔢 Balasan/Thread", "num", "max_thread")],
        [B("🔢 Balasan/User/Hari", "num", "max_user_day"), B("📩 DM/Jam", "num", "dm_hour")],
        [B("📏 Min. Panjang", "num", "min_len"), B("🔑 Filter Keyword", "kw")],
        [B("👥 Kelola DM", "dm")],
    ]
    return text, rows + back()


async def scr_num(key=""):
    label, lo, hi, step, presets = NUMS[key]
    s = await settings()
    text = (
        f"<b>🎚 {label}</b>\n\nNilai saat ini : <b>{s[key]}</b>\nRentang : {lo} – {hi}\n\n"
        f"<i>{html.escape(HELP.get(key, ''))}</i>"
    )
    rows = [
        [B(f"➖ {step * 5}", "adj", f"{key}|{-step * 5}"), B(f"➕ {step * 5}", "adj", f"{key}|{step * 5}")],
        [B(f"➖ {step}", "adj", f"{key}|{-step}"), B(f"➕ {step}", "adj", f"{key}|{step}")],
    ]
    rows += chunk([B(f"⭐ {p}", "set", f"{key}|{p}") for p in presets])
    return text, rows + back("chat")


async def scr_kw(_=""):
    s = await settings()
    words = ", ".join(s["keywords"]) or "(kosong: semua menfess dibalas)"
    text = (
        "<b>🔑 Filter Keyword</b>\n\n"
        "Menfess hanya dibalas jika memuat salah satu kata:\n"
        f"<code>{html.escape(words)}</code>"
    )
    rows = [[B("✏️ Ubah Daftar", "in", "keyword"), B("🧹 Kosongkan", "kwclr")]]
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


async def scr_stats(_=""):
    st = await stats()
    logs = await q("SELECT ts, level, text FROM logs ORDER BY id DESC LIMIT 8")
    lines = [
        f"[{time.strftime('%d/%m %H:%M', time.localtime(x_['ts']))}] {x_['level']}: {html.escape(x_['text'][:120])}"
        for x_ in logs
    ]
    log_text = "\n".join(f"<code>{line}</code>" for line in lines) or "<i>Belum ada log.</i>"
    text = (
        "<b>📊 Statistik</b>\n\n"
        f"💬 Komentar : <b>{st.get('comment', 0)}</b>\n"
        f"↩️ Balasan reply : <b>{st.get('reply', 0)}</b>\n"
        f"📩 Balasan DM : <b>{st.get('dm', 0)}</b>\n"
        f"⏭ Dilewati : <b>{st.get('skipped', 0)}</b>\n"
        f"❌ Error : <b>{st.get('error', 0)}</b>\n\n"
        f"<b>Log terakhir</b>\n{log_text}"
    )
    rows = [[B("🔄 Refresh", "stats"), B("🗑 Reset Statistik", "ask", "rstats")]]
    return text, rows + back()


async def scr_sys(_=""):
    s = await settings()
    up = int(time.time() - STARTED)
    hours, minutes = divmod(up // 60, 60)
    size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
    ub_line = f"🟢 Online ({html.escape(ub.me.first_name or '')})" if ub and ub.ready else "🔴 Offline"
    text = (
        "<b>⚙️ Sistem</b>\n\n"
        f"🤖 Userbot : {ub_line}\n"
        f"🗄 Database : {size:.1f} KB\n"
        f"⏱ Uptime : {hours} jam {minutes} menit\n"
        f"🔔 Notifikasi : {'aktif' if s['notify'] else 'nonaktif'}"
    )
    rows = [
        [B(f"{on(s['notify'])} Notifikasi", "tog", "notify"), B("🧹 Hapus Log", "ask", "clogs")],
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
}


async def scr_ask(spec=""):
    action = spec.split("|", 1)[0]
    question, back_to = ASK[action]
    rows = [[B("✅ Ya, lanjutkan", "ok", spec)]] + back(back_to)
    return f"<b>⚠️ Konfirmasi</b>\n\n{question}", rows


INPUT_TEXT = {
    "channel": ("➕ Tambah Channel",
                "Kirim username channel, contoh <code>@namachannel</code>.\n"
                "Userbot harus sudah menjadi member discussion group-nya."),
    "prompt": ("📝 Prompt Kustom", "Kirim instruksi kepribadian AI. Ini menggantikan persona bawaan."),
    "keyword": ("🔑 Filter Keyword", "Kirim kata dipisahkan koma. Kirim <code>-</code> untuk mengosongkan."),
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
    "channel": "chan", "prompt": "ai", "keyword": "kw", "white": "dm", "black": "dm",
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
    "home": scr_home, "chan": scr_chan, "chanv": scr_chanv, "ai": scr_ai,
    "aiord": scr_aiord, "aimdl": scr_aimdl, "aipers": scr_aipers, "aisty": scr_aisty,
    "chat": scr_chat, "num": scr_num, "kw": scr_kw, "dm": scr_dm, "duser": scr_duser,
    "stats": scr_stats, "sys": scr_sys, "sess": scr_sess,
}


async def render(to, arg=""):
    return await SCREENS.get(to, scr_home)(arg)


# ---------- panel: aiogram ----------

class In(StatesGroup):
    channel = State()
    prompt = State()
    keyword = State()
    white = State()
    black = State()
    sessfile = State()
    phone = State()
    code = State()
    pwd = State()


INPUTS = {
    "channel": In.channel, "prompt": In.prompt, "keyword": In.keyword,
    "white": In.white, "black": In.black, "sessfile": In.sessfile, "phone": In.phone,
}
PANEL = {"chat": None, "mid": None}

panel = Router(name="panel")
panel.message.filter(F.from_user.id == OWNER_ID)
panel.callback_query.filter(F.from_user.id == OWNER_ID)


async def show(text, rows, notice=None):
    """Edit satu pesan panel. Kalau belum ada atau sudah hilang, kirim yang baru."""
    if notice:
        text = f"<i>{html.escape(notice)}</i>\n\n{text}"
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
    if cq.message:
        PANEL["chat"], PANEL["mid"] = cq.message.chat.id, cq.message.message_id
    await cq.answer()
    await show(text, rows, notice)


async def show_result(notice):
    text, rows = await render("sess")
    await show(text, rows, notice)


async def show_string(string, me, source):
    PENDING["string"] = string
    text = (
        "<b>🔑 Session siap</b>\n\n"
        f"Akun : {html.escape(me.first_name or '-')} (lewat {source})\n\n"
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
    # /start TIDAK dihapus. Panel lama dihapus, panel baru dikirim di bawah /start.
    await state.clear()
    await drop_panel()
    text, rows = await render("home")
    await show(text, rows)


@panel.callback_query(CB.filter())
async def on_cb(cq: CallbackQuery, callback_data: CB, state: FSMContext):
    await state.clear()
    a, b = callback_data.a, callback_data.b
    to, arg, notice = a, b, None

    if a == "in":
        await state.set_state(INPUTS[b])
        text, rows = input_screen(b)
        return await show_cb(cq, text, rows)
    if a == "ask":
        text, rows = await scr_ask(b)
        return await show_cb(cq, text, rows)
    if a == "act":
        string = PENDING.get("string")
        if not string:
            text, rows = await render("sess")
            return await show_cb(cq, text, rows, "Session sudah tidak tersedia. Buat ulang.")
        ok, err = await activate(string)
        if ok:
            PENDING.clear()
            text, rows = await render("home")
            return await show_cb(cq, text, rows, "✅ Userbot aktif dengan session baru.")
        text, rows = await render("sess")
        return await show_cb(cq, text, rows, f"Gagal mengaktifkan: {err}")
    if a == "loginx":
        await login_close()
        text, rows = await render("sess")
        return await show_cb(cq, text, rows, "Login dibatalkan.")
    if a == "tog":
        s = await settings()
        await setv(b, not s[b])
        to, arg = ("sys" if b == "notify" else "chat"), ""
    elif a == "set":
        key, val = b.split("|", 1)
        if key == "order":
            await setv("order", val.split(","))
            to, arg = "aiord", ""
        elif key in ("persona", "style"):
            await setv(key, val)
            to, arg = ("aipers" if key == "persona" else "aisty"), ""
        else:
            await setv(key, int(val))
            to, arg = "num", key
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
        elif action == "chist":
            await x("DELETE FROM hist")
            to, notice = "sys", "Riwayat AI dihapus."
        elif action == "restart":
            asyncio.get_running_loop().call_later(2, restart)
            to, notice = "sys", "Bot restart dalam 2 detik..."

    text, rows = await render(to, arg)
    await show_cb(cq, text, rows, notice)


@panel.message(In.channel)
async def in_channel(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    name = (msg.text or "").strip().lstrip("@").lower()
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
            notice = f"@{name} berhasil ditambahkan."
        except Exception as e:
            notice = f"Gagal: {e}"
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
        return await show_result(f"Gagal memproses session: {e}")
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
        return await show_result(f"Gagal mengirim kode: {e}")
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
    except SessionPasswordNeededError:
        await state.set_state(In.pwd)
        text, rows = input_screen("pwd")
        return await show(text, rows, "Akun ini memakai 2FA. Kirim password-nya.")
    except PhoneCodeInvalidError:
        text, rows = input_screen("code")
        return await show(text, rows, "Kode salah. Coba lagi.")
    except Exception as e:
        await state.clear()
        await login_close()
        return await show_result(f"Login gagal: {e}")
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

async def run_bot():
    global bot, ub
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    s = await settings()
    # Session dari panel (jika pernah diaktifkan) didahulukan, lalu secret TG_STRING_SESSION.
    ub = UB(s.get("session") or SESSION)
    try:
        await ub.start()
    except Exception as e:
        log.error("Userbot gagal: %s", e)
        await log_("ERROR", f"Userbot gagal: {e}")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(panel)
    await notify("✅ Bot aktif." if ub.ready else "⚠️ Panel aktif. Userbot offline, buat session di menu 🔑 Session.")
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