import json
import os
import re
import random
import sqlite3
from difflib import SequenceMatcher
from typing import Dict, Any, Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "questions_from_word.json")
DB_FILE = os.getenv("DB_FILE", "bot_state.db")

# ضع التوكن في Railway كـ Variable باسم BOT_TOKEN
TOKEN = os.getenv("BOT_TOKEN", "")

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
# أدوات
# =========================
def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u0617-\u061A\u064B-\u0652]", "", text)  # تشكيل
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    text = re.sub(r"[^\u0600-\u06FF0-9A-Za-z\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

def sorted_mcq_keys(keys: List[str]) -> List[str]:
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    return sorted(keys, key=lambda k: order.get(k, 999))

def esc(s: str) -> str:
    """Escape for HTML parse_mode."""
    if s is None:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))

# =========================
# تحميل الأسئلة (Lazy) عشان ما يطيّح البوت لو الملف ناقص
# =========================
QUESTIONS: List[Dict[str, Any]] = []
QMAP: Dict[str, Dict[str, Any]] = {}
QUESTIONS_STATUS: Tuple[bool, str] = (False, "Not loaded yet")

def load_questions_from_json() -> List[Dict[str, Any]]:
    if not os.path.exists(QUESTIONS_FILE):
        raise FileNotFoundError(f"ما لقيت ملف الأسئلة: {QUESTIONS_FILE}")

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
                "id": str(qid),
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
                "id": str(qid),
                "type": "tf",
                "question": (it.get("statement") or "").strip(),
                "options": {"صح": "صح", "خطأ": "خطأ"},
                "correct_key": correct_key,
                "correct": correct_key or "",
            })

        elif t == "term":
            converted.append({
                "id": str(qid),
                "type": "short_answer",
                "question": (it.get("definition") or "").strip(),
                "correct": (it.get("term") or "").strip(),
            })

    return converted

def ensure_questions_loaded() -> bool:
    global QUESTIONS, QMAP, QUESTIONS_STATUS
    if QUESTIONS_STATUS[0]:
        return True
    try:
        q = load_questions_from_json()
        QUESTIONS = q
        QMAP = {item["id"]: item for item in QUESTIONS}
        QUESTIONS_STATUS = (True, f"Loaded {len(QUESTIONS)} questions")
        return True
    except Exception as e:
        QUESTIONS = []
        QMAP = {}
        QUESTIONS_STATUS = (False, str(e))
        return False

# =========================
# واجهة (أزرار) - القائمة الرئيسية
# =========================
def main_menu_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("🚀 ابدأ الاختبار", callback_data="menu|quiz"),
            InlineKeyboardButton("📊 نتيجتي", callback_data="menu|stats"),
        ],
        [
            InlineKeyboardButton("♻️ بنك جديد", callback_data="menu|reset"),
            InlineKeyboardButton("❓ مساعدة", callback_data="menu|help"),
        ],
    ]
    return InlineKeyboardMarkup(kb)

def question_footer(st: Dict[str, Any]) -> str:
    score = int(st.get("score", 0) or 0)
    answered = int(st.get("answered", 0) or 0)
    return f"📌 <b>الصحيح:</b> {score} | <b>المجاوب:</b> {answered}"

def help_text() -> str:
    return (
        "❓ <b>طريقة استخدام البوت</b>\n\n"
        "• اضغط <b>🚀 ابدأ الاختبار</b> عشان يطلع لك سؤال.\n"
        "• جاوب من الأزرار (اختيار/صح-خطأ).\n"
        "• أسئلة المصطلحات: اكتب الإجابة برسالة.\n"
        "• تقدر تضغط <b>⏭️ تخطي</b> لو تبي تعدّي.\n\n"
        "🧠 <b>أوامر سريعة</b>\n"
        "/quiz — يبدأ الاختبار\n"
        "/stats — يطلع نتيجتك\n"
        "/reset — بنك جديد\n"
        "/help — المساعدة\n\n"
        "✅ الميزة الحلوة: لو كتبت (تقنية) بدل (التقنية) غالبًا يحسبها صح 👌"
    )

# =========================
# حالة المستخدم
# =========================
def new_state() -> Dict[str, Any]:
    ensure_questions_loaded()
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
    ensure_questions_loaded()

    order = st.get("order", [])
    idx = int(st.get("idx", 0) or 0)

    while idx < len(order) and order[idx] not in QMAP:
        idx += 1
    st["idx"] = idx

    if idx >= len(order):
        return None
    return QMAP.get(order[idx])

# =========================
# إرسال السؤال
# =========================
async def send_next_question(update: Update, user_id: int, st: Dict[str, Any]):
    ok = ensure_questions_loaded()
    target = update.message if update.message else update.callback_query.message

    if not ok:
        await target.reply_text(
            "❌ ما قدرت أحمل الأسئلة.\n"
            f"السبب: {QUESTIONS_STATUS[1]}\n\n"
            "✅ تأكد إن ملف الأسئلة موجود في الريبو باسم:\n"
            f"<code>{esc(QUESTIONS_FILE)}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    q = get_current_q(st)

    if not q:
        await target.reply_text(
            "🎉 <b>خلصت الاختبار!</b>\n\n"
            f"📊 <b>نتيجتك:</b> {int(st['score'])} / {int(st['answered'])}\n\n"
            "تبغى تبدأ بنك جديد؟ اضغط ♻️ أو اكتب /reset",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard()
        )
        return

    st["current_qid"] = q["id"]
    st["expecting_text"] = (q.get("type") == "short_answer")
    set_state(user_id, st)

    idx = int(st.get("idx", 0) or 0)
    total = len(st.get("order", [])) or 1

    header = f"🧩 <b>سؤال {idx+1}</b> / {total}\n"
    qtext = esc((q.get("question") or "").strip())
    qtype = q.get("type")

    # أزرار ثابتة تحت كل سؤال
    def bottom_buttons(qid: str) -> List[List[InlineKeyboardButton]]:
        return [
            [
                InlineKeyboardButton("⏭️ تخطي", callback_data=f"skip|{qid}"),
                InlineKeyboardButton("❓ مساعدة", callback_data="menu|help"),
                InlineKeyboardButton("🏠 القائمة", callback_data="menu|home"),
            ]
        ]

    if qtype in ("mcq", "tf"):
        options: Dict[str, str] = q.get("options") or {}

        if qtype == "tf":
            keys = ["صح", "خطأ"]
            body = f"{header}{qtext}\n\n🟣 <b>اختر الإجابة:</b>\n\n{question_footer(st)}"
        else:
            keys = sorted_mcq_keys(list(options.keys()))
            lines = [f"{header}{qtext}", "", "🟣 <b>اختر الإجابة:</b>", ""]
            for k in keys:
                lines.append(f"<b>{esc(k)})</b> {esc(options.get(k, ''))}")
            lines.append("")
            lines.append(question_footer(st))
            body = "\n".join(lines)

        keyboard: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for k in keys:
            row.append(InlineKeyboardButton(text=str(k), callback_data=f"ans|{q['id']}|{k}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard += bottom_buttons(q["id"])

        await target.reply_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if qtype == "short_answer":
        body = (
            f"{header}✍️ <b>سؤال مصطلح / إجابة قصيرة</b>\n\n"
            f"{qtext}\n\n"
            "🟣 <b>اكتب الإجابة برسالة</b>\n\n"
            f"{question_footer(st)}"
        )
        await target.reply_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(bottom_buttons(q["id"]))
        )
        return

    # لو نوع غير معروف
    st["idx"] = idx + 1
    set_state(user_id, st)
    await send_next_question(update, user_id, st)

# =========================
# أوامر
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_questions_loaded()
    await update.message.reply_text(
        "هلااا 😄👋\n\n"
        "أنا <b>بوت مراجعة الاختبار</b> ✨\n"
        "أطلع لك أسئلة عشوائية + أحسب نتيجتك + أحاول أتفهم إجابة المصطلحات 👌\n\n"
        "اضغط زر من تحت 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        help_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard()
    )

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    await send_next_question(update, user_id, st)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    total = len(st.get("order", [])) or 0
    idx = int(st.get("idx", 0) or 0)
    await update.message.reply_text(
        "📊 <b>إحصائياتك</b>\n\n"
        f"✅ <b>الصحيح:</b> {int(st.get('score', 0) or 0)}\n"
        f"🧾 <b>المجاوب عليه:</b> {int(st.get('answered', 0) or 0)}\n"
        f"📍 <b>وصلت:</b> {min(idx+1, total) if total else 0} / {total}\n",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard()
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = new_state()
    set_state(user_id, st)
    await update.message.reply_text(
        "♻️ <b>تم إنشاء بنك جديد!</b>\nاضغط 🚀 ابدأ الاختبار أو اكتب /quiz",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard()
    )

# =========================
# أزرار الإجابة + التخطي + القائمة
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    st = get_state(user_id)

    data = query.data or ""
    parts = data.split("|")
    action = parts[0] if parts else ""

    # ---- قائمة / مساعدة ----
    if action == "menu":
        which = parts[1] if len(parts) > 1 else ""
        if which == "help":
            await query.message.reply_text(help_text(), parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
            return
        if which == "home":
            await query.message.reply_text("🏠 <b>القائمة الرئيسية</b>\nاختر اللي تبيه 👇", parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
            return
        if which == "quiz":
            await send_next_question(update, user_id, st)
            return
        if which == "stats":
            total = len(st.get("order", [])) or 0
            idx = int(st.get("idx", 0) or 0)
            await query.message.reply_text(
                "📊 <b>إحصائياتك</b>\n\n"
                f"✅ <b>الصحيح:</b> {int(st.get('score', 0) or 0)}\n"
                f"🧾 <b>المجاوب عليه:</b> {int(st.get('answered', 0) or 0)}\n"
                f"📍 <b>وصلت:</b> {min(idx+1, total) if total else 0} / {total}\n",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard()
            )
            return
        if which == "reset":
            st2 = new_state()
            set_state(user_id, st2)
            await query.message.reply_text(
                "♻️ <b>تم إنشاء بنك جديد!</b>\nاضغط 🚀 ابدأ الاختبار أو اكتب /quiz",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard()
            )
            return

    # ---- تخطي ----
    if action == "skip":
        st["idx"] = int(st.get("idx", 0) or 0) + 1
        st["expecting_text"] = False
        st["current_qid"] = None
        set_state(user_id, st)
        await query.message.reply_text(pick(SKIP_PHRASES))
        await send_next_question(update, user_id, st)
        return

    # ---- إجابة اختيار/صح-خطأ ----
    if action != "ans" or len(parts) != 3:
        return

    _, qid, chosen_key = parts

    if st.get("current_qid") != qid:
        await query.message.reply_text("⚠️ هذا سؤال قديم. اضغط 🚀 ابدأ الاختبار أو اكتب /quiz.")
        return

    ensure_questions_loaded()
    q = QMAP.get(qid)
    if not q:
        await query.message.reply_text("⚠️ ما لقيت السؤال. جرّب /reset.")
        return

    st["answered"] = int(st.get("answered", 0) or 0) + 1
    correct_key = q.get("correct_key")
    correct_text = q.get("correct", "")

    if chosen_key == correct_key:
        st["score"] = int(st.get("score", 0) or 0) + 1
        msg = f"{pick(PRAISE_CORRECT)} ✅\n<b>الإجابة:</b> {esc(str(correct_key))}) {esc(str(correct_text))}"
    else:
        msg = f"{pick(ENCOURAGE_WRONG)} ❌\n<b>الصحيح:</b> {esc(str(correct_key))}) {esc(str(correct_text))}"

    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)

    st["idx"] = int(st.get("idx", 0) or 0) + 1
    st["expecting_text"] = False
    st["current_qid"] = None
    set_state(user_id, st)

    await send_next_question(update, user_id, st)

# =========================
# إجابة النص (مصطلح)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)

    if not st.get("expecting_text"):
        return

    qid = st.get("current_qid")
    ensure_questions_loaded()
    q = QMAP.get(qid) if qid else None
    if not q or q.get("type") != "short_answer":
        st["expecting_text"] = False
        set_state(user_id, st)
        return

    user_answer = (update.message.text or "").strip()
    correct = (q.get("correct") or "").strip()

    a = normalize_arabic(user_answer)
    b = normalize_arabic(correct)

    # مطابقة ذكية:
    # 1) تطابق تام بعد التنظيف
    # 2) أو تطابق شبه كامل >= 0.85
    ok = False
    if a and a == b:
        ok = True
    elif a and b:
        ok = similarity(a, b) >= 0.85

    st["answered"] = int(st.get("answered", 0) or 0) + 1
    if ok:
        st["score"] = int(st.get("score", 0) or 0) + 1
        await update.message.reply_text(
            f"{pick(PRAISE_CORRECT)} ✅\n<b>الإجابة:</b> {esc(correct)}",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"{pick(ENCOURAGE_WRONG)} ❌\n<b>الصحيح:</b> {esc(correct)}",
            parse_mode=ParseMode.HTML
        )

    st["idx"] = int(st.get("idx", 0) or 0) + 1
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

    # تأكد قاعدة البيانات
    db_connect().close()

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))

    # Callbacks + text
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("✅ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()