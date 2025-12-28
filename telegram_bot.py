import os
import json
import random
import logging
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

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

# Admin IDs:
# - ADMIN_USER_ID = single id
# - ADMIN_IDS = comma separated ids
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

# Maintenance mode (1 = on, 0 = off)
MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "0").strip()
MAINTENANCE_ON = MAINTENANCE_MODE in ("1", "true", "True", "YES", "yes", "on", "ON")

# Optional bad words list (comma-separated)
BAD_WORDS = set(w.strip() for w in os.getenv("BAD_WORDS", "").split(",") if w.strip())

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
# Motivation phrases
# =========================
MOTIVATION_CORRECT = [
    "🔥 بطل! كمل كذا!",
    "👏 ممتاز!",
    "💪 رهيب!",
    "✅ صح عليك!",
    "🌟 كفو!",
    "🚀 يا سلام عليك!",
]

MOTIVATION_WRONG = [
    "😅 بسيطة! الجاية صح إن شاء الله.",
    "👀 ركّز شوي، تقدر!",
    "💡 مو مشكلة، تعلمنا!",
    "🔥 لا توقف! كمل!",
    "😎 قدها وقدود!",
]

MOTIVATION_BONUS = [
    "🏅 بونص! سلسلة نار 🔥",
    "🎯 ممتاز! خذت بونص!",
    "💥 كملت سلسلة الصح!",
]

# =========================
# Arabic normalization helpers
# =========================
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")

def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = _ARABIC_DIACRITICS.sub("", text)  # remove tashkeel/tatweel
    text = re.sub(r"[^\u0600-\u06FF0-9\s]", " ", text)  # keep arabic/digits/spaces
    text = re.sub(r"\s+", " ", text).strip()
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
    """
    Return True if blocked (maintenance ON and user not admin).
    """
    if not MAINTENANCE_ON:
        return False
    user_id = update.effective_user.id if update.effective_user else 0
    if is_admin(user_id):
        return False

    msg = "🛠️ البوت تحت صيانة حالياً… ارجعوا بعدين 🌿"
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

    conn.commit()
    conn.close()

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
    if not qid:
        return
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
# Questions load + chapter auto-classification
# =========================
def load_questions() -> List[Dict[str, Any]]:
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # دعم أكثر من شكل:
    # 1) {"items":[...]}
    # 2) [{"id":...}, ...]
    # 3) {"questions":[...]}
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("questions"), list):
            return data["questions"]
    return []

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

def classify_chapter(item: Dict[str, Any]) -> str:
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
    best = "حالات المادة"
    best_score = 0
    for chap, kws in CHAPTER_KEYWORDS.items():
        score = 0
        for kw in kws:
            if kw and normalize_arabic(kw) in blob_n:
                score += 1
        if score > best_score:
            best_score = score
            best = chap
    return best

def ensure_question_ids(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    يضمن أن كل سؤال له id (مهم لتجنب تكرار الأسئلة + seen_questions)
    """
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        if not it.get("id"):
            t = it.get("type") or "q"
            base = ""
            if t == "mcq":
                base = it.get("question") or ""
            elif t == "tf":
                base = it.get("statement") or ""
            elif t == "term":
                base = (it.get("term") or "") + "|" + (it.get("definition") or "")
            base = normalize_arabic(base)[:60]
            it["id"] = f"{t}_{i}_{abs(hash(base)) % 10_000_000}"
    return items

def build_chapter_buckets(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    buckets = {c: [] for c in CHAPTERS}
    for it in items:
        if not isinstance(it, dict):
            continue
        chap = classify_chapter(it)
        it["_chapter"] = chap
        if chap in buckets:
            buckets[chap].append(it)
        else:
            buckets["حالات المادة"].append(it)
    return buckets

def pick_round_questions(user_id: int, buckets: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    target_per_chapter = {c: ROUND_SIZE // len(CHAPTERS) for c in CHAPTERS}  # 4 لكل فصل
    chosen: List[Dict[str, Any]] = []
    leftovers: List[Dict[str, Any]] = []

    for chap in CHAPTERS:
        pool = buckets.get(chap, [])
        unseen = [q for q in pool if not has_seen(user_id, q.get("id", ""))]
        random.shuffle(unseen)
        take = target_per_chapter[chap]
        taken = unseen[:take]
        chosen.extend(taken)
        leftovers.extend(unseen[take:])

    if len(chosen) < ROUND_SIZE:
        random.shuffle(leftovers)
        need = ROUND_SIZE - len(chosen)
        chosen.extend(leftovers[:need])

    if len(chosen) < ROUND_SIZE:
        all_items: List[Dict[str, Any]] = []
        for chap in CHAPTERS:
            all_items.extend(buckets.get(chap, []))
        random.shuffle(all_items)
        need = ROUND_SIZE - len(chosen)
        chosen.extend(all_items[:need])

    # unique
    seen_ids = set()
    uniq = []
    for q in chosen:
        qid = q.get("id")
        if qid and qid not in seen_ids:
            uniq.append(q)
            seen_ids.add(qid)

    # fill if needed
    while len(uniq) < ROUND_SIZE:
        all_items: List[Dict[str, Any]] = []
        for chap in CHAPTERS:
            all_items.extend(buckets.get(chap, []))
        extra = random.choice(all_items) if all_items else None
        if not extra:
            break
        if extra.get("id") not in seen_ids:
            uniq.append(extra)
            seen_ids.add(extra.get("id"))

    random.shuffle(uniq)
    return uniq[:ROUND_SIZE]

# =========================
# UI helpers (INLINE ONLY)
# =========================
def main_menu_keyboard(user: Dict[str, Any]) -> InlineKeyboardMarkup:
    approved = bool(user.get("is_approved", 0))
    name = (user.get("full_name") or "").strip()
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
        if key in options:
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

def answer_keyboard_term(options: Dict[str, str]) -> InlineKeyboardMarkup:
    rows = []
    for key in ["A", "B", "C", "D"]:
        if key in options:
            rows.append([InlineKeyboardButton(f"{key}) {options[key]}", callback_data=f"ans_term:{key}")])
    rows.append([InlineKeyboardButton("⛔️ إنهاء الجولة", callback_data="end_round")])
    return InlineKeyboardMarkup(rows)

def admin_pending_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("✅ موافق", callback_data=f"admin_approve:{user_id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject:{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(kb)

def build_term_mcq(correct_term: str, term_pool: List[str]) -> Tuple[Dict[str, str], str]:
    correct_term = (correct_term or "").strip()
    pool = [t.strip() for t in term_pool if t and t.strip() and t.strip() != correct_term]
    random.shuffle(pool)
    distractors = pool[:3]

    choices = [correct_term] + distractors
    while len(choices) < 4:
        choices.append(correct_term)

    random.shuffle(choices)

    letters = ["A", "B", "C", "D"]
    options_map = {letters[i]: choices[i] for i in range(4)}
    correct_letter = next(k for k, v in options_map.items() if v == correct_term)
    return options_map, correct_letter

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

async def send_menu(chat_id: int, user: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=chat_id,
        text="اختر من القائمة 👇",
        reply_markup=main_menu_keyboard(user)
    )

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
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
        "ابدأ من هنا 👇"
    )
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
    await send_menu(update.message.chat_id, user, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
        return
    msg = (
        "الأوامر:\n"
        "/start — تشغيل البوت\n"
        "/admin — للأدمن\n"
        "/pending — للأدمن: عرض طلبات الأسماء\n"
    )
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # مسموح للأدمن حتى لو صيانة
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ الأمر هذا للأدمن فقط.", reply_markup=ReplyKeyboardRemove())
        return

    pending = get_pending_list()
    await update.message.reply_text(
        f"👑 لوحة الأدمن\n"
        f"• الصيانة: {'✅ شغالة' if MAINTENANCE_ON else '❌ مطفية'}\n"
        f"• طلبات الأسماء المعلّقة: {len(pending)}\n\n"
        f"استخدم /pending لعرض الطلبات.",
        reply_markup=ReplyKeyboardRemove()
    )

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
        uid = int(p["user_id"])
        nm = p["full_name"]
        await update.message.reply_text(
            f"📝 طلب معلّق:\n• المستخدم: {uid}\n• الاسم: {nm}",
            reply_markup=admin_pending_keyboard(uid)
        )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
        return

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id

    upsert_user(user_id)
    user = get_user(user_id)
    data = query.data

    if data == "set_name":
        context.user_data["awaiting_name"] = True
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "اكتب اسمك الحقيقي (عربي فقط) مثل: **محمد أحمد**\n"
                "شروطنا:\n"
                "• عربي فقط (بدون إنجليزي)\n"
                "• كلمتين على الأقل\n"
                "• واضح ومحترم\n\n"
                "✍️ اكتب الاسم الآن:"
            ),
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if data == "leaderboard":
        lb = get_leaderboard(TOP_N)
        if not lb:
            text = "🏆 لوحة التميز فاضية للحين… أول واحد يبدع 🔥"
        else:
            lines = ["🏆 **لوحة التميز (Top 10)**\n"]
            for i, row in enumerate(lb, start=1):
                lines.append(
                    f"{i}) {row['full_name']} — ⭐️ {row['total_points']} نقطة (أفضل جولة: {row['best_round_score']})"
                )
            text = "\n".join(lines)

        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        await send_menu(chat_id, user, context)
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
            f"أفضل جولة: 🥇 {best}\n"
        )
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        await send_menu(chat_id, user, context)
        return

    if data == "play_round":
        await start_round(chat_id, user_id, context)
        return

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    admin_id = query.from_user.id
    if not is_admin(admin_id):
        await context.bot.send_message(chat_id=query.message.chat_id, text="❌ ما لك صلاحية هنا.")
        return

    data = query.data
    chat_id = query.message.chat_id

    if data.startswith("admin_approve:"):
        uid = int(data.split(":")[1])
        approve_name(uid)
        await context.bot.send_message(chat_id=chat_id, text=f"✅ تم اعتماد المستخدم {uid}")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 تم اعتماد اسمك! الحين بتدخل لوحة التميز 🏆")
        except Exception:
            pass
        return

    if data.startswith("admin_reject:"):
        uid = int(data.split(":")[1])
        reject_name(uid)
        await context.bot.send_message(chat_id=chat_id, text=f"❌ تم رفض الاسم للمستخدم {uid}")
        try:
            await context.bot.send_message(chat_id=uid, text="❌ اسمك ما تم اعتماده. اكتب اسمك مرة ثانية بشكل واضح ومحترم.")
        except Exception:
            pass
        return

async def start_round(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(user_id)

    items = context.bot_data.get("questions_items")
    buckets = context.bot_data.get("questions_buckets")
    if not items or not buckets:
        await context.bot.send_message(chat_id=chat_id, text="❌ ملف الأسئلة غير جاهز. تأكد أن questions_from_word.json موجود.")
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

    # مسح أي حالات انتظار
    context.user_data["awaiting_name"] = False
    context.user_data["term_correct_letter"] = None

    await context.bot.send_message(chat_id=chat_id, text="🎮 بدأنا الجولة! جاهز؟ 🔥")
    await send_next_question(chat_id, user_id, context)

async def send_next_question(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get("round_index", 0)
    qs: List[Dict[str, Any]] = context.user_data.get("round_questions", [])

    if idx >= len(qs):
        await finish_round(chat_id, user_id, context, ended_by_user=False)
        return

    q = qs[idx]
    context.user_data["current_q"] = q
    context.user_data["term_correct_letter"] = None  # reset

    chap = q.get("_chapter", "—")
    # لا نعرض الفصل، لكن نحسبه للإحصائيات
    context.user_data["round_chapter_total"][chap] = context.user_data["round_chapter_total"].get(chap, 0) + 1

    header = f"📌 السؤال {idx+1}/{ROUND_SIZE}\n\n"
    t = q.get("type")

    if t == "mcq":
        question = (q.get("question") or "").strip()
        options = q.get("options") or {}
        text = header + f"❓ {question}"
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=answer_keyboard_mcq(options))
        return

    if t == "tf":
        st = (q.get("statement") or "").strip()
        text = header + f"✅/❌ {st}"
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=answer_keyboard_tf())
        return

    if t == "term":
        definition = (q.get("definition") or "").strip()
        correct_term = (q.get("term") or "").strip()
        term_pool = context.bot_data.get("term_pool") or []
        options_map, correct_letter = build_term_mcq(correct_term, term_pool)
        context.user_data["term_correct_letter"] = correct_letter

        text = header + "🧠 اختر المصطلح الصحيح للتعريف التالي:\n\n" + f"📘 {definition}"
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=answer_keyboard_term(options_map))
        return

    # fallback
    await context.bot.send_message(chat_id=chat_id, text="⚠️ نوع سؤال غير معروف… تخطيناه.")
    context.user_data["round_index"] = idx + 1
    await send_next_question(chat_id, user_id, context)

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await maintenance_block(update, context):
        return

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # نعطل أزرار السؤال اللي انضغط (عشان ما يضغط مرتين)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if "round_questions" not in context.user_data:
        await context.bot.send_message(chat_id=chat_id, text="ابدأ جولة من القائمة 👇\nاكتب /start")
        return

    q = context.user_data.get("current_q")
    if not q:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ ما عندي سؤال حالي.")
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
        picked = data.split(":")[1]  # "true"/"false"
        correct_bool = parse_tf_answer(q.get("answer"))
        if correct_bool is None:
            correct_bool = parse_tf_answer(q.get("correct"))
        if correct_bool is None:
            correct_bool = False
        is_correct = (picked == ("true" if correct_bool else "false"))

    elif t == "term" and data.startswith("ans_term:"):
        picked = data.split(":")[1]  # A/B/C/D
        correct_letter = context.user_data.get("term_correct_letter")
        is_correct = (picked == correct_letter)

    else:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ إجابة غير متوقعة.")
        return

    await apply_answer_result(chat_id, user_id, context, is_correct)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Router واحد للنصوص: فقط تسجيل الاسم (المصطلحات صارت أزرار)
    """
    if await maintenance_block(update, context):
        return

    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    text = update.message.text.strip()

    if context.user_data.get("awaiting_name"):
        if not looks_like_real_name(text):
            await update.message.reply_text(
                "❌ الاسم ما ينفع حسب الشروط.\n"
                "اكتبه عربي فقط وبكلمتين على الأقل وبشكل محترم.\n"
                "جرّب مرة ثانية 👇",
                reply_markup=ReplyKeyboardRemove()
            )
            return

        upsert_user(user_id)
        set_pending_name(user_id, text)
        context.user_data["awaiting_name"] = False

        await update.message.reply_text(
            "✅ تم استلام الاسم.\n"
            "صار بانتظار موافقة الأدمن 👑\n"
            "تقدر تلعب الحين، بس لوحة التميز ما تظهر إلا بعد الاعتماد.",
            reply_markup=ReplyKeyboardRemove()
        )

        # notify admins
        if ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"📝 طلب اعتماد اسم:\n• المستخدم: {user_id}\n• الاسم: {text}",
                        reply_markup=admin_pending_keyboard(user_id)
                    )
                except Exception as e:
                    logger.warning("Failed notifying admin %s: %s", admin_id, e)
        return

    # غير كذا تجاهل
    return

async def apply_answer_result(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, is_correct: bool):
    idx = context.user_data.get("round_index", 0)
    q = context.user_data.get("current_q") or {}
    chap = q.get("_chapter", "—")

    if is_correct:
        context.user_data["round_score"] += 1
        context.user_data["round_correct"] += 1
        context.user_data["round_streak"] += 1
        context.user_data["round_chapter_correct"][chap] = context.user_data["round_chapter_correct"].get(chap, 0) + 1

        streak = context.user_data["round_streak"]
        msg = random.choice(MOTIVATION_CORRECT)

        # streak bonus
        if streak % STREAK_BONUS_EVERY == 0:
            context.user_data["round_bonus"] += 1
            msg = f"{msg}\n\n{random.choice(MOTIVATION_BONUS)} (+1)"
        await context.bot.send_message(chat_id=chat_id, text=f"✅ صح!\n{msg}")

    else:
        context.user_data["round_streak"] = 0
        msg = random.choice(MOTIVATION_WRONG)
        await context.bot.send_message(chat_id=chat_id, text=f"❌ خطأ!\n{msg}")

    # mark seen
    qid = q.get("id", "")
    if qid:
        mark_seen(user_id, qid)

    # next
    context.user_data["round_index"] = idx + 1
    await send_next_question(chat_id, user_id, context)

async def finish_round(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, ended_by_user: bool):
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
        if tt == 0:
            continue
        lines.append(f"• {c}: {cc}/{tt}")

    if not user.get("is_approved", 0):
        lines.append("")
        lines.append("ℹ️ تقدر تجمع نقاط، بس لوحة التميز تظهر بعد اعتماد اسمك ✅")

    # تنظيف حالة الجولة
    for k in [
        "round_questions", "round_index", "round_score", "round_bonus",
        "round_correct", "round_streak", "round_chapter_correct",
        "round_chapter_total", "current_q", "term_correct_letter",
        "awaiting_name"
    ]:
        context.user_data.pop(k, None)

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
    upsert_user(user_id)
    user = get_user(user_id)
    await send_menu(chat_id, user, context)

# =========================
# Main
# =========================
def main():
    db_init()

    try:
        items = load_questions()
    except Exception as e:
        logger.exception("Failed loading questions file: %s", e)
        items = []

    items = ensure_question_ids(items)
    buckets = build_chapter_buckets(items) if items else None
    term_pool = [it.get("term") for it in items if isinstance(it, dict) and it.get("type") == "term" and it.get("term")]

    app = Application.builder().token(BOT_TOKEN).build()

    app.bot_data["questions_items"] = items
    app.bot_data["questions_buckets"] = buckets
    app.bot_data["term_pool"] = term_pool

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("pending", pending_command))

    # Callbacks (ترتيب مهم)
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^(ans_mcq:|ans_tf:|ans_term:|end_round)$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(play_round|leaderboard|my_stats|set_name)$"))

    # Text messages (اسم فقط)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_router))

    logger.info("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()