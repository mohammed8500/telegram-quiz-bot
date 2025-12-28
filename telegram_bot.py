import json
import os
import re
import random
import sqlite3
from difflib import SequenceMatcher
from typing import Dict, Any, Optional, List, Tuple, Set
from enum import Enum
from dataclasses import dataclass
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# الإعدادات والثوابت
# =========================
QUESTIONS_FILE = "questions_from_word.json"
DB_FILE = "bot_state.db"
TOKEN = os.getenv("BOT_TOKEN", "")

class QuestionType(Enum):
    MCQ = "mcq"
    TRUE_FALSE = "tf"
    SHORT_ANSWER = "short_answer"

class ButtonText:
    START_TEST = "🚀 ابدأ الاختبار"
    MY_RESULTS = "📊 نتيجتي"
    NEW_BANK = "♻️ بنك جديد"
    HELP = "❓ مساعدة"
    MAIN_MENU = "🏠 القائمة الرئيسية"
    SKIP = "⏭️ تخطي"

# =========================
# رسائل التحفيز والتشجيع
# =========================
class Messages:
    WELCOME = """
🎓 *مرحباً بك في بوت الاختبارات التعليمي* ✨

أنا هنا لأساعدك في الدراسة والتحضير للاختبارات
بطرق تفاعلية وممتعة!

📚 *ماذا يمكنك أن تفعل؟*
• حل اختبارات تفاعلية
• تحسين مهاراتك الدراسية
• متابعة تقدمك التعليمي

👇 اختر من الأزرار أدناه للبدء
"""

    PRAISE_CORRECT = [
        "🎯 إجابة صحيحة! رائع جداً!",
        "✨ ممتاز! أنت تبلي بلاءً حسناً!",
        "🏆 إجابة دقيقة! استمر في التميز!",
        "💫 أحسنت! دقة رائعة في التفكير!",
        "🌠 برافو! إجابة متقنة!",
        "✅ صحيح! أنت تسير على الطريق الصحيح!",
        "👏 إجابتك صحيحة! فخور بك!",
        "🚀 رائع! دقة وإبداع في الإجابة!"
    ]

    ENCOURAGE_WRONG = [
        "💪 ولا يهمك! كل تعلم يأتي مع تحديات",
        "📚 خطوة نحو التعلم! حاول مرة أخرى",
        "🌟 هذه فرصة للتعلم! جرب مرة أخرى",
        "🔍 راجع المعلومة وحاول مجدداً",
        "🌱 من الخطأ نتعلم! استمر في المحاولة",
        "🎓 التعلم رحلة! هذه محطة منها",
        "✨ اقتربت من الإجابة! حاول مرة أخرى",
        "🚀 لا تستسلم! الجولة القادمة أفضل"
    ]

    SKIP_PHRASES = [
        "⏭️ تم تخطي السؤال! دعنا ننتقل للتالي",
        "➡️ لنكمل! السؤال التالي في انتظارك",
        "🎯 دعنا ننتقل للسؤال التالي",
        "✨ سؤال جديد قادم! استعد له"
    ]

# =========================
# نماذج البيانات (Data Classes)
# =========================
@dataclass
class Question:
    id: str
    type: QuestionType
    question: str
    options: Dict[str, str]
    correct_key: Optional[str]
    correct_answer: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional['Question']:
        q_type = data.get("type")
        if not q_type:
            return None

        return cls(
            id=data.get("id", ""),
            type=QuestionType(q_type),
            question=(data.get("question", "") or "").strip(),
            options=data.get("options", {}) or {},
            correct_key=data.get("correct_key"),
            correct_answer=(data.get("correct", "") or "").strip()
        )

@dataclass
class UserState:
    user_id: int
    order: List[str]
    index: int
    score: int
    answered: int
    expecting_text: bool
    current_question_id: Optional[str]
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "index": self.index,
            "score": self.score,
            "answered": self.answered,
            "expecting_text": self.expecting_text,
            "current_question_id": self.current_question_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat()
        }

    @classmethod
    def from_dict(cls, user_id: int, data: Dict[str, Any]) -> 'UserState':
        now_iso = datetime.now().isoformat()
        return cls(
            user_id=user_id,
            order=data.get("order", []),
            index=int(data.get("index", 0)),
            score=int(data.get("score", 0)),
            answered=int(data.get("answered", 0)),
            expecting_text=bool(data.get("expecting_text", False)),
            current_question_id=data.get("current_question_id"),
            created_at=datetime.fromisoformat(data.get("created_at", now_iso)),
            updated_at=datetime.fromisoformat(data.get("updated_at", now_iso))
        )

# =========================
# معالجة النصوص العربية
# =========================
class ArabicTextProcessor:
    ARABIC_STOP_WORDS = {
        'هو', 'هي', 'هم', 'هن', 'هذا', 'هذه', 'ذلك', 'تلك',
        'الذي', 'التي', 'الذين', 'اللاتي',
        'يعني', 'تعني', 'يسمي', 'تسمي', 'يسمى', 'تسمى',
        'مادة', 'المادة', 'مواد', 'المواد',
        'شيء', 'الشيء', 'عبارة', 'تعريف', 'معنى',
        'عملية', 'عمليه', 'عمليات', 'عمليات'
    }

    @staticmethod
    def normalize_arabic(text: str) -> str:
        if not text:
            return ""

        # إزالة التشكيل
        text = re.sub(r'[\u0617-\u061A\u064B-\u0652\u0670]', '', text)

        # توحيد الحروف
        text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
        text = text.replace('ى', 'ي').replace('ئ', 'ي')
        text = text.replace('ة', 'ه')
        text = text.replace('ؤ', 'و')

        # تنظيف الرموز مع الحفاظ على المسافات
        text = re.sub(r'[^\u0600-\u06FF\u0750-\u077F0-9A-Za-z\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        return text.lower()

    @staticmethod
    def format_rtl(text: str) -> str:
        # للرسائل فقط (مو للأزرار)
        return f"{text}\u200F"

    @staticmethod
    def remove_al_prefix(word: str) -> str:
        if word.startswith('ال') and len(word) > 2:
            return word[2:]
        return word

    @staticmethod
    def extract_keywords(text: str) -> Set[str]:
        normalized = ArabicTextProcessor.normalize_arabic(text)
        words = normalized.split()
        keywords = {
            ArabicTextProcessor.remove_al_prefix(word)
            for word in words
            if word and word not in ArabicTextProcessor.ARABIC_STOP_WORDS
        }
        return {k for k in keywords if k}

    @staticmethod
    def calculate_similarity(text1: str, text2: str) -> float:
        n1 = ArabicTextProcessor.normalize_arabic(text1)
        n2 = ArabicTextProcessor.normalize_arabic(text2)
        if not n1 or not n2:
            return 0.0
        return SequenceMatcher(None, n1, n2).ratio()

    @staticmethod
    def check_term_match(user_answer: str, correct_answer: str) -> Tuple[bool, float]:
        user_norm = ArabicTextProcessor.normalize_arabic(user_answer)
        correct_norm = ArabicTextProcessor.normalize_arabic(correct_answer)

        if not user_norm or not correct_norm:
            return False, 0.0

        # 1) تطابق كامل
        if user_norm == correct_norm:
            return True, 1.0

        # 2) احتواء مباشر
        if user_norm in correct_norm or correct_norm in user_norm:
            return True, 0.95

        # 3) تشابه عالي
        sim = ArabicTextProcessor.calculate_similarity(user_answer, correct_answer)
        if sim >= 0.85:
            return True, sim

        # 4) كلمات مفتاحية (أقوى نقطة لتقبل "الغازية" = "المادة الغازية")
        user_keywords = ArabicTextProcessor.extract_keywords(user_answer)
        correct_keywords = ArabicTextProcessor.extract_keywords(correct_answer)

        if not user_keywords or not correct_keywords:
            return (sim >= 0.85), sim

        intersection = len(user_keywords.intersection(correct_keywords))
        if intersection >= 1:
            # تغطية الكلمات الصحيحة (مرن)
            coverage = intersection / max(len(correct_keywords), 1)
            if coverage >= 0.5:
                score = max(sim, 0.85, coverage)
                return True, score

            # جكارد (مرن شوي)
            union = len(user_keywords.union(correct_keywords))
            jaccard = intersection / max(union, 1)
            if jaccard >= 0.5:
                return True, max(sim, jaccard)

        return False, sim

# =========================
# إدارة الأسئلة
# =========================
class QuestionManager:
    def __init__(self, questions_file: str):
        self.questions_file = questions_file
        self.questions: List[Question] = []
        self.questions_map: Dict[str, Question] = {}
        self.load_questions()

    def load_questions(self) -> None:
        if not os.path.exists(self.questions_file):
            raise FileNotFoundError(f"ملف الأسئلة غير موجود: {self.questions_file}")

        with open(self.questions_file, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)

        if "items" not in data or not isinstance(data["items"], list):
            raise ValueError("صيغة ملف JSON غير صحيحة (لا يوجد items)")

        self.questions = []
        self.questions_map = {}

        for item in data["items"]:
            if item.get("has_figure", False):
                continue

            q = self._convert_question(item)
            if q:
                self.questions.append(q)
                self.questions_map[q.id] = q

    def _convert_question(self, item: Dict[str, Any]) -> Optional[Question]:
        qid = item.get("id")
        qtype = item.get("type")
        if not qid or not qtype:
            return None

        if qtype == "mcq":
            options = item.get("options", {}) or {}
            correct_key = item.get("correct")
            correct_text = options.get(correct_key, "") if correct_key else ""
            data = {
                "id": qid,
                "type": "mcq",
                "question": (item.get("question") or "").strip(),
                "options": options,
                "correct_key": correct_key,
                "correct": correct_text
            }
            return Question.from_dict(data)

        if qtype == "tf":
            ans = item.get("answer")
            if ans is True:
                correct_key = "T"
                correct_text = "صح"
            elif ans is False:
                correct_key = "F"
                correct_text = "خطأ"
            else:
                correct_key = None
                correct_text = ""

            data = {
                "id": qid,
                "type": "tf",
                "question": (item.get("statement") or "").strip(),
                "options": {"T": "صح", "F": "خطأ"},
                "correct_key": correct_key,
                "correct": correct_text
            }
            return Question.from_dict(data)

        if qtype == "term":
            data = {
                "id": qid,
                "type": "short_answer",
                "question": (item.get("definition") or "").strip(),
                "options": {},
                "correct_key": None,
                "correct": (item.get("term") or "").strip()
            }
            return Question.from_dict(data)

        # أي نوع غير معروف
        return None

    def get_question(self, question_id: str) -> Optional[Question]:
        return self.questions_map.get(question_id)

    def get_question_count(self) -> int:
        return len(self.questions)

    def shuffle_questions(self) -> List[str]:
        ids = [q.id for q in self.questions]
        random.shuffle(ids)
        return ids

# =========================
# إدارة قاعدة البيانات
# =========================
class DatabaseManager:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self._init_database()

    def _init_database(self) -> None:
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_states (
                    user_id INTEGER PRIMARY KEY,
                    state_data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_user_state(self, user_state: UserState) -> None:
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("""
                INSERT INTO user_states (user_id, state_data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    state_data = excluded.state_data,
                    updated_at = CURRENT_TIMESTAMP
            """, (user_state.user_id, json.dumps(user_state.to_dict(), ensure_ascii=False)))
            conn.commit()

    def load_user_state(self, user_id: int) -> Optional[UserState]:
        with sqlite3.connect(self.db_file) as conn:
            cur = conn.execute("SELECT state_data FROM user_states WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            try:
                data = json.loads(row[0])
                return UserState.from_dict(user_id, data)
            except Exception:
                return None

    def delete_user_state(self, user_id: int) -> None:
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
            conn.commit()

# =========================
# إدارة واجهة المستخدم
# =========================
class UIManager:
    @staticmethod
    def create_main_keyboard() -> ReplyKeyboardMarkup:
        keyboard = [
            [ButtonText.START_TEST, ButtonText.MY_RESULTS],
            [ButtonText.NEW_BANK, ButtonText.HELP],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    @staticmethod
    def create_question_keyboard(question: Question, question_number: int, total_questions: int) -> InlineKeyboardMarkup:
        rows: List[List[InlineKeyboardButton]] = []

        if question.type == QuestionType.MCQ:
            # A/B/C/D
            option_keys = ["A", "B", "C", "D"]
            for key in option_keys:
                if key not in question.options:
                    continue
                txt = f"{key}) {question.options[key]}"
                if len(txt) > 38:
                    txt = txt[:35] + "..."
                rows.append([InlineKeyboardButton(txt, callback_data=f"ans|{question.id}|{key}")])

        elif question.type == QuestionType.TRUE_FALSE:
            # صف واحد: صح / خطأ
            rows.append([
                InlineKeyboardButton("✅ صح", callback_data=f"ans|{question.id}|T"),
                InlineKeyboardButton("❌ خطأ", callback_data=f"ans|{question.id}|F"),
            ])

        # تحكم
        rows.append([
            InlineKeyboardButton(ButtonText.SKIP, callback_data=f"skip|{question.id}"),
            InlineKeyboardButton(ButtonText.HELP, callback_data="help"),
            InlineKeyboardButton(f"{question_number}/{total_questions}", callback_data="progress"),
        ])

        return InlineKeyboardMarkup(rows)

    @staticmethod
    def create_short_answer_keyboard(question_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(ButtonText.SKIP, callback_data=f"skip|{question_id}"),
            InlineKeyboardButton(ButtonText.HELP, callback_data="help"),
        ]])

    @staticmethod
    def format_question_text(question: Question, question_number: int, total_questions: int) -> str:
        progress = f"📍 السؤال: {question_number}/{total_questions}"

        if question.type == QuestionType.MCQ:
            text = f"""
🧩 *سؤال اختيار من متعدد*
────────────────────
{question.question}

{progress}
👇 اختر الإجابة الصحيحة:
""".strip()
            return ArabicTextProcessor.format_rtl(text)

        if question.type == QuestionType.TRUE_FALSE:
            text = f"""
🟩 *سؤال صح أو خطأ*
────────────────────
{question.question}

{progress}
👇 اختر صح أو خطأ:
""".strip()
            return ArabicTextProcessor.format_rtl(text)

        # SHORT_ANSWER
        text = f"""
✍️ *سؤال مصطلح / إجابة قصيرة*
────────────────────
{question.question}

{progress}
🟣 اكتب الإجابة برسالة:
""".strip()
        return ArabicTextProcessor.format_rtl(text)

    @staticmethod
    def format_results_text(score: int, answered: int, current_index: int, total_questions: int) -> str:
        percentage = (score / answered) * 100 if answered else 0
        text = f"""
📊 *نتيجتك الحالية*
────────────────────
✅ الصحيح: {score}
📝 المجاوب: {answered}
🎯 النسبة: {percentage:.1f}%

📍 موقفك: السؤال {min(current_index + 1, total_questions)} من {total_questions}
""".strip()
        return ArabicTextProcessor.format_rtl(text)

    @staticmethod
    def format_final_results_text(score: int, total: int) -> str:
        percentage = (score / total) * 100 if total else 0
        if percentage >= 90:
            emoji, msg = "🏆", "ممتاز! أنت متميز!"
        elif percentage >= 70:
            emoji, msg = "✨", "جيد جداً! استمر!"
        elif percentage >= 50:
            emoji, msg = "👍", "كويس! تقدر تتحسن أكثر!"
        else:
            emoji, msg = "💪", "ولا يهمك! أعد المحاولة وبتبدع!"

        text = f"""
🎉 *انتهى الاختبار!*
────────────────────
{emoji} ✅ نتيجتك: {score}/{total}
🎯 النسبة: {percentage:.1f}%

{msg}

♻️ تقدر تسوي *بنك جديد* وتعيد من البداية.
""".strip()
        return ArabicTextProcessor.format_rtl(text)

# =========================
# البوت الرئيسي
# =========================
class QuizBot:
    def __init__(self, token: str, questions_file: str, db_file: str):
        self.token = token
        self.question_manager = QuestionManager(questions_file)
        self.db_manager = DatabaseManager(db_file)
        self.ui = UIManager()
        self.text_processor = ArabicTextProcessor()

        self.application = Application.builder().token(token).build()
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        self.application.add_handler(CommandHandler("start", self.handle_start))
        self.application.add_handler(CommandHandler("help", self.handle_help))
        self.application.add_handler(CommandHandler("quiz", self.handle_quiz_start))
        self.application.add_handler(CommandHandler("stats", self.handle_stats))
        self.application.add_handler(CommandHandler("reset", self.handle_reset))

        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            ArabicTextProcessor.format_rtl(Messages.WELCOME.strip()),
            parse_mode="Markdown",
            reply_markup=self.ui.create_main_keyboard()
        )

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        help_text = """
❓ *طريقة استخدام البوت*
────────────────────
🚀 اضغط: *ابدأ الاختبار*
✅ أسئلة الاختيار/صح-خطأ: تختار من الأزرار
✍️ المصطلح/إجابة قصيرة: تكتب الإجابة برسالة

⏭️ تقدر *تخطي* أي سؤال
📊 تقدر تشوف نتيجتك من زر *نتيجتي*

أوامر:
• /start
• /quiz
• /stats
• /reset
""".strip()

        target = update.message or update.callback_query.message
        await target.reply_text(
            ArabicTextProcessor.format_rtl(help_text),
            parse_mode="Markdown",
            reply_markup=self.ui.create_main_keyboard()
        )

    async def handle_quiz_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await self.send_next_question(user_id, update)

    async def handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        st = self.db_manager.load_user_state(user_id)
        if not st:
            await update.message.reply_text(
                ArabicTextProcessor.format_rtl("📊 ما بدأت اختبار للحين! اضغط 🚀 ابدأ الاختبار."),
                reply_markup=self.ui.create_main_keyboard()
            )
            return

        text = self.ui.format_results_text(st.score, st.answered, st.index, len(st.order))
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=self.ui.create_main_keyboard())

    async def handle_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        new_order = self.question_manager.shuffle_questions()
        st = UserState(
            user_id=user_id,
            order=new_order,
            index=0,
            score=0,
            answered=0,
            expecting_text=False,
            current_question_id=None,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
        self.db_manager.save_user_state(st)
        await update.message.reply_text(
            ArabicTextProcessor.format_rtl("♻️ تم إنشاء بنك أسئلة جديد! اضغط 🚀 ابدأ الاختبار."),
            reply_markup=self.ui.create_main_keyboard()
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        data = query.data or ""
        if data == "help":
            await self.handle_help(update, context)
            return

        if data.startswith("skip|"):
            await self.handle_skip(update, context)
            return

        if data.startswith("ans|"):
            await self.handle_answer(update, context)
            return

    async def handle_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user_id = update.effective_user.id
        st = self.db_manager.load_user_state(user_id)
        if not st:
            return

        st.index += 1
        st.expecting_text = False
        st.current_question_id = None
        st.updated_at = datetime.now()
        self.db_manager.save_user_state(st)

        await query.message.reply_text(
            ArabicTextProcessor.format_rtl(random.choice(Messages.SKIP_PHRASES)),
            reply_markup=self.ui.create_main_keyboard()
        )
        await self.send_next_question(user_id, update)

    async def handle_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user_id = update.effective_user.id

        _, question_id, chosen_key = query.data.split("|", 2)

        st = self.db_manager.load_user_state(user_id)
        if not st or st.current_question_id != question_id:
            await query.message.reply_text(
                ArabicTextProcessor.format_rtl("⚠️ هذا السؤال انتهى، اضغط 🚀 ابدأ الاختبار للمتابعة."),
                reply_markup=self.ui.create_main_keyboard()
            )
            return

        q = self.question_manager.get_question(question_id)
        if not q:
            return

        st.answered += 1
        st.updated_at = datetime.now()

        is_correct = (chosen_key == (q.correct_key or ""))

        if is_correct:
            st.score += 1
            praise = random.choice(Messages.PRAISE_CORRECT)
            msg = f"{praise}\n📌 الصحيح: *{q.correct_answer}*"
        else:
            enc = random.choice(Messages.ENCOURAGE_WRONG)
            msg = f"{enc}\n📌 الصحيح: *{q.correct_answer}*"

        st.index += 1
        st.expecting_text = False
        st.current_question_id = None
        self.db_manager.save_user_state(st)

        await query.message.reply_text(
            ArabicTextProcessor.format_rtl(msg),
            parse_mode="Markdown",
            reply_markup=self.ui.create_main_keyboard()
        )

        await self.send_next_question(user_id, update)

    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.message.text or "").strip()
        user_id = update.effective_user.id

        # أزرار القائمة
        if text in [ButtonText.START_TEST, "ابدأ الاختبار", "اختبار"]:
            await self.handle_quiz_start(update, context)
            return
        if text in [ButtonText.MY_RESULTS, "نتيجتي", "إحصائيات"]:
            await self.handle_stats(update, context)
            return
        if text in [ButtonText.NEW_BANK, "بنك جديد", "اعادة"]:
            await self.handle_reset(update, context)
            return
        if text in [ButtonText.HELP, "مساعدة"]:
            await self.handle_help(update, context)
            return

        # إجابة كتابية
        st = self.db_manager.load_user_state(user_id)
        if not st or not st.expecting_text or not st.current_question_id:
            return

        q = self.question_manager.get_question(st.current_question_id)
        if not q or q.type != QuestionType.SHORT_ANSWER:
            return

        ok, score = self.text_processor.check_term_match(text, q.correct_answer)

        st.answered += 1
        st.updated_at = datetime.now()

        if ok:
            st.score += 1
            praise = random.choice(Messages.PRAISE_CORRECT)
            msg = f"{praise}\n📌 الصحيح: *{q.correct_answer}*"
        else:
            enc = random.choice(Messages.ENCOURAGE_WRONG)
            msg = f"{enc}\n📌 الصحيح: *{q.correct_answer}*"

        st.index += 1
        st.expecting_text = False
        st.current_question_id = None
        self.db_manager.save_user_state(st)

        await update.message.reply_text(
            ArabicTextProcessor.format_rtl(msg),
            parse_mode="Markdown",
            reply_markup=self.ui.create_main_keyboard()
        )

        await self.send_next_question(user_id, update)

    async def send_next_question(self, user_id: int, update: Update) -> None:
        st = self.db_manager.load_user_state(user_id)

        if not st:
            st = UserState(
                user_id=user_id,
                order=self.question_manager.shuffle_questions(),
                index=0,
                score=0,
                answered=0,
                expecting_text=False,
                current_question_id=None,
                created_at=datetime.now(),
                updated_at=datetime.now()
            )
            self.db_manager.save_user_state(st)

        # انتهاء الاختبار
        if st.index >= len(st.order):
            target = update.message or update.callback_query.message
            await target.reply_text(
                self.ui.format_final_results_text(st.score, len(st.order)),
                parse_mode="Markdown",
                reply_markup=self.ui.create_main_keyboard()
            )
            return

        qid = st.order[st.index]
        q = self.question_manager.get_question(qid)
        if not q:
            st.index += 1
            self.db_manager.save_user_state(st)
            await self.send_next_question(user_id, update)
            return

        st.current_question_id = qid
        st.expecting_text = (q.type == QuestionType.SHORT_ANSWER)
        st.updated_at = datetime.now()
        self.db_manager.save_user_state(st)

        text = self.ui.format_question_text(q, st.index + 1, len(st.order))
        target = update.message or update.callback_query.message

        if q.type == QuestionType.SHORT_ANSWER:
            kb = self.ui.create_short_answer_keyboard(qid)
        else:
            kb = self.ui.create_question_keyboard(q, st.index + 1, len(st.order))

        await target.reply_text(text, parse_mode="Markdown", reply_markup=kb)

    def run(self) -> None:
        if not self.token:
            raise ValueError("يرجى تعيين متغير البيئة BOT_TOKEN في Railway")

        print("🤖 Bot is running...")
        print(f"📚 Questions loaded: {self.question_manager.get_question_count()}")
        self.application.run_polling()

def main():
    bot = QuizBot(TOKEN, QUESTIONS_FILE, DB_FILE)
    bot.run()

if __name__ == "__main__":
    main()