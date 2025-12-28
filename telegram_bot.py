import os
import json
import re
import random
import sqlite3
import logging
from enum import Enum
from dataclasses import dataclass, asdict
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, Any, Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
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
# 1. الإعدادات والتهيئة
# =========================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CONFIG = {
    "TOKEN": os.getenv("BOT_TOKEN", ""),
    "QUESTIONS_FILE": "questions_from_word.json",
    "DB_FILE": "bot_state.db"
}

# =========================
# 2. أدوات اللغة العربية
# =========================
class ArabicUtils:
    """أدوات لمعالجة النصوص العربية وضبط الاتجاه"""

    RLM = "\u200F"

    @staticmethod
    def add_rtl(text: str) -> str:
        if not text:
            return ""
        return "\n".join([f"{ArabicUtils.RLM}{line}" for line in text.split('\n')])

    @staticmethod
    def normalize(text: str) -> str:
        if not text:
            return ""

        text = text.strip()
        # إزالة التشكيل والتطويل
        text = re.sub(r'[\u0617-\u061A\u064B-\u0652\u0640]', '', text)
        # توحيد الألف
        text = re.sub(r'[أإآ]', 'ا', text)
        # توحيد الياء
        text = text.replace('ى', 'ي')
        # توحيد التاء المربوطة
        text = text.replace('ة', 'ه')
        # إزالة الرموز
        text = re.sub(r'[^\w\s]', ' ', text)

        return re.sub(r'\s+', ' ', text).strip().lower()

    @staticmethod
    def smart_compare(user_answer: str, correct_answer: str) -> bool:
        """مقارنة ذكية للإجابات النصية + مرونة (مثل: التقنية/تقنية)"""
        norm_user = ArabicUtils.normalize(user_answer)
        norm_correct = ArabicUtils.normalize(correct_answer)

        if not norm_user or not norm_correct:
            return False

        # تطابق كامل
        if norm_user == norm_correct:
            return True

        # إذا الصحيح كلمة وحدة والمستخدم كتبها ضمن جملة
        if len(norm_correct.split()) == 1 and norm_correct in norm_user:
            return True

        # تشابه عام
        similarity = SequenceMatcher(None, norm_user, norm_correct).ratio()
        return similarity >= 0.85


# =========================
# 3. النصوص والهوية (اللهجة السعودية)
# =========================
class GameAssets:
    BTN_START = "🚀 ابدأ التحدي"
    BTN_STATS = "📊 وش سويت؟"
    BTN_RESET = "♻️ بنك جديد"
    BTN_HELP  = "💡 الفزعة"

    WELCOME_MSG = """
يا مرحبا ترحيبة البدو للعيد ⛺✨
حي الله عالِم المستقبل 🎓

البوت هذا فزعتك في المذاكرة!
نبي نختبر معلوماتك ونشوف إبداعك بطريقة ممتعة.

لا تبطي علينا..
اضغط *ابدأ التحدي* وورنا الدفرة! 💪
"""

    HELP_MSG = """
💡 *كيف تستخدم البوت؟*

• اضغط *ابدأ التحدي* عشان نطب في الأسئلة.
• في أسئلة المصطلحات، اكتب الإجابة وأرسلها (بدون لف ودوران 😉).
• إذا توهقت، اضغط *تخطي السؤال*.
• شيّك درجاتك من زر *وش سويت؟*.

الله يوفقك يا وحش 🌟
"""

    PRAISE_PHRASES = [
        "كفووو! جبتها صح يا ذيبان 🐺🔥",
        "يا سلام عليك! إجابة فخمة 😎✨",
        "حي عينك! كذا الشغل ولا بلاش 👏",
        "بيض الله وجهك! ما شاء الله عليك 🤍",
        "يا أسطورة! مضبوط مضبوط ✅👑",
        "فنااان! استمر يا وحش 🚀",
        "قدّها وقدود! ما عليك خوف 💪",
        "يا معلم! أنت اللي تعلمنا والله 😄"
    ]

    ENCOURAGE_PHRASES = [
        "ولا يضيق صدرك… بسيطة وتزين 💪🙂",
        "هاردلك! كانت قريبة والله 🤏😅",
        "معوض خير… ركّز بالسؤال الجاي 🔥",
        "ما عليه… الشاطر يتعلم من الغلطة 📚",
        "عوافي يا بطل… جرب مرة ثانية 👊",
        "ولاهمك… اللي بعده أسهل إن شاء الله ⏭️"
    ]

    SKIP_PHRASES = [
        "⏭️ تم… نكمل قدّام يا وحش!",
        "➡️ أوكي تخطينا… ركّز بالجاي!",
        "🎯 يلا السؤال اللي بعده!"
    ]


# =========================
# 4. نماذج البيانات
# =========================
class QuestionType(Enum):
    MCQ = "mcq"
    TRUE_FALSE = "tf"
    SHORT_ANSWER = "short_answer"

@dataclass
class Question:
    id: str
    type: QuestionType
    text: str
    options: Dict[str, str]
    correct_key: Optional[str]
    correct_text: str

    @staticmethod
    def _normalize_choice_key(k: Optional[str]) -> Optional[str]:
        if k is None:
            return None
        k = str(k).strip()
        if not k:
            return None
        # يعالج a/b/c/d -> A/B/C/D
        return k.upper()

    @classmethod
    def from_dict(cls, data: Dict) -> Optional['Question']:
        try:
            q_type_str = data.get("type")
            q_id = str(data.get("id", "")).strip()
            if not q_id or not q_type_str:
                return None

            if q_type_str == "mcq":
                options = data.get("options", {}) or {}
                raw_key = data.get("correct")
                correct_key = cls._normalize_choice_key(raw_key)

                # لو الخيارات مفاتيحها صغيرة، نجهز نسخة مفاتيحها Upper
                normalized_options = {}
                for k, v in options.items():
                    normalized_options[str(k).strip().upper()] = str(v).strip()

                correct_text = normalized_options.get(correct_key, "").strip()
                # لو فاضي: نخليه "A. النص" إذا قدرنا
                final_correct = (f"{correct_key}. {correct_text}".strip() if correct_key and correct_text else "").strip()

                return cls(
                    id=q_id,
                    type=QuestionType.MCQ,
                    text=str(data.get("question", "") or "").strip(),
                    options=normalized_options,               # مهم: نخزن المفاتيح A/B/C/D
                    correct_key=correct_key,
                    correct_text=final_correct or (correct_text or "").strip()
                )

            elif q_type_str == "tf":
                ans = data.get("answer")
                # مهم: لا نعتبر None = خطأ
                if ans is True:
                    ck, ct = "T", "صح"
                elif ans is False:
                    ck, ct = "F", "خطأ"
                else:
                    ck, ct = None, ""
                return cls(
                    id=q_id,
                    type=QuestionType.TRUE_FALSE,
                    text=str(data.get("statement", "") or "").strip(),
                    options={"T": "صح", "F": "خطأ"},
                    correct_key=ck,
                    correct_text=ct
                )

            elif q_type_str == "term":
                return cls(
                    id=q_id,
                    type=QuestionType.SHORT_ANSWER,
                    text=str(data.get("definition", "") or "").strip(),
                    options={},
                    correct_key=None,
                    correct_text=str(data.get("term", "") or "").strip()
                )

            return None

        except Exception as e:
            logger.error(f"Error parsing question {data.get('id')}: {e}")
            return None

@dataclass
class UserSession:
    user_id: int
    question_order: List[str]
    current_index: int = 0
    score: int = 0
    answered_count: int = 0
    current_q_id: Optional[str] = None
    is_waiting_text: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> 'UserSession':
        data = json.loads(json_str)
        return cls(**data)


# =========================
# 5. إدارة البيانات
# =========================
class QuestionBank:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.questions: Dict[str, Question] = {}
        self.load_questions()

    def load_questions(self):
        if not os.path.exists(self.filepath):
            logger.warning(f"File {self.filepath} not found.")
            return

        try:
            with open(self.filepath, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
                items = data.get("items", [])
                for item in items:
                    if item.get("has_figure"):
                        continue
                    q = Question.from_dict(item)
                    if q:
                        self.questions[q.id] = q
            logger.info(f"Loaded {len(self.questions)} questions.")
        except Exception as e:
            logger.error(f"Failed to load questions: {e}")

    def get_random_order(self) -> List[str]:
        ids = list(self.questions.keys())
        random.shuffle(ids)
        return ids

    def get_question(self, q_id: str) -> Optional[Question]:
        return self.questions.get(q_id)

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id INTEGER PRIMARY KEY,
                    data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def save_session(self, session: UserSession):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO sessions (user_id, data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (session.user_id, session.to_json()))

    def load_session(self, user_id: int) -> Optional[UserSession]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT data FROM sessions WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            if row:
                try:
                    return UserSession.from_json(row[0])
                except:
                    return None
            return None

    def get_stats(self) -> Tuple[int, int]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT data FROM sessions")
            rows = cur.fetchall()

        total_users = len(rows)
        active_users = 0
        for row in rows:
            try:
                data = json.loads(row[0])
                if data.get('answered_count', 0) > 0:
                    active_users += 1
            except:
                pass

        return total_users, active_users


# =========================
# 6. البوت ومنطق اللعبة
# =========================
class EducationalBot:
    def __init__(self):
        self.app = Application.builder().token(CONFIG["TOKEN"]).build()
        self.db = Database(CONFIG["DB_FILE"])
        self.q_bank = QuestionBank(CONFIG["QUESTIONS_FILE"])
        self.register_handlers()

    def register_handlers(self):
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("admin", self.cmd_admin))

        self.app.add_handler(MessageHandler(filters.Regex(f"^{GameAssets.BTN_START}$"), self.action_start_quiz))
        self.app.add_handler(MessageHandler(filters.Regex(f"^{GameAssets.BTN_STATS}$"), self.action_stats))
        self.app.add_handler(MessageHandler(filters.Regex(f"^{GameAssets.BTN_RESET}$"), self.action_reset))
        self.app.add_handler(MessageHandler(filters.Regex(f"^{GameAssets.BTN_HELP}$"), self.cmd_help))

        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_answer))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [GameAssets.BTN_START, GameAssets.BTN_STATS],
            [GameAssets.BTN_RESET, GameAssets.BTN_HELP]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            ArabicUtils.add_rtl(GameAssets.WELCOME_MSG),
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            ArabicUtils.add_rtl(GameAssets.HELP_MSG),
            parse_mode="Markdown"
        )

    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        ADMIN_ID = 290185541
        if update.effective_user.id != ADMIN_ID:
            return

        total, active = self.db.get_stats()
        msg = f"""
👮‍♂️ *لوحة المشرف*
────────────────
👥 عدد الطلاب (الدخول): {total}
📝 الطلاب المتفاعلين: {active}
💤 الطلاب الخاملين: {total - active}
────────────────
"""
        await update.message.reply_text(ArabicUtils.add_rtl(msg), parse_mode="Markdown")

    async def action_start_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        session = self.db.load_session(user_id)

        if not session or session.current_index >= len(session.question_order):
            order = self.q_bank.get_random_order()
            if not order:
                await update.message.reply_text("⚠️ عذراً، لا توجد أسئلة متاحة حالياً.")
                return
            session = UserSession(user_id=user_id, question_order=order)
            self.db.save_session(session)

        await self.ask_question(update, context, session)

    async def action_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        order = self.q_bank.get_random_order()
        session = UserSession(user_id=user_id, question_order=order)
        self.db.save_session(session)
        await update.message.reply_text(
            ArabicUtils.add_rtl("🔄 تم تصفير العداد وتجهيز بنك جديد!\nاضغط *ابدأ التحدي* 🔥"),
            parse_mode="Markdown"
        )

    async def action_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        session = self.db.load_session(user_id)
        if not session or session.answered_count == 0:
            await update.message.reply_text(ArabicUtils.add_rtl("📉 ما حليت شيء للحين… اضغط 🚀 ابدأ التحدي"))
            return

        percent = (session.score / session.answered_count) * 100
        msg = f"""
📊 *إحصائياتك الحالية:*

✅ الصح: {session.score}
📝 اللي جاوبت عليه: {session.answered_count}
📈 نسبتك: {percent:.1f}%

استمر يا ذيبان 💪🔥
"""
        await update.message.reply_text(ArabicUtils.add_rtl(msg), parse_mode="Markdown")

    async def ask_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE, session: UserSession):
        if session.current_index >= len(session.question_order):
            await self.finish_quiz(update, context, session)
            return

        q_id = session.question_order[session.current_index]
        question = self.q_bank.get_question(q_id)

        if not question:
            session.current_index += 1
            self.db.save_session(session)
            await self.ask_question(update, context, session)
            return

        session.current_q_id = q_id
        session.is_waiting_text = (question.type == QuestionType.SHORT_ANSWER)
        self.db.save_session(session)

        total = len(session.question_order)
        current = session.current_index + 1
        filled = int((current / total) * 10) if total else 0
        progress_bar = "🟩" * filled + "⬜" * (10 - filled)

        msg_text = f"""
📌 *السؤال {current} من {total}*
{progress_bar}

*{question.text}*
"""
        msg_text = ArabicUtils.add_rtl(msg_text.strip())

        keyboard = []
        if question.type == QuestionType.MCQ:
            for key in ["A", "B", "C", "D"]:
                if key in question.options:
                    btn_text = f"{key}. {question.options[key]}"
                    keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"ans:{key}")])

        elif question.type == QuestionType.TRUE_FALSE:
            keyboard.append([
                InlineKeyboardButton("✅ صح", callback_data="ans:T"),
                InlineKeyboardButton("❌ خطأ", callback_data="ans:F")
            ])
        else:
            msg_text += ArabicUtils.add_rtl("\n\n✍️ *اكتب إجابتك وأرسلها...*")

        keyboard.append([InlineKeyboardButton("⏭️ تخطي السؤال", callback_data="skip")])

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=msg_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        user_id = query.from_user.id
        session = self.db.load_session(user_id)

        if not session or not session.current_q_id:
            await query.message.reply_text(ArabicUtils.add_rtl("⚠️ انتهت صلاحية هذا السؤال."))
            return

        if data == "skip":
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except:
                pass

            session.current_index += 1
            self.db.save_session(session)

            await query.message.reply_text(ArabicUtils.add_rtl(random.choice(GameAssets.SKIP_PHRASES)))
            await self.ask_question(update, context, session)
            return

        if data.startswith("ans:"):
            selected_key = data.split(":")[1].strip().upper()
            question = self.q_bank.get_question(session.current_q_id)
            if not question:
                await query.message.reply_text(ArabicUtils.add_rtl("⚠️ ما لقيت السؤال، جرّب /start"))
                return

            is_correct = (selected_key == (question.correct_key or ""))
            # نرسل إجابة المستخدم كنص (للظهور عند الخطأ)
            user_choice_text = ""
            if question.type == QuestionType.MCQ:
                user_choice_text = f"{selected_key}. {question.options.get(selected_key, '').strip()}".strip()
            else:
                user_choice_text = "صح" if selected_key == "T" else "خطأ"

            await self.process_answer(update, context, session, question, is_correct, user_choice_text)

    async def handle_text_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        session = self.db.load_session(user_id)

        if not session or not session.is_waiting_text or not session.current_q_id:
            return

        user_text = update.message.text or ""
        question = self.q_bank.get_question(session.current_q_id)
        if not question:
            return

        is_correct = ArabicUtils.smart_compare(user_text, question.correct_text)
        await self.process_answer(update, context, session, question, is_correct, user_text)

    def _get_correct_display(self, q: Question) -> str:
        """ترجيع الإجابة الصحيحة بشكل مضمون"""
        if q.type == QuestionType.MCQ:
            ck = (q.correct_key or "").strip().upper()
            if ck and ck in q.options:
                return f"{ck}. {q.options[ck]}".strip()
            # fallback إذا correct_text جاهز
            if q.correct_text.strip():
                return q.correct_text.strip()
            return "غير متوفرة في ملف الأسئلة 🤷‍♂️"

        if q.type == QuestionType.TRUE_FALSE:
            if q.correct_key == "T":
                return "صح ✅"
            if q.correct_key == "F":
                return "خطأ ❌"
            return "غير متوفرة في ملف الأسئلة 🤷‍♂️"

        # SHORT_ANSWER
        return q.correct_text.strip() or "غير متوفرة في ملف الأسئلة 🤷‍♂️"

    async def process_answer(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session: UserSession,
        question: Question,
        is_correct: bool,
        user_answer_display: str
    ):
        session.answered_count += 1

        # إزالة الأزرار من السؤال السابق
        if update.callback_query:
            try:
                await update.callback_query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

        correct_display = self._get_correct_display(question)

        if is_correct:
            session.score += 1
            feedback = random.choice(GameAssets.PRAISE_PHRASES)
            msg = f"✅ *صح عليك!* \n\n{feedback}"
        else:
            feedback = random.choice(GameAssets.ENCOURAGE_PHRASES)
            msg = f"""
❌ *إجابة خاطئة!*

📝 *إجابتك:*
*{user_answer_display.strip()}*

✅ *الإجابة الصحيحة:*
*{correct_display}*

💡 {feedback}
""".strip()

        session.is_waiting_text = False
        session.current_index += 1
        self.db.save_session(session)

        chat_id = update.effective_chat.id
        message_id = update.effective_message.id

        await context.bot.send_message(
            chat_id=chat_id,
            text=ArabicUtils.add_rtl(msg),
            parse_mode="Markdown",
            reply_to_message_id=message_id
        )

        await self.ask_question(update, context, session)

    async def finish_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE, session: UserSession):
        total = len(session.question_order) or 1
        percent = (session.score / total) * 100

        if percent >= 90:
            grade = "🏆 كفوو! دافور أصلي"
        elif percent >= 75:
            grade = "✨ ممتاز يا وحش"
        elif percent >= 60:
            grade = "👍 حلو، بس شدّ حيلك شوي"
        else:
            grade = "📚 يبيلك مراجعة… ولا توقف!"

        final_msg = f"""
🏁 *انتهى التحدي!*

📊 نتيجتك: {session.score} من {total}
📈 النسبة: {percent:.1f}%
تقديرك: {grade}

اضغط *بنك جديد* وخلّنا نعيدها 🔥
"""
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=ArabicUtils.add_rtl(final_msg),
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([
                [GameAssets.BTN_RESET, GameAssets.BTN_START]
            ], resize_keyboard=True)
        )

    def run(self):
        print("🤖 Bot is starting...")
        self.app.run_polling()


if __name__ == "__main__":
    if not CONFIG["TOKEN"]:
        print("⚠️ تنبيه: BOT_TOKEN غير موجود. أضفه في Railway Variables.")
    bot = EducationalBot()
    bot.run()