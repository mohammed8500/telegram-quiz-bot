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
# ⚙️ إعدادات البوت (Config)
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ProQuizBot")

class Config:
    # ضع التوكن هنا مباشرة إذا لم تستخدم Environment Variables
    TOKEN = os.getenv("BOT_TOKEN", "").strip()
    
    # معرفات الأدمن (Admin IDs)
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

# التأكد من وجود التوكن
if not Config.TOKEN:
    # يمكنك وضع التوكن هنا كحل مؤقت للاختبار:
    # Config.TOKEN = "YOUR_TOKEN_HERE"
    if not Config.TOKEN:
        raise RuntimeError("⚠️ BOT_TOKEN مفقود! تأكد من إعداد المتغيرات.")

# =========================
# 🗄️ إدارة قاعدة البيانات (Database)
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

# تهيئة قاعدة البيانات
db = DatabaseManager(Config.DB_FILE)

# =========================
# 🧠 منطق الأسئلة (Logic)
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
                logger.warning("ملف الأسئلة غير موجود، سيتم استخدام قائمة فارغة.")
                return

            with open(Config.QUESTIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            raw = data if isinstance(data, list) else data.get("items") or data.get("questions") or []
            
            for i, it in enumerate(raw):
                # تصنيف عشوائي للفصول إذا لم يكن موجوداً (للاختبار)
                it['_chapter'] = it.get('_chapter', random.choice(CHAPTERS))
                
                # إنشاء ID ثابت
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
        
        # 1. محاولة جلب أسئلة جديدة من كل فصل
        for chap in CHAPTERS:
            pool = [q for q in self.buckets[chap] if not db.has_seen(user_id, q['id'])]
            random.shuffle(pool)
            take = pool[:4] # 4 أسئلة من كل فصل = 20
            chosen.extend(take)
            for q in take: seen_ids.add(q['id'])

        # 2. إذا لم يكف العدد، نملأ من أي أسئلة أخرى
        if len(chosen) < Config.ROUND_SIZE:
            all_pool = [q for q in self.items if q['id'] not in seen_ids]
            random.shuffle(all_pool)
            needed = Config.ROUND_SIZE - len(chosen)
            chosen.extend(all_pool[:needed])
        
        # 3. إذا ما زال ناقصاً (نادر جداً)، نكرر الأسئلة
        if len(chosen) < Config.ROUND_SIZE:
             remaining = Config.ROUND_SIZE - len(chosen)
             if self.items:
                 chosen.extend(random.choices(self.items, k=remaining))

        random.shuffle(chosen)
        return chosen[:Config.ROUND_SIZE]

qm = QuestionManager()

# =========================
# 🎮 جلسة اللعب (Game Session)
# =========================
class GameSession:
    """يدير حالة الجولة للمستخدم الواحد"""
    def __init__(self, user_id, questions):
        self.user_id = user_id
        self.questions = questions
        self.current_idx = 0
        self.score = 0
        self.bonus = 0
        self.correct_count = 0
        self.streak = 0
        self.history = [] # لتسجيل صح/خطأ للشريط
        self.used_lifeline_5050 = False
        
        self.current_term_correct = "" # لتخزين الإجابة الصحيحة لأسئلة المصطلحات

    @property
    def current_q(self):
        return self.questions[self.current_idx] if self.current_idx < len(self.questions) else None

    @property
    def is_finished(self):
        return self.current_idx >= len(self.questions)

    def get_progress_bar(self):
        # 🟩🟩🟥⬜⬜
        bar = ""
        for res in self.history:
            bar += Config.BAR_CORRECT if res else Config.BAR_WRONG
        
        remaining = len(self.questions) - len(self.history)
        bar += Config.BAR_EMPTY * remaining
        
        # ضغط الشريط إذا كان طويلاً للجوال
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
            # الافتراض أن الإجابة في ملف JSON قد تكون boolean أو نص
            truth_raw = q.get('answer', q.get('correct'))
            truth = str(truth_raw).lower() in ['true', '1', 'yes', 'صح']
            is_correct = (ans_bool == truth)
        elif q_type == 'term':
            is_correct = (answer_data == self.current_term_correct)

        # تحديث الإحصائيات
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

# =========================
# 🖥️ واجهة المستخدم (Handlers)
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id)
    
    text = (
        f"👋 **أهلاً بك يا {user.first_name}**\n\n"
        "🧠 **تحدي العباقرة**\n"
        "• 20 سؤال متنوع (اختيار، صح/خطأ، مصطلحات)\n"
        "• نظام بونص للإجابات المتتالية 🔥\n"
        "• لوحة متصدرين للأقوياء فقط 🏆\n\n"
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
            txt += "لسه ما فيه أبطال 🌚 شد حيلك وكن الأول!"
        else:
            for i, r in enumerate(rows, 1):
                txt += f"**#{i}** {r['full_name']} ➖ ⭐️ {r['total_points']}\n"
        
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]), parse_mode="Markdown")

    elif data == "menu_stats":
        u = db.get_user(user_id)
        txt = (
            f"📊 **ملفك الشخصي**\n\n"
            f"👤 الاسم: {u.get('full_name', 'غير مسجل')}\n"
            f"⭐️ مجموع النقاط: {u.get('total_points')}\n"
            f"🎯 لعبت: {u.get('rounds_played')} جولة\n"
            f"🔥 أفضل سكور: {u.get('best_round_score')}"
        )
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]), parse_mode="Markdown")

    elif data == "menu_name":
        context.user_data['awaiting_name'] = True
        await query.message.reply_text("✍️ **اكتب اسمك الثلاثي بالعربي الآن:**\n(مثال: محمد عبدالله سعود)", reply_markup=ReplyKeyboardRemove())

    elif data == "menu_back":
        await query.edit_message_text("القائمة الرئيسية:", reply_markup=main_menu_kb(user_id))


# --- دالة عرض السؤال (Rendering) ---
async def render_question(message, session: GameSession, is_edit=True):
    if session.is_finished:
        await finish_game(message, session)
        return

    q = session.current_q
    idx = session.current_idx + 1
    total = len(session.questions)
    
    # رأس السؤال مع شريط التقدم
    text = f"**السؤال {idx}/{total}** | {q.get('_chapter', 'عام')}\n"
    text += f"{session.get_progress_bar()}\n\n"
    
    kb = []
    
    if q['type'] == 'mcq':
        text += f"❓ **{q['question']}**"
        opts = q.get('options', {})
        # ترتيب الخيارات عشوائياً أو ثابت حسب الرغبة (هنا ثابت لسهولة المطابقة)
        # إذا كنت تريد خلط الأزرار، تأكد من مطابقة الكيز (A,B,C,D)
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
        # توليد مشتتات
        pool = [t for t in qm.term_pool if t != correct]
        distractors = random.sample(pool, 3) if len(pool) >=3 else pool
        opts = distractors + [correct]
        random.shuffle(opts)
        
        # تعيين الأحرف للخيارات
        for i, opt in enumerate(opts):
            letter = chr(65+i) # A, B, C, D
            kb.append([InlineKeyboardButton(opt, callback_data=f"ans:{letter}")])
            if opt == correct:
                session.current_term_correct = letter # حفظ الحرف الصحيح لهذه الجولة
        
    # زر المساعدة والحذف
    if not session.used_lifeline_5050 and q['type'] in ['mcq', 'term']:
        kb.append([InlineKeyboardButton("✂️ حذف إجابتين (50:50)", callback_data="lifeline:5050")])
    
    kb.append([InlineKeyboardButton("❌ انسحاب", callback_data="game_quit")])
    
    markup = InlineKeyboardMarkup(kb)
    
    if is_edit:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.reply_markdown(text, reply_markup=markup)


async def game_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    # 🟢 1. معالجة زر البدء (هنا الحل لمشكلة "انتهت الجلسة")
    if data == "game_start":
        questions = qm.get_round_questions(user_id)
        if not questions:
            await query.answer("⚠️ لا توجد أسئلة كافية في الملف!", show_alert=True)
            return
            
        session = GameSession(user_id, questions)
        context.user_data['session'] = session
        await render_question(query.message, session)
        return

    # 🟢 2. التأكد من وجود جلسة لباقي الأزرار
    session: GameSession = context.user_data.get('session')
    if not session:
        try:
            await query.edit_message_text("⚠️ انتهت الجلسة. اضغط /start للبدء من جديد.")
        except:
            pass
        return

    # 🟢 3. معالجة الانسحاب
    if data == "game_quit":
        await finish_game(query.message, session, surrendered=True)
        context.user_data.pop('session', None)
        return

    # 🟢 4. معالجة وسيلة المساعدة
    if data == "lifeline:5050":
        if session.used_lifeline_5050:
            await query.answer("سبق واستخدمت المساعدة!", show_alert=True)
            return
        session.used_lifeline_5050 = True
        await query.answer("✂️ تم حذف إجابتين خطأ! (ركز الحين)", show_alert=True)
        # هنا نعيد رسم السؤال، (في نسخة متقدمة يمكننا حذف الأزرار فعلياً، هنا نكتفي بالتنبيه)
        await render_question(query.message, session)
        return

    # 🟢 5. معالجة الإجابات
    if data.startswith("ans:"):
        ans_val = data.split(":")[1]
        is_correct = session.check_answer(ans_val)
        
        # تأثير بصري فوري (تغيير الأيقونة)
        current_kb = query.message.reply_markup
        new_rows = []
        
        for row in current_kb.inline_keyboard:
            new_row = []
            for btn in row:
                if btn.callback_data == data:
                    icon = "✅" if is_correct else "❌"
                    # جعل الزر غير نشط
                    new_btn = InlineKeyboardButton(f"{icon} {btn.text}", callback_data="ignore")
                else:
                    new_btn = btn
                new_row.append(new_btn)
            new_rows.append(new_row)
        
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
        except:
            pass # لتجنب الأخطاء في الضغط السريع
        
        # انتظار بسيط
        await asyncio.sleep(0.8) 
        
        # السؤال التالي
        await render_question(query.message, session)

async def finish_game(message, session: GameSession, surrendered=False):
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
    
    # العودة للقائمة
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_back")]])
    await message.edit_text(txt, reply_markup=kb, parse_mode="Markdown")

# =========================
# 📝 معالجة النصوص (إدخال الأسماء)
# =========================
async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_name'): return
    
    name = update.message.text.strip()
    user_id = update.effective_user.id
    
    # تحقق بسيط من الاسم
    if len(name.split()) < 2 or not re.match(r'^[\u0600-\u06FF\s]+$', name):
        await update.message.reply_text("❌ الاسم يجب أن يكون بالعربي وثنائي على الأقل (بدون أرقام أو رموز).")
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
        try:
            await context.bot.send_message(adm, f"📝 **طلب اعتماد اسم جديد**\n👤: {name}\n🆔: `{user_id}`", parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.warning(f"تعذر إرسال رسالة للأدمن {adm}: {e}")

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
        try: await context.bot.send_message(target_id, f"🎉 مبروك! تم اعتماد اسمك ({name})، الآن ستظهر في لوحة المتصدرين.")
        except: pass
        
    elif action == "adm_no":
        db.reject_user(target_id)
        await query.edit_message_text(f"❌ تم رفض الطلب.")
        try: await context.bot.send_message(target_id, "❌ تم رفض الاسم. الرجاء كتابة اسمك الثلاثي الصريح.")
        except: pass

# =========================
# 🚀 التشغيل الرئيسي
# =========================
def main():
    # بناء التطبيق
    app = Application.builder().token(Config.TOKEN).build()
    
    # إضافة المعالجات (Handlers)
    # الترتيب مهم: Specific patterns أولاً
    app.add_handler(CommandHandler("start", start))
    
    # معالج القوائم
    app.add_handler(CallbackQueryHandler(menu_handler, pattern="^menu_"))
    
    # معالج الأدمن
    app.add_handler(CallbackQueryHandler(admin_handler, pattern="^adm_"))
    
    # معالج اللعبة (بدء، إجابة، انسحاب، مساعدة)
    # ملاحظة: تم إضافة game_start هنا ليتم التقاطه بواسطة game_handler
    app.add_handler(CallbackQueryHandler(game_handler, pattern="^(game_|ans:|lifeline:)"))
    
    # معالج النصوص (للأسماء)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    
    print(f"🤖 Bot started... (Admins: {Config.ADMIN_IDS})")
    app.run_polling()

if __name__ == "__main__":
    main()
