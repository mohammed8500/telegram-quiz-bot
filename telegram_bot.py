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
# Logging محسن
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

ADMIN_IDS = set()
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

# تعريف ملفات الفصول
TERM1_FILE = os.getenv("TERM1_FILE", "questions_from_word.json").strip()
TERM2_FILE = os.getenv("TERM2_FILE", "questions_term2.json").strip()
DB_FILE = os.getenv("DB_FILE", "data.db").strip()

# =========================
# Game Settings
# =========================
ROUND_SIZE = 20
STREAK_BONUS_EVERY = 3
TOP_N = 10
MAX_ROUND_DURATION = 600  # 10 دقائق كحد أقصى للجولة
CLEANUP_INTERVAL = 3600   # تنظيف كل ساعة

CHAPTERS = [
    "طبيعة العلم",
    "المخاليط والمحاليل",
    "حالات المادة",
    "الطاقة وتحولاتها",
    "أجهزة الجسم",
    "النباتات" # إضافة فصل النباتات للفصل الثاني
]

# =========================
# Database Connection Pooling
# =========================
class DatabaseManager:
    """مدير اتصالات قاعدة البيانات مع connection pooling"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_pool()
        return cls._instance
    
    def _init_pool(self):
        """تهيئة اتصال دائم مع إعدادات متقدمة"""
        self.conn = sqlite3.connect(
            DB_FILE,
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=10000")
        self._init_tables()
    
    def _init_tables(self):
        """تهيئة الجداول"""
        cur = self.conn.cursor()
        
        # جدول المستخدمين
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
        
        # جدول طلبات الأسماء
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_names (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                requested_at TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        
        # جدول الأسئلة المشاهدة
        cur.execute("""
            CREATE TABLE IF NOT EXISTS seen_questions (
                user_id INTEGER,
                qid TEXT,
                seen_at TEXT,
                PRIMARY KEY (user_id, qid)
            )
        """)
        
        # جدول الجولات
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
        
        # جدول الجولات النشطة
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
        """الحصول على مؤشر للاستعلامات"""
        cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise e
        finally:
            cursor.close()
    
    def close(self):
        """إغلاق الاتصال"""
        if self.conn:
            self.conn.close()

db_manager = DatabaseManager()

# =========================
# Robust send helpers
# =========================
SEND_RETRIES = 3
SEND_TIMEOUT = 10  # 10 ثواني كحد أقصى للإرسال

async def safe_send(bot, chat_id: int, text: str, **kwargs):
    """إرسال آمن مع إعادة المحاولة"""
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return await asyncio.wait_for(
                bot.send_message(chat_id=chat_id, text=text, **kwargs),
                timeout=SEND_TIMEOUT
            )
        except RetryAfter as e:
            wait_time = getattr(e, 'retry_after', 1)
            logger.warning(f"Rate limited, waiting {wait_time}s")
            await asyncio.sleep(wait_time)
        except (TimedOut, NetworkError, asyncio.TimeoutError) as e:
            logger.warning(f"Network error (attempt {attempt}/{SEND_RETRIES}): {e}")
            if attempt < SEND_RETRIES:
                await asyncio.sleep(1 * attempt)  # زيادة الانتظار تدريجياً
            else:
                logger.error("Failed to send message after retries")
                return None
        except BadRequest as e:
            logger.error(f"Bad request: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in safe_send: {e}")
            if attempt == SEND_RETRIES:
                return None
            await asyncio.sleep(1)
    
    return None

async def safe_answer_callback(query, text: str = None, show_alert: bool = False):
    """إجابة آمنة على callback queries"""
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception as e:
        logger.warning(f"Failed to answer callback: {e}")

# =========================
# Arabic normalization
# =========================
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")

def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = _ARABIC_DIACRITICS.sub("", text)
    text = re.sub(r"[^\u0600-\u06FF0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    return text.lower()

def is_arabic_only_name(name: str) -> bool:
    if not name:
        return False
    name = name.strip()
    if re.search(r"[A-Za-z]", name):
        return False
    return bool(re.fullmatch(r"[\u0600-\u06FF\s]+", name))

def looks_like_real_name(name: str) -> bool:
    name = name.strip()
    if not is_arabic_only_name(name):
        return False
    parts = [p for p in name.split() if p]
    if len(parts) < 2:
        return False
    if len(name) < 6 or len(name) > 30:
        return False
    n_norm = normalize_arabic(name)
    for bw in BAD_WORDS:
        bw_norm = normalize_arabic(bw)
        if bw_norm and bw_norm in n_norm:
            return False
    return True

# =========================
# Maintenance guard
# =========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def maintenance_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not MAINTENANCE_ON:
        return False
    
    user_id = update.effective_user.id if update.effective_user else 0
    if is_admin(user_id):
        return False
    
    msg = "🛠️ البوت تحت صيانة حالياً… ارجعوا بعدين 🌿"
    
    try:
        if update.message:
            await safe_send(context.bot, update.message.chat_id, msg, reply_markup=ReplyKeyboardRemove())
        elif update.callback_query:
            await safe_answer_callback(update.callback_query, "البوت تحت صيانة", show_alert=True)
            await safe_send(context.bot, update.callback_query.message.chat_id, msg, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.error(f"Maintenance block failed: {e}")
    
    return True

# =========================
# Database operations
# =========================
def upsert_user(user_id: int):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if cur.fetchone():
            cur.execute(
                "UPDATE users SET updated_at=?, last_active=? WHERE user_id=?",
                (now, now, user_id)
            )
        else:
            cur.execute(
                "INSERT INTO users(user_id, created_at, updated_at, last_active) VALUES (?,?,?,?)",
                (user_id, now, now, now)
            )

def set_pending_name(user_id: int, full_name: str):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("""
            INSERT INTO pending_names(user_id, full_name, requested_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET 
                full_name=excluded.full_name, 
                requested_at=excluded.requested_at,
                status='pending'
        """, (user_id, full_name, now))

def approve_name(user_id: int):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT full_name FROM pending_names WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            full_name = row["full_name"]
            cur.execute("""
                UPDATE users SET full_name=?, is_approved=1, updated_at=?
                WHERE user_id=?
            """, (full_name, now, user_id))
            cur.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))

def reject_name(user_id: int):
    with db_manager.get_cursor() as cur:
        cur.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))

def get_user(user_id: int) -> Dict[str, Any]:
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else {}

def get_pending_list() -> List[Dict[str, Any]]:
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT * FROM pending_names WHERE status='pending' ORDER BY requested_at ASC")
        return [dict(row) for row in cur.fetchall()]

def mark_seen(user_id: int, qid: str):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("""
            INSERT OR IGNORE INTO seen_questions(user_id, qid, seen_at)
            VALUES(?,?,?)
        """, (user_id, qid, now))

def has_seen(user_id: int, qid: str) -> bool:
    if not qid:
        return False
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT 1 FROM seen_questions WHERE user_id=? AND qid=? LIMIT 1", (user_id, qid))
        return cur.fetchone() is not None

def save_round_result(user_id: int, score: int, bonus: int, correct: int, total: int, status: str = "completed"):
    now = datetime.utcnow().isoformat()
    with db_manager.get_cursor() as cur:
        cur.execute("""
            INSERT INTO rounds(user_id, started_at, finished_at, score, bonus, correct, total, status)
            VALUES(?,?,?,?,?,?,?,?)
        """, (user_id, now, now, score, bonus, correct, total, status))

        cur.execute("SELECT total_points, rounds_played, best_round_score FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            total_points = int(row["total_points"]) + int(score + bonus)
            rounds_played = int(row["rounds_played"]) + 1
            best_round_score = max(int(row["best_round_score"]), int(score + bonus))
            cur.execute("""
                UPDATE users
                SET total_points=?, rounds_played=?, best_round_score=?, updated_at=?
                WHERE user_id=?
            """, (total_points, rounds_played, best_round_score, now, user_id))

def save_active_round(user_id: int, round_data: Dict[str, Any]):
    now = datetime.utcnow().isoformat()
    data_json = json.dumps(round_data, ensure_ascii=False)
    with db_manager.get_cursor() as cur:
        cur.execute("""
            INSERT INTO active_rounds(user_id, data, started_at, last_activity)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                data=excluded.data,
                last_activity=excluded.last_activity
        """, (user_id, data_json, now, now))

def load_active_round(user_id: int) -> Optional[Dict[str, Any]]:
    with db_manager.get_cursor() as cur:
        cur.execute("SELECT data FROM active_rounds WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            return json.loads(row["data"])
    return None

def delete_active_round(user_id: int):
    with db_manager.get_cursor() as cur:
        cur.execute("DELETE FROM active_rounds WHERE user_id=?", (user_id,))

def get_leaderboard(top_n: int) -> List[Dict[str, Any]]:
    with db_manager.get_cursor() as cur:
        cur.execute("""
            SELECT full_name, total_points, best_round_score, rounds_played
            FROM users
            WHERE is_approved=1 AND full_name IS NOT NULL AND TRIM(full_name) <> ''
            ORDER BY total_points DESC, best_round_score DESC, rounds_played DESC
            LIMIT ?
        """, (top_n,))
        return [dict(row) for row in cur.fetchall()]

# =========================
# Question Manager with caching
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
            if not os.path.exists(self.filename):
                logger.warning(f"File not found: {self.filename}")
                return

            file_mtime = os.path.getmtime(self.filename)
            if self.last_loaded and file_mtime <= self.last_loaded:
                return
            
            with open(self.filename, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items", []) or data.get("questions", [])
            else:
                items = []
            
            # إضافة IDs فريدة إن لم تكن موجودة
            for i, item in enumerate(items):
                if "id" not in item:
                    item["id"] = f"q_{self.filename}_{i}_{hash(str(item))}"
            
            self.items = items
            self.buckets = self.build_chapter_buckets(items)
            self.term_pool = self.extract_terms(items)
            self.last_loaded = file_mtime
            
            logger.info(f"Loaded {len(items)} questions from {self.filename}")
            
        except Exception as e:
            logger.error(f"Failed to load questions from {self.filename}: {e}")
            self.items = []
            self.buckets = {}
            self.term_pool = []
    
    def build_chapter_buckets(self, items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        buckets = {c: [] for c in CHAPTERS}
        for item in items:
            chapter = self.classify_chapter(item)
            item["_chapter"] = chapter
            buckets[chapter].append(item)
        return buckets
    
    def classify_chapter(self, item: Dict[str, Any]) -> str:
        CHAPTER_KEYWORDS = {
            "طبيعة العلم": ["الطريقه العلميه", "فرضيه", "متغير", "ثابت", "ملاحظه", "تجربه", "استنتاج", "تواصل", "علم الاثار", "الرادار"],
            "المخاليط والمحاليل": ["مخلوط", "محلول", "مذيب", "مذاب", "تركيز", "ذائبيه", "حمض", "قاعده", "تعادل", "ترسب", "ph", "ايوني", "تساهمي"],
            "حالات المادة": ["صلب", "سائل", "غاز", "بلازما", "انصهار", "تبخر", "تكاثف", "تجمد", "تسامي", "ضغط", "كثافه", "توتر سطحي", "لزوج"],
            "الطاقة وتحولاتها": ["طاقه", "حركيه", "وضع", "كامنه", "اشعاعيه", "كيميائيه", "كهربائيه", "نوويه", "توربين", "مولد", "خليه شمسيه", "حفظ الطاقه"],
            "أجهزة الجسم": ["دم", "قلب", "شريان", "وريد", "شعيره", "مناعه", "اجسام مضاده", "مولدات الضد", "ايدز", "سكري", "هضم", "معده", "امعاء", "رئه", "تنفس", "كليه", "بول", "عظام", "مفصل", "جلد", "بشره"],
            "النباتات": ["لحاء", "خشب", "بذور", "ثغور", "مخاريط", "سرخسيات", "حزازيات"]
        }
        
        blob = ""
        t = item.get("type")
        if t == "mcq":
            blob = (item.get("question") or "")
            options = item.get("options") or {}
            blob += " " + " ".join(str(v) for v in options.values())
        elif t == "tf":
            blob = (item.get("statement") or "")
        elif t == "term":
            blob = (item.get("term") or "") + " " + (item.get("definition") or "")
        
        blob_n = normalize_arabic(blob)
        best_chapter = "حالات المادة"
        best_score = 0
        
        for chapter, keywords in CHAPTER_KEYWORDS.items():
            score = 0
            for kw in keywords:
                if kw and normalize_arabic(kw) in blob_n:
                    score += 1
            if score > best_score:
                best_score = score
                best_chapter = chapter
        
        return best_chapter
    
    def extract_terms(self, items: List[Dict[str, Any]]) -> List[str]:
        terms = []
        for item in items:
            if item.get("type") == "term":
                term = item.get("term")
                if term and term.strip():
                    terms.append(term.strip())
        return list(set(terms))
    
    def pick_round_questions(self, user_id: int) -> List[Dict[str, Any]]:
        self.load_questions()
        
        if not self.buckets:
            return []
        
        target_per_chapter = ROUND_SIZE // len(CHAPTERS)
        chosen = []
        seen_ids = set()
        
        for chapter in CHAPTERS:
            pool = self.buckets.get(chapter, [])
            unseen = [q for q in pool if not has_seen(user_id, q.get("id"))]
            random.shuffle(unseen)
            
            take = min(target_per_chapter, len(unseen))
            for q in unseen[:take]:
                if q["id"] not in seen_ids:
                    chosen.append(q)
                    seen_ids.add(q["id"])
        
        if len(chosen) < ROUND_SIZE:
            all_questions = []
            for chapter in CHAPTERS:
                all_questions.extend(self.buckets.get(chapter, []))
            
            random.shuffle(all_questions)
            for q in all_questions:
                if q["id"] not in seen_ids:
                    chosen.append(q)
                    seen_ids.add(q["id"])
                    if len(chosen) >= ROUND_SIZE:
                        break
        
        chosen = chosen[:ROUND_SIZE]
        random.shuffle(chosen)
        return chosen
    
    def convert_term_to_mcq(self, term_question: Dict[str, Any]) -> Dict[str, Any]:
        correct_term = term_question.get("term", "").strip()
        definition = term_question.get("definition", "").strip()
        
        wrong_terms = [t for t in self.term_pool if t != correct_term]
        if len(wrong_terms) >= 3:
            distractors = random.sample(wrong_terms, 3)
        else:
            distractors = ["مصطلح 1", "مصطلح 2", "مصطلح 3"]
        
        all_choices = [correct_term] + distractors
        random.shuffle(all_choices)
        
        options = {}
        correct_key = None
        for i, choice in enumerate(all_choices):
            key = ["A", "B", "C", "D"][i]
            options[key] = choice
            if choice == correct_term:
                correct_key = key
        
        mcq_question = term_question.copy()
        mcq_question.update({
            "type": "mcq",
            "question": f"ما هو المصطلح المناسب للتعريف التالي؟\n\n{definition}",
            "options": options,
            "correct": correct_key,
            "original_type": "term"
        })
        
        return mcq_question

# إنشاء مدير لكل فصل
qm_term1 = QuestionManager(TERM1_FILE)
qm_term2 = QuestionManager(TERM2_FILE)

# =========================
# Session Cleanup Task
# =========================
async def cleanup_task():
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            cutoff = datetime.utcnow() - timedelta(seconds=MAX_ROUND_DURATION)
            cutoff_str = cutoff.isoformat()
            
            with db_manager.get_cursor() as cur:
                cur.execute("""
                    SELECT user_id, data FROM active_rounds 
                    WHERE last_activity < ?
                """, (cutoff_str,))
                
                old_rounds = cur.fetchall()
                for row in old_rounds:
                    try:
                        data = json.loads(row["data"])
                        score = data.get("round_score", 0)
                        bonus = data.get("round_bonus", 0)
                        correct = data.get("round_correct", 0)
                        total = data.get("total_questions", ROUND_SIZE)
                        
                        save_round_result(row["user_id"], score, bonus, correct, total, "timeout")
                    except Exception as e:
                        logger.error(f"Error cleaning round: {e}")
                
                cur.execute("DELETE FROM active_rounds WHERE last_activity < ?", (cutoff_str,))
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")
            await asyncio.sleep(60)

# =========================
# UI Helpers
# =========================
def main_menu_keyboard(user: Dict[str, Any]) -> InlineKeyboardMarkup:
    approved = bool(user.get("is_approved", 0))
    name = user.get("full_name") or ""
    
    if approved and name:
        name_status = f"✅ {name[:15]}"
    elif name:
        name_status = "⏳ بانتظار الموافقة"
    else:
        name_status = "➕ سجّل اسمك"
    
    kb = [
        [InlineKeyboardButton("🎮 ابدأ جولة (20 سؤال)", callback_data="play_round")],
        [InlineKeyboardButton("🏆 لوحة التميز (Top 10)", callback_data="leaderboard")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats")],
        [InlineKeyboardButton(name_status, callback_data="set_name")],
    ]
    
    if user.get("user_id"):
        active_round = load_active_round(user["user_id"])
        if active_round:
            kb.insert(0, [InlineKeyboardButton("🔄 استعادة الجولة النشطة", callback_data="resume_round")])
    
    return InlineKeyboardMarkup(kb)

def term_selection_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📚 الفصل الدراسي الأول", callback_data="start_term1")],
        [InlineKeyboardButton("📘 الفصل الدراسي الثاني", callback_data="start_term2")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(kb)

def answer_keyboard_mcq(options: Dict[str, str]) -> InlineKeyboardMarkup:
    rows = []
    for key in ["A", "B", "C", "D"]:
        if key in options:
            text = options[key]
            if len(text) > 40:
                text = text[:37] + "..."
            rows.append([InlineKeyboardButton(f"{key}) {text}", callback_data=f"ans_mcq:{key}")])
    rows.append([InlineKeyboardButton("⛔️ إنهاء الجولة", callback_data="end_round")])
    return InlineKeyboardMarkup(rows)

def answer_keyboard_tf() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("✅ صح", callback_data="ans_tf:true"),
            InlineKeyboardButton("❌ خطأ", callback_data="ans_tf:false"),
        ],
        [InlineKeyboardButton("⛔️ إنهاء الجولة", callback_data="end_round")]
    ]
    return InlineKeyboardMarkup(kb)

def admin_pending_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("✅ موافق", callback_data=f"admin_approve:{user_id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject:{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(kb)

# =========================
# Helpers
# =========================
def parse_tf_answer(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    s_norm = normalize_arabic(s)
    if s in ("true", "1") or s_norm in ("صح", "صحيح", "ص"):
        return True
    if s in ("false", "0") or s_norm in ("خطا", "خطأ"):
        return False
    return None

# =========================
# Motivation phrases
# =========================
MOTIVATION_CORRECT = ["🔥 بطل! كمل كذا!", "👏 ممتاز!", "💪 رهيب!", "✅ صح عليك!", "🌟 كفو!", "🚀 يا سلام عليك!"]
MOTIVATION_WRONG = ["😅 بسيطة! الجاية صح إن شاء الله.", "👀 ركّز شوي، تقدر!", "💡 مو مشكلة، تعلمنا!", "🔥 لا توقف! كمل!", "😎 قدها وقدود!"]
MOTIVATION_BONUS = ["🏅 بونص! سلسلة نار 🔥", "🎯 ممتاز! خذت بونص!", "💥 كملت سلسلة الصح!"]

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
        return
    
    user_id = update.effective_user.id
    upsert_user(user_id)
    user = get_user(user_id)
    
    logger.info(f"User {user_id} started bot")
    
    msg = (
        "هلا 👋\n"
        "أنا بوت المسابقة 🎯\n"
        "• كل جولة = 20 سؤال موزعة على فصول المنهج\n"
        "• بونص: كل 3 إجابات صحيحة متتالية = +1\n"
        "• لوحة التميز Top 10 للطلاب المعتمدين ✅\n\n"
        "اختر من القائمة 👇"
    )
    
    await safe_send(context.bot, update.message.chat_id, msg, reply_markup=ReplyKeyboardRemove())
    await safe_send(context.bot, update.message.chat_id, "القائمة:", reply_markup=main_menu_keyboard(user))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
        return
    
    query = update.callback_query
    await safe_answer_callback(query)
    
    user_id = query.from_user.id
    upsert_user(user_id)
    user = get_user(user_id)
    data = query.data
    
    logger.info(f"Menu callback: {data} from user {user_id}")
    
    if data == "set_name":
        context.user_data["awaiting_name"] = True
        await query.message.reply_text(
            "اكتب اسمك الحقيقي (عربي فقط) مثل: **محمد أحمد**\n"
            "شروطنا:\n"
            "• عربي فقط (بدون إنجليزي)\n"
            "• كلمتين على الأقل\n"
            "• واضح ومحترم\n\n"
            "✍️ اكتب الاسم الآن:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return
    
    if data == "resume_round":
        active_round = load_active_round(user_id)
        if active_round:
            context.user_data.update(active_round)
            await query.message.reply_text("🔄 **تم استعادة جولتك النشطة**\nاستمر من حيث توقفت!", reply_markup=ReplyKeyboardRemove())
            await send_next_question(query.message.chat_id, user_id, context)
        else:
            await query.message.reply_text("❌ لا توجد جولة نشطة للاستعادة", reply_markup=ReplyKeyboardRemove())
        return
    
    if data == "leaderboard":
        lb = get_leaderboard(TOP_N)
        if not lb:
            text = "🏆 لوحة التميز فاضية للحين… أول واحد يبدع 🔥"
        else:
            lines = ["🏆 **لوحة التميز (Top 10)**\n"]
            for i, row in enumerate(lb, start=1):
                lines.append(f"{i}) {row['full_name']} — ⭐️ {row['total_points']} نقطة (أفضل جولة: {row['best_round_score']})")
            text = "\n".join(lines)
        
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await query.message.reply_text("القائمة:", reply_markup=main_menu_keyboard(user))
        return
    
    if data == "my_stats":
        name = user.get("full_name") or "—"
        approved = "✅" if user.get("is_approved", 0) else "⏳"
        total = user.get("total_points", 0)
        rounds = user.get("rounds_played", 0)
        best = user.get("best_round_score", 0)
        text = (f"📊 **إحصائياتك**\nالاسم: {name} {approved}\nالنقاط: ⭐️ {total}\nعدد الجولات: 🎮 {rounds}\nأفضل جولة: 🥇 {best}\n")
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await query.message.reply_text("القائمة:", reply_markup=main_menu_keyboard(user))
        return
    
    if data == "play_round":
        active_round = load_active_round(user_id)
        if active_round:
            await query.message.reply_text(
                "⚠️ **لديك جولة نشطة بالفعل**\n\nيمكنك:\n• استكمال الجولة من الزر 'استعادة الجولة النشطة'\n• أو إنهاء الجولة الحالية أولاً",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        
        await query.message.reply_text("اختر الفصل الدراسي للبدء 🎯:", reply_markup=term_selection_keyboard())
        return

    if data in ("start_term1", "start_term2"):
        await start_round(query, context, data)
        return

    if data == "back_to_main":
        await query.message.reply_text("القائمة:", reply_markup=main_menu_keyboard(user))
        return

async def start_round(query, context: ContextTypes.DEFAULT_TYPE, term: str):
    user_id = query.from_user.id
    upsert_user(user_id)
    
    # اختيار الملف الصحيح بناءً على ضغطة الطالب
    qm = qm_term1 if term == "start_term1" else qm_term2
    round_questions = qm.pick_round_questions(user_id)
    
    if len(round_questions) < 10:
        await query.message.reply_text("❌ **لا توجد أسئلة كافية للبدء في هذا الفصل**", reply_markup=ReplyKeyboardRemove())
        return
    
    processed_questions = []
    for q in round_questions:
        if q.get("type") == "term":
            processed_questions.append(qm.convert_term_to_mcq(q))
        else:
            processed_questions.append(q)
    
    round_data = {
        "round_questions": processed_questions,
        "round_index": 0,
        "round_score": 0,
        "round_bonus": 0,
        "round_correct": 0,
        "round_streak": 0,
        "round_chapter_correct": {c: 0 for c in CHAPTERS},
        "round_chapter_total": {c: 0 for c in CHAPTERS},
        "total_questions": len(processed_questions),
        "start_time": datetime.utcnow().isoformat(),
        "last_activity": datetime.utcnow().isoformat()
    }
    
    context.user_data.update(round_data)
    save_active_round(user_id, round_data)
    
    term_name = "الفصل الدراسي الأول" if term == "start_term1" else "الفصل الدراسي الثاني"
    await query.message.reply_text(
        f"🎮 **بدأت الجولة! ({term_name})**\n\n"
        f"عدد الأسئلة: {len(processed_questions)}\n"
        f"جاهز؟ 🔥",
        reply_markup=ReplyKeyboardRemove()
    )
    
    await send_next_question(query.message.chat_id, user_id, context)

async def send_next_question(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        now = datetime.utcnow().isoformat()
        context.user_data["last_activity"] = now
        
        round_data = {
            k: v for k, v in context.user_data.items() 
            if k.startswith("round_") or k in ["total_questions", "start_time", "last_activity"]
        }
        save_active_round(user_id, round_data)
        
        idx = context.user_data.get("round_index", 0)
        qs = context.user_data.get("round_questions", [])
        
        if idx >= len(qs):
            await finish_round(chat_id, user_id, context, ended_by_user=False)
            return
        
        q = qs[idx]
        context.user_data["current_q"] = q
        
        chap = q.get("_chapter", "—")
        context.user_data["round_chapter_total"][chap] = context.user_data["round_chapter_total"].get(chap, 0) + 1
        
        header = f"📌 السؤال {idx+1}/{len(qs)}\n\n"
        t = q.get("type")
        
        if t == "mcq":
            question = (q.get("question") or "").strip()
            options = q.get("options") or {}
            text = header + f"❓ {question}"
            await safe_send(context.bot, chat_id, text, reply_markup=answer_keyboard_mcq(options))
            return
        
        if t == "tf":
            st = (q.get("statement") or "").strip()
            text = header + f"✅/❌ {st}"
            await safe_send(context.bot, chat_id, text, reply_markup=answer_keyboard_tf())
            return
        
        await safe_send(context.bot, chat_id, "⚠️ نوع سؤال غير معروف… تخطيناه.", reply_markup=ReplyKeyboardRemove())
        context.user_data["round_index"] = idx + 1
        await send_next_question(chat_id, user_id, context)
        
    except Exception as e:
        logger.error(f"Error in send_next_question: {e}")
        await safe_send(context.bot, chat_id, "⚠️ حدث خطأ في تحميل السؤال. حاول مرة أخرى.", reply_markup=ReplyKeyboardRemove())

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
        return
    
    query = update.callback_query
    await safe_answer_callback(query)
    
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    if "round_questions" not in context.user_data:
        active_round = load_active_round(user_id)
        if active_round:
            context.user_data.update(active_round)
        else:
            await query.message.reply_text("❌ **لا توجد جولة نشطة**\nاكتب /start للعودة", reply_markup=ReplyKeyboardRemove())
            return
    
    q = context.user_data.get("current_q")
    if not q:
        await query.message.reply_text("⚠️ ما عندي سؤال حالي.", reply_markup=ReplyKeyboardRemove())
        return
    
    data = query.data
    
    if data == "end_round":
        await finish_round(chat_id, user_id, context, ended_by_user=True)
        return
    
    is_correct = False
    t = q.get("type")
    
    if t == "mcq" and data.startswith("ans_mcq:"):
        picked = data.split(":")[1]
        correct = (q.get("correct") or "").strip().upper()
        is_correct = (picked == correct)
    
    elif t == "tf" and data.startswith("ans_tf:"):
        picked = data.split(":")[1]
        correct_bool = parse_tf_answer(q.get("answer"))
        if correct_bool is None:
            correct_bool = parse_tf_answer(q.get("correct"))
        if correct_bool is None:
            correct_bool = False
        is_correct = (picked == ("true" if correct_bool else "false"))
    
    else:
        await query.message.reply_text("⚠️ إجابة غير متوقعة.", reply_markup=ReplyKeyboardRemove())
        return
    
    await apply_answer_result(chat_id, user_id, context, is_correct)

async def apply_answer_result(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, is_correct: bool):
    try:
        idx = context.user_data.get("round_index", 0)
        q = context.user_data.get("current_q") or {}
        chap = q.get("_chapter", "—")
        
        if is_correct:
            context.user_data["round_score"] += 1
            context.user_data["round_correct"] += 1
            context.user_data["round_streak"] += 1
            context.user_data["round_chapter_correct"][chap] = context.user_data["round_chapter_correct"].get(chap, 0) + 1
            
            streak = context.user_data["round_streak"]
            if streak % STREAK_BONUS_EVERY == 0:
                context.user_data["round_bonus"] += 1
                msg = f"{random.choice(MOTIVATION_BONUS)}\n✅ صح! 🔥\n+1 (كل {STREAK_BONUS_EVERY} صح = +1)"
            else:
                msg = f"✅ صح! {random.choice(MOTIVATION_CORRECT)}"
            
            await safe_send(context.bot, chat_id, msg, reply_markup=ReplyKeyboardRemove())
        else:
            context.user_data["round_streak"] = 0
            correct_text = "—"
            t = q.get("type")
            
            if t == "mcq":
                c_key = q.get("correct")
                opts = q.get("options", {})
                correct_text = opts.get(c_key, "غير معروف")
            elif t == "tf":
                c_bool = parse_tf_answer(q.get("answer") or q.get("correct"))
                correct_text = "✅ صح" if c_bool else "❌ خطأ"
            
            msg = f"❌ خطأ! {random.choice(MOTIVATION_WRONG)}\n\n✅ الإجابة الصحيحة كانت: **{correct_text}**"
            await safe_send(context.bot, chat_id, msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        
        qid = q.get("id", "")
        if qid:
            mark_seen(user_id, qid)
        
        context.user_data["round_index"] = idx + 1
        
        round_data = {
            k: v for k, v in context.user_data.items() 
            if k.startswith("round_") or k in ["total_questions", "start_time", "last_activity"]
        }
        round_data["last_activity"] = datetime.utcnow().isoformat()
        save_active_round(user_id, round_data)
        
        await asyncio.sleep(0.5)
        await send_next_question(chat_id, user_id, context)
        
    except Exception as e:
        logger.error(f"Error in apply_answer_result: {e}")
        await safe_send(context.bot, chat_id, "⚠️ حدث خطأ في معالجة إجابتك. حاول مرة أخرى.", reply_markup=ReplyKeyboardRemove())

async def finish_round(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, ended_by_user: bool):
    try:
        user = get_user(user_id)
        score = int(context.user_data.get("round_score", 0))
        bonus = int(context.user_data.get("round_bonus", 0))
        correct = int(context.user_data.get("round_correct", 0))
        total = int(context.user_data.get("total_questions", ROUND_SIZE))
        
        save_round_result(user_id, score, bonus, correct, total)
        delete_active_round(user_id)
        
        chap_correct = context.user_data.get("round_chapter_correct", {})
        chap_total = context.user_data.get("round_chapter_total", {})
        
        lines = []
        lines.append("🏁 **انتهت الجولة**" + (" (إنهاء مبكر)" if ended_by_user else ""))
        lines.append(f"✅ الصحيح: {correct}/{total}")
        lines.append(f"⭐️ نقاط الإجابات: {score}")
        lines.append(f"🔥 البونص: {bonus}")
        lines.append(f"🏆 مجموع الجولة: **{score + bonus}**")
        lines.append("")
        lines.append("📌 أداءك حسب الفصول:")
        
        for c in CHAPTERS:
            cc = chap_correct.get(c, 0)
            tt = chap_total.get(c, 0)
            if tt > 0:
                lines.append(f"• {c}: {cc}/{tt}")
        
        if not user.get("is_approved", 0):
            lines.append("")
            lines.append("ℹ️ تقدر تجمع نقاط، بس لوحة التميز تظهر بعد اعتماد اسمك ✅")
        
        await safe_send(context.bot, chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        
    except Exception as e:
        logger.error(f"Error in finish_round: {e}")
        await safe_send(context.bot, chat_id, "⚠️ حدث خطأ في إنهاء الجولة، لكن النقاط تم حفظها.", reply_markup=ReplyKeyboardRemove())
    finally:
        keys_to_remove = [
            "round_questions", "round_index", "round_score", "round_bonus",
            "round_correct", "round_streak", "round_chapter_correct",
            "round_chapter_total", "current_q", "total_questions",
            "start_time", "last_activity"
        ]
        for key in keys_to_remove:
            context.user_data.pop(key, None)
        
        upsert_user(user_id)
        user = get_user(user_id)
        await safe_send(context.bot, chat_id, "اختر من القائمة 👇", reply_markup=main_menu_keyboard(user))

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
        return
    
    if not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if context.user_data.get("awaiting_name"):
        if not looks_like_real_name(text):
            await update.message.reply_text("❌ الاسم ما ينفع حسب الشروط.\nجرّب مرة ثانية 👇", reply_markup=ReplyKeyboardRemove())
            return
        
        upsert_user(user_id)
        set_pending_name(user_id, text)
        context.user_data["awaiting_name"] = False
        
        await update.message.reply_text("✅ تم استلام الاسم. بانتظار موافقة الأدمن 👑", reply_markup=ReplyKeyboardRemove())
        
        for admin_id in ADMIN_IDS:
            try:
                await safe_send(context.bot, admin_id, f"📝 طلب اعتماد اسم:\n• المستخدم: {user_id}\n• الاسم: {text}", reply_markup=admin_pending_keyboard(user_id))
            except Exception:
                pass
        return
    
    await update.message.reply_text("استخدم القائمة للتنقل 👇\nاكتب /start للعودة", reply_markup=ReplyKeyboardRemove())

# =========================
# Admin Handlers
# =========================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.", reply_markup=ReplyKeyboardRemove())
        return
    pending = get_pending_list()
    await update.message.reply_text(f"👑 لوحة الأدمن\nطلبات الأسماء المعلّقة: {len(pending)}\nاستخدم /pending لعرض الطلبات.", reply_markup=ReplyKeyboardRemove())

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer_callback(query)
    
    admin_id = query.from_user.id
    if not is_admin(admin_id):
        await query.message.reply_text("❌ ما لك صلاحية هنا.", reply_markup=ReplyKeyboardRemove())
        return
    
    data = query.data
    
    if data.startswith("admin_approve:"):
        uid = int(data.split(":")[1])
        approve_name(uid)
        await query.message.reply_text(f"✅ تم اعتماد المستخدم {uid}", reply_markup=ReplyKeyboardRemove())
        try:
            await safe_send(context.bot, uid, "🎉 تم اعتماد اسمك! الحين بتدخل لوحة التميز 🏆", reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass
        return
    
    if data.startswith("admin_reject:"):
        uid = int(data.split(":")[1])
        reject_name(uid)
        await query.message.reply_text(f"❌ تم رفض الاسم للمستخدم {uid}", reply_markup=ReplyKeyboardRemove())
        try:
            await safe_send(context.bot, uid, "❌ اسمك ما تم اعتماده. اكتب اسمك مرة ثانية بشكل محترم.", reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass
        return

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.", reply_markup=ReplyKeyboardRemove())
        return
    pending = get_pending_list()
    if not pending:
        await update.message.reply_text("ما فيه طلبات معلّقة ✅", reply_markup=ReplyKeyboardRemove())
        return
    for p in pending[:20]:
        await update.message.reply_text(f"📝 طلب معلّق:\n• المستخدم: {p['user_id']}\n• الاسم: {p['full_name']}", reply_markup=admin_pending_keyboard(p['user_id']))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("الأوامر:\n/start — تشغيل البوت\n/admin — للأدمن\n/pending — طلبات الأسماء\n/reload — تحديث الأسئلة", reply_markup=ReplyKeyboardRemove())

async def reload_questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.", reply_markup=ReplyKeyboardRemove())
        return
    
    qm_term1.load_questions()
    qm_term2.load_questions()
    await update.message.reply_text(
        f"✅ تم إعادة تحميل الأسئلة للملفين\n"
        f"• أسئلة الفصل الأول: {len(qm_term1.items)}\n"
        f"• أسئلة الفصل الثاني: {len(qm_term2.items)}",
        reply_markup=ReplyKeyboardRemove()
    )

# =========================
# Global error handler
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update):
        try:
            if update.message:
                await safe_send(context.bot, update.message.chat_id, "⚠️ حدث خطأ غير متوقع.", reply_markup=ReplyKeyboardRemove())
            elif update.callback_query:
                await safe_answer_callback(update.callback_query, "حدث خطأ غير متوقع", show_alert=True)
        except Exception:
            pass

# =========================
# Main
# =========================
def main():
    qm_term1.load_questions()
    qm_term2.load_questions()
    
    logger.info(f"Loaded {len(qm_term1.items)} for Term 1, and {len(qm_term2.items)} for Term 2.")
    
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(request).concurrent_updates(True).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("reload", reload_questions_command))
    
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^(ans_mcq:|ans_tf:|end_round)"))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_router))
    app.add_error_handler(error_handler)
    
    def start_cleanup_thread():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            cleanup_coro = cleanup_task()
            try:
                loop.run_until_complete(cleanup_coro)
            except RuntimeError:
                asyncio.create_task(cleanup_coro)
        except Exception as e:
            logger.error(f"Failed to start cleanup thread: {e}")
    
    import threading
    threading.Thread(target=start_cleanup_thread, daemon=True).start()
    
    logger.info("Starting bot...")
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES, poll_interval=0.5, close_loop=False)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        raise

if __name__ == "__main__":
    main()
