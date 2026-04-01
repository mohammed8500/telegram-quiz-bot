import os
import json
import random
import logging
import re
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Set
from contextlib import contextmanager

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("telegram-quiz-bot")

# =========================
# Configuration
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")

# الآيدي الخاص بك كأدمن أساسي
ADMIN_IDS = {290185541}

_admin_single = os.getenv("ADMIN_USER_ID", "").strip()
if _admin_single.isdigit():
    ADMIN_IDS.add(int(_admin_single))

_admin_raw = os.getenv("ADMIN_IDS", "").strip()
if _admin_raw:
    for x in _admin_raw.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "0").strip()
MAINTENANCE_ON = MAINTENANCE_MODE in ("1", "true", "True", "YES", "yes", "on", "ON")

BAD_WORDS = set(w.strip() for w in os.getenv("BAD_WORDS", "").split(",") if w.strip())

TERM1_FILE = os.getenv("TERM1_FILE", "questions_from_word.json").strip()
TERM2_FILE = os.getenv("TERM2_FILE", "questions_term2.json").strip()
DB_FILE = os.getenv("DB_FILE", "data.db").strip()

# =========================
# Game Settings
# =========================
ROUND_SIZE = 20
STREAK_BONUS_EVERY = 3
TOP_N = 10
MAX_ROUND_DURATION = 600
CLEANUP_INTERVAL = 3600

CHAPTERS = [
    "طبيعة العلم",
    "المخاليط والمحاليل",
    "حالات المادة",
    "الطاقة وتحولاتها",
    "أجهزة الجسم",
    "النباتات"
]

# =========================
# Database
# =========================
class DatabaseManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_pool()
        return cls._instance
    
    def _init_pool(self):
        self.conn = sqlite3.connect(
            DB_FILE,
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
    
    def _init_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                is_approved INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                total_points INTEGER DEFAULT 0,
                rounds_played INTEGER DEFAULT 0,
                best_round_score INTEGER DEFAULT 0,
                last_active TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_names (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                requested_at TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS seen_questions (
                user_id INTEGER,
                qid TEXT,
                seen_at TEXT,
                PRIMARY KEY (user_id, qid)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                round_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                started_at TEXT,
                finished_at TEXT,
                score INTEGER DEFAULT 0,
                bonus INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                status TEXT DEFAULT 'completed'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS active_rounds (
                user_id INTEGER PRIMARY KEY,
                data TEXT,
                started_at TEXT,
                last_activity TEXT
            )
        """)
        self.conn.commit()
    
    @contextmanager
    def get_cursor(self):
        cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise e
        finally:
            cursor.close()

db_manager = DatabaseManager()

# =========================
# Robust Send
# =========================
SEND_RETRIES = 3
SEND_TIMEOUT = 10

async def safe_send(bot, chat_id: int, text: str, **kwargs):
    # تفريغ تنسيق Markdown لتجنب أخطاء التلغرام
    kwargs.pop("parse_mode", None)
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return await asyncio.wait_for(
                bot.send_message(chat_id=chat_id, text=text, **kwargs),
                timeout=SEND_TIMEOUT
            )
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, 'retry_after', 1))
        except Exception as e:
            if attempt < SEND_RETRIES:
                await asyncio.sleep(1)
            else:
                logger.error(f"Failed to send message to {chat_id}: {e}")
                return None
    return None

async def safe_answer_callback(query, text: str = None, show_alert: bool = False):
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

# =========================
# Utils
# =========================
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")

def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = _ARABIC_DIACRITICS.sub("", text.strip())
    text = re.sub(r"[^\u0600-\u06FF0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه").lower()

def is_arabic_only_name(name: str) -> bool:
    if not name:
        return False
    if re.search(r"[A-Za-z]", name.strip()):
        return False
    return bool(re.fullmatch(r"[\u0600-\u06FF\s]+", name.strip()))

def looks_like_real_name(name: str) -> bool:
    name = name.strip()
    if not is_arabic_only_name(name):
        return False
    parts = [p for p in name.split() if p]
    if len(parts) < 2 or len(name) < 6 or len(name) > 30:
        return False
    n_norm = normalize_arabic(name)
    for bw in BAD_WORDS:
        if normalize_arabic(bw) in n_norm:
            return False
    return True

async def maintenance_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not MAINTENANCE_ON:
        return False
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id in ADMIN_IDS:
        return False
    msg = "🛠️ البوت تحت صيانة حالياً… ارجعوا بعدين 🌿"
    try:
        if update.message:
            await safe_send(context.bot, update.message.chat_id, msg, reply_markup=ReplyKeyboardRemove())
        elif update.callback_query:
            await safe_answer_callback(update.callback_query, "صيانة", show_alert=True)
    except Exception:
        pass
    return True

# =========================
# DB Actions
# =========================
def upsert_user(user_id: int):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if cur.fetchone():
            cur.execute("UPDATE users SET updated_at=?, last_active=? WHERE user_id=?", (now, now, user_id))
        else:
            cur.execute("INSERT INTO users(user_id, created_at, updated_at, last_active) VALUES (?,?,?,?)", (user_id, now, now, now))

def set_pending_name(user_id: int, full_name: str):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("""
            INSERT INTO pending_names(user_id, full_name, requested_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, status='pending'
        """, (user_id, full_name, now))

def approve_name(user_id: int):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT full_name FROM pending_names WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE users SET full_name=?, is_approved=1, updated_at=? WHERE user_id=?", (row["full_name"], now, user_id))
            cur.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))

def reject_name(user_id: int):
    with db_manager.get_cursor() as cur:
        cur.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))

def get_user(user_id: int) -> Dict:
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else {}

def get_pending_list() -> List[Dict]:
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT * FROM pending_names WHERE status='pending'")
        return [dict(row) for row in cur.fetchall()]

def mark_seen(user_id: int, qid: str):
    with db_manager.get_cursor() as cur:
        cur.execute("INSERT OR IGNORE INTO seen_questions(user_id, qid, seen_at) VALUES(?,?,?)", (user_id, qid, datetime.utcnow().isoformat()))

def has_seen(user_id: int, qid: str) -> bool:
    if not qid: return False
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT 1 FROM seen_questions WHERE user_id=? AND qid=? LIMIT 1", (user_id, qid))
        return cur.fetchone() is not None

def save_round_result(user_id: int, score: int, bonus: int, correct: int, total: int, status: str = "completed"):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("INSERT INTO rounds(user_id, started_at, finished_at, score, bonus, correct, total, status) VALUES(?,?,?,?,?,?,?,?)",
                    (user_id, now, now, score, bonus, correct, total, status))
        cur.execute("SELECT total_points, rounds_played, best_round_score FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE users SET total_points=?, rounds_played=?, best_round_score=?, updated_at=? WHERE user_id=?",
                        (row["total_points"] + score + bonus, row["rounds_played"] + 1, max(row["best_round_score"], score + bonus), now, user_id))

def save_active_round(user_id: int, round_data: Dict):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("""
            INSERT INTO active_rounds(user_id, data, started_at, last_activity) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, last_activity=excluded.last_activity
        """, (user_id, json.dumps(round_data, ensure_ascii=False), now, now))

def load_active_round(user_id: int) -> Optional[Dict]:
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT data FROM active_rounds WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return json.loads(row["data"]) if row else None

def delete_active_round(user_id: int):
    with db_manager.get_cursor() as cur:
        cur.execute("DELETE FROM active_rounds WHERE user_id=?", (user_id,))

def get_leaderboard(top_n: int) -> List[Dict]:
    with db_manager.get_cursor() as cur:
        cur.execute("""
            SELECT full_name, total_points, best_round_score, rounds_played FROM users
            WHERE is_approved=1 AND full_name IS NOT NULL AND TRIM(full_name) <> ''
            ORDER BY total_points DESC, best_round_score DESC, rounds_played DESC LIMIT ?
        """, (top_n,))
        return [dict(row) for row in cur.fetchall()]

# =========================
# Questions
# =========================
class QuestionManager:
    def __init__(self, filename):
        self.filename = filename
        self.items = []
        self.buckets = {}
        self.term_pool = []
        self.last_loaded = None
        self.load_questions()
    
    def load_questions(self):
        try:
            if not os.path.exists(self.filename): return
            mtime = os.path.getmtime(self.filename)
            if self.last_loaded and mtime <= self.last_loaded: return
            with open(self.filename, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data if isinstance(data, list) else (data.get("items", []) or data.get("questions", []))
            for i, item in enumerate(items):
                if "id" not in item: item["id"] = f"q_{self.filename}_{i}_{hash(str(item))}"
            self.items = items
            self.buckets = {c: [] for c in CHAPTERS}
            self.term_pool = [i.get("term").strip() for i in items if i.get("type") == "term" and i.get("term")]
            for item in items:
                chap = self.classify_chapter(item)
                item["_chapter"] = chap
                self.buckets[chap].append(item)
            self.last_loaded = mtime
        except Exception:
            self.items, self.buckets, self.term_pool = [], {}, []
    
    def classify_chapter(self, item: Dict) -> str:
        kw_map = {
            "طبيعة العلم": ["علميه", "فرضيه", "متغير", "ملاحظه", "تجربه", "علم الاثار", "رادار"],
            "المخاليط والمحاليل": ["مخلوط", "محلول", "مذيب", "تركيز", "حمض", "قاعده", "تساهمي"],
            "حالات المادة": ["صلب", "سائل", "غاز", "بلازما", "انصهار", "ضغط", "كثافه"],
            "الطاقة وتحولاتها": ["طاقه", "حركيه", "اشعاعيه", "كيميائيه", "كهربائيه", "توربين"],
            "أجهزة الجسم": ["دم", "قلب", "وريد", "مناعه", "سكري", "معده", "عظام", "جلد"],
            "النباتات": ["لحاء", "خشب", "بذور", "ثغور", "مخاريط", "سرخسيات"]
        }
        blob = normalize_arabic(str(item))
        best_chapter, best_score = "حالات المادة", 0
        for chap, kws in kw_map.items():
            score = sum(1 for kw in kws if normalize_arabic(kw) in blob)
            if score > best_score:
                best_score, best_chapter = score, chap
        return best_chapter

    def pick_round_questions(self, user_id: int) -> List[Dict]:
        self.load_questions()
        if not self.buckets: return []
        chosen, seen_ids = [], set()
        for chapter in CHAPTERS:
            unseen = [q for q in self.buckets.get(chapter, []) if not has_seen(user_id, q.get("id"))]
            random.shuffle(unseen)
            for q in unseen[:ROUND_SIZE // len(CHAPTERS)]:
                if q["id"] not in seen_ids:
                    chosen.append(q)
                    seen_ids.add(q["id"])
        if len(chosen) < ROUND_SIZE:
            all_qs = [q for c in CHAPTERS for q in self.buckets.get(c, [])]
            random.shuffle(all_qs)
            for q in all_qs:
                if q["id"] not in seen_ids:
                    chosen.append(q)
                    seen_ids.add(q["id"])
                    if len(chosen) >= ROUND_SIZE: break
        chosen = chosen[:ROUND_SIZE]
        random.shuffle(chosen)
        return chosen
    
    def convert_term_to_mcq(self, q: Dict) -> Dict:
        correct = q.get("term", "").strip()
        wrongs = random.sample([t for t in self.term_pool if t != correct] or ["A", "B", "C"], min(3, max(1, len(self.term_pool)-1)))
        opts = [correct] + wrongs
        random.shuffle(opts)
        options = {["A", "B", "C", "D"][i]: opt for i, opt in enumerate(opts)}
        res = q.copy()
        res.update({"type": "mcq", "question": f"ما المصطلح للتعريف:\n{q.get('definition', '')}", "options": options, "correct": [k for k, v in options.items() if v == correct][0]})
        return res

qm_term1 = QuestionManager(TERM1_FILE)
qm_term2 = QuestionManager(TERM2_FILE)

# =========================
# Keyboards
# =========================
def main_menu_keyboard(user: Dict) -> InlineKeyboardMarkup:
    name_btn = f"✅ {user.get('full_name')[:15]}" if user.get("is_approved") else ("⏳ بانتظار الموافقة" if user.get("full_name") else "➕ سجّل اسمك")
    kb = [
        [InlineKeyboardButton("🎮 ابدأ جولة (20 سؤال)", callback_data="play_round")],
        [InlineKeyboardButton("🏆 لوحة التميز (Top 10)", callback_data="leaderboard")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats")],
        [InlineKeyboardButton(name_btn, callback_data="set_name")],
        [InlineKeyboardButton("💬 تواصل مع المشرف", callback_data="contact_admin")]
    ]
    if user.get("user_id") and load_active_round(user["user_id"]):
        kb.insert(0, [InlineKeyboardButton("🔄 استعادة الجولة النشطة", callback_data="resume_round")])
    return InlineKeyboardMarkup(kb)

def term_selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 الفصل الدراسي الأول", callback_data="start_term1")],
        [InlineKeyboardButton("📘 الفصل الدراسي الثاني", callback_data="start_term2")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
    ])

def answer_keyboard_mcq(opts: Dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{k}) {v[:37]}", callback_data=f"ans_mcq:{k}")] for k, v in opts.items()] + [[InlineKeyboardButton("⛔️ إنهاء الجولة", callback_data="end_round")]])

def answer_keyboard_tf() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ صح", callback_data="ans_tf:true"), InlineKeyboardButton("❌ خطأ", callback_data="ans_tf:false")], [InlineKeyboardButton("⛔️ إنهاء الجولة", callback_data="end_round")]])

def admin_pending_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ موافق", callback_data=f"admin_approve:{uid}"), InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject:{uid}")]])

def parse_tf_answer(raw: Any) -> bool:
    s = normalize_arabic(str(raw))
    return s in ("true", "1", "صح", "صحيح", "ص")

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if await maintenance_block(update, context): return
        user_id = update.effective_user.id
        upsert_user(user_id)
        msg = "هلا 👋\nأنا بوت المسابقة 🎯\n• كل جولة = 20 سؤال\n• بونص: كل 3 إجابات صح متتالية = +1\nاختر من القائمة 👇"
        await safe_send(context.bot, update.message.chat_id, msg, reply_markup=main_menu_keyboard(get_user(user_id)))
    except Exception as e:
        logger.error(f"Start error: {e}")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if await maintenance_block(update, context): return
        query = update.callback_query
        await safe_answer_callback(query)
        user_id, chat_id, data = query.from_user.id, query.message.chat_id, query.data
        upsert_user(user_id)
        user = get_user(user_id)
        
        if data == "set_name":
            context.user_data.update({"awaiting_name": True, "awaiting_contact": False})
            await safe_send(context.bot, chat_id, "اكتب اسمك الحقيقي (عربي فقط) مثل: محمد أحمد\nشروطنا: عربي, كلمتين, محترم\n✍️ اكتب الاسم الآن:")
        elif data == "contact_admin":
            context.user_data.update({"awaiting_contact": True, "awaiting_name": False})
            await safe_send(context.bot, chat_id, "📝 اكتب رسالتك أو استفسارك الآن، وراح توصل للمشرف مباشرة:")
        elif data == "resume_round":
            ar = load_active_round(user_id)
            if ar:
                context.user_data.update(ar)
                await safe_send(context.bot, chat_id, "🔄 استعادة الجولة النشطة!")
                await send_next_question(chat_id, user_id, context)
            else:
                await safe_send(context.bot, chat_id, "❌ لا توجد جولة.")
        elif data == "leaderboard":
            lb = get_leaderboard(TOP_N)
            text = "🏆 لوحة التميز (Top 10)\n\n" + "\n".join(f"{i}) {r['full_name']} — ⭐️ {r['total_points']} نقطة" for i, r in enumerate(lb, 1)) if lb else "لوحة التميز فاضية للحين!"
            await safe_send(context.bot, chat_id, text)
            await safe_send(context.bot, chat_id, "القائمة:", reply_markup=main_menu_keyboard(user))
        elif data == "my_stats":
            text = f"📊 إحصائياتك\nالاسم: {user.get('full_name', '—')}\nالنقاط: ⭐️ {user.get('total_points', 0)}\nالجولات: 🎮 {user.get('rounds_played', 0)}\nأفضل جولة: 🥇 {user.get('best_round_score', 0)}"
            await safe_send(context.bot, chat_id, text)
            await safe_send(context.bot, chat_id, "القائمة:", reply_markup=main_menu_keyboard(user))
        elif data == "play_round":
            if load_active_round(user_id):
                await safe_send(context.bot, chat_id, "⚠️ لديك جولة نشطة، استكملها أو انهها أولاً.")
            else:
                await safe_send(context.bot, chat_id, "اختر الفصل الدراسي للبدء 🎯:", reply_markup=term_selection_keyboard())
        elif data in ("start_term1", "start_term2"):
            await start_round(query, context, data)
        elif data == "back_to_main":
            await safe_send(context.bot, chat_id, "القائمة:", reply_markup=main_menu_keyboard(user))
    except Exception as e:
        logger.error(f"Menu error: {e}")

async def start_round(query, context, term: str):
    try:
        uid, cid = query.from_user.id, query.message.chat_id
        qm = qm_term1 if term == "start_term1" else qm_term2
        qs = qm.pick_round_questions(uid)
        if len(qs) < 10:
            await safe_send(context.bot, cid, "❌ لا توجد أسئلة كافية.")
            return
        qs = [qm.convert_term_to_mcq(q) if q.get("type") == "term" else q for q in qs]
        rd = {"round_questions": qs, "round_index": 0, "round_score": 0, "round_bonus": 0, "round_correct": 0, "round_streak": 0, "round_chapter_correct": {c: 0 for c in CHAPTERS}, "round_chapter_total": {c: 0 for c in CHAPTERS}, "total_questions": len(qs), "start_time": datetime.utcnow().isoformat(), "last_activity": datetime.utcnow().isoformat()}
        context.user_data.update(rd)
        save_active_round(uid, rd)
        await safe_send(context.bot, cid, f"🎮 بدأت الجولة! عدد الأسئلة: {len(qs)}")
        await send_next_question(cid, uid, context)
    except Exception as e:
        logger.error(f"Start round error: {e}")

async def send_next_question(cid: int, uid: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        now = datetime.utcnow().isoformat()
        context.user_data["last_activity"] = now
        save_active_round(uid, {k: v for k, v in context.user_data.items() if k.startswith("round_") or k in ["total_questions", "start_time", "last_activity"]})
        idx = context.user_data.get("round_index", 0)
        qs = context.user_data.get("round_questions", [])
        if idx >= len(qs):
            await finish_round(cid, uid, context, False)
            return
        q = qs[idx]
        context.user_data["current_q"] = q
        chap = q.get("_chapter", "—")
        context.user_data["round_chapter_total"][chap] = context.user_data["round_chapter_total"].get(chap, 0) + 1
        txt = f"📌 السؤال {idx+1}/{len(qs)}\n\n❓ {q.get('question', q.get('statement', ''))}"
        if q.get("type") == "mcq":
            await safe_send(context.bot, cid, txt, reply_markup=answer_keyboard_mcq(q.get("options", {})))
        else:
            await safe_send(context.bot, cid, txt, reply_markup=answer_keyboard_tf())
    except Exception as e:
        logger.error(f"Send Q error: {e}")

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if await maintenance_block(update, context): return
        query = update.callback_query
        await safe_answer_callback(query)
        uid, cid, data = query.from_user.id, query.message.chat_id, query.data
        if "round_questions" not in context.user_data:
            ar = load_active_round(uid)
            if ar: context.user_data.update(ar)
            else: return await safe_send(context.bot, cid, "❌ لا توجد جولة نشطة.")
        if data == "end_round": return await finish_round(cid, uid, context, True)
        
        q = context.user_data.get("current_q", {})
        is_correct = False
        if q.get("type") == "mcq" and data.startswith("ans_mcq:"):
            is_correct = (data.split(":")[1] == str(q.get("correct")).strip().upper())
        elif q.get("type") == "tf" and data.startswith("ans_tf:"):
            is_correct = (data.split(":")[1] == ("true" if parse_tf_answer(q.get("answer", q.get("correct"))) else "false"))
        
        idx, chap = context.user_data.get("round_index", 0), q.get("_chapter", "—")
        if is_correct:
            context.user_data["round_score"] += 1
            context.user_data["round_correct"] += 1
            context.user_data["round_streak"] += 1
            context.user_data["round_chapter_correct"][chap] = context.user_data["round_chapter_correct"].get(chap, 0) + 1
            if context.user_data["round_streak"] % STREAK_BONUS_EVERY == 0:
                context.user_data["round_bonus"] += 1
                await safe_send(context.bot, cid, f"✅ صح! بونص +1")
            else:
                await safe_send(context.bot, cid, "✅ صح!")
        else:
            context.user_data["round_streak"] = 0
            ans_txt = q.get("options", {}).get(q.get("correct")) if q.get("type") == "mcq" else ("صح" if parse_tf_answer(q.get("answer")) else "خطأ")
            await safe_send(context.bot, cid, f"❌ خطأ!\nالجواب الصحيح: {ans_txt}")
        
        if q.get("id"): mark_seen(uid, q.get("id"))
        context.user_data["round_index"] = idx + 1
        save_active_round(uid, {k: v for k, v in context.user_data.items() if k.startswith("round_") or k in ["total_questions", "start_time", "last_activity"]})
        await asyncio.sleep(0.5)
        await send_next_question(cid, uid, context)
    except Exception as e:
        logger.error(f"Ans callback error: {e}")

async def finish_round(cid: int, uid: int, context: ContextTypes.DEFAULT_TYPE, user_ended: bool):
    try:
        user = get_user(uid)
        sc, bn, cr, tot = context.user_data.get("round_score", 0), context.user_data.get("round_bonus", 0), context.user_data.get("round_correct", 0), context.user_data.get("total_questions", ROUND_SIZE)
        save_round_result(uid, sc, bn, cr, tot)
        delete_active_round(uid)
        txt = f"🏁 انتهت الجولة\n✅ الصح: {cr}/{tot}\n⭐️ النقاط: {sc}\n🔥 البونص: {bn}\n🏆 المجموع: {sc+bn}\n"
        if not user.get("is_approved"): txt += "\nℹ️ لتظهر بلوحة التميز، سجل اسمك واعتمد."
        await safe_send(context.bot, cid, txt)
        for k in ["round_questions", "round_index", "round_score", "round_bonus", "round_correct", "round_streak", "round_chapter_correct", "round_chapter_total", "current_q", "total_questions", "start_time", "last_activity"]: context.user_data.pop(k, None)
        await safe_send(context.bot, cid, "القائمة:", reply_markup=main_menu_keyboard(get_user(uid)))
    except Exception as e:
        logger.error(f"Finish round error: {e}")

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if await maintenance_block(update, context) or not update.message or not update.message.text: return
        uid, cid, txt = update.effective_user.id, update.message.chat_id, update.message.text.strip()

        # ميزة رد المشرف
        if is_admin(uid) and update.message.reply_to_message:
            match = re.search(r"\(ID:\s*(\d+)\)", update.message.reply_to_message.text or "")
            if match:
                await safe_send(context.bot, int(match.group(1)), f"👨‍🏫 رد من المشرف:\n\n{txt}")
                return await safe_send(context.bot, cid, "✅ تم إرسال الرد للطالب.")

        # تواصل مع المشرف
        if context.user_data.get("awaiting_contact"):
            context.user_data["awaiting_contact"] = False
            user = get_user(uid)
            name = user.get("full_name") or update.effective_user.full_name or "بدون اسم"
            await safe_send(context.bot, cid, "✅ تم إرسال رسالتك للمشرف.")
            for adm in ADMIN_IDS:
                await safe_send(context.bot, adm, f"📩 رسالة جديدة من مستخدم:\n\n👤 {name} (ID: {uid})\n📝 الرسالة:\n{txt}")
            return await safe_send(context.bot, cid, "القائمة:", reply_markup=main_menu_keyboard(user))

        # التسجيل
        if context.user_data.get("awaiting_name"):
            if not looks_like_real_name(txt):
                return await safe_send(context.bot, cid, "❌ الاسم غير مطابق للشروط. يرجى كتابة اسم حقيقي بالعربي من كلمتين.")
            upsert_user(uid)
            set_pending_name(uid, txt)
            context.user_data["awaiting_name"] = False
            await safe_send(context.bot, cid, "✅ تم رفع طلب الاسم للمشرف.")
            for adm in ADMIN_IDS:
                await safe_send(context.bot, adm, f"📝 طلب اعتماد اسم:\n👤 {uid}\n📝 {txt}", reply_markup=admin_pending_keyboard(uid))
            return
            
    except Exception as e:
        logger.error(f"Text router error: {e}")

# =========================
# Admin Commands
# =========================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await safe_send(context.bot, update.message.chat_id, f"👑 طلبات معلقة: {len(get_pending_list())}\nاستخدم /pending")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await safe_answer_callback(query)
        if not is_admin(query.from_user.id): return
        uid = int(query.data.split(":")[1])
        if query.data.startswith("admin_approve:"):
            approve_name(uid)
            await safe_send(context.bot, query.message.chat_id, f"✅ تم الموافقة على {uid}")
            await safe_send(context.bot, uid, "🎉 تم اعتماد اسمك!")
        elif query.data.startswith("admin_reject:"):
            reject_name(uid)
            await safe_send(context.bot, query.message.chat_id, f"❌ تم رفض {uid}")
            await safe_send(context.bot, uid, "❌ تم رفض الاسم، يرجى إعادة التسجيل باسم صحيح.")
    except Exception as e:
        logger.error(f"Admin CB error: {e}")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    pending = get_pending_list()
    if not pending: return await safe_send(context.bot, update.message.chat_id, "✅ لا توجد طلبات.")
    for p in pending[:20]:
        await safe_send(context.bot, update.message.chat_id, f"📝 طلب:\n👤 {p['user_id']}\n📝 {p['full_name']}", reply_markup=admin_pending_keyboard(p['user_id']))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(context.bot, update.message.chat_id, "الأوامر:\n/start - البداية\n/admin - الأدمن\n/pending - الطلبات\n/reload - تحديث الأسئلة")

async def reload_questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    qm_term1.load_questions(); qm_term2.load_questions()
    await safe_send(context.bot, update.message.chat_id, f"✅ تم التحديث\nالترم 1: {len(qm_term1.items)}\nالترم 2: {len(qm_term2.items)}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception:", exc_info=context.error)

# =========================
# Main
# =========================
def main():
    app = Application.builder().token(BOT_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)).concurrent_updates(True).build()
    for cmd, hnd in [("start", start), ("help", help_command), ("admin", admin_command), ("pending", pending_command), ("reload", reload_questions_command)]: app.add_handler(CommandHandler(cmd, hnd))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^(ans_mcq:|ans_tf:|end_round)"))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_router))
    app.add_error_handler(error_handler)
    import threading
    threading.Thread(target=lambda: asyncio.run(cleanup_task()), daemon=True).start()
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()
