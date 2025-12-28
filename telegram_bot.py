import os
import json
import random
import logging
import re
import sqlite3
import hashlib
from datetime import datetime
from typing import Dict, List, Any, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")

# Maintenance (env)
MAINTENANCE_MODE_ENV = os.getenv("MAINTENANCE_MODE", "0").strip()  # "1" = on

# Admin IDs (supports both ADMIN_USER_ID single, and ADMIN_IDS comma list)
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

# Optional bad words list (comma-separated)
BAD_WORDS = set(
    w.strip() for w in os.getenv("BAD_WORDS", "").split(",") if w.strip()
)

# Files
QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "questions_from_word.json").strip()
DB_FILE = os.getenv("DB_FILE", "data.db").strip()

# Game settings
ROUND_SIZE = 20
STREAK_BONUS_EVERY = 3  # كل 3 صح = +1
TOP_N = 10

CHAPTERS = [
    "طبيعة العلم",
    "المخاليط والمحاليل",
    "حالات المادة",
    "الطاقة وتحولاتها",
    "أجهزة الجسم",
]

# =========================
# Arabic normalization helpers
# =========================
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")

def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = _ARABIC_DIACRITICS.sub("", text)              # tashkeel/tatweel
    # keep arabic + digits + spaces
    text = re.sub(r"[^\u0600-\u06FF0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # normalize common letters
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    return text

def is_arabic_only_name(name: str) -> bool:
    if not name:
        return False
    name = name.strip()
    if re.search(r"[A-Za-z]", name):
        return False
    return bool(re.fullmatch(r"[\u0600-\u06FF\s]+", name))

def looks_like_real_name(name: str) -> bool:
    """
    قواعد بسيطة:
    - عربي فقط
    - كلمتين على الأقل
    - طول مناسب
    - بدون كلمات سيئة (BAD_WORDS)
    """
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

def strip_al(s: str) -> str:
    return re.sub(r"^ال", "", s)

# =========================
# DB
# =========================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            is_approved INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            total_points INTEGER DEFAULT 0,
            rounds_played INTEGER DEFAULT 0,
            best_round_score INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_names (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            requested_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_questions (
            user_id INTEGER,
            qid TEXT,
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
            total INTEGER DEFAULT 0
        )
    """)

    # settings table for runtime toggles (maintenance on/off)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # ensure maintenance default exists
    cur.execute("""
        INSERT OR IGNORE INTO settings(key, value) VALUES ('maintenance', '0')
    """)

    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "0") -> str:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=? LIMIT 1", (key,))
    row = cur.fetchone()
    conn.close()
    return str(row["value"]) if row else default

def set_setting(key: str, value: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings(key, value) VALUES (?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()

def is_maintenance_active() -> bool:
    # active if env is 1 OR DB toggle is 1
    env_on = (MAINTENANCE_MODE_ENV == "1")
    db_on = (get_setting("maintenance", "0") == "1")
    return env_on or db_on

def upsert_user(user_id: int):
    now = datetime.utcnow().isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users(user_id, created_at, updated_at) VALUES (?,?,?)",
            (user_id, now, now)
        )
    else:
        cur.execute("UPDATE users SET updated_at=? WHERE user_id=?", (now, user_id))
    conn.commit()
    conn.close()

def set_pending_name(user_id: int, full_name: str):
    now = datetime.utcnow().isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pending_names(user_id, full_name, requested_at)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, requested_at=excluded.requested_at
    """, (user_id, full_name, now))
    conn.commit()
    conn.close()

def approve_name(user_id: int):
    now = datetime.utcnow().isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT full_name FROM pending_names WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row:
        full_name = row["full_name"]
        cur.execute("""
            UPDATE users SET full_name=?, is_approved=1, updated_at=?
            WHERE user_id=?
        """, (full_name, now, user_id))
        cur.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))
        conn.commit()
    conn.close()

def reject_name(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_user(user_id: int) -> Dict[str, Any]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else {}

def get_pending_list() -> List[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM pending_names ORDER BY requested_at ASC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_seen(user_id: int, qid: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO seen_questions(user_id, qid) VALUES(?,?)", (user_id, qid))
    conn.commit()
    conn.close()

def has_seen(user_id: int, qid: str) -> bool:
    if not qid:
        return False
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen_questions WHERE user_id=? AND qid=? LIMIT 1", (user_id, qid))
    ok = cur.fetchone() is not None
    conn.close()
    return ok

def save_round_result(user_id: int, score: int, bonus: int, correct: int, total: int) -> None:
    now = datetime.utcnow().isoformat()
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO rounds(user_id, started_at, finished_at, score, bonus, correct, total)
        VALUES(?,?,?,?,?,?,?)
    """, (user_id, now, now, score, bonus, correct, total))

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

    conn.commit()
    conn.close()

def get_leaderboard(top_n: int) -> List[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT full_name, total_points, best_round_score, rounds_played
        FROM users
        WHERE is_approved=1 AND full_name IS NOT NULL AND TRIM(full_name) <> ''
        ORDER BY total_points DESC, best_round_score DESC, rounds_played DESC
        LIMIT ?
    """, (top_n,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# =========================
# Questions load + normalize + chapter auto-classification
# =========================
def _stable_qid(item: Dict[str, Any]) -> str:
    blob = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:12]

def normalize_question_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Supports types:
      - mcq: question, options {A,B,C,D}, correct (A/B/C/D OR option text)
      - tf: statement, answer (bool or "صح/خطأ")
      - term: term, definition
    """
    if not isinstance(raw, dict):
        return None

    qtype = raw.get("type") or raw.get("نوع_السؤال") or raw.get("النوع")
    if not qtype:
        return None

    qtype = str(qtype).strip().lower()
    item: Dict[str, Any] = {}

    # map common Arabic labels
    if qtype in ["mcq", "اختيار من متعدد", "اختيار", "multiple choice"]:
        item["type"] = "mcq"
        item["question"] = raw.get("question") or raw.get("السؤال") or ""
        opts = raw.get("options") or raw.get("الخيارات") or {}
        if isinstance(opts, list):
            # list -> A,B,C,D
            keys = ["A", "B", "C", "D"]
            opts = {keys[i]: str(opts[i]) for i in range(min(4, len(opts)))}
        item["options"] = opts if isinstance(opts, dict) else {}
        item["correct"] = (raw.get("correct") or raw.get("الإجابة") or raw.get("answer") or "").strip()

    elif qtype in ["tf", "صح او خطأ", "صح أو خطأ", "true/false", "صح وخطأ"]:
        item["type"] = "tf"
        item["statement"] = raw.get("statement") or raw.get("السؤال") or raw.get("عبارة") or raw.get("statement_text") or ""
        ans = raw.get("answer") if "answer" in raw else raw.get("الإجابة")
        if isinstance(ans, bool):
            item["answer"] = ans
        else:
            ans_s = normalize_arabic(str(ans or ""))
            # accept "صح" / "صحيح" / "خطا" / "خاطئ"
            if "صح" in ans_s or "صحيح" in ans_s:
                item["answer"] = True
            elif "خطا" in ans_s or "خاط" in ans_s:
                item["answer"] = False
            else:
                # fallback: unknown -> False
                item["answer"] = False

    elif qtype in ["term", "مصطلح", "مصطلح علمي", "تعريف"]:
        item["type"] = "term"
        item["term"] = raw.get("term") or raw.get("المصطلح") or raw.get("الإجابة") or ""
        item["definition"] = raw.get("definition") or raw.get("التعريف") or raw.get("question") or raw.get("السؤال") or ""

    else:
        return None

    # ensure id
    item["id"] = str(raw.get("id") or raw.get("qid") or raw.get("ID") or "").strip()
    if not item["id"]:
        item["id"] = _stable_qid(item)

    return item

def load_questions() -> List[Dict[str, Any]]:
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # accept multiple shapes
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("items") or data.get("questions") or []
    else:
        raw_items = []

    items: List[Dict[str, Any]] = []
    for r in raw_items:
        q = normalize_question_item(r)
        if q:
            items.append(q)
    return items

# Keywords for auto classification
CHAPTER_KEYWORDS = {
    "طبيعة العلم": [
        "الطريقه العلميه", "فرضيه", "متغير", "ثابت", "ملاحظه", "تجربه", "استنتاج", "تواصل",
        "علم الاثار", "الرادار", "خرائط", "نماذج", "قياس"
    ],
    "المخاليط والمحاليل": [
        "مخلوط", "محلول", "مذيب", "مذاب", "تركيز", "ذائبيه", "حمض", "قاعده", "تعادل", "ترسب",
        "معلق", "غرويات", "ترشيح", "تقطير"
    ],
    "حالات المادة": [
        "صلب", "سائل", "غاز", "بلازما", "انصهار", "تبخر", "تكاثف", "تجمد", "تسامي",
        "ضغط", "كثافه", "لزوج", "حجم", "كتله"
    ],
    "الطاقة وتحولاتها": [
        "طاقه", "حركيه", "وضع", "كامنه", "اشعاعيه", "كيميائيه", "كهربائيه", "نوويه",
        "حفظ الطاقه", "تحول", "متحوله", "مولد", "توربين"
    ],
    "أجهزة الجسم": [
        "دم", "قلب", "شريان", "وريد", "شعيره", "مناعه", "اجسام مضاده", "مولدات الضد",
        "هضم", "معده", "امعاء", "رئه", "تنفس", "كليه", "بول", "عصب", "عضلات"
    ],
}

def classify_chapter(item: Dict[str, Any]) -> str:
    blob = ""
    if item.get("type") == "mcq":
        blob = item.get("question", "")
        blob += " " + " ".join((item.get("options") or {}).values())
    elif item.get("type") == "tf":
        blob = item.get("statement", "")
    elif item.get("type") == "term":
        blob = (item.get("term", "") + " " + item.get("definition", "")).strip()

    blob_n = normalize_arabic(blob)
    best = CHAPTERS[0]
    best_score = -1

    for chap, kws in CHAPTER_KEYWORDS.items():
        score = 0
        for kw in kws:
            kw_n = normalize_arabic(kw)
            if kw_n and kw_n in blob_n:
                score += 1
        if score > best_score:
            best_score = score
            best = chap

    # لو ما لقى شيء، خليها "حالات المادة" كافتراضي
    return best if best in CHAPTERS else "حالات المادة"

def build_chapter_buckets(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    buckets = {c: [] for c in CHAPTERS}
    for it in items:
        chap = classify_chapter(it)
        it["_chapter"] = chap
        buckets.setdefault(chap, []).append(it)
    # ensure all chapters exist
    for c in CHAPTERS:
        buckets.setdefault(c, [])
    return buckets

def _calc_targets(user_id: int, buckets: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """
    توزيع نسبي مع تغطية جميع الفصول:
    - نعطي حد أدنى 2 لكل فصل (إذا متوفر)
    - الباقي يتوزع حسب عدد الأسئلة غير المشاهدة بكل فصل
    """
    min_each = 2
    base = {c: 0 for c in CHAPTERS}

    # unseen counts
    unseen_counts = {}
    for c in CHAPTERS:
        pool = buckets.get(c, [])
        unseen = [q for q in pool if not has_seen(user_id, q.get("id", ""))]
        unseen_counts[c] = len(unseen)

    # assign minimums where possible
    assigned = 0
    for c in CHAPTERS:
        if unseen_counts[c] >= min_each:
            base[c] = min_each
            assigned += min_each
        elif unseen_counts[c] > 0:
            base[c] = unseen_counts[c]
            assigned += unseen_counts[c]
        else:
            base[c] = 0

    remaining = ROUND_SIZE - assigned
    if remaining <= 0:
        return base

    # proportional distribute remaining based on unseen_counts
    total_unseen = sum(unseen_counts.values()) or 1
    weights = {c: unseen_counts[c] / total_unseen for c in CHAPTERS}

    # add 1-by-1 to avoid rounding gaps
    while remaining > 0:
        # pick chapter with max (weight and still has capacity)
        candidates = []
        for c in CHAPTERS:
            cap = unseen_counts[c] - base[c]
            if cap > 0:
                candidates.append(c)
        if not candidates:
            break

        # choose best candidate by weight
        c_best = max(candidates, key=lambda c: weights[c])
        base[c_best] += 1
        remaining -= 1

    # if still remaining (means unseen not enough), we’ll later fill from any questions (including seen)
    return base

def pick_round_questions(user_id: int, buckets: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    20 سؤال تغطي الفصول الخمسة بتوزيع نسبي + تقليل التكرار قدر الإمكان.
    """
    targets = _calc_targets(user_id, buckets)

    chosen: List[Dict[str, Any]] = []
    leftovers_unseen: List[Dict[str, Any]] = []

    # pick per chapter
    for chap in CHAPTERS:
        pool = buckets.get(chap, [])
        unseen = [q for q in pool if not has_seen(user_id, q.get("id", ""))]
        random.shuffle(unseen)

        take = targets.get(chap, 0)
        taken = unseen[:take]
        chosen.extend(taken)

        leftovers_unseen.extend(unseen[take:])

    # fill remaining from unseen leftovers
    if len(chosen) < ROUND_SIZE:
        random.shuffle(leftovers_unseen)
        need = ROUND_SIZE - len(chosen)
        chosen.extend(leftovers_unseen[:need])

    # if still short, allow repeats from all items
    if len(chosen) < ROUND_SIZE:
        all_items = []
        for chap in CHAPTERS:
            all_items.extend(buckets.get(chap, []))
        random.shuffle(all_items)
        need = ROUND_SIZE - len(chosen)
        chosen.extend(all_items[:need])

    # unique by id
    seen_ids = set()
    uniq = []
    for q in chosen:
        qid = q.get("id")
        if qid and qid not in seen_ids:
            uniq.append(q)
            seen_ids.add(qid)

    # top-up if uniqueness reduced count
    if len(uniq) < ROUND_SIZE:
        all_items = []
        for chap in CHAPTERS:
            all_items.extend(buckets.get(chap, []))
        random.shuffle(all_items)
        for extra in all_items:
            if len(uniq) >= ROUND_SIZE:
                break
            qid = extra.get("id")
            if qid and qid not in seen_ids:
                uniq.append(extra)
                seen_ids.add(qid)

    random.shuffle(uniq)
    return uniq[:ROUND_SIZE]

# =========================
# UI helpers
# =========================
def main_menu_keyboard(user: Dict[str, Any]) -> InlineKeyboardMarkup:
    approved = bool(user.get("is_approved", 0))
    name = user.get("full_name") or ""
    name_status = "✅ معتمد" if approved else ("⏳ بانتظار الموافقة" if name else "➕ سجّل اسمك")
    kb = [
        [InlineKeyboardButton("🎮 ابدأ جولة (20 سؤال)", callback_data="play_round")],
        [InlineKeyboardButton("🏆 لوحة التميز (Top 10)", callback_data="leaderboard")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats")],
        [InlineKeyboardButton(name_status, callback_data="set_name")],
    ]
    return InlineKeyboardMarkup(kb)

def answer_keyboard_mcq(options: Dict[str, str]) -> InlineKeyboardMarkup:
    rows = []
    for key in ["A", "B", "C", "D"]:
        if key in options and str(options[key]).strip():
            rows.append([InlineKeyboardButton(f"{key}) {options[key]}", callback_data=f"ans_mcq:{key}")])
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
    kb = [[
        InlineKeyboardButton("✅ موافق", callback_data=f"admin_approve:{user_id}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject:{user_id}")
    ]]
    return InlineKeyboardMarkup(kb)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def block_if_maintenance(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns True if blocked (maintenance active and not admin)
    """
    if not is_maintenance_active():
        return False

    # determine user_id and reply method
    user_id = None
    reply_func = None

    if hasattr(update_or_query, "effective_user"):
        user_id = update_or_query.effective_user.id
        if update_or_query.message:
            reply_func = update_or_query.message.reply_text
    else:
        # callback query object
        user_id = update_or_query.from_user.id
        reply_func = update_or_query.message.reply_text

    if user_id is not None and not is_admin(user_id):
        await reply_func("🛠️ البوت تحت الصيانة الآن.\nارجع بعد شوي 🙏")
        return True

    return False

# =========================
# Game flow helpers
# =========================
def calc_streak_bonus(streak: int) -> int:
    return streak // STREAK_BONUS_EVERY

def _mcq_is_correct(q: Dict[str, Any], picked_letter: str) -> bool:
    correct = (q.get("correct") or "").strip()
    if not correct:
        return False

    # if correct is A/B/C/D
    c_up = correct.upper().strip()
    if c_up in ["A", "B", "C", "D"]:
        return picked_letter.upper() == c_up

    # else treat correct as option text
    options = q.get("options") or {}
    picked_text = str(options.get(picked_letter.upper(), "")).strip()
    return normalize_arabic(picked_text) == normalize_arabic(correct)

def _tf_is_correct(q: Dict[str, Any], picked: str) -> bool:
    correct_val = q.get("answer", False)
    correct_bool = bool(correct_val)
    return picked == ("true" if correct_bool else "false")

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await block_if_maintenance(update, context):
        return

    user_id = update.effective_user.id
    upsert_user(user_id)
    user = get_user(user_id)

    msg = (
        "هلا 👋\n"
        "أنا بوت المسابقة 🎯\n"
        "• كل جولة = 20 سؤال موزعة على فصول المنهج\n"
        "• بونص: كل 3 إجابات صحيحة متتالية = +1\n"
        "• لوحة التميز Top 10 للطلاب المعتمدين ✅\n\n"
        "اختر من القائمة 👇"
    )
    await update.message.reply_text(msg, reply_markup=main_menu_keyboard(user))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if await block_if_maintenance(query, context):
        return

    user_id = query.from_user.id
    upsert_user(user_id)
    user = get_user(user_id)

    data = query.data

    if data == "set_name":
        await query.edit_message_text(
            "اكتب اسمك الحقيقي (عربي فقط) مثل: **محمد أحمد**\n"
            "شروطنا:\n"
            "• عربي فقط (بدون إنجليزي)\n"
            "• كلمتين على الأقل\n"
            "• واضح ومحترم\n\n"
            "✍️ اكتب الاسم الآن:",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_name"] = True
        return

    if data == "leaderboard":
        lb = get_leaderboard(TOP_N)
        if not lb:
            text = "🏆 لوحة التميز فارغة للحين… أول واحد يبدع 🔥"
        else:
            lines = ["🏆 **لوحة التميز (Top 10)**\n"]
            for i, row in enumerate(lb, start=1):
                lines.append(
                    f"{i}) {row['full_name']} — ⭐️ {row['total_points']} نقطة (أفضل جولة: {row['best_round_score']})"
                )
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(user))
        return

    if data == "my_stats":
        name = user.get("full_name") or "—"
        approved = "✅" if user.get("is_approved", 0) else "⏳"
        total = user.get("total_points", 0)
        rounds = user.get("rounds_played", 0)
        best = user.get("best_round_score", 0)
        text = (
            f"📊 **إحصائياتك**\n"
            f"الاسم: {name} {approved}\n"
            f"النقاط: ⭐️ {total}\n"
            f"عدد الجولات: 🎮 {rounds}\n"
            f"أفضل جولة: 🥇 {best}\n\n"
            f"تبغى تكمل؟"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(user))
        return

    if data == "play_round":
        await start_round(query, context)
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Single text handler: handles name input and term answers.
    """
    if await block_if_maintenance(update, context):
        return

    # name flow
    if context.user_data.get("awaiting_name"):
        user_id = update.effective_user.id
        name = (update.message.text or "").strip()

        if not looks_like_real_name(name):
            await update.message.reply_text(
                "❌ الاسم ما ينفع حسب الشروط.\n"
                "اكتبه عربي فقط وبكلمتين على الأقل وبشكل محترم.\n"
                "جرّب مرة ثانية 👇"
            )
            return

        upsert_user(user_id)
        set_pending_name(user_id, name)
        context.user_data["awaiting_name"] = False

        await update.message.reply_text(
            "✅ تم استلام الاسم.\n"
            "صار بانتظار موافقة الأدمن 👑\n"
            "تقدر تلعب الحين، بس لوحة التميز ما تظهر إلا بعد الاعتماد."
        )

        # notify admins
        if ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"📝 طلب اعتماد اسم:\n• المستخدم: {user_id}\n• الاسم: {name}",
                        reply_markup=admin_pending_keyboard(user_id)
                    )
                except Exception as e:
                    logger.warning("Failed notifying admin %s: %s", admin_id, e)
        return

    # term answer flow
    if context.user_data.get("awaiting_term_answer") and "round_questions" in context.user_data:
        q = context.user_data.get("current_q")
        if not q or q.get("type") != "term":
            context.user_data["awaiting_term_answer"] = False
            return

        user_answer = normalize_arabic(update.message.text or "")
        correct_term = normalize_arabic(q.get("term") or "")

        is_correct = (
            user_answer == correct_term
            or strip_al(user_answer) == strip_al(correct_term)
        )

        context.user_data["awaiting_term_answer"] = False

        # create dummy-like object with message + from_user
        class Dummy:
            def __init__(self, msg):
                self.message = msg
                self.from_user = msg.from_user

        dummy = Dummy(update.message)
        await apply_answer_result(dummy, context, is_correct)
        return

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    if not is_admin(admin_id):
        await query.edit_message_text("❌ ما لك صلاحية هنا.")
        return

    data = query.data
    if data.startswith("admin_approve:"):
        uid = int(data.split(":")[1])
        approve_name(uid)
        await query.edit_message_text(f"✅ تم اعتماد المستخدم {uid}")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 تم اعتماد اسمك! الحين بتدخل لوحة التميز 🏆")
        except Exception:
            pass
        return

    if data.startswith("admin_reject:"):
        uid = int(data.split(":")[1])
        reject_name(uid)
        await query.edit_message_text(f"❌ تم رفض الاسم للمستخدم {uid}")
        try:
            await context.bot.send_message(chat_id=uid, text="❌ اسمك ما تم اعتماده. اكتب اسمك مرة ثانية بشكل واضح ومحترم.")
        except Exception:
            pass
        return

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.")
        return

    pending = get_pending_list()
    if not pending:
        await update.message.reply_text("ما فيه طلبات معلّقة ✅")
        return

    for p in pending[:20]:
        uid = int(p["user_id"])
        nm = p["full_name"]
        await update.message.reply_text(
            f"📝 طلب معلّق:\n• المستخدم: {uid}\n• الاسم: {nm}",
            reply_markup=admin_pending_keyboard(uid)
        )

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.")
        return
    set_setting("maintenance", "1")
    await update.message.reply_text("🛠️ تم تشغيل وضع الصيانة ✅\n(المستخدمين ما يقدرون يستخدمون البوت)")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.")
        return
    set_setting("maintenance", "0")
    await update.message.reply_text("✅ تم إيقاف وضع الصيانة.\nالبوت رجع متاح للكل 👌")

async def start_round(query, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    upsert_user(user_id)

    buckets = context.bot_data.get("questions_buckets")
    if not buckets:
        await query.message.reply_text("❌ ملف الأسئلة غير جاهز. تأكد أن questions_from_word.json موجود وبصيغة صحيحة.")
        return

    round_questions = pick_round_questions(user_id, buckets)

    context.user_data["round_questions"] = round_questions
    context.user_data["round_index"] = 0
    context.user_data["round_score"] = 0
    context.user_data["round_bonus"] = 0
    context.user_data["round_correct"] = 0
    context.user_data["round_streak"] = 0
    context.user_data["round_chapter_correct"] = {c: 0 for c in CHAPTERS}
    context.user_data["round_chapter_total"] = {c: 0 for c in CHAPTERS}

    await query.message.reply_text("🎮 بدأنا الجولة! جاهز؟ 🔥")
    await send_next_question(query, context)

async def send_next_question(query, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    idx = context.user_data.get("round_index", 0)
    qs: List[Dict[str, Any]] = context.user_data.get("round_questions", [])

    if idx >= len(qs):
        await finish_round(query, context, ended_by_user=False)
        return

    q = qs[idx]
    context.user_data["current_q"] = q

    chap = q.get("_chapter", "—")
    context.user_data["round_chapter_total"][chap] = context.user_data["round_chapter_total"].get(chap, 0) + 1

    header = f"🧩 الفصل: {chap}\n📌 السؤال {idx+1}/{ROUND_SIZE}\n\n"

    if q.get("type") == "mcq":
        question = (q.get("question") or "").strip()
        options = q.get("options") or {}
        text = header + f"❓ {question}"
        await query.message.reply_text(text, reply_markup=answer_keyboard_mcq(options))
        return

    if q.get("type") == "tf":
        st = (q.get("statement") or "").strip()
        text = header + f"✅/❌ {st}"
        await query.message.reply_text(text, reply_markup=answer_keyboard_tf())
        return

    if q.get("type") == "term":
        definition = (q.get("definition") or "").strip()
        text = header + "🧠 اكتب المصطلح المناسب للتعريف التالي:\n\n" + f"📘 {definition}\n\n✍️ اكتب الإجابة:"
        await query.message.reply_text(text)
        context.user_data["awaiting_term_answer"] = True
        return

    # fallback
    await query.message.reply_text("⚠️ نوع سؤال غير معروف… تخطيناه.")
    context.user_data["round_index"] = idx + 1
    await send_next_question(query, context)

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if await block_if_maintenance(query, context):
        return

    if "round_questions" not in context.user_data:
        await query.message.reply_text("ابدأ جولة من القائمة 👇\nاكتب /start")
        return

    q = context.user_data.get("current_q")
    if not q:
        await query.message.reply_text("⚠️ ما عندي سؤال حالي.")
        return

    data = query.data

    if data == "end_round":
        await finish_round(query, context, ended_by_user=True)
        return

    is_correct = False

    if q.get("type") == "mcq" and data.startswith("ans_mcq:"):
        picked = data.split(":")[1].strip().upper()
        is_correct = _mcq_is_correct(q, picked)

    elif q.get("type") == "tf" and data.startswith("ans_tf:"):
        picked = data.split(":")[1].strip().lower()
        is_correct = _tf_is_correct(q, picked)

    else:
        await query.message.reply_text("⚠️ إجابة غير متوقعة.")
        return

    await apply_answer_result(query, context, is_correct)

async def apply_answer_result(query, context: ContextTypes.DEFAULT_TYPE, is_correct: bool):
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
            await query.message.reply_text("✅ صح! +1\n🔥 بونص سلسلة! (كل 3 صح = +1)")
        else:
            await query.message.reply_text("✅ صح!")
    else:
        context.user_data["round_streak"] = 0
        await query.message.reply_text("❌ خطأ!")

    # mark seen
    qid = q.get("id", "")
    if qid:
        mark_seen(query.from_user.id, qid)

    # next
    context.user_data["round_index"] = idx + 1
    await send_next_question(query, context)

async def finish_round(query, context: ContextTypes.DEFAULT_TYPE, ended_by_user: bool):
    user_id = query.from_user.id
    user = get_user(user_id)

    score = int(context.user_data.get("round_score", 0))
    bonus = int(context.user_data.get("round_bonus", 0))
    correct = int(context.user_data.get("round_correct", 0))
    total = ROUND_SIZE

    save_round_result(user_id, score, bonus, correct, total)

    chap_correct = context.user_data.get("round_chapter_correct", {})
    chap_total = context.user_data.get("round_chapter_total", {})

    lines = []
    lines.append("🏁 **انتهت الجولة**" + (" (انتهيت بدري)" if ended_by_user else ""))
    lines.append(f"✅ الصحيح: {correct}/{total}")
    lines.append(f"⭐️ نقاط الإجابات: {score}")
    lines.append(f"🔥 البونص: {bonus}")
    lines.append(f"🏆 مجموع الجولة: **{score + bonus}**")
    lines.append("")
    lines.append("📌 أداءك حسب الفصول:")
    for c in CHAPTERS:
        cc = chap_correct.get(c, 0)
        tt = chap_total.get(c, 0)
        if tt:
            lines.append(f"• {c}: {cc}/{tt}")

    if not user.get("is_approved", 0):
        lines.append("")
        lines.append("ℹ️ تقدر تجمع نقاط، بس لوحة التميز تظهر بعد اعتماد اسمك ✅")

    # clear round state
    for k in [
        "round_questions", "round_index", "round_score", "round_bonus",
        "round_correct", "round_streak", "round_chapter_correct",
        "round_chapter_total", "current_q", "awaiting_term_answer"
    ]:
        context.user_data.pop(k, None)

    await query.message.reply_text("\n".join(lines), parse_mode="Markdown")

    upsert_user(user_id)
    user = get_user(user_id)
    await query.message.reply_text("اختر من القائمة 👇", reply_markup=main_menu_keyboard(user))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "الأوامر:\n"
        "/start — تشغيل البوت\n"
        "/pending — للأدمن: عرض طلبات الأسماء\n"
        "/maintenance_on — للأدمن: تشغيل الصيانة\n"
        "/maintenance_off — للأدمن: إيقاف الصيانة\n"
    )
    await update.message.reply_text(msg)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)
    # notify admins (best-effort)
    if ADMIN_IDS:
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ خطأ في البوت:\n{repr(context.error)}"
                )
            except Exception:
                pass

# =========================
# Main
# =========================
def main():
    db_init()

    try:
        items = load_questions()
        logger.info("Loaded %s questions from %s", len(items), QUESTIONS_FILE)
    except Exception as e:
        logger.exception("Failed loading questions file: %s", e)
        items = []

    buckets = build_chapter_buckets(items) if items else None

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["questions_buckets"] = buckets

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # Callbacks
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^(ans_mcq:|ans_tf:|end_round)$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(play_round|leaderboard|my_stats|set_name)$"))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("Bot started. Admins=%s Maintenance(env)=%s", list(ADMIN_IDS), MAINTENANCE_MODE_ENV)
    app.run_polling()

if __name__ == "__main__":
    main()