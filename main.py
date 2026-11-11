"""
Menfess AI Bot: panel inline (aiogram) + userbot (Telethon) dalam satu proses.

Jalankan bot:    python main.py
Buat session:    python main.py session
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import time
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
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, PeerChannel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ---------- konfigurasi (dari environment / GitHub Secrets) ----------
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
# Nama model bisa berubah. Sesuaikan jika ada error HTTP 404.
MODELS = {
    "gemini": ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
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
}
# key: (label, min, max, langkah, preset)
NUMS = {
    "reply_prob": ("Probabilitas balas menfess (%)", 0, 100, 5, [25, 50, 75, 100]),
    "thread_prob": ("Probabilitas balas reply (%)", 0, 100, 5, [30, 60, 80, 100]),
    "delay_min": ("Jeda minimal (detik)", 0, 600, 5, [5, 15, 30]),
    "delay_max": ("Jeda maksimal (detik)", 0, 900, 5, [15, 30, 60, 120]),
    "max_comment_hour": ("Komentar per jam", 1, 60, 1, [3, 6, 10, 20]),
    "max_thread": ("Balasan per thread", 1, 20, 1, [1, 2, 3, 5]),
    "max_user_day": ("Balasan per user per hari", 1, 100, 1, [3, 5, 10, 20]),
    "dm_hour": ("Balasan DM per jam", 1, 200, 5, [10, 20, 50, 100]),
}
DEFAULTS = {
    "comment_on": True, "reply_on": True, "dm_on": True, "dm_whitelist": False, "notify": True,
    "reply_prob": 60, "thread_prob": 70, "delay_min": 5, "delay_max": 25,
    "max_comment_hour": 6, "max_thread": 3, "max_user_day": 10, "dm_hour": 20,
    "keywords": [], "order": ["gemini", "groq", "openrouter"], "disabled": [],
    "model_gemini": MODELS["gemini"][0],
    "model_groq": MODELS["groq"][0],
    "model_openrouter": MODELS["openrouter"][0],
    "persona": "teman", "style": "santai", "prompt": "",
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
        "- Balas dalam Bahasa Indonesia, singkat (1-3 kalimat), seperti chat sungguhan.\n"
        "- Jika ditanya, jujur bahwa kamu asisten AI. Jangan mengaku manusia.\n"
        "- Jangan menghina, memprovokasi, membahas SARA, pornografi, atau kekerasan.\n"
        "- Jangan meminta atau membagikan data pribadi.\n"
        "- Jangan memaksakan percakapan. Tutup dengan sopan jika sudah cukup.\n"
        "- Jika pesan spam, provokasi, tidak bermakna, atau sebaiknya tidak dibalas, jawab persis: SKIP\n"
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
    """Coba provider sesuai urutan. Jika semua gagal, raise AIError."""
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
    def __init__(self):
        self.c = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
        self.me = None
        self.ready = False
        self.by_disc = {}
        self.ai_at = {}
        self.tasks = set()
        self.alert_at = 0.0

    async def start(self):
        await self.c.connect()
        if not await self.c.is_user_authorized():
            raise RuntimeError("TG_STRING_SESSION tidak valid")
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

    # dispatch: setiap event diproses di task terpisah agar delay tidak memblokir update lain
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
        # Catat waktu sebelum kirim, supaya event pesan keluar ini tidak dianggap takeover.
        self.ai_at[chat] = time.time()
        await self.c.send_message(chat, text, reply_to=reply_to)

    async def send(self, chat, text, reply_to=None):
        async with self.c.action(chat, "typing"):
            await asyncio.sleep(min(1.5 + len(text) * 0.05, 8))
        await self.say(chat, text, reply_to)

    # A. komentar menfess
    async def group(self, ev):
        ch = self.by_disc[ev.chat_id]
        msg = ev.message
        s = await settings()
        fwd = msg.fwd_from
        if fwd and fwd.channel_post and fwd.from_id and utils.get_peer_id(fwd.from_id) == ch["chat_id"]:
            return await self.menfess(msg, ch, s)
        if msg.reply_to_msg_id and msg.sender_id != self.me.id:
            return await self.reply(msg, s)

    async def menfess(self, msg, ch, s):
        if ch["paused"] or not s["comment_on"]:
            return
        text = (msg.message or "").strip()
        if len(text) < 15 or random.uniform(0, 100) > s["reply_prob"]:
            return await stat("skipped")
        if s["keywords"] and not any(k.lower() in text.lower() for k in s["keywords"]):
            return await stat("skipped")
        hk = f"hour:{datetime.now():%Y%m%d%H}:c"
        if await count(hk) >= s["max_comment_hour"]:
            return await stat("skipped")

        await humanize(s)
        prompt = f'Menfess:\n"""\n{text[:800]}\n"""\nTulis satu komentar singkat.'
        reply = await ask_ai("menfess", [{"role": "user", "content": prompt}], s)
        if is_skip(reply):
            return await stat("skipped")
        await self.send(msg.chat_id, reply, msg.id)
        await incr(hk)
        await stat("comment")
        await log_("INFO", f"Komentar menfess #{msg.id}")

    # B. balas reply ke komentar userbot
    async def reply(self, msg, s):
        if not s["reply_on"]:
            return
        target = await msg.get_reply_message()
        if not target or target.sender_id != self.me.id:
            return
        root = await find_root(target)
        if not root:
            return
        uid = msg.sender_id
        if (await user(uid))["lst"] == "black":
            return
        text = (msg.message or "").strip()
        if len(text) < 2:
            return
        thread = f"t:{msg.chat_id}:{root.id}"
        closed = f"closed:{thread}:{uid}"
        if await count(closed):
            return
        if CLOSING.match(text):
            await incr(closed)
            return
        day = f"day:{uid}:{datetime.now():%Y%m%d}"
        if await count(f"n:{thread}") >= s["max_thread"] or await count(day) >= s["max_user_day"]:
            return
        if random.uniform(0, 100) > s["thread_prob"]:
            return await stat("skipped")

        sender = await msg.get_sender()
        name = getattr(sender, "first_name", None) or "Seseorang"
        await hist_add(thread, "user", f"{name}: {text}")
        await humanize(s)
        post = (root.message or "")[:500]
        msgs = [{"role": "user", "content": f"Menfess asli:\n{post}"}] + await hist_get(thread, 10)
        reply = await ask_ai("reply", msgs, s)
        if is_skip(reply):
            return await stat("skipped")
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
            return await stat("skipped")
        if not u["greeted"]:
            await self.say(uid, DISCLOSURE)
            await user_set(uid, greeted=1)

        key = f"dm:{uid}"
        await hist_add(key, "user", text)
        await humanize(s)
        reply = await ask_ai("dm", await hist_get(key, 12), s)
        if is_skip(reply):
            return await stat("skipped")
        await self.send(uid, reply)
        await hist_add(key, "assistant", reply)
        await incr(hk)
        await stat("dm")

    async def takeover(self, ev):
        """Jika owner mengetik manual di DM yang ditangani AI, AI dimatikan untuk user itu."""
        uid = ev.chat_id
        if time.time() - self.ai_at.get(uid, 0) < 10:
            return
        u = await user(uid)
        if u["greeted"] and u["ai_on"]:
            await user_set(uid, ai_on=0)
            await log_("INFO", f"Takeover DM {uid}")
            await notify(f"🙋 Kamu mengambil alih chat {uid}. AI dimatikan untuk user ini.")


# ---------- panel: callback & screen helpers ----------

class CB(CallbackData, prefix="c"):
    a: str
    b: str = ""


def B(text, a, b=""):
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
        [B("⚙️ Sistem", "sys")],
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
        B("🗑 Hapus", "ask", f"delchan:{username}"),
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
    presets = [[B(" → ".join(NAMES[p] for p in perm), "set", f"order:{','.join(perm)}")]
               for perm in permutations(NAMES)]
    toggles = [B(f"{'⛔' if p in s['disabled'] else '✅'} {NAMES[p]}", "prov", p) for p in NAMES]
    return "<b>🔀 Urutan Provider</b>\n\nFallback mengikuti urutan ini.", presets + chunk(toggles) + back("ai")


async def scr_aimdl(_=""):
    s = await settings()
    btns = [
        B(f"{'✅ ' if s['model_' + p] == m else ''}{NAMES[p]} · {m.split('/')[-1][:22]}", "mdl", f"{p}:{i}")
        for p in NAMES for i, m in enumerate(MODELS[p])
    ]
    lines = "\n".join(f"• {NAMES[p]}: <code>{html.escape(s['model_' + p])}</code>" for p in NAMES)
    text = f"<b>🧩 Model AI</b>\n\n{lines}\n\nModel gratis OpenRouter bisa berubah sewaktu-waktu."
    return text, chunk(btns) + back("ai")


async def scr_aipers(_=""):
    s = await settings()
    btns = [B(f"{'✅ ' if s['persona'] == k else ''}{v[0]}", "set", f"persona:{k}") for k, v in PERSONAS.items()]
    desc = "\n".join(f"• <b>{v[0]}</b>: {html.escape(v[1])}" for v in PERSONAS.values())
    text = f"<b>🎭 Persona</b>\n\n{desc}\n\n<i>Prompt kustom (jika diisi) menggantikan persona.</i>"
    return text, chunk(btns) + back("ai")


async def scr_aisty(_=""):
    s = await settings()
    btns = [B(f"{'✅ ' if s['style'] == k else ''}{v[0]}", "set", f"style:{k}") for k, v in STYLES.items()]
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
        [B("🎚 Prob. Menfess", "num", "reply_prob"), B("🎚 Prob. Reply", "num", "thread_prob")],
        [B("⏱ Jeda Min", "num", "delay_min"), B("⏱ Jeda Max", "num", "delay_max")],
        [B("🔢 Komentar/Jam", "num", "max_comment_hour"), B("🔢 Balasan/Thread", "num", "max_thread")],
        [B("🔢 Balasan/User/Hari", "num", "max_user_day"), B("📩 DM/Jam", "num", "dm_hour")],
        [B("🔑 Filter Keyword", "kw"), B("👥 Kelola DM", "dm")],
    ]
    return text, rows + back()


async def scr_num(key=""):
    label, lo, hi, step, presets = NUMS[key]
    s = await settings()
    text = f"<b>🎚 {label}</b>\n\nNilai saat ini : <b>{s[key]}</b>\nRentang : {lo} – {hi}"
    rows = [
        [B(f"➖ {step * 5}", "adj", f"{key}:{-step * 5}"), B(f"➕ {step * 5}", "adj", f"{key}:{step * 5}")],
        [B(f"➖ {step}", "adj", f"{key}:{-step}"), B(f"➕ {step}", "adj", f"{key}:{step}")],
    ]
    rows += chunk([B(f"⭐ {p}", "set", f"{key}:{p}") for p in presets])
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
    text = (
        f"<b>👤 Pengguna {uid}</b>\n\n"
        f"Status : {user_tag(u)}\nAI : {ai_state}\nBalasan : {stop_state}"
    )
    rows = [
        [B("🔁 Aktifkan AI" if not u["ai_on"] else "⛔ Matikan AI", "aitog", uid)],
        [B("✅ Whitelist", "list", f"{uid}:white"), B("🚫 Blacklist", "list", f"{uid}:black")],
        [B("♻️ Hapus dari list", "list", f"{uid}:none")],
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
    userbot_line = f"🟢 Online ({html.escape(ub.me.first_name or '')})" if ub and ub.ready else "🔴 Offline"
    text = (
        "<b>⚙️ Sistem</b>\n\n"
        f"🤖 Userbot : {userbot_line}\n"
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


ASK = {
    "delchan": ("Hapus channel ini dari daftar?", "chan"),
    "rstats": ("Reset semua statistik?", "stats"),
    "clogs": ("Hapus semua log?", "sys"),
    "chist": ("Hapus riwayat percakapan AI? Konteks balasan akan hilang.", "sys"),
    "restart": ("Restart bot sekarang?", "sys"),
}


async def scr_ask(spec=""):
    action = spec.split(":", 1)[0]
    question, back_to = ASK[action]
    rows = [[B("✅ Ya, lanjutkan", "ok", spec)]] + back(back_to)
    return f"<b>⚠️ Konfirmasi</b>\n\n{question}", rows


INPUT_TEXT = {
    "channel": ("➕ Tambah Channel", "Kirim username channel, contoh <code>@namachannel</code>.\n"
                "Userbot harus sudah menjadi member discussion group-nya."),
    "prompt": ("📝 Prompt Kustom", "Kirim instruksi kepribadian AI. Ini menggantikan persona bawaan."),
    "keyword": ("🔑 Filter Keyword", "Kirim kata dipisahkan koma. Kirim <code>-</code> untuk mengosongkan."),
    "white": ("✅ Whitelist DM", "Kirim user ID Telegram (angka)."),
    "black": ("🚫 Blacklist DM", "Kirim user ID Telegram (angka)."),
}
INPUT_BACK = {"channel": "chan", "prompt": "ai", "keyword": "kw", "white": "dm", "black": "dm"}


def input_screen(kind):
    title, desc = INPUT_TEXT[kind]
    return f"<b>{title}</b>\n\n{desc}", back(INPUT_BACK[kind])


SCREENS = {
    "home": scr_home, "chan": scr_chan, "chanv": scr_chanv, "ai": scr_ai,
    "aiord": scr_aiord, "aimdl": scr_aimdl, "aipers": scr_aipers, "aisty": scr_aisty,
    "chat": scr_chat, "num": scr_num, "kw": scr_kw, "dm": scr_dm, "duser": scr_duser,
    "stats": scr_stats, "sys": scr_sys,
}


async def render(to, arg=""):
    return await SCREENS.get(to, scr_home)(arg)


# ---------- panel: aiogram handlers ----------

class In(StatesGroup):
    channel = State()
    prompt = State()
    keyword = State()
    white = State()
    black = State()


INPUTS = {"channel": In.channel, "prompt": In.prompt, "keyword": In.keyword, "white": In.white, "black": In.black}
PANEL = {"chat": None, "mid": None}

panel = Router(name="panel")
panel.message.filter(F.from_user.id == OWNER_ID)
panel.callback_query.filter(F.from_user.id == OWNER_ID)


async def show(text, rows, notice=None):
    """Edit satu pesan panel. Jika belum ada atau sudah hilang, kirim yang baru."""
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


async def delete_msg(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass


def restart():
    os.execv(sys.executable, [sys.executable] + sys.argv)


@panel.message(CommandStart())
async def on_start(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    text, rows = await render("home")
    await show(text, rows)


@panel.callback_query(CB.filter())
async def on_cb(cq: CallbackQuery, callback_data: CB, state: FSMContext):
    await state.clear()
    a, b = callback_data.a, callback_data.b
    notice = None
    to, arg = a, b

    if a == "in":
        await state.set_state(INPUTS[b])
        text, rows = input_screen(b)
        return await show_cb(cq, text, rows)
    if a == "ask":
        text, rows = await scr_ask(b)
        return await show_cb(cq, text, rows)
    if a == "tog":
        s = await settings()
        await setv(b, not s[b])
        to, arg = ("sys" if b == "notify" else "chat"), ""
    elif a == "set":
        key, val = b.split(":", 1)
        if key == "order":
            await setv("order", val.split(","))
        elif key in ("persona", "style"):
            await setv(key, val)
        else:
            await setv(key, int(val))
        to = {"order": "aiord", "persona": "aipers", "style": "aisty"}.get(key, "num")
        arg = "" if key in ("order", "persona", "style") else key
    elif a == "mdl":
        prov, idx = b.split(":")
        await setv(f"model_{prov}", MODELS[prov][int(idx)])
        to, arg = "aimdl", ""
    elif a == "adj":
        key, delta = b.split(":")
        _, lo, hi, _, _ = NUMS[key]
        s = await settings()
        await setv(key, max(lo, min(hi, s[key] + int(delta))))
        to, arg = "num", key
    elif a == "prov":
        s = await settings()
        disabled = set(s["disabled"]) ^ {b}
        await setv("disabled", sorted(disabled))
        to, arg = "aiord", ""
    elif a == "pause":
        await x("UPDATE channels SET paused = 1 - paused WHERE username=?", (b,))
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
        uid, lst = b.split(":")
        await user_set(int(uid), lst=None if lst == "none" else lst)
        to, arg = "duser", uid
    elif a == "ok":
        action, target = b.split(":", 1) if ":" in b else (b, "")
        to, arg = "home", ""
        if action == "delchan":
            await x("DELETE FROM channels WHERE username=?", (target,))
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
        notice = "Userbot belum online."
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
    notice = "Prompt kustom disimpan." if raw else "Kirim teks untuk prompt."
    if raw:
        await setv("prompt", raw[:1500])
    text, rows = await render("ai")
    await show(text, rows, notice)


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


@panel.message()
async def on_any(msg: Message, state: FSMContext):
    await state.clear()
    await delete_msg(msg)
    text, rows = await render("home")
    await show(text, rows)


# ---------- entry points ----------

async def run_bot():
    global bot, ub
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    ub = UB()
    try:
        await ub.start()
    except Exception as e:
        log.error("Userbot gagal: %s", e)
        await log_("ERROR", f"Userbot gagal: {e}")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(panel)
    await notify("✅ Bot aktif." if ub.ready else "⚠️ Panel aktif, userbot offline.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        try:
            await ub.stop()
        except Exception:
            pass
        await bot.session.close()
        await db.close()


async def make_session():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()
    print("\nSalin nilai ini ke GitHub Secret TG_STRING_SESSION:\n")
    print(client.session.save())
    await client.disconnect()


if __name__ == "__main__":
    if sys.argv[1:] == ["session"]:
        asyncio.run(make_session())
    else:
        if not BOT_TOKEN or not OWNER_ID:
            raise SystemExit("BOT_TOKEN dan OWNER_ID wajib diisi")
        asyncio.run(run_bot())
