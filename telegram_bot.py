import os
import json
import random
import logging
import re
import sqlite3
import asyncio
from datetime import datetime
from typing import Dict, List, Any, Optional

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

from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest
from telegram.request import HTTPXRequest

# =========================
# ⚙️ إعدادات البوت
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ProQuizBot")


class Config:
    TOKEN = os.getenv("BOT_TOKEN", "").strip()

    # معرفات الأدمن (Admin IDs)
    ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
    _single = os.getenv("ADMIN_USER_ID", "").strip()
    if _single.isdigit():
        ADMIN_IDS.add(int(_single))

    DB_FILE = os.getenv("DB_FILE", "data.db")
    QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "questions_from_word.json")

    ROUND_SIZE = 20
    STREAK_BONUS_EVERY = 3
    TOP_N = 10

    # 🎨 عناصر التصميم
    BAR_CORRECT = "🟩"
    BAR_WRONG = "🟥"
    BAR_EMPTY = "⬜"


if not Config.TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables as BOT_TOKEN.")


# =========================
# ✅ إرسال/تعديل آمن (حل TimedOut)
# =========================
async def safe_send_message(bot, chat_id: int, text: str, **kwargs):
    retries = 4
    for attempt in range(1, retries + 1):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 3))
            logger.warning("RetryAfter: waiting %ss", wait)
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            logger.warning("Send timeout/network (attempt %s/%s): %s", attempt, retries, e)
            await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            logger.exception("Unexpected send error: %s", e)
            return None


async def safe_edit_message(query, text: str, **kwargs):
    """
    تعديل رسالة إنلاين بشكل آمن (بعض الأحيان تلغرام يرفض/يتأخر)
    """
    retries = 3
    for attempt in range(1, retries + 1):
        try:
            return await query.edit_message_text(text=text, **kwargs)
        except BadRequest as e:
            # مثال: Message is not modified / parse errors
            logger.warning("BadRequest edit: %s", e)
            return None
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 3))
            logger.warning("RetryAfter(edit): waiting %ss", wait)
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            logger.warning("Edit timeout/network (attempt %s/%s): %s", attempt, retries, e)
            await asyncio.sleep(1.2 * attempt)
        except Exception as e:
            logger.exception("Unexpected edit error: %s", e)
            return None


# =========================
# 🗄️ إدارة قاعدة البيانات
# =========================
class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    is_approved INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    total_points INTEGER DEFAULT 0,
                    rounds_played INTEGER DEFAULT 0,
                    best_round_score INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS pending_names (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    requested_at TEXT
                );
                CREATE TABLE IF NOT EXISTS seen_questions (
                    user_id INTEGER,
                    qid TEXT,
                    PRIMARY KEY (user_id, qid)
                );
                CREATE TABLE IF NOT EXISTS rounds (
                    round_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    score INTEGER DEFAULT 0,
                    bonus INTEGER DEFAULT 0,
                    correct INTEGER DEFAULT 0,
                    total INTEGER DEFAULT 0
                );
            """)

    def get_user(self, user_id: int):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            return dict(row) if row else {}

    def upsert_user(self, user_id: int):
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not exists:
                conn.execute("INSERT INTO users(user_id, created_at, updated_at) VALUES (?,?,?)", (user_id, now, now))
            else:
                conn.execute("UPDATE users SET updated_at=? WHERE user_id=?", (now, user_id))

    def get_all_users(self):
        with self._connect() as conn:
            return [row['user_id'] for row in conn.execute("SELECT user_id FROM users")]

    def set_pending_name(self, user_id: int, name: str):
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_names(user_id, full_name, requested_at) VALUES(?,?,?)",
                (user_id, name, now)
            )

    def approve_user(self, user_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT full_name FROM pending_names WHERE user_id=?", (user_id,)).fetchone()
            if row:
                conn.execute("UPDATE users SET full_name=?, is_approved=1 WHERE user_id=?", (row['full_name'], user_id))
                conn.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))
                return row['full_name']
        return ""

    def reject_user(self, user_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_names WHERE user_id=?", (user_id,))

    def get_pending_requests(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM pending_names ORDER BY requested_at")]

    def mark_seen(self, user_id: int, qid: str):
        if not qid:
            return
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO seen_questions(user_id, qid) VALUES(?,?)", (user_id, qid))

    def has_seen(self, user_id: int, qid: str) -> bool:
        if not qid:
            return False
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM seen_questions WHERE user_id=? AND qid=?",
                (user_id, qid)
            ).fetchone() is not None

    def save_round(self, user_id, score, bonus, correct, total):
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO rounds(user_id, started_at, finished_at, score, bonus, correct, total)
                VALUES(?,?,?,?,?,?,?)
            """, (user_id, now, now, score, bonus, correct, total))

            user = conn.execute(
                "SELECT total_points, rounds_played, best_round_score FROM users WHERE user_id=?",
                (user_id,)
            ).fetchone()
            if user:
                new_total = int(user['total_points']) + int(score) + int(bonus)
                new_rounds = int(user['rounds_played']) + 1
                new_best = max(int(user['best_round_score']), int(score) + int(bonus))
                conn.execute("""
                    UPDATE users SET total_points=?, rounds_played=?, best_round_score=?, updated_at=?
                    WHERE user_id=?
                """, (new_total, new_rounds, new_best, now, user_id))

    def get_leaderboard(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(f"""
                SELECT full_name, total_points, best_round_score
                FROM users
                WHERE is_approved=1 AND full_name IS NOT NULL AND TRIM(full_name) <> ''
                ORDER BY total_points DESC, best_round_score DESC
                LIMIT {Config.TOP_N}
            """)]

    def get_stats(self):
        with self._connect() as conn:
            users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            rounds_count = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
            pending_count = conn.execute("SELECT COUNT(*) FROM pending_names").fetchone()[0]
            return {"users": users_count, "rounds": rounds_count, "pending": pending_count}


db = DatabaseManager(Config.DB_FILE)

# =========================
# 🧠 أدوات صح/خطأ (مهم)
# =========================
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")

def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = _ARABIC_DIACRITICS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def parse_tf_value(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = normalize_arabic(str(v)).lower()
    # true
    if s in ("true", "1", "yes", "y", "صح", "صحيح", "ص"):
        return True
    # false
    if s in ("false", "0", "no", "n", "خطأ", "خطا"):
        return False
    return None


# =========================
# 🧠 منطق الأسئلة
# =========================
CHAPTERS = ["طبيعة العلم", "المخاليط والمحاليل", "حالات المادة", "الطاقة وتحولاتها", "أجهزة الجسم"]

class QuestionManager:
    def __init__(self):
        self.items = []
        self.buckets = {c: [] for c in CHAPTERS}
        self.term_pool = []
        self._load()

    def _load(self):
        try:
            if not os.path.exists(Config.QUESTIONS_FILE):
                logger.warning("ملف الأسئلة غير موجود.")
                return

            with open(Config.QUESTIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            raw = data if isinstance(data, list) else data.get("items") or data.get("questions") or []

            for i, it in enumerate(raw):
                # فصل افتراضي
                it['_chapter'] = it.get('_chapter', random.choice(CHAPTERS))

                # ID ثابت (أفضل من hash اللي يتغير أحياناً بين بيئات)
                base = str(it.get('id') or it.get('question') or it.get('term') or f"idx_{i}")
                base = re.sub(r"\s+", " ", base).strip()
                it['id'] = it.get('id') or f"q_{i}_{abs(hash(base))}"

                self.items.append(it)
                if it['_chapter'] in self.buckets:
                    self.buckets[it['_chapter']].append(it)

                if it.get('type') == 'term':
                    term = (it.get('term') or "").strip()
                    if term:
                        self.term_pool.append(term)

            logger.info(f"تم تحميل {len(self.items)} سؤال.")
        except Exception as e:
            logger.exception(f"خطأ في تحميل الأسئلة: {e}")

    def get_round_questions(self, user_id: int) -> List[dict]:
        chosen = []
        seen_ids = set()

        # 4 من كل فصل
        for chap in CHAPTERS:
            pool = [q for q in self.buckets.get(chap, []) if not db.has_seen(user_id, q.get('id', ''))]
            random.shuffle(pool)
            take = pool[:4]
            chosen.extend(take)
            for q in take:
                seen_ids.add(q.get('id'))

        # تكملة إلى 20
        if len(chosen) < Config.ROUND_SIZE:
            all_pool = [q for q in self.items if q.get('id') not in seen_ids]
            random.shuffle(all_pool)
            needed = Config.ROUND_SIZE - len(chosen)
            chosen.extend(all_pool[:needed])

        # إذا ما كفت الأسئلة (يعيد باستخدام أي شيء)
        if len(chosen) < Config.ROUND_SIZE and self.items:
            remaining = Config.ROUND_SIZE - len(chosen)
            chosen.extend(random.choices(self.items, k=remaining))

        random.shuffle(chosen)
        return chosen[:Config.ROUND_SIZE]

qm = QuestionManager()

# =========================
# 🎮 جلسة اللعب (Session)
# =========================
class GameSession:
    def __init__(self, user_id, questions):
        self.user_id = user_id
        self.questions = questions
        self.current_idx = 0
        self.score = 0
        self.bonus = 0
        self.correct_count = 0
        self.streak = 0
        self.history = []
        self.current_term_correct = ""
        self.current_term_text_map = {}

    @property
    def current_q(self):
        return self.questions[self.current_idx] if self.current_idx < len(self.questions) else None

    @property
    def is_finished(self):
        return self.current_idx >= len(self.questions)

    def get_progress_bar(self):
        bar = ""
        for res in self.history:
            bar += Config.BAR_CORRECT if res else Config.BAR_WRONG
        remaining = len(self.questions) - len(self.history)
        bar += Config.BAR_EMPTY * max(0, remaining)

        # لو كثير أسئلة اختصر
        if len(self.questions) > 15:
            return f"✅ {self.correct_count} | ❌ {len(self.history)-self.correct_count} | ⏳ {remaining}"
        return bar

    def check_answer(self, answer_data: str) -> bool:
        q = self.current_q
        if not q:
            return False

        q_type = q.get('type')
        is_correct = False

        if q_type == 'mcq':
            is_correct = (answer_data == str(q.get('correct', '')).strip().upper())

        elif q_type == 'tf':
            picked = True if answer_data == 'true' else False
            truth = parse_tf_value(q.get('answer'))
            if truth is None:
                truth = parse_tf_value(q.get('correct'))
            if truth is None:
                truth = False
            is_correct = (picked == truth)

        elif q_type == 'term':
            # هنا answer_data حرف A/B/C/D
            is_correct = (answer_data == self.current_term_correct)

        self.history.append(is_correct)

        if is_correct:
            self.score += 1
            self.correct_count += 1
            self.streak += 1
            if self.streak % Config.STREAK_BONUS_EVERY == 0:
                self.bonus += 1
            db.mark_seen(self.user_id, q.get('id'))
        else:
            self.streak = 0

        self.current_idx += 1
        return is_correct

    def get_correct_text(self):
        q = self.current_q
        if not q:
            return ""
        q_type = q.get('type')

        if q_type == 'mcq':
            correct_key = str(q.get('correct', '')).strip().upper()
            opts = q.get('options', {}) or {}
            return opts.get(correct_key, correct_key)

        if q_type == 'tf':
            truth = parse_tf_value(q.get('answer'))
            if truth is None:
                truth = parse_tf_value(q.get('correct'))
            if truth is None:
                truth = False
            return "صح" if truth else "خطأ"

        if q_type == 'term':
            return self.current_term_text_map.get(self.current_term_correct, q.get('term', ''))

        return ""


# =========================
# 🖥️ واجهة المستخدم (Handlers)
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id)
    text = (
        f"👋 **أهلاً بك يا {user.first_name}**\n\n"
        "🧠 **تحدي العباقرة**\n"
        "• 20 سؤال متنوع\n"
        "• كل 3 إجابات صحيحة متتالية = 🎁 +1 بونص\n\n"
        "👇 اختر من القائمة لبدء التحدي!"
    )
    await update.message.reply_markdown(text, reply_markup=main_menu_kb(user.id))


def main_menu_kb(user_id):
    user_data = db.get_user(user_id)
    status = "✅ معتمد" if user_data.get('is_approved') else "⚠️ غير معتمد"
    kb = [
        [InlineKeyboardButton("🎮 ابدأ التحدي", callback_data="game_start")],
        [InlineKeyboardButton("🏆 المتصدرين", callback_data="menu_leaderboard"),
         InlineKeyboardButton("📊 إحصائياتي", callback_data="menu_stats")],
        [InlineKeyboardButton(f"حالة الحساب: {status}", callback_data="menu_name")]
    ]
    return InlineKeyboardMarkup(kb)


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu_leaderboard":
        rows = db.get_leaderboard()
        txt = "🏆 **لوحة الأبطال (TOP 10)**\n\n"
        if not rows:
            txt += "لسه ما فيه أبطال 🌚"
        else:
            for i, r in enumerate(rows, 1):
                name = r.get('full_name') or "—"
                txt += f"**#{i}** {name} ➖ ⭐️ {r['total_points']}\n"

        await safe_edit_message(
            query,
            txt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]),
            parse_mode="Markdown"
        )
        return

    if data == "menu_stats":
        u = db.get_user(user_id)
        txt = (
            f"📊 **ملفك الشخصي**\n\n"
            f"👤 الاسم: {u.get('full_name', 'غير مسجل')}\n"
            f"⭐️ مجموع النقاط: {u.get('total_points', 0)}\n"
            f"🎯 لعبت: {u.get('rounds_played', 0)} جولة"
        )
        await safe_edit_message(
            query,
            txt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]),
            parse_mode="Markdown"
        )
        return

    if data == "menu_name":
        context.user_data['awaiting_name'] = True
        await safe_send_message(
            context.bot,
            query.message.chat_id,
            "✍️ **اكتب اسمك الثلاثي بالعربي الآن:**\n(مثال: محمد عبدالله سعود)",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if data == "menu_back":
        await safe_edit_message(query, "القائمة الرئيسية:", reply_markup=main_menu_kb(user_id))
        return


# --- دالة عرض السؤال ---
async def send_new_question(bot, chat_id, session: GameSession):
    if session.is_finished:
        await finish_game_msg(bot, chat_id, session)
        return

    q = session.current_q
    if not q:
        await safe_send_message(bot, chat_id, "⚠️ ما لقيت سؤال… جرب /start", reply_markup=ReplyKeyboardRemove())
        return

    idx = session.current_idx + 1
    total = len(session.questions)

    text = f"**السؤال {idx}/{total}**\n"
    text += f"{session.get_progress_bar()}\n\n"

    kb = []

    qtype = q.get('type')

    if qtype == 'mcq':
        text += f"❓ **{q.get('question','').strip()}**"
        opts = q.get('options', {}) or {}
        for k in ['A', 'B', 'C', 'D']:
            if k in opts:
                kb.append([InlineKeyboardButton(f"{k}) {opts[k]}", callback_data=f"ans:{k}")])

    elif qtype == 'tf':
        text += f"✅/❌ **{q.get('statement','').strip()}**"
        kb = [
            [InlineKeyboardButton("✅ صح", callback_data="ans:true"),
             InlineKeyboardButton("❌ خطأ", callback_data="ans:false")]
        ]

    elif qtype == 'term':
        text += f"📖 **{q.get('definition','').strip()}**\n\nما هو المصطلح المناسب؟"
        correct = (q.get('term') or "").strip()

        pool = [t for t in qm.term_pool if t != correct]
        distractors = random.sample(pool, 3) if len(pool) >= 3 else pool
        opts = distractors + ([correct] if correct else [])
        while len(opts) < 4:
            opts.append("—")
        opts = opts[:4]
        random.shuffle(opts)

        session.current_term_text_map = {}
        session.current_term_correct = ""

        for i, opt in enumerate(opts):
            letter = chr(65 + i)  # A B C D
            session.current_term_text_map[letter] = opt
            kb.append([InlineKeyboardButton(f"{letter}) {opt}", callback_data=f"ans:{letter}")])
            if opt == correct:
                session.current_term_correct = letter

    else:
        text += "⚠️ نوع سؤال غير معروف…"
        kb.append([InlineKeyboardButton("التالي ▶️", callback_data="ans:SKIP")])

    kb.append([InlineKeyboardButton("❌ انسحاب", callback_data="game_quit")])

    await safe_send_message(
        bot,
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


async def game_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if data == "game_start":
        db.upsert_user(user_id)
        questions = qm.get_round_questions(user_id)
        if not questions:
            await query.answer("⚠️ لا توجد أسئلة كافية!", show_alert=True)
            return
        session = GameSession(user_id, questions)
        context.user_data['session'] = session
        await send_new_question(context.bot, chat_id, session)
        return

    session: GameSession = context.user_data.get('session')
    if not session:
        await safe_send_message(context.bot, chat_id, "⚠️ انتهت الجلسة. اضغط /start من جديد.")
        return

    if data == "game_quit":
        await finish_game_msg(context.bot, chat_id, session, surrendered=True)
        context.user_data.pop('session', None)
        return

    if data.startswith("ans:"):
        ans_val = data.split(":")[1]

        # خزّن نص السؤال الحالي قبل ما يتغير المؤشر
        original_text = query.message.text or ""

        correct_text = session.get_correct_text()
        is_correct = session.check_answer(ans_val)

        if is_correct:
            result_msg = f"✅ **إجابة صحيحة!**\nالجواب: {correct_text}"
        else:
            result_msg = f"❌ **إجابة خاطئة!**\nالصحيح هو: {correct_text}"

        final_text = f"{original_text}\n\n───────────────\n{result_msg}"

        # نطفي الأزرار اللي راحت
        await safe_edit_message(query, final_text, reply_markup=None, parse_mode="Markdown")

        # احتفال بسيط كل 3 صح
        if is_correct and session.streak > 0 and session.streak % Config.STREAK_BONUS_EVERY == 0:
            try:
                msg = await safe_send_message(context.bot, chat_id, "🎆")
                await asyncio.sleep(2.0)
                if msg:
                    await msg.delete()
            except Exception:
                pass

        await asyncio.sleep(0.4)
        await send_new_question(context.bot, chat_id, session)
        return


async def finish_game_msg(bot, chat_id, session: GameSession, surrendered=False):
    db.save_round(session.user_id, session.score, session.bonus, session.correct_count, len(session.questions))

    total_score = session.score + session.bonus
    pct = int((session.correct_count / len(session.questions)) * 100) if session.questions else 0
    grade = "👑 أسطورة!" if pct >= 90 else "🔥 ممتاز" if pct >= 70 else "😅 حاول مرة ثانية"

    txt = (
        f"🏁 **انتهت الجولة**{' (انسحاب)' if surrendered else ''}\n\n"
        f"{grade}\n"
        f"✅ الإجابات: {session.correct_count}/{len(session.questions)}\n"
        f"🎁 البونص: {session.bonus}\n"
        f"💎 المجموع: **{total_score} نقطة**\n\n"
        f"{session.get_progress_bar()}"
    )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_back")]])
    await safe_send_message(bot, chat_id=chat_id, text=txt, reply_markup=kb, parse_mode="Markdown")


# =========================
# 📝 معالجة النصوص (إدخال الأسماء)
# =========================
async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not context.user_data.get('awaiting_name'):
        return

    name = update.message.text.strip()
    user_id = update.effective_user.id
    db.upsert_user(user_id)

    if len(name.split()) < 2 or not re.match(r'^[\u0600-\u06FF\s]+$', name):
        await update.message.reply_text("❌ الاسم لازم يكون بالعربي وكلمتين على الأقل.")
        return

    db.set_pending_name(user_id, name)
    context.user_data['awaiting_name'] = False

    await update.message.reply_text("✅ تم إرسال اسمك للمراجعة.", reply_markup=main_menu_kb(user_id))

    # إشعار الأدمن
    for adm in Config.ADMIN_IDS:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ قبول", callback_data=f"adm_ok:{user_id}"),
             InlineKeyboardButton("❌ رفض", callback_data=f"adm_no:{user_id}")]
        ])
        await safe_send_message(
            context.bot,
            adm,
            f"📝 **طلب اعتماد اسم**\n👤: {name}\n🆔: `{user_id}`",
            parse_mode="Markdown",
            reply_markup=kb
        )


# =========================
# 👮‍♂️ لوحة تحكم الأدمن + الإذاعة 📢
# =========================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.message.reply_text("⛔ هذا الأمر للمسؤولين فقط.")
        return

    stats = db.get_stats()
    txt = (
        f"👮‍♂️ **لوحة التحكم**\n\n"
        f"👥 المستخدمين: {stats['users']}\n"
        f"🎮 الجولات الملعوبة: {stats['rounds']}\n"
        f"⏳ طلبات الانتظار: {stats['pending']}\n\n"
        f"💡 للإرسال للجميع استخدم:\n`/broadcast رسالتك`"
    )

    kb_rows = [[InlineKeyboardButton("🔄 تحديث", callback_data="admin_refresh")]]
    if stats['pending'] > 0:
        kb_rows.insert(0, [InlineKeyboardButton(f"📋 عرض الطلبات المعلقة ({stats['pending']})", callback_data="admin_show_pending")])

    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode="Markdown")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        return

    message_to_send = " ".join(context.args).strip()
    if not message_to_send:
        await update.message.reply_text(
            "⚠️ **طريقة الاستخدام:**\n"
            "/broadcast اكتب رسالتك هنا\n\n"
            "مثال:\n`/broadcast السلام عليكم، رجعنا لكم بتحديث جديد! 🔥`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("⏳ **جاري الإرسال للجميع...**")

    all_users = db.get_all_users()
    success = 0
    failed = 0

    for uid in all_users:
        try:
            final_msg = f"📢 **إشعار إداري**\n\n{message_to_send}"
            res = await safe_send_message(context.bot, chat_id=uid, text=final_msg, parse_mode="Markdown")
            if res:
                success += 1
            else:
                failed += 1
            await asyncio.sleep(0.08)  # تأخير لتخفيف الضغط
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ **تم الانتهاء!**\n\n"
        f"📨 تم الإرسال لـ: {success}\n"
        f"🚫 فشل الإرسال لـ: {failed}"
    )


async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if user_id not in Config.ADMIN_IDS:
        return

    if data == "admin_refresh":
        stats = db.get_stats()
        txt = (
            f"👮‍♂️ **لوحة التحكم**\n\n"
            f"👥 المستخدمين: {stats['users']}\n"
            f"🎮 الجولات الملعوبة: {stats['rounds']}\n"
            f"⏳ طلبات الانتظار: {stats['pending']}\n\n"
            f"💡 للإرسال للجميع استخدم:\n`/broadcast رسالتك`"
        )
        kb_rows = [[InlineKeyboardButton("🔄 تحديث", callback_data="admin_refresh")]]
        if stats['pending'] > 0:
            kb_rows.insert(0, [InlineKeyboardButton(f"📋 عرض الطلبات المعلقة ({stats['pending']})", callback_data="admin_show_pending")])

        await safe_edit_message(query, txt, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode="Markdown")
        return

    if data == "admin_show_pending":
        pendings = db.get_pending_requests()
        if not pendings:
            await safe_send_message(context.bot, user_id, "✅ لا توجد طلبات معلقة حالياً.")
            return

        for p in pendings:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ قبول", callback_data=f"adm_ok:{p['user_id']}"),
                 InlineKeyboardButton("❌ رفض", callback_data=f"adm_no:{p['user_id']}")]
            ])
            await safe_send_message(
                context.bot,
                user_id,
                f"📝 **طلب معلق**\n👤: {p['full_name']}\n🆔: `{p['user_id']}`",
                parse_mode="Markdown",
                reply_markup=kb
            )
        return

    if data.startswith("adm_"):
        action, target_id = data.split(":")
        target_id = int(target_id)

        if action == "adm_ok":
            name = db.approve_user(target_id)
            await safe_edit_message(query, f"✅ تم اعتماد: {name}")
            await safe_send_message(context.bot, target_id, f"🎉 مبروك! تم اعتماد اسمك ({name})!")

        elif action == "adm_no":
            db.reject_user(target_id)
            await safe_edit_message(query, f"❌ تم رفض الطلب.")
            await safe_send_message(context.bot, target_id, "❌ تم رفض الاسم.")


# =========================
# 🧯 Error Handler (عشان ما يطيح التطبيق)
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling an update:", exc_info=context.error)


# =========================
# 🚀 التشغيل الرئيسي
# =========================
def main():
    # timeouts أعلى (يحسّن الاستقرار على Railway)
    request = HTTPXRequest(
        connect_timeout=20,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=60
    )

    app = (
        Application.builder()
        .token(Config.TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    # أوامر عامة
    app.add_handler(CommandHandler("start", start))

    # أوامر الأدمن
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(menu_handler, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(admin_handler, pattern="^(adm_|admin_)"))
    app.add_handler(CallbackQueryHandler(game_handler, pattern="^(game_|ans:)"))

    # إدخال اسم
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))

    # أهم شيء: error handler
    app.add_error_handler(error_handler)

    logger.info("🤖 Bot started... (Admins: %s)", Config.ADMIN_IDS)
    app.run_polling()


if __name__ == "__main__":
    main()