"""
Menfess AI Bot: panel inline (aiogram) + userbot (Telethon) dalam satu file.

Alur utama:
  1. Postingan baru di channel terdeteksi otomatis (realtime + cek berkala sebagai cadangan).
  2. Setiap postingan dibalas di kolom komentar, berapa pun panjang teksnya.
  3. Kalau gagal membalas, bot mengirim alasannya ke owner (waktu WIB).
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
from datetime import datetime, timedelta, timezone
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
    "min_len": ("Panjang minimal menfess (karakter)", 0, 500, 1, [0, 1, 5, 20]),
    "delay_min": ("Jeda minimal (detik)", 0, 600, 5, [0, 3, 10, 30]),
    "delay_max": ("Jeda maksimal (detik)", 0, 900, 5, [3, 10, 30, 60]),
    "max_comment_hour": ("Batas komentar per jam (0 = tanpa batas)", 0, 1000, 10, [0, 30, 100, 500]),
    "max_age_min": ("Abaikan postingan lebih tua dari (menit)", 5, 1440, 5, [15, 30, 60, 180]),
    "max_thread": ("Balasan per thread", 1, 20, 1, [1, 2, 3, 5]),
    "max_user_day": ("Balasan per user per hari", 1, 100, 1, [3, 5, 10, 20]),
    "dm_hour": ("Balasan DM per jam", 1, 200, 5, [10, 20, 50, 100]),
}
HELP = {
    "reply_prob": "Dari 100 menfess yang masuk, berapa yang dibalas. 100 berarti SEMUA dibalas.",
    "thread_prob": "Peluang bot membalas orang yang membalas komentarnya.",
    "min_len": "Menfess yang teksnya lebih pendek dari angka ini dilewati. 0 berarti semua dibalas, termasuk foto tanpa teks.",
    "delay_min": "Waktu tunggu minimal sebelum membalas, supaya terlihat manusiawi.",
    "delay_max": "Waktu tunggu maksimal. Bot memilih acak antara jeda minimal dan maksimal.",
    "max_comment_hour": "Batas komentar menfess per jam. 0 berarti tanpa batas.",
    "max_age_min": "Kalau bot sempat mati, postingan yang lebih tua dari ini tidak dibalas supaya tidak terlihat aneh.",
    "max_thread": "Berapa kali bot boleh membalas dalam satu thread (satu menfess).",
    "max_user_day": "Berapa kali bot boleh membalas satu orang yang sama dalam sehari.",
    "dm_hour": "Batas balasan DM per jam untuk setiap pengguna.",
}
DEFAULTS = {
    "comment_on": True, "reply_on": True, "dm_on": True, "dm_whitelist": False,
    "notify": True, "notify_ok": False,
    "reply_prob": 100, "thread_prob": 70, "min_len": 0, "delay_min": 0, "delay_max": 3,
    "max_comment_hour": 40, "max_age_min": 60, "max_thread": 3, "max_user_day": 10, "dm_hour": 20,
    "keywords": [], "order": ["gemini", "groq"], "disabled": [],
    "model_gemini": MODELS["gemini"][0],
    "model_groq": MODELS["groq"][0],
    "model_openrouter": MODELS["openrouter"][0],
    "persona": "teman", "style": "x", "prompt": "",
}
SETTINGS_VERSION = 9   # naikkan kalau DEFAULTS diubah, supaya pengaturan lama tidak menimpa
POLL_SEC = 10          # jeda cek berkala channel (cadangan kalau update realtime tidak masuk)
SEND_GAP = 2.5         # jeda minimal antar komentar, mencegah FloodWait
DISCLOSURE = (
    "Halo! 👋 Akun ini menggunakan asisten AI untuk membalas pesan secara otomatis.\n"
    "Ketik /stop kapan saja untuk menghentikan balasan otomatis."
)
CLOSING = re.compile(r"^\s*(ok|oke|okay|sip|makasih|mksh|thanks|thx|noted|siap|yoi)[\s!.]*$", re.I)
MODE_TEXT = {
    "menfess": (
        "Kamu sedang memberi komentar pada sebuah menfess (pesan anonim) di channel. "
        "Tanggapi isinya dengan relevan. Selalu tulis komentar. "
        "Jika menfess berisi curhat berat atau keinginan menyakiti diri, balas dengan empati singkat "
        "dan sarankan bicara dengan orang terpercaya atau profesional."
    ),
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
CREATE TABLE IF NOT EXISTS seen (chat_id INTEGER, post_id INTEGER, PRIMARY KEY(chat_id, post_id));
CREATE TABLE IF NOT EXISTS cursor (chat_id INTEGER PRIMARY KEY, last_id INTEGER);
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


def system_prompt(s, mode):
    persona = s["prompt"].strip() or PERSONAS[s["persona"]][1]
    rules = [
        "Balas dalam Bahasa Indonesia, sangat singkat (1 kalimat, maksimal 20 kata), seperti chat sungguhan.",
        "Jika ditanya langsung, jujur bahwa kamu asisten AI. Jangan mengaku manusia.",
        "Jangan menghina, memprovokasi, membahas SARA, konten seksual, atau kekerasan.",
        "Jangan meminta atau membagikan data pribadi.",
        "Jangan memaksakan percakapan.",
        "Keluarkan hanya teks balasan, tanpa label atau tanda kutip.",
    ]
    if mode != "menfess":
        rules.insert(4, "Jika pesan spam, provokasi, atau tidak bermakna, jawab persis: SKIP")
    body = "\n".join(f"- {r}" for r in rules)
    return f"{persona}\n{STYLES[s['style']][1]}\n{MODE_TEXT[mode]}\n\nAturan wajib:\n{body}"


def merge_roles(msgs):
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n" + m["content"]}
        else:
            out.append({"role": m["role"], "content": m["content"]})
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
                "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1024},
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

    body = {"model": model, "messages": [{"role": "system", "content": system}, *msgs],
            "temperature": 0.9, "max_tokens": 200}
    if name == "groq" and model.startswith(("openai/gpt-oss", "qwen/")):
        # model reasoning: token "berpikir" ikut dihitung, jadi budget dilonggarkan dan effort dibuat rendah
        body["max_tokens"] = 1500
        body["reasoning_effort"] = "low" if model.startswith("openai/") else "none"
    r = await http.post(
        URLS[name],
        headers={"Authorization": f"Bearer {KEYS[name]}"},
        json=body,
    )
    if r.status_code >= 400:
        raise RuntimeError(http_err(r.status_code))
    return r.json()["choices"][0]["message"]["content"]


async def ask_ai(mode, msgs, s):
    """Coba provider sesuai urutan. Kalau semua gagal, raise AIError berisi alasan tiap provider."""
    system = system_prompt(s, mode)
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
                    text = (await call(http, name, model, system, msgs) or "").strip()
                    text = text.strip("\"'").strip()
                    if text:
                        if s["style"] == "x":
                            text = text.lower()
                        return text[:600]
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
        self.tasks = set()
        self.poller = None

    async def start(self):
        await self.c.connect()
        if not await self.c.is_user_authorized():
            raise RuntimeError("session kosong, tidak valid, atau sudah logout")
        self.me = await self.c.get_me()
        try:
            # Memuat dialog supaya Telethon mengenal grup diskusi dan menerima update-nya.
            await self.c.get_dialogs(limit=200)
        except Exception as e:
            log.warning("get_dialogs gagal: %s", e)
        self.c.add_event_handler(self.on_in, events.NewMessage(incoming=True))
        self.c.add_event_handler(self.on_out, events.NewMessage(outgoing=True))
        await self.refresh()
        self.ready = True
        self.poller = asyncio.create_task(self.poll_loop())
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

    async def on_out(self, ev):
        if ev.is_private and ev.chat_id != self.me.id:
            self.spawn(self.takeover(ev))

    # ----- cek berkala (cadangan kalau update realtime tidak masuk) -----
    async def poll_loop(self):
        await asyncio.sleep(3)
        while self.ready:
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
        if msg.reply_to_msg_id and msg.sender_id != self.me.id:
            return await self.reply(msg, await settings())

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
        elif s["reply_prob"] < 100 and random.uniform(0, 100) > s["reply_prob"]:
            reason = f"Terkena peluang balas {s['reply_prob']}%."
        elif s["keywords"] and not any(k.lower() in text.lower() for k in s["keywords"]):
            reason = "Tidak ada keyword filter yang cocok."
        if reason:
            await skip(f"#{post_id}: {reason}")
            return await report(
                "⏭ Menfess tidak dibalas", f"{self.ctx(ch, post_id, text)}\n❓ {esc(reason)}",
                key="skip:" + reason[:18], every=30,
            )
        self.spawn(self.reply_post(ch, post_id, text, group_msg, s))

    @staticmethod
    def ctx(ch, post_id, text):
        return f"📡 @{esc(ch['username'])} · menfess #{post_id}\n📝 {esc(text[:80]) or '[tanpa teks]'}"

    async def reply_post(self, ch, post_id, text, group_msg, s):
        ctx = self.ctx(ch, post_id, text)
        hk = f"hour:{now_wib():%Y%m%d%H}:c"
        if s["max_comment_hour"] > 0 and await count(hk) >= s["max_comment_hour"]:
            await skip(f"#{post_id}: batas komentar per jam")
            return await report("⏭ Menfess tidak dibalas",
                                f"{ctx}\n❓ Batas komentar per jam ({s['max_comment_hour']}) tercapai.", key="hourlimit")

        await humanize(s)
        content = text[:800] or "(menfess ini berupa foto, video, atau stiker tanpa teks)"
        prompt = (
            f'Menfess:\n"""\n{content}\n"""\n'
            "Tulis satu komentar singkat untuk menfess ini. Jangan jawab SKIP."
        )
        reply, last_err = None, ""
        for attempt in range(1, 4):
            try:
                async with AI_SEM:
                    reply = await ask_ai("menfess", [{"role": "user", "content": prompt}], s)
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
                f"{ctx}\n❓ AI menolak membalas (kemungkinan isi menfess sensitif).", key="aiskip",
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
        if random.uniform(0, 100) > s["thread_prob"]:
            return await skip("reply kena peluang balas")

        sender = await msg.get_sender()
        name = getattr(sender, "first_name", None) or "Seseorang"
        await hist_add(thread, "user", f"{name}: {text}")
        await humanize(s)
        post = (root.message or "")[:500]
        msgs = [{"role": "user", "content": f"Menfess asli:\n{post}"}] + await hist_get(thread, 10)
        async with AI_SEM:
            reply = await ask_ai("reply", msgs, s)
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
    async def say(self, chat, text):
        self.ai_at[chat] = time.time()  # dicatat sebelum kirim supaya tidak dianggap takeover
        await self.c.send_message(chat, text)

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
        hk = f"dm:{uid}:{now_wib():%Y%m%d%H}"
        if await count(hk) >= s["dm_hour"]:
            return await skip("batas DM per jam tercapai")
        if not u["greeted"]:
            await self.say(uid, DISCLOSURE)
            await user_set(uid, greeted=1)

        key = f"dm:{uid}"
        await hist_add(key, "user", text)
        await humanize(s)
        async with AI_SEM:
            reply = await ask_ai("dm", await hist_get(key, 12), s)
        if is_skip(reply):
            return await skip("AI memilih SKIP")
        await self.typing(uid, min(1 + len(reply) * 0.04, 5))
        await self.say(uid, reply)
        await hist_add(key, "assistant", reply)
        await incr(hk)
        await stat("dm")

    async def takeover(self, ev):
        """Kalau kamu mengetik manual di DM yang ditangani AI, AI dimatikan untuk user itu."""
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

async def scr_home(_=""):
    s = await settings()
    chans = await q("SELECT paused FROM channels")
    st = await stats()
    total = st.get("comment", 0) + st.get("reply", 0) + st.get("dm", 0)
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
    ]
    return text, rows


async def scr_chan(_=""):
    chans = await q("SELECT * FROM channels ORDER BY username")
    body = "\n".join(
        f"{'⏸' if c['paused'] else '🟢'} @{esc(c['username'])} · {esc(c['title'] or '-')}" for c in chans
    ) or "<i>Belum ada channel.</i>"
    rows = [[B(f"{'⏸' if c['paused'] else '🟢'} @{c['username']}", "chanv", c["username"])] for c in chans]
    rows.append([B("➕ Tambah Channel", "in", "channel"), B("🩺 Diagnostik", "diag")])
    return f"<b>📡 Channel Menfess</b>\n\n{body}", rows + back()


async def scr_chanv(username=""):
    rows_ = await q("SELECT * FROM channels WHERE username=?", (username,))
    if not rows_:
        return await scr_chan()
    c = rows_[0]
    text = (
        f"<b>📡 @{esc(c['username'])}</b>\n\n"
        f"Judul : {esc(c['title'] or '-')}\n"
        f"Status : {'⏸ Dijeda' if c['paused'] else '🟢 Aktif'}\n"
        f"Diskusi ID : <code>{c['disc_id']}</code>"
    )
    rows = [
        [B("▶️ Lanjutkan" if c["paused"] else "⏸ Jeda", "pause", username),
         B("🗑 Hapus", "ask", f"delchan|{username}")],
        [B("🩺 Diagnostik channel ini", "diag", username)],
    ]
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
        f"Urutan : {' → '.join(NAMES[p] for p in s['order'])}\n"
        f"Aktif : {esc(active_ai(s))}\n"
        f"Persona : {PERSONAS[s['persona']][0]}\n"
        f"Gaya : {STYLES[s['style']][0]}\n"
        f"Prompt kustom : {'aktif' if s['prompt'] else 'nonaktif'}"
    )
    rows = [
        [B("🔀 Urutan", "aiord"), B("🧩 Model", "aimdl")],
        [B("🎭 Persona", "aipers"), B("✍️ Gaya", "aisty")],
        [B("📝 Prompt Kustom", "in", "prompt"), B("♻️ Reset Prompt", "rprompt")],
        [B("🧪 Tes AI", "aitest")],
    ]
    return text, rows + back()


async def scr_aitest(_=""):
    s = await settings()
    lines = await test_providers(s)
    text = "<b>🧪 Tes AI</b>\n🕒 " + fmt() + "\n\n" + "\n".join(esc(line) for line in lines)
    text += "\n\n<i>✅ model bisa dipakai, ❌ menjelaskan penyebabnya. Model yang lolos otomatis dipakai.</i>"
    return text, [[B("🔄 Tes ulang", "aitest")]] + back("ai")


async def scr_aiord(_=""):
    s = await settings()
    presets = [[B(" → ".join(NAMES[p] for p in perm), "set", f"order|{','.join(perm)}")]
               for perm in permutations(NAMES)]
    toggles = [B(f"{'⛔' if p in s['disabled'] else '✅'} {NAMES[p]}", "prov", p) for p in NAMES]
    text = "<b>🔀 Urutan Provider</b>\n\nJika satu provider gagal, bot mencoba provider berikutnya."
    return text, presets + chunk(toggles) + back("ai")


async def scr_aimdl(_=""):
    s = await settings()
    btns = [
        B(f"{'✅ ' if s['model_' + p] == m else ''}{NAMES[p]} · {m.split('/')[-1][:22]}", "mdl", f"{p}|{i}")
        for p in NAMES for i, m in enumerate(MODELS[p])
    ]
    lines = "\n".join(f"• {NAMES[p]}: <code>{esc(s['model_' + p])}</code>" for p in NAMES)
    text = f"<b>🧩 Model AI</b>\n\n{lines}\n\nModel gratis OpenRouter bisa berubah sewaktu-waktu."
    return text, chunk(btns) + back("ai")


async def scr_aipers(_=""):
    s = await settings()
    btns = [B(f"{'✅ ' if s['persona'] == k else ''}{v[0]}", "set", f"persona|{k}") for k, v in PERSONAS.items()]
    desc = "\n".join(f"• <b>{v[0]}</b>: {esc(v[1])}" for v in PERSONAS.values())
    text = f"<b>🎭 Persona</b>\n\n{desc}\n\n<i>Prompt kustom (jika diisi) menggantikan persona.</i>"
    return text, chunk(btns) + back("ai")


async def scr_aisty(_=""):
    s = await settings()
    btns = [B(f"{'✅ ' if s['style'] == k else ''}{v[0]}", "set", f"style|{k}") for k, v in STYLES.items()]
    desc = "\n".join(f"• <b>{v[0]}</b>: {esc(v[1])}" for v in STYLES.values())
    return f"<b>✍️ Gaya Bahasa</b>\n\n{desc}", chunk(btns) + back("ai")


async def scr_chat(_=""):
    s = await settings()
    text = (
        "<b>💬 Mode Chat</b>\n\n"
        "🟢 = aktif. Bawaan: <b>semua menfess dibalas</b>, berapa pun panjang teksnya.\n"
        "Ketuk tombol angka untuk melihat penjelasannya."
    )
    rows = [
        [B(f"{on(s['comment_on'])} Komentar", "tog", "comment_on"),
         B(f"{on(s['reply_on'])} Reply", "tog", "reply_on")],
        [B(f"{on(s['dm_on'])} DM", "tog", "dm_on"),
         B(f"{on(s['dm_whitelist'])} DM Whitelist saja", "tog", "dm_whitelist")],
        [B("🎚 Peluang Menfess", "num", "reply_prob"), B("🎚 Peluang Reply", "num", "thread_prob")],
        [B("⏱ Jeda Min", "num", "delay_min"), B("⏱ Jeda Max", "num", "delay_max")],
        [B("🔢 Batas/Jam", "num", "max_comment_hour"), B("🔢 Balasan/Thread", "num", "max_thread")],
        [B("🔢 Balasan/User/Hari", "num", "max_user_day"), B("📩 DM/Jam", "num", "dm_hour")],
        [B("📏 Min. Panjang", "num", "min_len"), B("🕰 Umur Maks Post", "num", "max_age_min")],
        [B("🔑 Filter Keyword", "kw"), B("👥 Kelola DM", "dm")],
    ]
    return text, rows + back()


async def scr_num(key=""):
    label, lo, hi, step, presets = NUMS[key]
    s = await settings()
    text = (
        f"<b>🎚 {label}</b>\n\nNilai saat ini : <b>{s[key]}</b>\nRentang : {lo} – {hi}\n\n"
        f"<i>{esc(HELP.get(key, ''))}</i>"
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
        "Kalau diisi, menfess hanya dibalas jika memuat salah satu kata:\n"
        f"<code>{esc(words)}</code>"
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
    lines = [f"[{fmt_short(x_['ts'])}] {x_['level']}: {esc(x_['text'][:110])}" for x_ in logs]
    log_text = "\n".join(f"<code>{line}</code>" for line in lines) or "<i>Belum ada log.</i>"
    text = (
        "<b>📊 Statistik</b>\n\n"
        f"💬 Komentar menfess : <b>{st.get('comment', 0)}</b>\n"
        f"↩️ Balasan reply : <b>{st.get('reply', 0)}</b>\n"
        f"📩 Balasan DM : <b>{st.get('dm', 0)}</b>\n"
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
    "home": scr_home, "chan": scr_chan, "chanv": scr_chanv, "diag": scr_diag,
    "ai": scr_ai, "aitest": scr_aitest, "aiord": scr_aiord, "aimdl": scr_aimdl,
    "aipers": scr_aipers, "aisty": scr_aisty, "chat": scr_chat, "num": scr_num,
    "kw": scr_kw, "dm": scr_dm, "duser": scr_duser, "stats": scr_stats,
    "sys": scr_sys, "sess": scr_sess,
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
    if a == "tog":
        s = await settings()
        await setv(b, not s[b])
        to, arg = ("sys" if b in ("notify", "notify_ok") else "chat"), ""
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
