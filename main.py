# =========================================================
# SPEAKING ZONE BOT — main.py (NON-BLOCKING + FULL) — FIXED
# ✅ /start fix (SkipHandler)
# ✅ Speaking: real questions saved + evaluated (more accurate CEFR)
# ✅ Part 3: 3 questions (more realistic)
# ✅ Timer: no empty answers appended
# ✅ Writing: fallback prompts if list is empty (no crash)
# ✅ Strict prompts for Speaking/Writing evaluation
# =========================================================

from __future__ import annotations

import os
import re
import json
import time
import math
import asyncio
import random
import tempfile
from typing import Dict, List, Tuple, Optional, Any
from threading import Thread, Lock

import requests
from flask import Flask
from pydub import AudioSegment

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    FSInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


# =========================================================
# CONFIG (env shart emas — tokenlarni shu yerga qo‘yasiz)
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# ✅ PUBLIC kanal username (misol: @my_channel)
CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME", "@speakingzoneway") or "@speakingzoneway").strip()
CHANNEL_URL = (os.getenv("CHANNEL_URL", "https://t.me/speakingzoneway") or "https://t.me/speakingzoneway").strip()

# ✅ PRIVATE kanal bo‘lsa shu kerak (misol: -1001234567890)
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

PORT = int(os.getenv("PORT", "10000"))
GROQ_BASE = "https://api.groq.com/openai/v1"

GROQ_CHAT_MODELS = [
    (os.getenv("GROQ_CHAT_MODEL", "") or "").strip() or "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama3-8b-8192",
]

STATS_FILE = "stats.json"
ADMINS_FILE = "admins.json"
USERS_FILE = "users.json"
IMAGE_FOLDER = "images"

DEFAULT_ADMIN_IDS = {858726164, 1593591147}

# ✅ speaking timer each 1 second
TIMER_EDIT_EVERY = 1

# stats/users batch save
STATS_AUTOSAVE_EVERY = 10
USERS_AUTOSAVE_EVERY = 10

# ✅ subscription cache (strict)
SUB_CACHE_TTL = 5


# =========================================================
# Flask keep alive (Render)
# =========================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "OK"

@app.get("/health")
def health():
    return "healthy", 200

def run_web():
    app.run(host="0.0.0.0", port=PORT)


# =========================================================
# States (FSM)
# =========================================================
class SpeakingStates(StatesGroup):
    running = State()

class DictionaryStates(StatesGroup):
    waiting_word = State()

class WritingStates(StatesGroup):
    writing_text = State()


# =========================================================
# Keyboards
# =========================================================
def sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Obuna bo‘lish", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="🔍 Obunani tekshirish", callback_data="check_sub")],
        ]
    )

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗣 Speaking")],
            [KeyboardButton(text="📚 Dictionary")],
            [KeyboardButton(text="✍️ Writing")],
        ],
        resize_keyboard=True
    )

def back_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Orqaga")]],
        resize_keyboard=True
    )

def speaking_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏸ Pause"), KeyboardButton(text="▶️ Resume")],
            [KeyboardButton(text="⛔ Stop"), KeyboardButton(text="⬅️ Orqaga")],
        ],
        resize_keyboard=True
    )

def dictionary_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇺🇿 UZ → EN")],
            [KeyboardButton(text="🇬🇧 EN → UZ 🔊")],
            [KeyboardButton(text="⬅️ Orqaga")],
        ],
        resize_keyboard=True
    )


# =========================================================
# Bot init
# =========================================================
if not BOT_TOKEN or "PASTE_" in BOT_TOKEN:
    print("❌ ERROR: BOT_TOKEN qo‘yilmagan. main.py ichida BOT_TOKEN ni to‘ldiring.")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN and "PASTE_" not in BOT_TOKEN else None
dp = Dispatcher()


# =========================================================
# Online tracking
# =========================================================
LAST_SEEN: Dict[int, float] = {}

def touch_user(user_id: int):
    now = time.time()
    LAST_SEEN[user_id] = now
    try:
        with _users_lock:
            if user_id not in USERS_DB:
                USERS_DB[user_id] = {"first": now, "last": now}
                mark_users_dirty()
            else:
                rec = USERS_DB.get(user_id) or {}
                if not isinstance(rec, dict):
                    rec = {"first": now, "last": now}
                if not rec.get("first"):
                    rec["first"] = now
                rec["last"] = now
                USERS_DB[user_id] = rec
                mark_users_dirty()
    except Exception:
        pass

def online_users(within_seconds: int = 300) -> List[int]:
    now = time.time()
    return [uid for uid, ts in LAST_SEEN.items() if now - ts <= within_seconds]


# =========================================================
# JSON helpers + Stats/Admins/Users
# =========================================================
_stats_lock = Lock()
_users_lock = Lock()

stats_dirty = False
users_dirty = False

stats = {
    "exams_completed": {},
    "dict_lookups": {},
    "writings_completed": {}
}

USERS_DB: Dict[int, Dict[str, Any]] = {}

def load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_stats():
    global stats
    loaded = load_json(STATS_FILE, stats)
    if isinstance(loaded, dict):
        stats = loaded

def mark_stats_dirty():
    global stats_dirty
    stats_dirty = True

def inc_stat(section: str, user_id: int, amount: int = 1):
    global stats
    uid = str(user_id)
    with _stats_lock:
        if section not in stats or not isinstance(stats.get(section), dict):
            stats[section] = {}
        stats[section][uid] = int(stats[section].get(uid, 0)) + int(amount)
        mark_stats_dirty()

async def autosave_stats_job():
    global stats_dirty
    while True:
        await asyncio.sleep(STATS_AUTOSAVE_EVERY)
        try:
            with _stats_lock:
                if stats_dirty:
                    save_json(STATS_FILE, stats)
                    stats_dirty = False
        except Exception:
            pass

def load_admins() -> set:
    data = load_json(ADMINS_FILE, list(DEFAULT_ADMIN_IDS))
    try:
        return set(int(x) for x in data)
    except Exception:
        return set(DEFAULT_ADMIN_IDS)

def save_admins(admins: set):
    save_json(ADMINS_FILE, sorted(list(admins)))

ADMINS = load_admins()

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def load_users():
    global USERS_DB
    raw = load_json(USERS_FILE, {})
    db: Dict[int, Dict[str, Any]] = {}

    if isinstance(raw, list):
        for x in raw:
            try:
                uid = int(x)
                db[uid] = {"first": 0.0, "last": 0.0, "sub_ok": 0, "sub_first": 0.0, "sub_last": 0.0}
            except Exception:
                pass

    elif isinstance(raw, dict):
        for k, v in raw.items():
            try:
                uid = int(k)
                rec: Dict[str, Any] = {}
                if isinstance(v, dict):
                    rec["first"] = float(v.get("first", 0.0) or 0.0)
                    rec["last"] = float(v.get("last", 0.0) or 0.0)
                    rec["sub_ok"] = int(v.get("sub_ok", 0) or 0)
                    rec["sub_first"] = float(v.get("sub_first", 0.0) or 0.0)
                    rec["sub_last"] = float(v.get("sub_last", 0.0) or 0.0)
                else:
                    rec = {"first": 0.0, "last": 0.0, "sub_ok": 0, "sub_first": 0.0, "sub_last": 0.0}
                db[uid] = rec
            except Exception:
                pass

    USERS_DB = db

def mark_users_dirty():
    global users_dirty
    users_dirty = True

def register_user(user_id: int):
    now = time.time()
    with _users_lock:
        if user_id not in USERS_DB:
            USERS_DB[user_id] = {"first": now, "last": now, "sub_ok": 0, "sub_first": 0.0, "sub_last": 0.0}
            mark_users_dirty()
        else:
            rec = USERS_DB.get(user_id) or {}
            if not isinstance(rec, dict):
                rec = {"first": now, "last": now, "sub_ok": 0, "sub_first": 0.0, "sub_last": 0.0}
            if not rec.get("first"):
                rec["first"] = now
            rec["last"] = now
            rec.setdefault("sub_ok", 0)
            rec.setdefault("sub_first", 0.0)
            rec.setdefault("sub_last", 0.0)
            USERS_DB[user_id] = rec
            mark_users_dirty()

def mark_user_subscribed_ok(user_id: int):
    now = time.time()
    with _users_lock:
        rec = USERS_DB.get(user_id) or {"first": now, "last": now}
        if not isinstance(rec, dict):
            rec = {"first": now, "last": now}
        rec.setdefault("sub_ok", 0)
        rec.setdefault("sub_first", 0.0)
        rec.setdefault("sub_last", 0.0)

        if int(rec.get("sub_ok", 0) or 0) != 1:
            rec["sub_ok"] = 1
            rec["sub_first"] = now
        rec["sub_last"] = now

        USERS_DB[user_id] = rec
        mark_users_dirty()

async def autosave_users_job():
    global users_dirty
    while True:
        await asyncio.sleep(USERS_AUTOSAVE_EVERY)
        try:
            with _users_lock:
                if users_dirty:
                    payload = {str(uid): rec for uid, rec in USERS_DB.items()}
                    save_json(USERS_FILE, payload)
                    users_dirty = False
        except Exception:
            pass


def _count_active_users(days: int) -> int:
    now = time.time()
    limit = now - days * 86400
    with _users_lock:
        return sum(1 for _uid, rec in USERS_DB.items() if float((rec or {}).get("last", 0.0) or 0.0) >= limit)

def _total_users() -> int:
    with _users_lock:
        return len(USERS_DB)

def _count_sub_passed(days: int) -> int:
    now = time.time()
    limit = now - days * 86400
    with _users_lock:
        c = 0
        for _uid, rec in USERS_DB.items():
            if not isinstance(rec, dict):
                continue
            if int(rec.get("sub_ok", 0) or 0) != 1:
                continue
            if float(rec.get("sub_last", 0.0) or 0.0) >= limit:
                c += 1
        return c

def _total_sub_passed() -> int:
    with _users_lock:
        return sum(1 for _uid, rec in USERS_DB.items()
                   if isinstance(rec, dict) and int(rec.get("sub_ok", 0) or 0) == 1)


# =========================================================
# CEFR / IELTS mapping
# =========================================================
def clamp_20_75(x: int) -> int:
    return max(20, min(75, int(x)))

def cefr_from_score_20_75(score: int) -> str:
    s = clamp_20_75(score)
    if 20 <= s <= 27: return "A1"
    if 28 <= s <= 37: return "A2"
    if 38 <= s <= 50: return "B1"
    if 51 <= s <= 64: return "B2"
    if 65 <= s <= 73: return "C1"
    return "C2"

def ielts_from_cefr(cefr: str) -> str:
    return {
        "A1": "~1.0–2.5",
        "A2": "~3.0–3.5",
        "B1": "~4.0–5.0",
        "B2": "~5.5–6.5",
        "C1": "~7.0–8.0",
        "C2": "~8.5–9.0",
    }.get(cefr, "~3.0–3.5")


# =========================================================
# Subscription check + cache (STRICT FIX)
# =========================================================
_SUB_CACHE: Dict[int, Tuple[bool, float]] = {}

async def is_subscribed(user_id: int) -> bool:
    if not bot:
        return False

    now = time.time()
    cached = _SUB_CACHE.get(user_id)
    if cached and (now - cached[1] <= SUB_CACHE_TTL):
        return cached[0]

    chat_ref = CHANNEL_ID if CHANNEL_ID != 0 else CHANNEL_USERNAME
    try:
        member = await bot.get_chat_member(chat_ref, user_id)
        ok = member.status in ("creator", "administrator", "member")
    except Exception:
        ok = False

    _SUB_CACHE[user_id] = (ok, now)
    return ok

async def require_sub(message: Message, state: Optional[FSMContext] = None) -> bool:
    uid = message.from_user.id
    _SUB_CACHE.pop(uid, None)

    if not await is_subscribed(uid):
        if state:
            await state.clear()
        await message.answer(
            "Botdan foydalanish uchun avval kanalga obuna bo‘ling:\n"
            f"➡️ {CHANNEL_URL}\n\n"
            "Obuna bo‘lgach, «Obunani tekshirish» ni bosing.",
            reply_markup=sub_keyboard()
        )
        return False

    mark_user_subscribed_ok(uid)
    return True

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    _SUB_CACHE.pop(uid, None)
    if await is_subscribed(uid):
        mark_user_subscribed_ok(uid)
        await call.message.answer("✅ Obuna tasdiqlandi. Menu:", reply_markup=main_menu())
    else:
        await call.message.answer("❌ Hali obuna emassiz. Avval obuna bo‘ling:", reply_markup=sub_keyboard())
    await call.answer()


# =========================================================
# ✅ GLOBAL SUB GUARD — /start ni “yemaydi” (SkipHandler)
# =========================================================
@dp.message()
async def _global_subscription_guard(message: Message, state: FSMContext):
    try:
        touch_user(message.from_user.id)
        register_user(message.from_user.id)
    except Exception:
        pass

    txt = (message.text or "")

    # ✅ /start va admin buyruqlarni keyingi handlerlarga o'tkazamiz
# /start va admin buyruqlarini o‘tkazib yuboramiz
    if txt.startswith(("/start", "/admin", "/all", "/online", "/sub")):
        return

# Orqaga bosilsa ham o‘tkazamiz
    if txt == "⬅️ Orqaga":
        return
# Qolgan hamma narsa obuna talab qiladi
    if not await require_sub(message, state):
        return


# =========================================================
# Audio + Groq
# =========================================================
def convert_ogg_to_wav_sync(ogg_path: str, wav_path: str) -> None:
    audio = AudioSegment.from_file(ogg_path)
    audio.export(wav_path, format="wav")

def groq_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {GROQ_API_KEY}"}

def groq_stt_whisper_sync(wav_path: str) -> str:
    if not GROQ_API_KEY:
        return ""
    url = f"{GROQ_BASE}/audio/transcriptions"
    try:
        with open(wav_path, "rb") as f:
            files = {"file": ("audio.wav", f, "audio/wav")}
            data = {"model": "whisper-large-v3", "language": "en", "response_format": "json"}
            r = requests.post(url, headers=groq_headers(), files=files, data=data, timeout=60)
        if r.status_code != 200:
            return ""
        js = r.json()
        return (js.get("text") or "").strip()
    except Exception:
        return ""

def groq_chat_json_sync(system: str, user_json: Dict) -> Optional[Dict]:
    if not GROQ_API_KEY:
        return None

    url = f"{GROQ_BASE}/chat/completions"
    last_err = None

    for model in GROQ_CHAT_MODELS:
        if not model:
            continue

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_json, ensure_ascii=False)},
            ],
            "temperature": 0.1,
        }

        try:
            r = requests.post(
                url,
                headers={**groq_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=60
            )

            if r.status_code != 200:
                last_err = (r.status_code, r.text[:300])
                continue

            content = r.json()["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                last_err = ("NO_JSON", content[:250])
                continue

            return json.loads(m.group(0))

        except Exception as e:
            last_err = ("EXC", repr(e))
            continue

    print("GROQ CHAT FAILED:", last_err)
    return None


# =========================================================
# WRITING EVALUATION (STRICT)
# =========================================================
def _split_writing_3_tasks(text: str) -> Tuple[str, str, str]:
    t = (text or "").strip()
    if not t:
        return ("", "", "")

    t = t.replace("1.", "1)").replace("2.", "2)").replace("3.", "3)")
    m1 = re.search(r"(?:^|\n)\s*1\)\s*(.*?)(?=(?:\n\s*2\)\s)|\Z)", t, re.S)
    m2 = re.search(r"(?:^|\n)\s*2\)\s*(.*?)(?=(?:\n\s*3\)\s)|\Z)", t, re.S)
    m3 = re.search(r"(?:^|\n)\s*3\)\s*(.*)\Z", t, re.S)

    a1 = (m1.group(1).strip() if m1 else "").strip()
    a2 = (m2.group(1).strip() if m2 else "").strip()
    a3 = (m3.group(1).strip() if m3 else "").strip()

    if not any([a1, a2, a3]):
        return (t, "", "")
    return (a1, a2, a3)

def _safe_list(x, limit: int = 8) -> List[str]:
    if isinstance(x, list):
        out = []
        for it in x:
            s = str(it).strip()
            if s:
                out.append(s)
            if len(out) >= limit:
                break
        return out
    return []

async def groq_writing_eval(tasks: List[Dict[str, str]]) -> Dict[str, Any]:
    system = (
        "You are a VERY STRICT IELTS/CEFR Writing examiner and English teacher.\n"
        "Evaluate 3 tasks: (1) friend message, (2) manager email, (3) essay.\n"
        "Score must be realistic. Penalize:\n"
        "- wrong format (email structure, greeting/closing),\n"
        "- grammar errors (tense, S-V agreement, articles, prepositions, punctuation),\n"
        "- weak coherence, repetition, poor vocabulary, off-topic.\n"
        "Return ONLY valid JSON:\n"
        "{\n"
        "  \"score_20_75\": number (20..75),\n"
        "  \"feedback_uz\": string (Uzbek, practical),\n"
        "  \"overall_mistakes\": [string,...],\n"
        "  \"corrected_best_version\": string,\n"
        "  \"per_task\": [\n"
        "    {\"task_no\":1|2|3,\"strengths\":[...],\"issues\":[...],\"grammar_mistakes\":[...],\"rewrite\":string}\n"
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- If any task answer is missing/too short/off-topic, CAP score hard.\n"
        "- grammar_mistakes must name exact type.\n"
        "- rewrite must keep original meaning but be natural.\n"
    )

    data = await asyncio.to_thread(groq_chat_json_sync, system, {"tasks": tasks})
    if not data:
        joined = "\n\n".join((t.get("answer") or "").strip() for t in tasks if (t.get("answer") or "").strip())
        joined = joined.strip() or "—"
        return {
            "score_20_75": 30,
            "feedback_uz": "Writing tekshirish xizmati ishlamadi. Keyinroq urinib ko‘ring.",
            "overall_mistakes": ["Groq ishlamadi (fallback)"],
            "corrected_best_version": joined,
            "per_task": []
        }

    try:
        score = clamp_20_75(int(data.get("score_20_75", 30)))
    except Exception:
        score = 30

    return {
        "score_20_75": score,
        "feedback_uz": str(data.get("feedback_uz", "")).strip() or "—",
        "overall_mistakes": _safe_list(data.get("overall_mistakes"), 10),
        "corrected_best_version": str(data.get("corrected_best_version", "")).strip() or "—",
        "per_task": data.get("per_task") if isinstance(data.get("per_task"), list) else []
    }


# =========================================================
# Speaking content
# =========================================================
SPEAKING_PART1_POOL = [
    "👤 Tell me a bit about yourself.",
    "🎓 Do you work or are you a student?",
    "💼 What is your dream job?",
    "🎯 Please tell me about your hobbies.",
    "🏛️ Do you visit museums?",
    "🎮 Do you like computer games?",
    "👨‍👩‍👧‍👦 Can you describe your family?",
    "🏫 Can you describe your school?",
    "📅 What do you do on weekends?",
    "🌤️ What is your favourite season?",
    "📚🎬 What kind of books or movies do you like?",
    "👥 Who do you spend most of your time with?",
    "✈️ If you could visit any country, which one would you choose?",
    "🏃 How often do you exercise?",
    "😌 What’s your favorite way to relax after a busy week?",
    "🏙️ Can you describe your hometown?",
    "🏠 Do you live in a house or a flat?",
    "🍲 What is your favourite food?",
    "🧳 Do you enjoy travelling?",
    "⏳ What do you do in your free time?",
    "📸 Do you like taking photos?",
    "📖 Do you like reading books?",
    "📍 Where do you live in your country?",
    "👨‍🍳 What kind of things can you cook?",
    "🫂 Do you have a lot of friends?",
    "🎧 When do you listen to music?",
    "💪 What do you do to stay healthy?",
    "🥤 What is your favourite drink?",
    "🌙 What do you do in the evenings?",
    "🤝 Can you describe your best friend?",
    "🌍 Describe your country."
]

PART12_QUESTIONS_BY_IMAGE: Dict[int, List[str]] = {
    i: [
        "What can you see in the picture?",
        "What are the people doing (or what is happening)?",
        "Would you like to do this? Why or why not?"
    ] for i in range(1, 12)
}

PART2_CUE_BY_IMAGE: Dict[int, Dict[str, List[str]]] = {
    i: {
        "title": "Describe the situation shown in the picture.",
        "points": [
            "What the situation is",
            "Why it is important or interesting",
            "How it relates to real life (example)"
        ]
    } for i in range(12, 26)
}

PART3_TOPIC_BY_IMAGE: Dict[int, Dict[str, List[str]]] = {
    26: {"topic": "Smartphones should be banned in schools", "qs": [
        "Why do some people want to ban smartphones at school?",
        "What disadvantages could a ban create for students and parents?",
        "What rules could balance benefits and problems?"
    ]},
    27: {"topic": "Social media does more harm than good", "qs": [
        "What are the main harms of social media for teenagers?",
        "What benefits does social media provide?",
        "What rules would reduce the harm?"
    ]},
    28: {"topic": "Should laptops be allowed in classrooms?", "qs": [
        "How can laptops help students learn better?",
        "Why can laptops be distracting in class?",
        "Should schools set strict device rules? Why?"
    ]},
    29: {"topic": "Technology in education is more positive or more negative", "qs": [
        "How does technology improve learning at school?",
        "What problems can technology cause in education?",
        "How can schools use technology safely and effectively?"
    ]},
    30: {"topic": "Watching television has become a popular free time activity", "qs": [
        "Is watching TV a good way to relax? Why?",
        "How can TV be harmful, especially for children?",
        "What is better: TV or online content? Why?"
    ]},
    31: {"topic": "All countries should adopt a single global language", "qs": [
        "What are the advantages of having one global language?",
        "How could it affect culture and identity?",
        "Do you think it is realistic? Why or why not?"
    ]},
    32: {"topic": "Cars should be banned from city centers", "qs": [
        "Why do some people support banning cars in city centers?",
        "What problems could this cause for some people?",
        "What is a good compromise solution?"
    ]},
    33: {"topic": "Online education is more effective than traditional classroom education", "qs": [
        "What are the advantages of online education?",
        "What disadvantages can it have?",
        "Do you think online education will replace schools? Why?"
    ]},
    34: {"topic": "Gardening should be taught in schools", "qs": [
        "What are the benefits of teaching gardening at school?",
        "Why might some people disagree?",
        "Should schools focus more on life skills or academics? Why?"
    ]},
}

def image_path(idx: int) -> str:
    return os.path.join(IMAGE_FOLDER, f"image{idx}.jpg")

async def send_image(message: Message, idx: int, caption: str):
    path = image_path(idx)
    if os.path.exists(path):
        await message.answer_photo(FSInputFile(path), caption=caption)
    else:
        await message.answer(
            f"⚠️ Rasm topilmadi: {path}\n"
            f"✅ images/ ichida image{idx}.jpg bo‘lsin."
        )


# =========================================================
# Speaking evaluation (STRICT + real Q/A)
# =========================================================
def enforce_caps_from_relevance(score: int, avg_rel: float) -> int:
    s = clamp_20_75(score)
    if avg_rel < 2.0:
        return min(s, 37)
    if avg_rel < 3.0 and s >= 38:
        return min(s, 37)
    return s

async def evaluate_speaking_strict(questions: List[str], answers: List[str]) -> Dict:
    system = (
        "You are a VERY STRICT IELTS Speaking examiner.\n"
        "Evaluate based on: Fluency & Coherence, Lexical Resource, Grammar Range & Accuracy.\n"
        "Be strict: short/off-topic answers must be penalized.\n"
        "Return ONLY JSON with keys:\n"
        "{\n"
        "  \"score_20_75\": number (20..75),\n"
        "  \"feedback_uz\": string (Uzbek, practical),\n"
        "  \"corrected_best_version\": string (English improved),\n"
        "  \"per_question\": [\n"
        "    {\"relevance_to_question\":0..5,\"mistakes\":[string,...]}\n"
        "  ]\n"
        "}\n"
        "Mistakes must be short but specific (tense, articles, S-V agreement, word choice, cohesion, etc.).\n"
    )

    data = await asyncio.to_thread(groq_chat_json_sync, system, {
        "items": [{"question": q, "answer": a} for q, a in zip(questions, answers)]
    })

    if not data:
        joined = " ".join(a.strip() for a in answers if a and a.strip())
        score = 24 if len(joined.split()) < 12 else 35
        score = clamp_20_75(score)
        return {
            "score_20_75": score,
            "feedback_uz": "Baholash xizmati ishlamadi. Taxminiy natija.",
            "corrected_best_version": joined or "—",
            "avg_relevance": 0.0,
            "mistakes": ["Groq ishlamadi (fallback)"]
        }

    score = clamp_20_75(int(data.get("score_20_75", 20)))
    per_q = data.get("per_question") or []

    rels: List[float] = []
    mistakes: List[str] = []

    for it in per_q:
        try:
            rels.append(float(it.get("relevance_to_question", 0)))
        except Exception:
            pass
        ms = it.get("mistakes")
        if isinstance(ms, list):
            mistakes.extend([str(x) for x in ms[:3]])

    avg_rel = sum(rels) / len(rels) if rels else 0.0
    score = enforce_caps_from_relevance(score, avg_rel)

    return {
        "score_20_75": score,
        "feedback_uz": str(data.get("feedback_uz", "")).strip() or "—",
        "corrected_best_version": (str(data.get("corrected_best_version", "")).strip() or "—"),
        "avg_relevance": avg_rel,
        "mistakes": mistakes[:10]
    }


# =========================================================
# Speaking timers
# =========================================================
SPEAKING_TASKS: Dict[int, asyncio.Task] = {}

def cancel_task(user_id: int):
    t = SPEAKING_TASKS.get(user_id)
    if t and not t.done():
        t.cancel()
    SPEAKING_TASKS.pop(user_id, None)

def start_timer(message: Message, state: FSMContext, seconds: int, kind: str):
    uid = message.from_user.id
    cancel_task(uid)
    SPEAKING_TASKS[uid] = asyncio.create_task(_timer_job(message, state, seconds, kind))

async def _timer_job(message: Message, state: FSMContext, seconds: int, kind: str):
    seconds = max(0, int(seconds))
    end_ts = time.monotonic() + seconds
    await state.update_data(phase_kind=kind, phase_end=end_ts)

    label = "Tayyorlanish" if kind == "prep" else "Javob"
    timer_msg = await message.answer(f"⏳ {label}: {seconds}s")
    last_shown = None

    while True:
        if await state.get_state() != SpeakingStates.running.state:
            return

        data = await state.get_data()
        if data.get("paused"):
            return
        if data.get("stage") in ("done", "stopped"):
            return

        remain = math.ceil(end_ts - time.monotonic())
        if remain <= 0:
            break

        if last_shown != remain:
            try:
                await timer_msg.edit_text(f"⏳ {label}: {remain}s")
            except Exception:
                pass
            last_shown = remain

        await asyncio.sleep(TIMER_EDIT_EVERY)

    if await state.get_state() != SpeakingStates.running.state:
        return

    data = await state.get_data()
    if data.get("paused") or data.get("stage") in ("done", "stopped"):
        return

    await state.update_data(phase_kind=None, phase_end=None)

    if kind == "prep":
        await message.answer("🎤 Endi JAVOB bering. Voice yuboring.")
        data2 = await state.get_data()
        speak_sec = int(data2.get("current_speak_seconds") or 30)
        start_timer(message, state, speak_sec, "speak")
    else:
        # ✅ endi bo‘sh javob qo‘shmaymiz (aniqlik uchun)
        await message.answer("⏰ Vaqt tugadi. Keyingisiga o‘tdim.")
        await speaking_advance(message, state, time_up=True)


# =========================================================
# Speaking engine
# =========================================================
async def _remember_question(state: FSMContext, q: str):
    data = await state.get_data()
    asked = data.get("asked_questions") or []
    if not isinstance(asked, list):
        asked = []
    asked.append(q)
    await state.update_data(asked_questions=asked)

async def speaking_advance(message: Message, state: FSMContext, time_up: bool = False):
    data = await state.get_data()
    stage = data.get("stage", "part1")
    idx = int(data.get("idx", 0))

    if stage in ("done", "stopped"):
        return

    if stage == "part1":
        questions = data.get("questions", [])
        if not questions:
            questions = random.sample(SPEAKING_PART1_POOL, k=3)
            await state.update_data(questions=questions)

        if idx >= 3:
            await state.update_data(
                stage="part12",
                idx=0,
                questions=[],
                part12_image=random.randint(1, 11),
            )
            await message.answer("✅ Part 1 tugadi. Endi Part 1.2 (rasm) ...", reply_markup=speaking_menu())
            return await speaking_advance(message, state)

        q = questions[idx]
        await _remember_question(state, q)
        await message.answer(f"PART 1 — Savol {idx+1}/3:\n{q}", reply_markup=speaking_menu())
        await state.update_data(current_speak_seconds=30)
        start_timer(message, state, 10, "prep")
        await state.update_data(idx=idx + 1)
        return

    if stage == "part12":
        img = int(data.get("part12_image", 1))
        qs = PART12_QUESTIONS_BY_IMAGE.get(img, [
            "What can you see in the picture?",
            "What is happening?",
            "Would you like to do this? Why?"
        ])
        timings = [(15, 45), (10, 30), (10, 30)]

        if idx == 0:
            await send_image(message, img, f"🖼 PART 1.2 (image{img})")

        if idx >= 3:
            await state.update_data(stage="part2", idx=0, part2_image=random.randint(12, 25))
            await message.answer("✅ Part 1.2 tugadi. Endi Part 2 (rasm) ...", reply_markup=speaking_menu())
            return await speaking_advance(message, state)

        q = qs[idx]
        await _remember_question(state, q)
        prep, speak = timings[idx]
        await message.answer(f"PART 1.2 — Savol {idx+1}/3:\n{q}", reply_markup=speaking_menu())
        await state.update_data(current_speak_seconds=speak)
        start_timer(message, state, prep, "prep")
        await state.update_data(idx=idx + 1)
        return

    if stage == "part2":
        img = int(data.get("part2_image", 12))
        cue = PART2_CUE_BY_IMAGE.get(img, {
            "title": "Describe the situation shown in the picture.",
            "points": ["What it is", "Why it matters", "Example"]
        })

        if idx == 0:
            await send_image(message, img, f"🖼 PART 2 (image{img})")
            cue_text = "📌 CUE CARD:\n" + cue["title"] + "\n" + "\n".join(f"- {p}" for p in cue["points"])
            await message.answer(cue_text, reply_markup=speaking_menu())
            # cue card’ni ham “question” sifatida saqlab qo‘yamiz
            await _remember_question(state, "PART 2 CUE CARD: " + cue["title"])
            await state.update_data(current_speak_seconds=120)
            start_timer(message, state, 60, "prep")
            await state.update_data(idx=1)
            return

        await state.update_data(stage="part3", idx=0, part3_image=random.randint(26, 34), part3_q_idx=0)
        await message.answer("✅ Part 2 tugadi. Endi Part 3 ...", reply_markup=speaking_menu())
        return await speaking_advance(message, state)

    if stage == "part3":
        img = int(data.get("part3_image", 26))
        topic = PART3_TOPIC_BY_IMAGE.get(img, {"topic": "Discuss the topic", "qs": ["Why?"]})
        qs = topic.get("qs") or ["Why?"]
        q_idx = int(data.get("part3_q_idx", 0) or 0)

        if idx == 0:
            await send_image(message, img, f"🖼 PART 3 (image{img})")
            await message.answer(f"📌 TOPIC: {topic['topic']}", reply_markup=speaking_menu())
            await state.update_data(idx=1, part3_q_idx=0)
            # davomida savollarni beramiz
            return await speaking_advance(message, state)

        # 3 ta savol
        if q_idx < min(3, len(qs)):
            q = qs[q_idx]
            await _remember_question(state, f"PART 3: {q}")
            await message.answer(f"PART 3 — Savol {q_idx+1}/3:\n{q}", reply_markup=speaking_menu())
            await state.update_data(current_speak_seconds=120)
            start_timer(message, state, 30, "prep")
            await state.update_data(part3_q_idx=q_idx + 1)
            return

        await state.update_data(stage="done")
        return await speaking_finish(message, state)

async def speaking_finish(message: Message, state: FSMContext):
    cancel_task(message.from_user.id)

    data = await state.get_data()
    answers: List[str] = data.get("answers", []) or []
    questions: List[str] = data.get("asked_questions", []) or []

    # ✅ bo‘sh javoblarni filtr
    qa = []
    for q, a in zip(questions, answers):
        a = (a or "").strip()
        if a:
            qa.append((q, a))

    if not qa:
        await message.answer("❌ Javob topilmadi (voice kelmadi). Qayta urinib ko‘ring.", reply_markup=main_menu())
        await state.clear()
        return

    questions2 = [x[0] for x in qa][:20]
    answers2 = [x[1] for x in qa][:20]

    await message.answer("✅ Imtihon tekshirilmoqda...")
    res = await evaluate_speaking_strict(questions2, answers2)

    score = clamp_20_75(int(res.get("score_20_75", 20)))
    cefr = cefr_from_score_20_75(score)
    ielts = ielts_from_cefr(cefr)

    mistakes = res.get("mistakes") or []
    mistakes_text = "\n".join(f"- {m}" for m in mistakes[:8]) if mistakes else "—"

    await message.answer(
        "📊 Natija (Speaking):\n"
        f"🏷 CEFR: {cefr}\n"
        f"🎯 IELTS (taxminiy): {ielts}\n"
        f"⭐ Umumiy ball: {score}/75\n\n"
        f"🧠 Izoh (UZ): {res.get('feedback_uz','—')}\n\n"
        f"❗ Xatolar (qisqa):\n{mistakes_text}\n\n"
        f"✅ To‘g‘rilangan eng yaxshi variant:\n{res.get('corrected_best_version','—')}",
        reply_markup=main_menu()
    )

    inc_stat("exams_completed", message.from_user.id, 1)
    await state.clear()


# =========================================================
# /start + Speaking controls + voice
# =========================================================
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)
    await state.clear()

    if not bot:
        return await message.answer("❌ BOT_TOKEN qo‘yilmagan. main.py ichida tokenni to‘ldiring.")

    if not await require_sub(message):
        return

    await message.answer(
        "👋 Xush salom kelibsiz!\n\n"
        "🗣 Speaking — CEFR / IELTS simulyatsiya\n"
        "📚 Dictionary — so‘z izlash\n"
        "✍️ Writing — yozma baholash",
        reply_markup=main_menu()
    )


@dp.message(Command("sub"))
async def cmd_sub(message: Message):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if message.from_user.id not in ADMINS:
        return await message.answer("⛔ Siz admin emassiz.")

    total = _total_sub_passed()
    today = _count_sub_passed(1)
    last7 = _count_sub_passed(7)
    month = _count_sub_passed(30)

    await message.answer(
        "📌 OBUNA STATISTIKASI (BOT orqali)\n\n"
        f"✅ Jami obuna bo‘lib o‘tganlar: {total}\n"
        f"📅 Bugun: {today}\n"
        f"🗓 Oxirgi 7 kun: {last7}\n"
        f"📆 Oxirgi 30 kun: {month}\n\n"
        "ℹ️ Bu botdan o‘tgan (subscribe check’dan o‘tgan) userlar soni."
    )


@dp.message(Command("all"))
async def cmd_all(message: Message):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if message.from_user.id not in ADMINS:
        return await message.answer("⛔ Siz admin emassiz.")

    text = (message.text or "").strip()
    parts = text.split()
    arg = parts[1].lower() if len(parts) > 1 else ""

    total = _total_users()
    today = _count_active_users(1)
    last7 = _count_active_users(7)
    month = _count_active_users(30)

    if arg in ("today", "bugun"):
        return await message.answer(f"📅 Bugun aktiv bo‘lganlar: {today}")
    if arg in ("last7days", "7days", "week", "hafta"):
        return await message.answer(f"🗓 Oxirgi 7 kun aktiv bo‘lganlar: {last7}")
    if arg in ("month", "30days", "oy"):
        return await message.answer(f"📆 Oxirgi 30 kun aktiv bo‘lganlar: {month}")

    await message.answer(
        "📊 FOYDALANUVCHI STATISTIKASI\n\n"
        f"👥 Jami (hammasi): {total}\n"
        f"📅 Bugun aktiv: {today}\n"
        f"🗓 Oxirgi 7 kun aktiv: {last7}\n"
        f"📆 Oxirgi 30 kun aktiv: {month}\n\n"
        "ℹ️ Buyruqlar:\n"
        "/all today\n"
        "/all last7days\n"
        "/all month"
    )


@dp.message(F.text == "🗣 Speaking")
async def speaking_start(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if not await require_sub(message, state):
        return

    cancel_task(message.from_user.id)

    await state.set_state(SpeakingStates.running)
    await state.update_data(
        stage="part1",
        idx=0,
        answers=[],
        asked_questions=[],   # ✅ REAL Qs saved
        questions=[],
        paused=False,
        part12_image=None,
        part2_image=None,
        part3_image=None,
        part3_q_idx=0,
        phase_kind=None,
        phase_end=None,
        current_speak_seconds=30,
    )

    await message.answer(
        "🗣 Speaking boshlandi.\n"
        "⏱ Taymer ishlaydi.\n"
        "🎤 Faqat voice yuboring.",
        reply_markup=speaking_menu()
    )
    await speaking_advance(message, state)


@dp.message(SpeakingStates.running, F.text == "⏸ Pause")
async def speaking_pause(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    cancel_task(message.from_user.id)
    await state.update_data(paused=True)
    await message.answer("⏸ Pauza qilindi. ▶️ Resume bosing.")

@dp.message(SpeakingStates.running, F.text == "▶️ Resume")
async def speaking_resume(message: Message, state: FSMContext):
    touch_user(message.from_user.id)

    data = await state.get_data()
    if not data.get("paused"):
        return await message.answer("▶️ Allaqachon davom etyapti.")

    await state.update_data(paused=False)
    await message.answer("▶️ Davom ettiramiz...")

    kind = data.get("phase_kind")
    end_ts = data.get("phase_end")

    if kind and end_ts:
        remain = math.ceil(float(end_ts) - time.monotonic())
        remain = max(1, remain)
        start_timer(message, state, remain, kind)
    else:
        await speaking_advance(message, state)

@dp.message(SpeakingStates.running, F.text == "⛔ Stop")
async def speaking_stop(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    cancel_task(message.from_user.id)
    await state.update_data(stage="stopped", paused=True)
    await message.answer("⛔ To‘xtatildi. Hozirgi javoblar bo‘yicha baholayman...")
    await speaking_finish(message, state)

@dp.message(SpeakingStates.running, F.text == "⬅️ Orqaga")
async def speaking_back(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    cancel_task(message.from_user.id)
    await state.clear()
    await message.answer("🔙 Menu", reply_markup=main_menu())

@dp.message(SpeakingStates.running, F.voice)
async def speaking_voice_handler(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    data = await state.get_data()
    if data.get("paused"):
        await message.answer("⏸ Pauza. ▶️ Resume bosing.")
        return

    if not bot:
        await message.answer("❌ BOT_TOKEN yo‘q.")
        return

    voice = message.voice
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    os.close(ogg_fd)
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)

    try:
        await bot.download(voice.file_id, destination=ogg_path)
        await asyncio.to_thread(convert_ogg_to_wav_sync, ogg_path, wav_path)

        await message.answer("🎧 Ovoz matnga aylantirilmoqda...")
        transcript = await asyncio.to_thread(groq_stt_whisper_sync, wav_path)

        if not transcript:
            await message.answer("❌ Ovoz tushunilmadi.")
            return

        answers = (await state.get_data()).get("answers", [])
        if not isinstance(answers, list):
            answers = []
        answers.append(transcript)
        await state.update_data(answers=answers)

        await message.answer(f"📝 {transcript}")

        cancel_task(message.from_user.id)
        await speaking_advance(message, state)

    finally:
        for p in (ogg_path, wav_path):
            try:
                os.remove(p)
            except Exception:
                pass


# =========================================================
# Dictionary
# =========================================================
def is_uzbek_text(s: str) -> bool:
    if re.search(r"[\u0400-\u04FF]", s):
        return True
    if any(ch in s for ch in ["ʻ", "’", "‘", "ʼ", "ğ", "ö", "ü", "қ", "ғ", "ҳ", "ў", "й"]):
        return True
    if re.search(r"\b(yo'q|bo'lsa|qanday|nega|qayer|bugun|kecha|ertaga)\b", s.lower()):
        return True
    if re.search(r"[^\x00-\x7F]", s):
        return True
    return False

def translate_uz_to_en_sync(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "uz", "tl": "en", "dt": "t", "q": text}
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return "".join([chunk[0] for chunk in data[0] if chunk and chunk[0]]).strip()
    except Exception:
        pass
    return ""

def translate_en_to_uz_sync(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "en", "tl": "uz", "dt": "t", "q": text}
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return "".join([chunk[0] for chunk in data[0] if chunk and chunk[0]]).strip()
    except Exception:
        pass
    return ""

def dict_lookup_en_sync(word: str) -> Tuple[str, str, Optional[str]]:
    try:
        r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=15)
        if r.status_code != 200:
            return ("—", "—", None)

        data = r.json()[0]
        ipa = "—"
        audio_url = None

        for ph in data.get("phonetics", []):
            if ph.get("text") and ipa == "—":
                ipa = ph["text"]
            if ph.get("audio") and not audio_url:
                audio_url = ph["audio"]

        definition = "—"
        meanings = data.get("meanings", [])
        if meanings and meanings[0].get("definitions"):
            definition = meanings[0]["definitions"][0].get("definition", "—")

        return (ipa, definition, audio_url)
    except Exception:
        return ("—", "—", None)

def download_to_temp_sync(url: str, suffix: str) -> Optional[str]:
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200 or not r.content:
            return None
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception:
        return None

def google_tts_url(text: str, lang: str) -> str:
    return (
        "https://translate.google.com/translate_tts"
        f"?ie=UTF-8&q={requests.utils.quote(text)}&tl={lang}&client=tw-ob"
    )

@dp.message(F.text == "📚 Dictionary")
async def dict_start(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if not await require_sub(message, state):
        return

    await state.clear()
    await message.answer("📚 Dictionary bo‘limini tanlang:", reply_markup=dictionary_menu())

@dp.message(F.text == "🇺🇿 UZ → EN")
async def dict_mode_uz_en(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if not await require_sub(message, state):
        return

    await state.set_state(DictionaryStates.waiting_word)
    await state.update_data(dict_mode="uz_en")
    await message.answer("🇺🇿 Uzbekcha so‘z kiriting:", reply_markup=back_menu())

@dp.message(F.text == "🇬🇧 EN → UZ 🔊")
async def dict_mode_en_uz(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if not await require_sub(message, state):
        return

    await state.set_state(DictionaryStates.waiting_word)
    await state.update_data(dict_mode="en_uz")
    await message.answer("🇬🇧 English so‘z kiriting:", reply_markup=back_menu())

@dp.message(DictionaryStates.waiting_word)
async def dict_handler(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if message.text == "⬅️ Orqaga":
        await state.clear()
        await message.answer("🔙 Menu", reply_markup=main_menu())
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ So‘z kiriting")
        return

    data = await state.get_data()
    mode = (data.get("dict_mode") or "").strip()
    if not mode:
        mode = "uz_en" if is_uzbek_text(raw) else "en_uz"

    if mode == "uz_en":
        await message.answer("⏳ UZ → EN tarjima qilinyapti...")
        en = await asyncio.to_thread(translate_uz_to_en_sync, raw)
        if not en:
            await message.answer("❌ Tarjima topilmadi.")
            return

        token = en.split()[0]
        word = re.sub(r"[^a-zA-Z'\-]", "", token).lower()
        ipa, definition, audio = ("—", "—", None)

        if word:
            ipa, definition, audio = await asyncio.to_thread(dict_lookup_en_sync, word)

        await message.answer(
            f"🇺🇿 UZ: {raw}\n"
            f"🇬🇧 EN: {en}\n\n"
            f"🔊 IPA: {ipa}\n"
            f"📘 Meaning: {definition}"
        )

        audio_sent = False
        if audio:
            if audio.startswith("//"):
                audio = "https:" + audio
            path = await asyncio.to_thread(download_to_temp_sync, audio, ".mp3")
            if path:
                await message.answer_voice(FSInputFile(path), caption="🔊 English pronunciation")
                try:
                    os.remove(path)
                except Exception:
                    pass
                audio_sent = True

        if (not audio_sent) and word:
            tts = google_tts_url(word, "en")
            path = await asyncio.to_thread(download_to_temp_sync, tts, ".mp3")
            if path:
                await message.answer_voice(FSInputFile(path), caption="🔊 English (Google TTS)")
                try:
                    os.remove(path)
                except Exception:
                    pass

        inc_stat("dict_lookups", message.from_user.id, 1)
        return

    if mode == "en_uz":
        await message.answer("⏳ EN → UZ tarjima qilinyapti...")
        uz = await asyncio.to_thread(translate_en_to_uz_sync, raw)
        if not uz:
            await message.answer("❌ Tarjima topilmadi.")
            return

        await message.answer(
            f"🇬🇧 EN: {raw}\n"
            f"🇺🇿 UZ: {uz}\n\n"
            "🔊 O‘qib berilyapti (Google Translate)..."
        )

        tts = google_tts_url(raw, "en")
        path = await asyncio.to_thread(download_to_temp_sync, tts, ".mp3")
        if path:
            await message.answer_voice(FSInputFile(path), caption="🔊 English (Google TTS)")
            try:
                os.remove(path)
            except Exception:
                pass

        inc_stat("dict_lookups", message.from_user.id, 1)
        return

    await message.answer("❌ Dictionary mode xato. Menu → Dictionary dan qayta tanlang.")
    await state.clear()


# =========================================================
# Writing prompts (fallback bor)
# =========================================================
WRITING_PROMPTS = {
    "friend": [],
    "manager": [],
    "essay": []
}

@dp.message(F.text == "✍️ Writing")
async def writing_start(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if not await require_sub(message, state):
        return

    # ✅ fallback (list bo‘sh bo‘lsa crash bo‘lmasin)
    if not WRITING_PROMPTS["friend"] or not WRITING_PROMPTS["manager"] or not WRITING_PROMPTS["essay"]:
        WRITING_PROMPTS["friend"] = ["Write a message to your friend. Ask them to remind you about an important date."]
        WRITING_PROMPTS["manager"] = ["Write an email to your manager. Explain that you lost access to a client account and need reset."]
        WRITING_PROMPTS["essay"] = ["Essay: Choosing a career: passion vs salary. Discuss."]

    p1 = random.choice(WRITING_PROMPTS["friend"])
    p2 = random.choice(WRITING_PROMPTS["manager"])
    p3 = random.choice(WRITING_PROMPTS["essay"])

    await state.set_state(WritingStates.writing_text)
    await state.update_data(prompts=[p1, p2, p3])

    await message.answer(
        "✍️ Writing\n\n"
        f"1) {p1}\n\n"
        f"2) {p2}\n\n"
        f"3) {p3}\n\n"
        "✅ Hammasini BIR xabarda yuboring.\n"
        "Format:\n1) ...\n2) ...\n3) ...",
        reply_markup=back_menu()
    )

@dp.message(WritingStates.writing_text)
async def writing_handler(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    register_user(message.from_user.id)

    if message.text == "⬅️ Orqaga":
        await state.clear()
        await message.answer("🔙 Menu", reply_markup=main_menu())
        return

    user_text = (message.text or "").strip()
    if not user_text:
        await message.answer("❌ Matn yozing. Format:\n1) ...\n2) ...\n3) ...")
        return

    data = await state.get_data()
    prompts = data.get("prompts") or ["", "", ""]
    if not isinstance(prompts, list) or len(prompts) < 3:
        prompts = ["", "", ""]

    a1, a2, a3 = _split_writing_3_tasks(user_text)

    tasks = [
        {"prompt": str(prompts[0] or ""), "answer": a1},
        {"prompt": str(prompts[1] or ""), "answer": a2},
        {"prompt": str(prompts[2] or ""), "answer": a3},
    ]

    await message.answer("✅ Writing tekshirilmoqda (strict grammar + xatolar + rewrite)...")
    res = await groq_writing_eval(tasks)

    score = clamp_20_75(int(res.get("score_20_75", 30)))
    cefr = cefr_from_score_20_75(score)
    ielts = ielts_from_cefr(cefr)

    overall_mistakes = res.get("overall_mistakes") or []
    overall_txt = "\n".join(f"- {m}" for m in overall_mistakes[:10]) if overall_mistakes else "—"

    per_task = res.get("per_task") if isinstance(res.get("per_task"), list) else []

    per_txt_lines = []
    for t in per_task[:3]:
        try:
            no = int(t.get("task_no", 0))
        except Exception:
            no = 0

        strengths = _safe_list(t.get("strengths"), 3)
        issues = _safe_list(t.get("issues"), 3)
        grammar = _safe_list(t.get("grammar_mistakes"), 4)
        rewrite = str(t.get("rewrite", "")).strip()

        per_txt_lines.append(f"🧩 Task {no}:" if no else "🧩 Task:")
        if strengths:
            per_txt_lines.append("✅ Kuchli tomonlar: " + "; ".join(strengths))
        if issues:
            per_txt_lines.append("⚠️ Kamchiliklar: " + "; ".join(issues))
        if grammar:
            per_txt_lines.append("❗ Grammar xatolar: " + "; ".join(grammar))
        if rewrite:
            per_txt_lines.append("✍️ Rewrite:\n" + rewrite)
        per_txt_lines.append("")

    per_txt = "\n".join(per_txt_lines).strip() or "—"
    corrected = str(res.get("corrected_best_version", "")).strip() or "—"
    feedback_uz = str(res.get("feedback_uz", "")).strip() or "—"

    await message.answer(
        "📊 Writing natija\n"
        f"🏷 CEFR: {cefr}\n"
        f"🎯 IELTS (taxminiy): {ielts}\n"
        f"⭐ Ball: {score}/75\n\n"
        f"🧠 Izoh (UZ): {feedback_uz}\n\n"
        f"❗ Umumiy xatolar:\n{overall_txt}\n\n"
        f"{per_txt}\n"
        f"✅ To‘g‘rilangan eng yaxshi variant:\n{corrected}",
        reply_markup=main_menu()
    )

    inc_stat("writings_completed", message.from_user.id, 1)
    await state.clear()


# =========================================================
# Admin commands
# =========================================================
async def user_label(user_id: int) -> str:
    if not bot:
        return str(user_id)
    try:
        chat = await bot.get_chat(user_id)
        uname = getattr(chat, "username", None)
        first = getattr(chat, "first_name", None) or getattr(chat, "title", None) or "User"
        if uname:
            return f"@{uname} ({user_id})"
        return f"{first} ({user_id})"
    except Exception:
        return str(user_id)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    touch_user(message.from_user.id)

    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Siz admin emassiz.")

    with _stats_lock:
        exams = dict(stats.get("exams_completed", {}))
        dicts = dict(stats.get("dict_lookups", {}))
        writes = dict(stats.get("writings_completed", {}))

    total_speaking = sum(int(v) for v in exams.values())
    total_dict = sum(int(v) for v in dicts.values())
    total_writing = sum(int(v) for v in writes.values())

    users = set(exams) | set(dicts) | set(writes)
    online_count = len(online_users(300))

    text = (
        "👑 ADMIN STATS\n\n"
        f"👥 Unique users: {len(users)}\n"
        f"🟢 Online (5 min): {online_count}\n\n"
        f"🗣 Speaking total: {total_speaking}\n"
        f"📚 Dictionary total: {total_dict}\n"
        f"✍️ Writing total: {total_writing}\n"
    )

    def user_total(uid_str: str) -> int:
        return int(exams.get(uid_str, 0)) + int(dicts.get(uid_str, 0)) + int(writes.get(uid_str, 0))

    top = sorted(list(users), key=user_total, reverse=True)[:10]
    if top:
        text += "\n🏆 TOP 10 (eng aktiv):\n"
        for i, uid_str in enumerate(top, 1):
            uid = int(uid_str)
            label = await user_label(uid)
            text += (
                f"{i}) {label}\n"
                f"   🗣 {int(exams.get(uid_str,0))} | 📚 {int(dicts.get(uid_str,0))} | ✍️ {int(writes.get(uid_str,0))}\n"
            )

    await message.answer(text)

@dp.message(Command("online"))
async def cmd_online(message: Message):
    touch_user(message.from_user.id)

    if message.from_user.id not in ADMINS:
        return await message.answer("⛔ Admin emas")

    on = online_users()
    if not on:
        return await message.answer("🟢 Online user yo‘q")

    lines = ["🟢 ONLINE:\n"]
    for uid in on:
        lines.append("👤 " + await user_label(uid))

    await message.answer("\n".join(lines))


# =========================================================
# RUN
# =========================================================
async def main():
    if not bot:
        print("❌ BOT_TOKEN yo‘q yoki PASTE_ holatda. Tokenni qo‘yib qayta ishga tushiring.")
        return

    load_stats()
    load_users()

    Thread(target=run_web, daemon=True).start()

    asyncio.create_task(autosave_stats_job())
    asyncio.create_task(autosave_users_job())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
