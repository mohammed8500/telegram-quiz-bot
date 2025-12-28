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
    
    ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
    if os.getenv("ADMIN_USER_ID"):
        if os.getenv("ADMIN_USER_ID").strip().isdigit():
            ADMIN_IDS.add(int(os.getenv("ADMIN_USER_ID")))
    
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
    raise RuntimeError("⚠️ BOT_TOKEN مفقود! تأكد من إعداد المتغيرات.")

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

    def set_pending_name(self, user_id: int, name: str):
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO pending_names(user_id, full_name, requested_at) VALUES(?,?,?)", 
                         (user_id, name, now))

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

    def mark_seen(self, user_id: int, qid: str):
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO seen_questions(user_id, qid) VALUES(?,?)", (user_id, qid))

    def has_seen(self, user_id: int, qid: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM seen_questions WHERE user_id=? AND qid=?", (user_id, qid)).fetchone() is not None

    def save_round(self, user_id, score, bonus, correct, total):
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO rounds(user_id, started_at, finished_at, score, bonus, correct, total)
                VALUES(?,?,?,?,?,?,?)
            """, (user_id, now, now, score, bonus, correct, total))
            
            user = conn.execute("SELECT total_points, rounds_played, best_round_score FROM users WHERE user_id=?", (user_id,)).fetchone()
            if user:
                new_total = user['total_points'] + score + bonus
                new_rounds = user['rounds_played'] + 1
                new_best = max(user['best_round_score'], score + bonus)
                conn.execute("""
                    UPDATE users SET total_points=?, rounds_played=?, best_round_score=?, updated_at=?
                    WHERE user_id=?
                """, (new_total, new_rounds, new_best, now, user_id))

    def get_leaderboard(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(f"""
                SELECT full_name, total_points, best_round_score 
                FROM users WHERE is_approved=1 AND full_name IS NOT NULL 
                ORDER BY total_points DESC, best_round_score DESC LIMIT {Config.TOP_N}
            """)]

db = DatabaseManager(Config.DB_FILE)

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
                it['_chapter'] = it.get('_chapter', random.choice(CHAPTERS))
                base = str(it.get('question') or it.get('term') or i)
                it['id'] = f"q_{abs(hash(base))}"
                
                self.items.append(it)
                if it['_chapter'] in self.buckets:
                    self.buckets[it['_chapter']].append(it)
                
                if it.get('type') == 'term':
                    self.term_pool.append(it.get('term'))
            
            logger.info(f"تم تحميل {len(self.items)} سؤال.")
        except Exception as e:
            logger.error(f"خطأ في تحميل الأسئلة: {e}")

    def get_round_questions(self, user_id: int) -> List[dict]:
        chosen = []
        seen_ids = set()
        
        for chap in CHAPTERS:
            pool = [q for q in self.buckets[chap] if not db.has_seen(user_id, q['id'])]
            random.shuffle(pool)
            take = pool[:4]
            chosen.extend(take)
            for q in take: seen_ids.add(q['id'])

        if len(chosen) < Config.ROUND_SIZE:
            all_pool = [q for q in self.items if q['id'] not in seen_ids]
            random.shuffle(all_pool)
            needed = Config.ROUND_SIZE - len(chosen)
            chosen.extend(all_pool[:needed])
        
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
        self.current_term_correct = "" # تخزين الحرف الصحيح
        self.current_term_text_map = {} # تخزين النصوص المقابلة للأحرف

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
        bar += Config.BAR_EMPTY * remaining
        
        if len(self.questions) > 15 and len(bar) > 10:
             return f"✅ {self.correct_count} | ❌ {len(self.history)-self.correct_count} | ⏳ {remaining}"
        return bar

    def check_answer(self, answer_data: str) -> bool:
        q = self.current_q
        q_type = q.get('type')
        is_correct = False

        if q_type == 'mcq':
            is_correct = (answer_data == str(q.get('correct', '')).upper())
        elif q_type == 'tf':
            ans_bool = (answer_data == 'true')
            truth_raw = q.get('answer', q.get('correct'))
            truth = str(truth_raw).lower() in ['true', '1', 'yes', 'صح']
            is_correct = (ans_bool == truth)
        elif q_type == 'term':
            is_correct = (answer_data == self.current_term_correct)

        self.history.append(is_correct)
        if is_correct:
            self.score += 1
            self.correct_count += 1
            self.streak += 1
            if self.streak > 0 and self.streak % Config.STREAK_BONUS_EVERY == 0:
                self.bonus += 1
            db.mark_seen(self.user_id, q.get('id'))
        else:
            self.streak = 0
        
        self.current_idx += 1
        return is_correct

    def get_correct_text(self):
        """إرجاع النص الصحيح للسؤال الحالي لعرضه للمستخدم"""
        q = self.current_q
        if not q: return ""
        q_type = q.get('type')

        if q_type == 'mcq':
            correct_key = str(q.get('correct', '')).upper()
            opts = q.get('options', {})
            return opts.get(correct_key, correct_key) # يرجع نص الخيار
        
        elif q_type == 'tf':
            truth_raw = q.get('answer', q.get('correct'))
            truth = str(truth_raw).lower() in ['true', '1', 'yes', 'صح']
            return "صح" if truth else "خطأ"
            
        elif q_type == 'term':
            # نستخدم الـ map الذي خزنناه وقت العرض
            return self.current_term_text_map.get(self.current_term_correct, q.get('term'))
            
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
        "• احتفالات متحركة للمتفوقين 🎉\n"
        "• واجهة عصرية وسهلة\n\n"
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
                txt += f"**#{i}** {r['full_name']} ➖ ⭐️ {r['total_points']}\n"
        try:
            await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]), parse_mode="Markdown")
        except: pass

    elif data == "menu_stats":
        u = db.get_user(user_id)
        txt = (
            f"📊 **ملفك الشخصي**\n\n"
            f"👤 الاسم: {u.get('full_name', 'غير مسجل')}\n"
            f"⭐️ مجموع النقاط: {u.get('total_points')}\n"
            f"🎯 لعبت: {u.get('rounds_played')} جولة"
        )
        try:
            await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]), parse_mode="Markdown")
        except: pass

    elif data == "menu_name":
        context.user_data['awaiting_name'] = True
        await query.message.reply_text("✍️ **اكتب اسمك الثلاثي بالعربي الآن:**\n(مثال: محمد عبدالله سعود)", reply_markup=ReplyKeyboardRemove())

    elif data == "menu_back":
        try:
            await query.edit_message_text("القائمة الرئيسية:", reply_markup=main_menu_kb(user_id))
        except: pass

# --- دالة عرض السؤال (مع إخفاء اسم الفصل) ---
async def send_new_question(bot, chat_id, session: GameSession):
    if session.is_finished:
        await finish_game_msg(bot, chat_id, session)
        return

    q = session.current_q
    idx = session.current_idx + 1
    total = len(session.questions)
    
    # تم إخفاء اسم الفصل من هنا حسب الطلب
    text = f"**السؤال {idx}/{total}**\n"
    text += f"{session.get_progress_bar()}\n\n"
    
    kb = []
    
    if q['type'] == 'mcq':
        text += f"❓ **{q['question']}**"
        opts = q.get('options', {})
        for k in ['A', 'B', 'C', 'D']:
            if k in opts:
                kb.append([InlineKeyboardButton(opts[k], callback_data=f"ans:{k}")])
                
    elif q['type'] == 'tf':
        text += f"✅/❌ **{q['statement']}**"
        kb = [
            [InlineKeyboardButton("✅ صح", callback_data="ans:true"), InlineKeyboardButton("❌ خطأ", callback_data="ans:false")]
        ]
        
    elif q['type'] == 'term':
        text += f"📖 **{q['definition']}**\n\nما هو المصطلح المناسب؟"
        correct = q['term']
        pool = [t for t in qm.term_pool if t != correct]
        distractors = random.sample(pool, 3) if len(pool) >=3 else pool
        opts = distractors + [correct]
        random.shuffle(opts)
        
        session.current_term_text_map = {} # تصفير وتعبئة
        for i, opt in enumerate(opts):
            letter = chr(65+i) # A, B, C, D
            session.current_term_text_map[letter] = opt # حفظ الرابط بين الحرف والنص
            kb.append([InlineKeyboardButton(opt, callback_data=f"ans:{letter}")])
            if opt == correct:
                session.current_term_correct = letter
    
    kb.append([InlineKeyboardButton("❌ انسحاب", callback_data="game_quit")])
    
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def game_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    # 🟢 1. بدء اللعبة
    if data == "game_start":
        questions = qm.get_round_questions(user_id)
        if not questions:
            await query.answer("⚠️ لا توجد أسئلة كافية!", show_alert=True)
            return
        session = GameSession(user_id, questions)
        context.user_data['session'] = session
        await send_new_question(context.bot, chat_id, session)
        return

    # 🟢 2. التأكد من الجلسة
    session: GameSession = context.user_data.get('session')
    if not session:
        try: await context.bot.send_message(chat_id, "⚠️ انتهت الجلسة. اضغط /start من جديد.")
        except: pass
        return

    # 🟢 3. الانسحاب
    if data == "game_quit":
        await finish_game_msg(context.bot, chat_id, session, surrendered=True)
        context.user_data.pop('session', None)
        return

    # 🟢 4. معالجة الإجابة وتعديل السؤال القديم
    if data.startswith("ans:"):
        ans_val = data.split(":")[1]
        
        # حفظ بيانات السؤال الحالي قبل التحديث للحصول على النص
        correct_text = session.get_correct_text()
        original_text = query.message.text_markdown
        
        # التحقق وتحديث السكور
        is_correct = session.check_answer(ans_val)
        
        # بناء نص النتيجة
        if is_correct:
            result_msg = f"✅ **إجابة صحيحة!**\nالجواب: {correct_text}"
        else:
            result_msg = f"❌ **إجابة خاطئة!**\nالصحيح هو: {correct_text}"
        
        # 🔒 تعديل الرسالة القديمة (قفل السؤال)
        try:
            final_text = f"{original_text}\n\n───────────────\n{result_msg}"
            await query.edit_message_text(text=final_text, reply_markup=None, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Edit error: {e}")

        # 🎉 احتفال متحرك كل 3 إجابات (يظهر ويختفي)
        if is_correct and session.streak > 0 and session.streak % 3 == 0:
            try:
                # نرسل ستيكر ألعاب نارية (متحرك)
                # ملاحظة: نستخدم هذا الـ ID الخاص بتليجرام للألعاب النارية أو نرسل Emoji متحرك
                msg = await context.bot.send_message(chat_id, "🎆")
                await asyncio.sleep(2.5) # ننتظر قليلاً
                await msg.delete() # نحذف الرسالة (اختفاء)
            except Exception:
                pass 

        # ⏳ تأخير بسيط لقراءة النتيجة
        await asyncio.sleep(0.5) 
        
        # 📤 إرسال السؤال التالي
        await send_new_question(context.bot, chat_id, session)

async def finish_game_msg(bot, chat_id, session: GameSession, surrendered=False):
    db.save_round(session.user_id, session.score, session.bonus, session.correct_count, len(session.questions))
    
    total_score = session.score + session.bonus
    pct = int((session.correct_count / len(session.questions)) * 100) if session.questions else 0
    grade = "👑 أسطورة!" if pct >= 90 else "🔥 ممتاز" if pct >= 70 else "😅 حاول مرة ثانية"
    
    txt = (
        f"🏁 **انتهت الجولة**\n\n"
        f"{grade}\n"
        f"✅ الإجابات: {session.correct_count}/{len(session.questions)}\n"
        f"🎁 البونص: {session.bonus}\n"
        f"💎 المجموع: **{total_score} نقطة**\n\n"
        f"{session.get_progress_bar()}"
    )
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_back")]])
    await bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb, parse_mode="Markdown")

# =========================
# 📝 معالجة النصوص (إدخال الأسماء)
# =========================
async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_name'): return
    name = update.message.text.strip()
    user_id = update.effective_user.id
    
    if len(name.split()) < 2 or not re.match(r'^[\u0600-\u06FF\s]+$', name):
        await update.message.reply_text("❌ الاسم يجب أن يكون بالعربي وثنائي على الأقل.")
        return
        
    db.set_pending_name(user_id, name)
    context.user_data['awaiting_name'] = False
    
    await update.message.reply_text("✅ تم إرسال اسمك للمراجعة.", reply_markup=main_menu_kb(user_id))
    
    for adm in Config.ADMIN_IDS:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ قبول", callback_data=f"adm_ok:{user_id}"), 
             InlineKeyboardButton("❌ رفض", callback_data=f"adm_no:{user_id}")]
        ])
        try:
            await context.bot.send_message(adm, f"📝 **طلب اعتماد اسم**\n👤: {name}\n🆔: `{user_id}`", parse_mode="Markdown", reply_markup=kb)
        except: pass

async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if query.from_user.id not in Config.ADMIN_IDS: return
    
    action, target_id = data.split(":")
    target_id = int(target_id)
    
    if action == "adm_ok":
        name = db.approve_user(target_id)
        await query.edit_message_text(f"✅ تم اعتماد: {name}")
        try: await context.bot.send_message(target_id, f"🎉 مبروك! تم اعتماد اسمك ({name})!")
        except: pass
        
    elif action == "adm_no":
        db.reject_user(target_id)
        await query.edit_message_text(f"❌ تم رفض الطلب.")
        try: await context.bot.send_message(target_id, "❌ تم رفض الاسم.")
        except: pass

# =========================
# 🚀 التشغيل الرئيسي
# =========================
def main():
    app = Application.builder().token(Config.TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_handler, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(admin_handler, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(game_handler, pattern="^(game_|ans:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    
    print(f"🤖 Bot started... (Admins: {Config.ADMIN_IDS})")
    app.run_polling()

if __name__ == "__main__":
    main()
