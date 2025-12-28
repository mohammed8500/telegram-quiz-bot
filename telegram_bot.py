import json
import os
import re
import random
import sqlite3
import html
from difflib import SequenceMatcher
from typing import Dict, Any, Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# إعدادات
# =========================
QUESTIONS_FILE = "questions_from_word.json"
DB_FILE = "bot_state.db"

# ضع التوكن في Railway كـ Variable باسم BOT_TOKEN
TOKEN = os.getenv("BOT_TOKEN", "").strip()

# =========================
# تحفيز
# =========================
PRAISE_CORRECT = [
    "😄 كفووو! بطل!",
    "🔥 يا سلام عليك!",
    "🏆 بطّطّل! ممتاز!",
    "⭐ أسطووورة!",
    "🎯 فناااان!",
    "🫡 حي راسك!",
    "😎 معلم! استمر!",
    "🥳 يا شيخ! إبداع!",
]
ENCOURAGE_WRONG = [
    "🙂 ولا يهمك! حاول مرة ثانية 💪",
    "😅 بسيطة! الجاي أسهل 🔥",
    "📚 مو مشكلة! نتعلم ونكمل ✨",
    "💪 قدّها يا بطل!",
    "🌟 كمل.. أنت أسطووورة!",
]
SKIP_PHRASES = [
    "⏭️ تمام! نعدّيها ونكمل 😄",
    "⏭️ أوكي! الجاي عليك 🔥",
    "⏭️ ما عليه! نكمل بسرعة 🚀",
]

def pick(arr: List[str]) -> str:
    return random.choice(arr) if arr else ""

def safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default

# =========================
# واجهة (أزرار ثابتة)
# =========================
BTN_MENU = "القائمة 🏠"
BTN_HELP = "مساعدة ❓"
BTN_SKIP = "تخطي ⏭️"

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(BTN_MENU), KeyboardButton(BTN_HELP), KeyboardButton(BTN_SKIP)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="اختر من القائمة أو جاوب…",
    )

# =========================
# SQLite: حفظ تقدم المستخدم
# =========================
def db_connect():
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            state_json TEXT NOT NULL
        )
    """)
    return con

def load_user_state(user_id: int) -> Optional[Dict[str, Any]]:
    con = db_connect()
    try:
        cur = con.execute("SELECT state_json FROM user_state WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None
    finally:
        con.close()

def save_user_state(user_id: int, state: Dict[str, Any]) -> None:
    con = db_connect()
    try:
        con.execute(
            "INSERT INTO user_state(user_id,state_json) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET state_json=excluded.state_json",
            (user_id, json.dumps(state, ensure_ascii=False))
        )
        con.commit()
    finally:
        con.close()

# =========================
# أدوات مقارنة / تطبيع
# =========================
def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    # إزالة التشكيل
    text = re.sub(r"[\u0617-\u061A\u064B-\u0652]", "", text)
    # توحيد بعض الحروف
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    # إزالة الرموز
    text = re.sub(r"[^\u0600-\u06FF0-9A-Za-z\s]", " ", text)
    # مسافات
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

def sorted_mcq_keys(keys: List[str]) -> List[str]:
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    return sorted(keys, key=lambda k: order.get(k, 999))

# =========================
# تحميل الأسئلة من JSON (صيغة ملفك)
# =========================
def load_questions() -> List[Dict[str, Any]]:
    if not os.path.exists(QUESTIONS_FILE):
        raise FileNotFoundError(f"ما لقيت {QUESTIONS_FILE} بنفس مجلد البوت.")

    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "items" not in data or not isinstance(data["items"], list):
        raise ValueError("صيغة JSON غير صحيحة: لازم يكون فيه مفتاح items على شكل قائمة.")

    converted: List[Dict[str, Any]] = []
    for it in data["items"]:
        if it.get("has_figure", False):
            continue

        qid = it.get("id")
        t = it.get("type")
        if not qid or not t:
            continue

        if t == "mcq":
            options = it.get("options") or {}
            correct_key = it.get("correct")
            converted.append({
                "id": qid,
                "type": "mcq",
                "question": (it.get("question") or "").strip(),
                "options": options,
                "correct_key": correct_key,
                "correct": options.get(correct_key, "") if correct_key else "",
            })

        elif t == "tf":
            ans = it.get("answer")
            correct_key = "صح" if ans is True else "خطأ" if ans is False else None
            converted.append({
                "id": qid,
                "type": "tf",
                "question": (it.get("statement") or "").strip(),
                "options": {"صح": "صح", "خطأ": "خطأ"},
                "correct_key": correct_key,
                "correct": correct_key or "",
            })

        elif t == "term":
            converted.append({
                "id": qid,
                "type": "short_answer",
                "question": (it.get("definition") or "").strip(),
                "correct": (it.get("term") or "").strip(),
            })

    return converted

QUESTIONS = load_questions()
QMAP = {q["id"]: q for q in QUESTIONS}

# =========================
# حالة المستخدم
# =========================
def new_state() -> Dict[str, Any]:
    order = [q["id"] for q in QUESTIONS]
    random.shuffle(order)
    return {
        "order": order,
        "idx": 0,
        "score": 0,
        "answered": 0,
        "expecting_text": False,
        "current_qid": None,
    }

def get_state(user_id: int) -> Dict[str, Any]:
    st = load_user_state(user_id)
    if not st:
        st = new_state()
        save_user_state(user_id, st)
    return st

def set_state(user_id: int, st: Dict[str, Any]) -> None:
    save_user_state(user_id, st)

def get_current_q(st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    order = st.get("order", [])
    idx = safe_int(st.get("idx", 0), 0)

    while idx < len(order) and order[idx] not in QMAP:
        idx += 1
    st["idx"] = idx

    if idx >= len(order):
        return None
    return QMAP.get(order[idx])

# =========================
# تنسيق جميل للسؤال
# =========================
def escape(s: str) -> str:
    return html.escape(s or "")

def label_type(qtype: str) -> str:
    if qtype == "mcq":
        return "اختيار من متعدد 🎯"
    if qtype == "tf":
        return "صح / خطأ ✅❌"
    if qtype == "short_answer":
        return "مصطلح / إجابة قصيرة ✍️"
    return "سؤال"

def format_header(st: Dict[str, Any]) -> str:
    idx = safe_int(st.get("idx", 0), 0) + 1
    total = len(st.get("order", []))
    score = safe_int(st.get("score", 0), 0)
    answered = safe_int(st.get("answered", 0), 0)
    return (
        f"🧩 <b>سؤال {idx} / {total}</b>\n"
        f"📌 <b>الصحيح:</b> {score} | <b>المجاوب:</b> {answered}\n"
        f"────────────────────"
    )

def build_question_message(st: Dict[str, Any], q: Dict[str, Any]) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    qtype = q.get("type")
    title = label_type(qtype)
    qtext = escape((q.get("question") or "").strip())

    head = format_header(st)
    body_lines = [
        head,
        f"✳️ <b>{title}</b>",
        "",
        f"{qtext}",
        "",
    ]

    # اختيارات / صح خطأ
    if qtype in ("mcq", "tf"):
        options: Dict[str, str] = q.get("options") or {}
        if qtype == "tf":
            keys = ["صح", "خطأ"]
            body_lines.append("🟣 <b>اختر الإجابة:</b>")
        else:
            keys = sorted_mcq_keys(list(options.keys()))
            body_lines.append("🟣 <b>اختر الإجابة:</b>")
            for k in keys:
                body_lines.append(f"• <b>{escape(k)})</b> {escape(options.get(k, ''))}")

        # Inline buttons
        keyboard = []
        row = []
        for k in keys:
            row.append(InlineKeyboardButton(text=k, callback_data=f"ans|{q['id']}|{k}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("⏭️ تخطي", callback_data=f"skip|{q['id']}")])
        return "\n".join(body_lines), InlineKeyboardMarkup(keyboard)

    # مصطلح
    if qtype == "short_answer":
        body_lines.append("🟣 <b>اكتب الإجابة برسالة</b>")
        body_lines.append("💡 مثال: لو الصحيح (المادة الغازية) تقدر تكتب (الغازية) وتتحسب صح ✅")
        return "\n".join(body_lines), None

    return "\n".join(body_lines), None

# =========================
# إرسال السؤال التالي
# =========================
async def send_next_question(update: Update, user_id: int, st: Dict[str, Any]):
    q = get_current_q(st)
    target = update.message if update.message else update.callback_query.message

    if not q:
        score = safe_int(st.get("score", 0), 0)
        answered = safe_int(st.get("answered", 0), 0)
        await target.reply_text(
            "🎉 <b>خلصت الاختبار!</b>\n\n"
            f"📊 <b>نتيجتك:</b> {score} / {answered}\n"
            "♻️ اكتب /reset لو تبغى بنك جديد",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )
        return

    st["current_qid"] = q["id"]
    st["expecting_text"] = (q.get("type") == "short_answer")
    set_state(user_id, st)

    msg, markup = build_question_message(st, q)
    await target.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=markup if markup else main_menu_kb(),
    )

# =========================
# أوامر
# =========================
def help_text() -> str:
    return (
        "📚 <b>طريقة الاستخدام</b>\n"
        "────────────────────\n"
        "🚀 <b>/quiz</b> يبدأ الأسئلة\n"
        "📊 <b>/stats</b> يعرض نتيجتك\n"
        "♻️ <b>/reset</b> بنك جديد\n\n"
        "🟣 <b>الأزرار:</b>\n"
        f"• {BTN_MENU} = قائمة الأوامر\n"
        f"• {BTN_HELP} = شرح سريع\n"
        f"• {BTN_SKIP} = يتخطّى السؤال الحالي\n\n"
        "✍️ <b>المصطلحات:</b>\n"
        "لو الصحيح (المادة الغازية) وكتبت (الغازية) تتحسب ✅ صح.\n"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _ = get_state(user_id)
    await update.message.reply_text(
        "هلااا 😄👋\n"
        "أنا بوت أسئلة علوم ثاني متوسط ✨\n\n"
        "اضغط <b>القائمة 🏠</b> أو اكتب /quiz للبدء 🔥",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        help_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    await send_next_question(update, user_id, st)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    idx = min(safe_int(st.get("idx", 0), 0) + 1, len(st.get("order", [])))
    await update.message.reply_text(
        "📊 <b>إحصائياتك</b>\n"
        "────────────────────\n"
        f"✅ <b>الصحيح:</b> {safe_int(st.get('score', 0), 0)}\n"
        f"🧾 <b>المجاوب عليه:</b> {safe_int(st.get('answered', 0), 0)}\n"
        f"📍 <b>وصلت للسؤال:</b> {idx} من {len(st.get('order', []))}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = new_state()
    set_state(user_id, st)
    await update.message.reply_text(
        "♻️ تم إنشاء بنك جديد!\nاكتب /quiz للبدء 😄",
        reply_markup=main_menu_kb(),
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 <b>القائمة</b>\n"
        "────────────────────\n"
        "🚀 /quiz بدء الاختبار\n"
        "📊 /stats نتيجتك\n"
        "♻️ /reset بنك جديد\n"
        "❓ /help المساعدة",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )

# =========================
# أزرار الإجابة + التخطي (Inline)
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    st = get_state(user_id)

    data = query.data or ""
    parts = data.split("|")
    action = parts[0] if parts else ""

    if action == "skip":
        st["idx"] = safe_int(st.get("idx", 0), 0) + 1
        st["expecting_text"] = False
        st["current_qid"] = None
        set_state(user_id, st)
        await query.message.reply_text(pick(SKIP_PHRASES), reply_markup=main_menu_kb())
        await send_next_question(update, user_id, st)
        return

    if action != "ans" or len(parts) != 3:
        return

    _, qid, chosen_key = parts
    if st.get("current_qid") != qid:
        await query.message.reply_text("⚠️ هذا سؤال قديم. اكتب /quiz للمتابعة.", reply_markup=main_menu_kb())
        return

    q = QMAP.get(qid)
    if not q:
        return

    st["answered"] = safe_int(st.get("answered", 0), 0) + 1
    correct_key = q.get("correct_key")
    correct_text = q.get("correct", "")

    if chosen_key == correct_key:
        st["score"] = safe_int(st.get("score", 0), 0) + 1
        await query.message.reply_text(
            f"{pick(PRAISE_CORRECT)} ✅\n"
            f"✅ الصحيح: <b>{escape(correct_key)})</b> {escape(correct_text)}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )
    else:
        await query.message.reply_text(
            f"{pick(ENCOURAGE_WRONG)} ❌\n"
            f"✅ الصحيح: <b>{escape(correct_key)})</b> {escape(correct_text)}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )

    st["idx"] = safe_int(st.get("idx", 0), 0) + 1
    st["expecting_text"] = False
    st["current_qid"] = None
    set_state(user_id, st)

    await send_next_question(update, user_id, st)

# =========================
# إجابة النص (يشمل: القائمة/المساعدة/تخطي + المصطلحات)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)

    text_raw = (update.message.text or "").strip()

    # أزرار القائمة (Reply Keyboard)
    if not st.get("expecting_text"):
        if text_raw == BTN_MENU:
            await menu(update, context)
            return
        if text_raw == BTN_HELP:
            await help_cmd(update, context)
            return
        if text_raw == BTN_SKIP:
            # تخطي السؤال الحالي حتى لو كان MCQ/TF
            st["idx"] = safe_int(st.get("idx", 0), 0) + 1
            st["expecting_text"] = False
            st["current_qid"] = None
            set_state(user_id, st)
            await update.message.reply_text(pick(SKIP_PHRASES), reply_markup=main_menu_kb())
            await send_next_question(update, user_id, st)
            return

    # إذا كنا ننتظر إجابة مصطلح
    if not st.get("expecting_text"):
        return

    qid = st.get("current_qid")
    q = QMAP.get(qid) if qid else None
    if not q or q.get("type") != "short_answer":
        st["expecting_text"] = False
        set_state(user_id, st)
        return

    user_answer = text_raw
    correct = (q.get("correct") or "").strip()

    a = normalize_arabic(user_answer)
    b = normalize_arabic(correct)

    # كلمات عامة ما نبغاها تأثر
    STOPWORDS = {
        "الماده", "ماده", "هو", "هي", "يسمى", "تسمى", "يعرف", "تعرف",
        "من", "في", "على", "الى", "إلى", "هذا", "هذه", "ذلك", "تلك",
        "يكون", "تكون", "عباره", "عبارة", "نوع", "شكل", "حجم", "الماده", "المادة"
    }

    def filt_tokens(s: str) -> List[str]:
        toks = [t for t in s.split() if t and t not in STOPWORDS]
        return toks

    ok = False

    # 1) تطابق كامل
    if a and a == b:
        ok = True

    # 2) احتواء واضح (الغازية داخل المادة الغازية)
    elif a and b and len(a) >= 4 and (a in b or b in a):
        ok = True

    else:
        ta = filt_tokens(a)
        tb = filt_tokens(b)

        if ta and tb:
            set_a = set(ta)
            set_b = set(tb)

            # 3) كل كلمات الطالب موجودة في الصحيح
            coverage = len(set_a & set_b) / max(1, len(set_a))
            if coverage >= 1.0:
                ok = True
            elif coverage >= 0.8 and len(set_a) >= 2:
                ok = True

            # 4) الطالب كتب آخر كلمة فقط (المادة الغازية -> الغازية)
            if not ok and len(ta) == 1 and tb:
                last_token = tb[-1]
                if ta[0] == last_token and len(ta[0]) >= 4 and ta[0] not in STOPWORDS:
                    ok = True

        # 5) تشابه عام كخيار أخير
        if not ok and a and b:
            ok = similarity(a, b) >= 0.78

    st["answered"] = safe_int(st.get("answered", 0), 0) + 1

    if ok:
        st["score"] = safe_int(st.get("score", 0), 0) + 1
        await update.message.reply_text(
            f"{pick(PRAISE_CORRECT)} ✅\n✅ الصحيح: <b>{escape(correct)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )
    else:
        await update.message.reply_text(
            f"{pick(ENCOURAGE_WRONG)} ❌\n✅ الصحيح: <b>{escape(correct)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )

    st["idx"] = safe_int(st.get("idx", 0), 0) + 1
    st["expecting_text"] = False
    st["current_qid"] = None
    set_state(user_id, st)

    await send_next_question(update, user_id, st)

# =========================
# تشغيل
# =========================
def main():
    if not TOKEN:
        raise RuntimeError("لازم تضيف BOT_TOKEN في Variables داخل Railway.")

    # تأكد DB جاهز
    db_connect().close()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("✅ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()