import os
import json
import random
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Set
from contextlib import contextmanager
from functools import lru_cache

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Logging متطور
# =========================
class CustomFormatter(logging.Formatter):
    """تنسيق مخصص للسجلات"""
    def format(self, record):
        # إضافة معرف المستخدم للسجلات إن وجد
        if hasattr(record, 'user_id'):
            record.user_id = f"[USER:{record.user_id}]"
        else:
            record.user_id = ""
        return super().format(record)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s %(user_id)s",
    level=logging.INFO
)
logger = logging.getLogger("telegram-quiz-bot")

# =========================
# Configuration Manager
# =========================
class Config:
    """مدير التكوين الديناميكي"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance
    
    def _load_config(self):
        """تحميل الإعدادات من environment variables"""
        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
        if not self.BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")
        
        # Admin IDs
        self.ADMIN_IDS: Set[int] = set()
        _admin_single = os.getenv("ADMIN_USER_ID", "").strip()
        if _admin_single.isdigit():
            self.ADMIN_IDS.add(int(_admin_single))
        
        _admin_raw = os.getenv("ADMIN_IDS", "").strip()
        if _admin_raw:
            for x in _admin_raw.split(","):
                x = x.strip()
                if x.isdigit():
                    self.ADMIN_IDS.add(int(x))
        
        # Maintenance mode
        MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "0").strip()
        self.MAINTENANCE_ON = MAINTENANCE_MODE in ("1", "true", "True", "YES", "yes", "on", "ON")
        
        # Bad words
        self.BAD_WORDS = set(w.strip() for w in os.getenv("BAD_WORDS", "").split(",") if w.strip())
        
        # Files
        self.QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "questions_from_word.json").strip()
        self.DB_FILE = os.getenv("DB_FILE", "data.db").strip()
        
        # Game settings
        self.ROUND_SIZE = int(os.getenv("ROUND_SIZE", "20"))
        self.TOP_N = int(os.getenv("TOP_N", "10"))
        
        # Chapters
        self.CHAPTERS = [
            "طبيعة العلم",
            "المخاليط والمحاليل",
            "حالات المادة",
            "الطاقة وتحولاتها",
            "أجهزة الجسم",
        ]
        
        # Bonus system
        self.BONUS_CONFIG = {
            3: ("🔥 سلسلة نار!", 1),
            5: ("🚀 صاروخي!", 2),
            10: ("👑 ملك الأسئلة!", 3)
        }
        
        # Rate limiting
        self.RATE_LIMIT_ATTEMPTS = int(os.getenv("RATE_LIMIT_ATTEMPTS", "10"))
        self.RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
        
        # Cache settings
        self.QUESTION_CACHE_TTL = int(os.getenv("QUESTION_CACHE_TTL", "300"))

config = Config()

# =========================
# Database Manager مع Connection Pooling
# =========================
class DatabaseManager:
    """مدير قاعدة البيانات مع connection pooling"""
    
    def __init__(self, db_file: str):
        self.db_file = db_file
        self._init_database()
    
    @contextmanager
    def get_connection(self):
        """الحصول على اتصال بقاعدة البيانات"""
        conn = sqlite3.connect(self.db_file, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()
    
    def _init_database(self):
        """تهيئة قاعدة البيانات وإنشاء الجداول"""
        with self.get_connection() as conn:
            cur = conn.cursor()
            
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
                    total_correct INTEGER DEFAULT 0,
                    total_questions INTEGER DEFAULT 0,
                    avg_accuracy REAL DEFAULT 0
                )
            """)
            
            # جدول طلبات الأسماء المعلقة
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_names (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    requested_at TEXT,
                    reviewed_by INTEGER,
                    reviewed_at TEXT,
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
                    duration_seconds INTEGER DEFAULT 0,
                    chapter_stats TEXT
                )
            """)
            
            # جدول الإحصائيات اليومية
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    total_rounds INTEGER DEFAULT 0,
                    total_players INTEGER DEFAULT 0,
                    total_correct INTEGER DEFAULT 0,
                    total_questions INTEGER DEFAULT 0
                )
            """)
            
            # إنشاء الفهارس لتحسين الأداء
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_total_points ON users(total_points)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_is_approved ON users(is_approved)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rounds_user_id ON rounds(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seen_questions_user ON seen_questions(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_names_status ON pending_names(status)")
            
            conn.commit()

db_manager = DatabaseManager(config.DB_FILE)

# =========================
# Question Manager مع Caching
# =========================
class QuestionManager:
    """مدير الأسئلة مع نظام cache"""
    
    def __init__(self, questions_file: str):
        self.questions_file = questions_file
        self._last_modified = 0
        self._cache = None
        self._buckets_cache = None
        self._load_questions()
    
    def _load_questions(self):
        """تحميل الأسئلة من الملف"""
        try:
            with open(self.questions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items", []) or data.get("questions", [])
            else:
                items = []
            
            # تعيين معرف فريد لكل سؤال إذا لم يكن موجوداً
            for i, item in enumerate(items):
                if "id" not in item:
                    item["id"] = f"q_{i}_{hash(json.dumps(item, sort_keys=True))}"
            
            self._cache = items
            self._buckets_cache = self._build_chapter_buckets(items)
            self._last_modified = os.path.getmtime(self.questions_file)
            logger.info(f"Loaded {len(items)} questions")
            
        except Exception as e:
            logger.error(f"Failed to load questions: {e}")
            self._cache = []
            self._buckets_cache = {}
    
    def _build_chapter_buckets(self, items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """بناء مجوعات الفصول"""
        buckets = {c: [] for c in config.CHAPTERS}
        for item in items:
            chapter = self._classify_chapter(item)
            item["_chapter"] = chapter
            buckets[chapter].append(item)
        return buckets
    
    def _classify_chapter(self, item: Dict[str, Any]) -> str:
        """تصنيف السؤال إلى الفصل المناسب"""
        CHAPTER_KEYWORDS = {
            "طبيعة العلم": [
                "الطريقه العلميه", "فرضيه", "متغير", "ثابت", "ملاحظه", "تجربه", "استنتاج", "تواصل",
                "علم الاثار", "الرادار"
            ],
            "المخاليط والمحاليل": [
                "مخلوط", "محلول", "مذيب", "مذاب", "تركيز", "ذائبيه", "حمض", "قاعده", "تعادل", "ترسب", "ph",
                "ايوني", "تساهمي"
            ],
            "حالات المادة": [
                "صلب", "سائل", "غاز", "بلازما", "انصهار", "تبخر", "تكاثف", "تجمد", "تسامي", "ضغط", "كثافه",
                "توتر سطحي", "لزوج"
            ],
            "الطاقة وتحولاتها": [
                "طاقه", "حركيه", "وضع", "كامنه", "اشعاعيه", "كيميائيه", "كهربائيه", "نوويه",
                "توربين", "مولد", "خليه شمسيه", "حفظ الطاقه"
            ],
            "أجهزة الجسم": [
                "دم", "قلب", "شريان", "وريد", "شعيره", "مناعه", "اجسام مضاده", "مولدات الضد",
                "ايدز", "سكري", "هضم", "معده", "امعاء", "رئه", "تنفس", "كليه", "بول"
            ],
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
    
    def get_questions(self) -> List[Dict[str, Any]]:
        """الحصول على الأسئلة مع التحقق من التعديلات"""
        try:
            current_modified = os.path.getmtime(self.questions_file)
            if current_modified > self._last_modified or not self._cache:
                logger.info("Questions file modified, reloading...")
                self._load_questions()
        except:
            pass
        
        return self._cache or []
    
    def get_buckets(self) -> Dict[str, List[Dict[str, Any]]]:
        """الحصول على مجوعات الفصول"""
        self.get_questions()  # تحديث إذا لزم الأمر
        return self._buckets_cache or {}
    
    def pick_round_questions(self, user_id: int) -> List[Dict[str, Any]]:
        """اختيار أسئلة الجولة للمستخدم"""
        buckets = self.get_buckets()
        if not buckets:
            return []
        
        # الحصول على الأسئلة المشاهدة للمستخدم
        seen_questions = self._get_seen_questions(user_id)
        
        target_per_chapter = config.ROUND_SIZE // len(config.CHAPTERS)
        chosen = []
        leftovers = []
        
        for chapter in config.CHAPTERS:
            pool = buckets.get(chapter, [])
            unseen = [q for q in pool if q.get("id") not in seen_questions]
            random.shuffle(unseen)
            
            take = min(target_per_chapter, len(unseen))
            chosen.extend(unseen[:take])
            leftovers.extend(unseen[take:])
        
        # إذا كان عدد الأسئلة المختارة أقل من المطلوب
        if len(chosen) < config.ROUND_SIZE:
            random.shuffle(leftovers)
            need = config.ROUND_SIZE - len(chosen)
            chosen.extend(leftovers[:need])
        
        # إذا ما زال العدد غير كافي، نأخذ أي أسئلة
        if len(chosen) < config.ROUND_SIZE:
            all_items = []
            for chapter in config.CHAPTERS:
                all_items.extend(buckets.get(chapter, []))
            random.shuffle(all_items)
            need = config.ROUND_SIZE - len(chosen)
            for item in all_items:
                if item.get("id") not in seen_questions and item not in chosen:
                    chosen.append(item)
                    need -= 1
                    if need <= 0:
                        break
        
        # إزالة التكرارات
        seen_ids = set()
        unique_chosen = []
        for q in chosen:
            qid = q.get("id")
            if qid and qid not in seen_ids:
                unique_chosen.append(q)
                seen_ids.add(qid)
        
        random.shuffle(unique_chosen)
        return unique_chosen[:config.ROUND_SIZE]
    
    def _get_seen_questions(self, user_id: int) -> Set[str]:
        """الحصول على مجموعة الأسئلة المشاهدة للمستخدم"""
        with db_manager.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT qid FROM seen_questions WHERE user_id=?", (user_id,))
            rows = cur.fetchall()
            return {row["qid"] for row in rows}

question_manager = QuestionManager(config.QUESTIONS_FILE)

# =========================
# Rate Limiter
# =========================
class RateLimiter:
    """محدد معدل الطلبات"""
    
    def __init__(self):
        self.user_attempts = {}
    
    def check_rate_limit(self, user_id: int, max_attempts: int = None, window_seconds: int = None) -> bool:
        """التحقق من معدل الطلبات"""
        if max_attempts is None:
            max_attempts = config.RATE_LIMIT_ATTEMPTS
        if window_seconds is None:
            window_seconds = config.RATE_LIMIT_WINDOW
        
        now = datetime.now()
        
        if user_id not in self.user_attempts:
            self.user_attempts[user_id] = []
        
        # إزالة المحاولات القديمة
        cutoff_time = now - timedelta(seconds=window_seconds)
        self.user_attempts[user_id] = [
            attempt for attempt in self.user_attempts[user_id]
            if attempt > cutoff_time
        ]
        
        if len(self.user_attempts[user_id]) >= max_attempts:
            return False
        
        self.user_attempts[user_id].append(now)
        return True
    
    def get_wait_time(self, user_id: int) -> int:
        """الحصول على وقت الانتظار المتبقي"""
        if user_id not in self.user_attempts or not self.user_attempts[user_id]:
            return 0
        
        oldest = min(self.user_attempts[user_id])
        wait_seconds = config.RATE_LIMIT_WINDOW - (datetime.now() - oldest).seconds
        return max(0, wait_seconds)

rate_limiter = RateLimiter()

# =========================
# Arabic normalization helpers
# =========================
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")

def normalize_arabic(text: str) -> str:
    """تطبيع النص العربي"""
    if not text:
        return ""
    text = text.strip()
    text = _ARABIC_DIACRITICS.sub("", text)  # remove tashkeel/tatweel
    # keep arabic/digits/spaces; replace other with space
    text = re.sub(r"[^\u0600-\u06FF0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # normalize alifs
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    return text

def is_arabic_only_name(name: str) -> bool:
    """التحقق إذا كان الاسم عربي فقط"""
    if not name:
        return False
    name = name.strip()
    if re.search(r"[A-Za-z]", name):
        return False
    return bool(re.fullmatch(r"[\u0600-\u06FF\s]+", name))

def looks_like_real_name(name: str) -> bool:
    """
    قواعد بسيطة عشان الاسم يكون 'حقيقي واضح':
    - عربي فقط
    - كلمتين على الأقل
    - طول مناسب
    - بدون كلمات سيئة من BAD_WORDS
    """
    name = name.strip()
    
    # تحقق من الطول أولاً (أسرع)
    if len(name) < 6 or len(name) > 30:
        return False
    
    # تحقق من الحروف العربية فقط
    if not is_arabic_only_name(name):
        return False
    
    # تحقق من عدد الكلمات
    parts = [p for p in name.split() if len(p) > 1]  # تجاهل الأحرف المنفردة
    if len(parts) < 2:
        return False
    
    # تحقق من الكلمات المحظورة
    n_norm = normalize_arabic(name.lower())
    for bw in config.BAD_WORDS:
        bw_norm = normalize_arabic(bw.lower())
        if bw_norm and bw_norm in n_norm:
            return False
    
    # تحقق من الأسماء الواضحة
    # مثل: لا تكون كل الحروف متشابهة
    if len(set(name.replace(" ", ""))) < 3:
        return False
    
    return True

# =========================
# Database Operations
# =========================
def upsert_user(user_id: int):
    """إضافة أو تحديث المستخدم"""
    now = datetime.utcnow().isoformat()
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if cur.fetchone():
            cur.execute("UPDATE users SET updated_at=? WHERE user_id=?", (now, user_id))
        else:
            cur.execute(
                "INSERT INTO users(user_id, created_at, updated_at) VALUES (?,?,?)",
                (user_id, now, now)
            )
        conn.commit()

def set_pending_name(user_id: int, full_name: str):
    """تعيين اسم معلق للموافقة"""
    now = datetime.utcnow().isoformat()
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pending_names(user_id, full_name, requested_at, status)
            VALUES(?,?,?, 'pending')
            ON CONFLICT(user_id) DO UPDATE 
            SET full_name=excluded.full_name, 
                requested_at=excluded.requested_at,
                status='pending'
        """, (user_id, full_name, now))
        conn.commit()

def approve_name(user_id: int, admin_id: int = None):
    """اعتماد اسم المستخدم"""
    now = datetime.utcnow().isoformat()
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT full_name FROM pending_names WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            full_name = row["full_name"]
            cur.execute("""
                UPDATE users SET full_name=?, is_approved=1, updated_at=?
                WHERE user_id=?
            """, (full_name, now, user_id))
            
            cur.execute("""
                UPDATE pending_names 
                SET status='approved', reviewed_by=?, reviewed_at=?
                WHERE user_id=?
            """, (admin_id, now, user_id))
            
            conn.commit()

def reject_name(user_id: int, admin_id: int = None):
    """رفض اسم المستخدم"""
    now = datetime.utcnow().isoformat()
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE pending_names 
            SET status='rejected', reviewed_by=?, reviewed_at=?
            WHERE user_id=?
        """, (admin_id, now, user_id))
        conn.commit()

def get_user(user_id: int) -> Dict[str, Any]:
    """الحصول على بيانات المستخدم"""
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else {}

def get_user_stats(user_id: int) -> Dict[str, Any]:
    """الحصول على إحصائيات مفصلة للمستخدم"""
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                u.*,
                COUNT(DISTINCT r.round_id) as total_rounds,
                COALESCE(AVG(r.correct * 100.0 / r.total), 0) as avg_accuracy,
                SUM(r.correct) as total_correct_all,
                SUM(r.total) as total_questions_all
            FROM users u
            LEFT JOIN rounds r ON u.user_id = r.user_id
            WHERE u.user_id = ?
            GROUP BY u.user_id
        """, (user_id,))
        row = cur.fetchone()
        return dict(row) if row else {}

def get_pending_list() -> List[Dict[str, Any]]:
    """الحصول على قائمة الأسماء المعلقة"""
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.*, u.created_at as user_created
            FROM pending_names p
            LEFT JOIN users u ON p.user_id = u.user_id
            WHERE p.status = 'pending'
            ORDER BY p.requested_at ASC
        """)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def mark_seen(user_id: int, qid: str):
    """تسجيل السؤال كمشاهد"""
    now = datetime.utcnow().isoformat()
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO seen_questions(user_id, qid, seen_at)
            VALUES(?,?,?)
        """, (user_id, qid, now))
        conn.commit()

def save_round_result(user_id: int, score: int, bonus: int, correct: int, 
                     total: int, duration: int, chapter_stats: Dict[str, Any]):
    """حفظ نتيجة الجولة"""
    now = datetime.utcnow().isoformat()
    chapter_stats_json = json.dumps(chapter_stats, ensure_ascii=False)
    
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        
        # حفظ الجولة
        cur.execute("""
            INSERT INTO rounds(user_id, started_at, finished_at, score, bonus, 
                             correct, total, duration_seconds, chapter_stats)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (user_id, now, now, score, bonus, correct, total, duration, chapter_stats_json))
        
        # تحديث إحصائيات المستخدم
        total_points = score + bonus
        cur.execute("""
            UPDATE users 
            SET total_points = total_points + ?,
                rounds_played = rounds_played + 1,
                total_correct = total_correct + ?,
                total_questions = total_questions + ?,
                avg_accuracy = CASE 
                    WHEN total_questions + ? > 0 
                    THEN (total_correct + ?) * 100.0 / (total_questions + ?)
                    ELSE 0
                END,
                best_round_score = MAX(best_round_score, ?),
                updated_at = ?
            WHERE user_id = ?
        """, (total_points, correct, total, total, correct, total, total_points, now, user_id))
        
        # تحديث الإحصائيات اليومية
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cur.execute("""
            INSERT INTO daily_stats(date, total_rounds, total_players, total_correct, total_questions)
            VALUES(?, 1, 0, ?, ?)
            ON CONFLICT(date) DO UPDATE 
            SET total_rounds = total_rounds + 1,
                total_correct = total_correct + excluded.total_correct,
                total_questions = total_questions + excluded.total_questions
        """, (today, correct, total))
        
        conn.commit()

def get_leaderboard(top_n: int = None) -> List[Dict[str, Any]]:
    """الحصول على لوحة المتصدرين"""
    if top_n is None:
        top_n = config.TOP_N
    
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                full_name, 
                total_points, 
                best_round_score, 
                rounds_played,
                avg_accuracy,
                total_correct,
                total_questions
            FROM users
            WHERE is_approved=1 
                AND full_name IS NOT NULL 
                AND TRIM(full_name) <> ''
            ORDER BY total_points DESC, avg_accuracy DESC, rounds_played DESC
            LIMIT ?
        """, (top_n,))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def get_daily_stats(days: int = 7) -> List[Dict[str, Any]]:
    """الحصول على الإحصائيات اليومية"""
    with db_manager.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT date, total_rounds, total_correct, total_questions,
                   total_correct * 100.0 / total_questions as accuracy
            FROM daily_stats
            WHERE date >= date('now', ?)
            ORDER BY date DESC
        """, (f"-{days} days",))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

# =========================
# Maintenance guard
# =========================
def is_admin(user_id: int) -> bool:
    """التحقق إذا كان المستخدم مشرف"""
    return user_id in config.ADMIN_IDS

async def maintenance_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """منع الوصول في وضع الصيانة"""
    if not config.MAINTENANCE_ON:
        return False
    user_id = update.effective_user.id if update.effective_user else 0
    if is_admin(user_id):
        return False

    msg = "🛠️ البوت تحت صيانة حالياً… ارجعوا بعدين 🌿\n\n" \
          "📅 فريق التطوير يعمل على تحسين تجربتك!"
    if update.message:
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
    elif update.callback_query:
        await update.callback_query.answer("البوت تحت صيانة", show_alert=True)
        try:
            await update.callback_query.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass
    return True

# =========================
# UI helpers (INLINE ONLY)
# =========================
def main_menu_keyboard(user: Dict[str, Any]) -> InlineKeyboardMarkup:
    """لوحة المنيو الرئيسية"""
    approved = bool(user.get("is_approved", 0))
    name = user.get("full_name") or ""
    
    if approved:
        name_status = f"✅ {name[:15]}" if name else "✅ معتمد"
    elif name:
        name_status = "⏳ بانتظار الموافقة"
    else:
        name_status = "➕ سجّل اسمك"
    
    kb = [
        [InlineKeyboardButton("🎮 ابدأ جولة جديدة", callback_data="play_round")],
        [InlineKeyboardButton("🏆 لوحة التميز", callback_data="leaderboard")],
        [InlineKeyboardButton("📊 إحصائياتي التفصيلية", callback_data="my_stats")],
        [InlineKeyboardButton("📈 الإحصائيات العامة", callback_data="global_stats")],
        [InlineKeyboardButton(name_status, callback_data="set_name")],
    ]
    
    if is_admin(user.get("user_id", 0)):
        kb.append([InlineKeyboardButton("👑 لوحة الأدمن", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(kb)

def answer_keyboard_mcq(options: Dict[str, str]) -> InlineKeyboardMarkup:
    """لوحة إجابة الأسئلة المتعددة"""
    rows = []
    for key in ["A", "B", "C", "D"]:
        if key in options:
            text = f"{key}) {options[key][:30]}"
            rows.append([InlineKeyboardButton(text, callback_data=f"ans_mcq:{key}")])
    rows.append([InlineKeyboardButton("⛔️ إنهاء الجولة", callback_data="end_round")])
    return InlineKeyboardMarkup(rows)

def answer_keyboard_tf() -> InlineKeyboardMarkup:
    """لوحة إجابة الصح/خطأ"""
    kb = [
        [
            InlineKeyboardButton("✅ صح", callback_data="ans_tf:true"),
            InlineKeyboardButton("❌ خطأ", callback_data="ans_tf:false"),
        ],
        [InlineKeyboardButton("⛔️ إنهاء الجولة", callback_data="end_round")]
    ]
    return InlineKeyboardMarkup(kb)

def admin_pending_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """لوحة إدارة الطلبات للمشرفين"""
    kb = [
        [
            InlineKeyboardButton("✅ اعتماد", callback_data=f"admin_approve:{user_id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject:{user_id}")
        ],
        [
            InlineKeyboardButton("👀 عرض الملف", callback_data=f"admin_view:{user_id}"),
            InlineKeyboardButton("💬 إرسال رسالة", callback_data=f"admin_msg:{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(kb)

def admin_main_keyboard() -> InlineKeyboardMarkup:
    """اللوحة الرئيسية للمشرفين"""
    kb = [
        [InlineKeyboardButton("📋 الطلبات المعلقة", callback_data="admin_pending")],
        [InlineKeyboardButton("📊 إحصائيات النظام", callback_data="admin_stats")],
        [InlineKeyboardButton("🔄 إعادة تحميل الأسئلة", callback_data="admin_reload")],
        [InlineKeyboardButton("⚙️ إعدادات الصيانة", callback_data="admin_maintenance")],
        [InlineKeyboardButton("📤 نسخة احتياطية", callback_data="admin_backup")],
    ]
    return InlineKeyboardMarkup(kb)

# =========================
# Helpers
# =========================
def parse_tf_answer(raw: Any) -> Optional[bool]:
    """تحويل الإجابة إلى قيمة منطقية"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    s_norm = normalize_arabic(s)
    if s in ("true", "1") or s_norm in ("صح", "صحيح", "ص", "نعم", "ايوه"):
        return True
    if s in ("false", "0") or s_norm in ("خطا", "خطأ", "غلط", "لا", "لاء"):
        return False
    return None

# =========================
# Motivation phrases - المحافظة على العبارات الحالية وإضافة المزيد
# =========================
MOTIVATION_CORRECT = [
    "🔥 بطل! كمل كذا!",
    "👏 ممتاز!",
    "💪 رهيب!",
    "✅ صح عليك!",
    "🌟 كفو!",
    "🚀 يا سلام عليك!",
    "🎯 إصابة مباشرة!",
    "💫 عبقرية!",
    "🏆 مستواك عالمي!",
    "✨ هذا مستواك الحقيقي!",
]

MOTIVATION_WRONG = [
    "😅 بسيطة! الجاية صح إن شاء الله.",
    "👀 ركّز شوي، تقدر!",
    "💡 مو مشكلة، تعلمنا!",
    "🔥 لا توقف! كمل!",
    "😎 قدها وقدود!",
    "🌱 كل خطوة بتعلمك شيء جديد!",
    "📚 راجع المعلومة وراح تتذكرها!",
    "💪 القوة في الاستمرار!",
    "🌟 الخطأ طريق التعلم!",
    "🚀 انت قادر على التحدي!",
]

MOTIVATION_BONUS = [
    "🏅 بونص! سلسلة نار 🔥",
    "🎯 ممتاز! خذت بونص!",
    "💥 كملت سلسلة الصح!",
    "⚡️ توهج مستمر! +",
    "🌟 مهاراتك في الذروة!",
    "🚀 صاعد للأعلى!",
]

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /start"""
    if await maintenance_block(update, context):
        return

    user_id = update.effective_user.id
    upsert_user(user_id)
    user = get_user(user_id)
    
    # تسجيل حدث
    logger.info(f"User {user_id} started the bot", extra={'user_id': user_id})

    msg = (
        "✨ **أهلاً وسهلاً!** ✨\n\n"
        "🏆 **أنا بوت المسابقة الذكي**\n"
        "• كل جولة = 20 سؤال موزعة على فصول المنهج 📚\n"
        "• نظام بونص متطور: كل 3 إجابات صحيحة متتالية = +1 نقطة 🎯\n"
        "• لوحة التميز Top 10 للطلاب المتميزين 🌟\n"
        "• إحصائيات مفصلة لكل فصل 📊\n\n"
        "**🎮 هيا نبدأ التحدي!**"
    )

    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    await update.message.reply_text("🔍 **اختر من القائمة:**", reply_markup=main_menu_keyboard(user))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /admin"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(
            "🔒 **عذراً، هذا الأمر متاح للمشرفين فقط.**",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    pending_count = len(get_pending_list())
    daily_stats = get_daily_stats(1)
    today_stats = daily_stats[0] if daily_stats else {}
    
    msg = (
        f"👑 **لوحة التحكم الإدارية**\n\n"
        f"📊 **إحصائيات اليوم:**\n"
        f"• الجولات: {today_stats.get('total_rounds', 0)}\n"
        f"• الدقة: {today_stats.get('accuracy', 0):.1f}%\n\n"
        f"⚙️ **حالة النظام:**\n"
        f"• الصيانة: {'✅ نشطة' if config.MAINTENANCE_ON else '❌ معطلة'}\n"
        f"• طلبات الأسماء: {pending_count}\n\n"
        f"🔧 **الأدوات المتاحة:**"
    )
    
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_main_keyboard())

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج callback المنيو"""
    if await maintenance_block(update, context):
        return

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    upsert_user(user_id)
    user = get_user(user_id)
    data = query.data

    if data == "set_name":
        context.user_data["awaiting_name"] = True
        context.user_data["awaiting_term_answer"] = False
        await query.message.reply_text(
            "✍️ **تسجيل الاسم الشخصي**\n\n"
            "📋 **الشروط المطلوبة:**\n"
            "• الاسم باللغة العربية فقط 🇸🇦\n"
            "• كلمتين على الأقل (الاسم الكامل)\n"
            "• واضح ومحترم ومناسب\n"
            "• الطول بين 6 و30 حرفاً\n\n"
            "**مثال:** محمد أحمد علي\n\n"
            "📍 **اكتب اسمك الآن:**",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if data == "leaderboard":
        lb = get_leaderboard()
        if not lb:
            text = "🏆 **لوحة التميز**\n\n" \
                   "لم يشارك أي لاعب بعد! كن أول المتميزين! 🌟"
        else:
            lines = ["🏆 **أفضل 10 لاعبين**\n"]
            emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            
            for i, row in enumerate(lb, start=1):
                emoji = emojis[i-1] if i <= 10 else "🎖️"
                accuracy = row.get('avg_accuracy', 0)
                name = row['full_name'][:15] + "..." if len(row['full_name']) > 15 else row['full_name']
                lines.append(
                    f"{emoji} **{name}**\n"
                    f"   ⭐️ {row['total_points']} نقطة | 📊 {accuracy:.1f}% دقة"
                )
            text = "\n".join(lines)

        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await query.message.reply_text("🔍 **اختر من القائمة:**", reply_markup=main_menu_keyboard(user))
        return

    if data == "my_stats":
        stats = get_user_stats(user_id)
        name = stats.get("full_name") or "لم يتم التسجيل"
        approved = "✅ معتمد" if stats.get("is_approved", 0) else "⏳ قيد المراجعة"
        total = stats.get("total_points", 0)
        rounds = stats.get("rounds_played", 0)
        best = stats.get("best_round_score", 0)
        accuracy = stats.get("avg_accuracy", 0)
        total_correct = stats.get("total_correct_all", 0)
        total_questions = stats.get("total_questions_all", 0)
        
        # حساب المستوى
        level = (total // 100) + 1
        
        text = (
            f"📊 **الإحصائيات الشخصية**\n\n"
            f"👤 **الاسم:** {name} {approved}\n"
            f"📈 **المستوى:** {level}\n\n"
            f"🏆 **الإنجازات:**\n"
            f"• النقاط الإجمالية: ⭐️ {total}\n"
            f"• عدد الجولات: 🎮 {rounds}\n"
            f"• أفضل جولة: 🥇 {best}\n\n"
            f"🎯 **الدقة:**\n"
            f"• الإجابات الصحيحة: ✅ {total_correct}/{total_questions}\n"
            f"• نسبة الدقة: 📊 {accuracy:.1f}%\n\n"
            f"🔥 **استمر في التحدي!**"
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await query.message.reply_text("🔍 **اختر من القائمة:**", reply_markup=main_menu_keyboard(user))
        return

    if data == "global_stats":
        daily_stats = get_daily_stats(7)
        
        if not daily_stats:
            text = "📈 **الإحصائيات العامة**\n\nلم يتم تسجيل أي نشاط بعد."
        else:
            lines = ["📈 **إحصائيات الأسبوع**\n"]
            total_rounds = sum(s['total_rounds'] for s in daily_stats)
            total_correct = sum(s['total_correct'] for s in daily_stats)
            total_questions = sum(s['total_questions'] for s in daily_stats)
            avg_accuracy = (total_correct / total_questions * 100) if total_questions > 0 else 0
            
            lines.append(f"• إجمالي الجولات: 🎮 {total_rounds}")
            lines.append(f"• إجمالي الأسئلة: 📚 {total_questions}")
            lines.append(f"• متوسط الدقة: 🎯 {avg_accuracy:.1f}%\n")
            
            lines.append("📅 **آخر 3 أيام:**")
            for stat in daily_stats[:3]:
                date_obj = datetime.strptime(stat['date'], "%Y-%m-%d")
                day_name = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"][date_obj.weekday()]
                lines.append(f"• {day_name}: {stat['total_rounds']} جولة ({stat['accuracy']:.1f}%)")
            
            text = "\n".join(lines)
        
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await query.message.reply_text("🔍 **اختر من القائمة:**", reply_markup=main_menu_keyboard(user))
        return

    if data == "play_round":
        # التحقق من rate limiting
        if not rate_limiter.check_rate_limit(user_id):
            wait_time = rate_limiter.get_wait_time(user_id)
            await query.message.reply_text(
                f"⏳ **تم تجاوز الحد المسموح**\n\n"
                f"يرجى الانتظار {wait_time} ثانية قبل بدء جولة جديدة.",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        
        await start_round(query, context)
        return

    if data == "admin_panel":
        if is_admin(user_id):
            await query.message.reply_text(
                "👑 **لوحة المشرفين**\n\n"
                "اختر الإدارة المناسبة:",
                reply_markup=admin_main_keyboard()
            )
        return

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج callback المشرفين"""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    
    if not is_admin(admin_id):
        await query.message.reply_text("❌ صلاحية غير كافية.", reply_markup=ReplyKeyboardRemove())
        return

    data = query.data
    
    if data.startswith("admin_approve:"):
        uid = int(data.split(":")[1])
        approve_name(uid, admin_id)
        
        # إرسال إشعار للمستخدم
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="🎉 **مبروك! تم اعتماد اسمك**\n\n"
                     "✅ الآن يمكنك الظهور في لوحة المتصدرين!\n"
                     "🏆 استمر في التحدي لجمع النقاط!",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception:
            pass
        
        await query.message.reply_text(f"✅ تم اعتماد المستخدم {uid}", reply_markup=ReplyKeyboardRemove())
        return

    if data.startswith("admin_reject:"):
        uid = int(data.split(":")[1])
        reject_name(uid, admin_id)
        
        # إرسال إشعار للمستخدم
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="📝 **ملاحظة على اسمك**\n\n"
                     "❌ لم يتم اعتماد الاسم المرسل.\n"
                     "📋 يرجى التأكد من:\n"
                     "• الكتابة بالعربية فقط\n"
                     "• استخدام الاسم الكامل\n"
                     "• الابتعاد عن الأسماء غير الواضحة\n\n"
                     "🔁 أرسل اسمك مرة أخرى عبر القائمة الرئيسية.",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception:
            pass
        
        await query.message.reply_text(f"❌ تم رفض الاسم للمستخدم {uid}", reply_markup=ReplyKeyboardRemove())
        return

    if data == "admin_pending":
        pending = get_pending_list()
        if not pending:
            await query.message.reply_text("✅ لا توجد طلبات معلقة.", reply_markup=ReplyKeyboardRemove())
        else:
            for p in pending[:10]:  # عرض أول 10 فقط
                uid = int(p["user_id"])
                nm = p["full_name"]
                date = datetime.fromisoformat(p["requested_at"]).strftime("%Y-%m-%d %H:%M")
                await query.message.reply_text(
                    f"📝 **طلب تسجيل اسم**\n\n"
                    f"👤 المستخدم: `{uid}`\n"
                    f"📛 الاسم: {nm}\n"
                    f"📅 التاريخ: {date}",
                    parse_mode="Markdown",
                    reply_markup=admin_pending_keyboard(uid)
                )
        return

    if data == "admin_stats":
        # إحصائيات النظام
        with db_manager.get_connection() as conn:
            cur = conn.cursor()
            
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM users WHERE is_approved=1")
            approved_users = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM rounds")
            total_rounds = cur.fetchone()[0]
            
            cur.execute("SELECT SUM(total_points) FROM users")
            total_points = cur.fetchone()[0] or 0
            
            cur.execute("SELECT COUNT(*) FROM seen_questions")
            total_seen = cur.fetchone()[0]
        
        stats_text = (
            f"📊 **إحصائيات النظام**\n\n"
            f"👥 **المستخدمون:**\n"
            f"• الإجمالي: {total_users}\n"
            f"• المعتمدون: {approved_users}\n\n"
            f"🎮 **النشاط:**\n"
            f"• الجولات: {total_rounds}\n"
            f"• النقاط: {total_points:,}\n"
            f"• الأسئلة المشاهدة: {total_seen:,}\n\n"
            f"⚙️ **التكوين:**\n"
            f"• الأسئلة: {len(question_manager.get_questions())}\n"
            f"• الصيانة: {'✅' if config.MAINTENANCE_ON else '❌'}"
        )
        
        await query.message.reply_text(stats_text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return

    if data == "admin_reload":
        # إعادة تحميل الأسئلة
        question_manager._load_questions()
        await query.message.reply_text(
            "🔄 **تم إعادة تحميل الأسئلة بنجاح**\n\n"
            f"• عدد الأسئلة: {len(question_manager.get_questions())}\n"
            f"• آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            reply_markup=ReplyKeyboardRemove()
        )
        return

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /pending"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.", reply_markup=ReplyKeyboardRemove())
        return

    pending = get_pending_list()
    if not pending:
        await update.message.reply_text("✅ لا توجد طلبات معلّقة", reply_markup=ReplyKeyboardRemove())
        return

    await update.message.reply_text(
        f"📋 **الطلبات المعلقة ({len(pending)})**\n\n"
        f"استخدم لوحة الأدمن للتعامل مع الطلبات.",
        parse_mode="Markdown",
        reply_markup=admin_main_keyboard()
    )

async def start_round(query, context: ContextTypes.DEFAULT_TYPE):
    """بدء جولة جديدة"""
    user_id = query.from_user.id
    upsert_user(user_id)
    
    # تسجيل وقت بدء الجولة
    context.user_data["round_start_time"] = datetime.now()
    
    # جلب الأسئلة
    round_questions = question_manager.pick_round_questions(user_id)
    
    if not round_questions or len(round_questions) < 5:
        await query.message.reply_text(
            "⚠️ **لا توجد أسئلة كافية للبدء**\n\n"
            "يرجى التواصل مع المشرف لإضافة المزيد من الأسئلة.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
    
    # إعداد بيانات الجولة
    context.user_data.update({
        "round_questions": round_questions,
        "round_index": 0,
        "round_score": 0,
        "round_bonus": 0,
        "round_correct": 0,
        "round_streak": 0,
        "round_chapter_correct": {c: 0 for c in config.CHAPTERS},
        "round_chapter_total": {c: 0 for c in config.CHAPTERS},
        "awaiting_term_answer": False,
        "awaiting_name": False
    })
    
    # رسالة البداية
    welcome_msg = (
        "🎮 **بدأت الجولة!**\n\n"
        f"• عدد الأسئلة: {len(round_questions)}\n"
        f"• الفصول: {', '.join(config.CHAPTERS)}\n"
        f"• نظام البونص: نشط 🎯\n\n"
        "**🔥 استعد للتحدي!**"
    )
    
    await query.message.reply_text(welcome_msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    await send_next_question(query.message.chat_id, user_id, context)

async def send_next_question(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """إرسال السؤال التالي"""
    idx = context.user_data.get("round_index", 0)
    qs = context.user_data.get("round_questions", [])
    
    if idx >= len(qs):
        await finish_round(chat_id, user_id, context, ended_by_user=False)
        return
    
    q = qs[idx]
    context.user_data["current_q"] = q
    
    chap = q.get("_chapter", "—")
    context.user_data["round_chapter_total"][chap] = context.user_data["round_chapter_total"].get(chap, 0) + 1
    
    # شريط التقدم
    progress = f"📊 {idx+1}/{len(qs)}"
    
    t = q.get("type")
    header = f"{progress} | الفصل: {chap}\n\n"
    
    if t == "mcq":
        question = (q.get("question") or "").strip()
        options = q.get("options") or {}
        text = header + f"❓ **{question}**"
        await context.bot.send_message(chat_id=chat_id, text=text, 
                                     parse_mode="Markdown", reply_markup=answer_keyboard_mcq(options))
        return
    
    if t == "tf":
        st = (q.get("statement") or "").strip()
        text = header + f"✅/❌ **{st}**"
        await context.bot.send_message(chat_id=chat_id, text=text, 
                                     parse_mode="Markdown", reply_markup=answer_keyboard_tf())
        return
    
    if t == "term":
        definition = (q.get("definition") or "").strip()
        text = header + "🧠 **اكتب المصطلح المناسب:**\n\n" + f"📘 *{definition}*\n\n✍️ اكتب الإجابة:"
        context.user_data["awaiting_term_answer"] = True
        await context.bot.send_message(chat_id=chat_id, text=text, 
                                     parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return
    
    # نوع غير معروف - التخطي
    context.user_data["round_index"] = idx + 1
    await send_next_question(chat_id, user_id, context)

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج إجابات callback"""
    if await maintenance_block(update, context):
        return

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if "round_questions" not in context.user_data:
        await query.message.reply_text(
            "🔍 **ابدأ جولة جديدة من القائمة**\n\n"
            "استخدم /start للعودة إلى القائمة الرئيسية.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    q = context.user_data.get("current_q")
    if not q:
        await query.message.reply_text("⚠️ خطأ في تحميل السؤال.", reply_markup=ReplyKeyboardRemove())
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
        correct_bool = parse_tf_answer(q.get("answer")) or parse_tf_answer(q.get("correct"))
        if correct_bool is None:
            correct_bool = False
        is_correct = (picked == ("true" if correct_bool else "false"))
    
    else:
        await query.message.reply_text("⚠️ إجابة غير متوقعة.", reply_markup=ReplyKeyboardRemove())
        return
    
    # إزالة أزرار السؤال السابق
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    
    await apply_answer_result(chat_id, user_id, context, is_correct)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """توجيه الرسائل النصية"""
    if await maintenance_block(update, context):
        return

    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    text = update.message.text.strip()

    # 1) تسجيل اسم
    if context.user_data.get("awaiting_name"):
        if not looks_like_real_name(text):
            await update.message.reply_text(
                "❌ **الاسم غير مقبول**\n\n"
                "📋 **يرجى التأكد من:**\n"
                "• الكتابة بالعربية فقط\n"
                "• كلمتين على الأقل\n"
                "• الطول بين 6 و30 حرفاً\n"
                "• الاسم واضح ومحترم\n\n"
                "🔁 **أعد إرسال الاسم:**",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
            return

        upsert_user(user_id)
        set_pending_name(user_id, text)
        context.user_data["awaiting_name"] = False

        await update.message.reply_text(
            "✅ **تم استلام الاسم بنجاح**\n\n"
            "⏳ **جاري المراجعة من قبل المشرفين**\n\n"
            "🎮 **يمكنك البدء باللعب الآن!**\n"
            "• ستظهر نتيجتك في لوحة المتصدرين بعد الاعتماد\n"
            "• استمر في جمع النقاط!",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )

        # إشعار المشرفين
        if config.ADMIN_IDS:
            notification = (
                f"📝 **طلب تسجيل اسم جديد**\n\n"
                f"👤 المستخدم: `{user_id}`\n"
                f"📛 الاسم: {text}\n"
                f"📅 الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            for admin_id in config.ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=notification,
                        parse_mode="Markdown",
                        reply_markup=admin_pending_keyboard(user_id)
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify admin {admin_id}: {e}")
        return

    # 2) إجابة مصطلح
    if context.user_data.get("awaiting_term_answer"):
        if "round_questions" not in context.user_data:
            context.user_data["awaiting_term_answer"] = False
            return

        q = context.user_data.get("current_q")
        if not q or q.get("type") != "term":
            context.user_data["awaiting_term_answer"] = False
            return

        user_answer = normalize_arabic(text)
        correct_term = normalize_arabic(q.get("term") or "")

        def strip_al(s: str) -> str:
            return re.sub(r"^ال", "", s)

        is_correct = (user_answer == correct_term) or (strip_al(user_answer) == strip_al(correct_term))
        context.user_data["awaiting_term_answer"] = False
        
        await apply_answer_result(chat_id, user_id, context, is_correct)
        return

    # 3) رسالة عادية
    await update.message.reply_text(
        "🔍 **استخدم القائمة للتنقل**\n\n"
        "اضغط /start للعودة إلى القائمة الرئيسية.",
        reply_markup=ReplyKeyboardRemove()
    )

async def apply_answer_result(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, is_correct: bool):
    """تطبيق نتيجة الإجابة"""
    idx = int(context.user_data.get("round_index", 0))
    q = context.user_data.get("current_q") or {}
    chap = q.get("_chapter", "—")
    
    bonus_hit = False
    bonus_message = ""
    
    if is_correct:
        # زيادة النقاط
        context.user_data["round_score"] = int(context.user_data.get("round_score", 0)) + 1
        context.user_data["round_correct"] = int(context.user_data.get("round_correct", 0)) + 1
        context.user_data["round_streak"] = int(context.user_data.get("round_streak", 0)) + 1
        context.user_data["round_chapter_correct"][chap] = context.user_data["round_chapter_correct"].get(chap, 0) + 1
        
        # التحقق من البونص
        streak = int(context.user_data["round_streak"])
        for threshold, (message, bonus_points) in config.BONUS_CONFIG.items():
            if streak == threshold:
                context.user_data["round_bonus"] = int(context.user_data.get("round_bonus", 0)) + bonus_points
                bonus_hit = True
                bonus_message = f"\n{message} (+{bonus_points} نقطة بونص!)"
        
        # رسالة النجاح
        msg = f"✅ **صح!** {random.choice(MOTIVATION_CORRECT)}{bonus_message}"
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    
    else:
        # إعادة تعيين السلسلة
        context.user_data["round_streak"] = 0
        msg = f"❌ **خطأ!** {random.choice(MOTIVATION_WRONG)}"
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    
    # تسجيل السؤال كمشاهد
    qid = q.get("id", "")
    if qid:
        mark_seen(user_id, qid)
    
    # الانتقال للسؤال التالي
    context.user_data["round_index"] = idx + 1
    
    # تأخير قصير قبل السؤال التالي
    await asyncio.sleep(1)
    await send_next_question(chat_id, user_id, context)

async def finish_round(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, ended_by_user: bool):
    """إنهاء الجولة وعرض النتائج"""
    user = get_user(user_id)
    
    # حساب المدة
    start_time = context.user_data.get("round_start_time", datetime.now())
    duration = int((datetime.now() - start_time).total_seconds())
    
    # جمع النتائج
    score = int(context.user_data.get("round_score", 0))
    bonus = int(context.user_data.get("round_bonus", 0))
    correct = int(context.user_data.get("round_correct", 0))
    total = len(context.user_data.get("round_questions", []))
    total_score = score + bonus
    
    chap_correct = context.user_data.get("round_chapter_correct", {})
    chap_total = context.user_data.get("round_chapter_total", {})
    
    # حفظ النتيجة
    save_round_result(user_id, score, bonus, correct, total, duration, chap_correct)
    
    # بناء رسالة النتائج
    lines = []
    lines.append("🏁 **نتيجة الجولة**" + (" (إنهاء مبكر)" if ended_by_user else ""))
    lines.append("")
    lines.append(f"🎯 **الإجابات الصحيحة:** {correct}/{total}")
    lines.append(f"⭐️ **نقاط الإجابات:** {score}")
    lines.append(f"🔥 **البونص:** {bonus}")
    lines.append(f"🏆 **المجموع النهائي:** **{total_score}** نقطة")
    lines.append(f"⏱️ **المدة:** {duration} ثانية")
    lines.append("")
    
    # دقة الفصول
    lines.append("📚 **أداء الفصول:**")
    for c in config.CHAPTERS:
        cc = chap_correct.get(c, 0)
        tt = chap_total.get(c, 0)
        if tt > 0:
            accuracy = (cc / tt) * 100
            stars = "⭐" * int(accuracy // 20) if accuracy >= 20 else "🔸"
            lines.append(f"• {c}: {cc}/{tt} ({accuracy:.1f}%) {stars}")
    
    lines.append("")
    
    # رسالة تشجيعية حسب النتيجة
    accuracy = (correct / total * 100) if total > 0 else 0
    if accuracy >= 80:
        lines.append("🌟 **مذهل! مستواك متقدم جداً!**")
    elif accuracy >= 60:
        lines.append("✨ **أداء ممتاز! استمر في التطور!**")
    elif accuracy >= 40:
        lines.append("💪 **جيد! ركز أكثر في المرات القادمة!**")
    else:
        lines.append("📚 **راجع الدروس وحاول مرة أخرى!**")
    
    if not user.get("is_approved", 0):
        lines.append("")
        lines.append("ℹ️ **ملاحظة:** نقاطك محفوظة، لكنك تحتاج اعتماد الاسم للظهور في لوحة المتصدرين.")
    
    # تنظيف بيانات الجولة
    for key in list(context.user_data.keys()):
        if key.startswith("round_") or key in ["current_q", "awaiting_term_answer", "awaiting_name", "round_start_time"]:
            context.user_data.pop(key, None)
    
    # إرسال النتائج
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    
    # العودة للقائمة الرئيسية
    upsert_user(user_id)
    user = get_user(user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text="🔍 **اختر من القائمة:**",
        reply_markup=main_menu_keyboard(user)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /help"""
    msg = (
        "📖 **دليل استخدام البوت**\n\n"
        "🔹 **الأوامر الرئيسية:**\n"
        "/start - تشغيل البوت والعودة للقائمة\n"
        "/help - عرض هذه الرسالة\n\n"
        "🔹 **للمشرفين:**\n"
        "/admin - لوحة التحكم الإدارية\n"
        "/pending - عرض الطلبات المعلقة\n\n"
        "🔹 **كيفية اللعب:**\n"
        "1. سجل اسمك من القائمة\n"
        "2. ابدأ جولة جديدة\n"
        "3. أجب على الأسئلة\n"
        "4. تابع تقدمك في لوحة المتصدرين\n\n"
        "🌟 **نظام البونص:**\n"
        "• كل 3 إجابات صحيحة متتالية = +1 نقطة\n"
        "• كل 5 إجابات = +2 نقطة\n"
        "• كل 10 إجابات = +3 نقطة\n\n"
        "📞 **للإبلاغ عن مشاكل:**\n"
        "تواصل مع المشرفين."
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نسخة احتياطية للبيانات"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ الأمر للمشرفين فقط.", reply_markup=ReplyKeyboardRemove())
        return
    
    try:
        # إنشاء نسخة احتياطية
        backup_data = {
            "timestamp": datetime.now().isoformat(),
            "users": [],
            "rounds": []
        }
        
        with db_manager.get_connection() as conn:
            cur = conn.cursor()
            
            # بيانات المستخدمين
            cur.execute("SELECT * FROM users")
            backup_data["users"] = [dict(row) for row in cur.fetchall()]
            
            # بيانات الجولات
            cur.execute("SELECT * FROM rounds ORDER BY finished_at DESC LIMIT 1000")
            backup_data["rounds"] = [dict(row) for row in cur.fetchall()]
        
        # حفظ الملف
        backup_file = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)
        
        await update.message.reply_text(
            f"✅ **تم إنشاء نسخة احتياطية**\n\n"
            f"📁 الملف: `{backup_file}`\n"
            f"👥 المستخدمون: {len(backup_data['users'])}\n"
            f"🎮 الجولات: {len(backup_data['rounds'])}\n\n"
            f"💾 **احفظ الملف في مكان آمن.**",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        await update.message.reply_text(
            f"❌ **فشل إنشاء النسخة الاحتياطية**\n\n{str(e)}",
            reply_markup=ReplyKeyboardRemove()
        )

# =========================
# Main Application
# =========================
import asyncio

def main():
    """الدالة الرئيسية لتشغيل البوت"""
    
    logger.info("Starting Telegram Quiz Bot...")
    
    # التحقق من وجود ملف الأسئلة
    if not os.path.exists(config.QUESTIONS_FILE):
        logger.error(f"Questions file not found: {config.QUESTIONS_FILE}")
        print(f"❌ ملف الأسئلة غير موجود: {config.QUESTIONS_FILE}")
        print(f"📁 يرجى إنشاء ملف: {config.QUESTIONS_FILE}")
        return
    
    # تحميل الأسئلة
    questions = question_manager.get_questions()
    if not questions:
        logger.warning("No questions loaded!")
        print("⚠️ لم يتم تحميل أي أسئلة!")
    else:
        logger.info(f"Loaded {len(questions)} questions")
        print(f"✅ تم تحميل {len(questions)} سؤال")
    
    # إنشاء تطبيق البوت
    app = Application.builder().token(config.BOT_TOKEN).build()
    
    # إضافة Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("backup", backup_command))
    
    # Callback Handlers
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^(ans_mcq:|ans_tf:|end_round)"))
    app.add_handler(CallbackQueryHandler(menu_callback))
    
    # Text Message Handler
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_router))
    
    # تشغيل البوت
    logger.info("Bot is running...")
    print("🤖 البوت يعمل الآن!")
    print("📊 للتحقق: أرسل /start للبوت")
    
    app.run_polling()

if __name__ == "__main__":
    main()