import json
import os
import re
import random
import sqlite3
from difflib import SequenceMatcher
from typing import Dict, Any, Optional, List

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
# إعدادات
# =========================
QUESTIONS_FILE = "questions_from_word.json"
DB_FILE = "bot_state.db"

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
# SQLite: حفظ تقدم المستخدم ومنع تكرار الأسئلة
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
    idx = int(st.get("idx", 0) or 0)

    while idx < len(order) and order[idx] not in QMAP:
        idx += 1
    st["idx"] = idx

    if idx >= len(order):
        return None
    return QMAP.get(order[idx])

async def send_next_question(update: Update, user_id: int, st: Dict[str, Any]):
    q = get_current_q(st)
    target = update.message if update.message else update.callback_query.message

    if not q:
        await target.reply_text(
            f"🎉 خلصت الاختبار!\n\n"
            f"📊 نتيجتك: {st['score']} / {st['answered']}\n"
            f"اكتب /reset لو تبغى بنك جديد ♻️"
        )
        return

    st["current_qid"] = q["id"]
    st["expecting_text"] = (q.get("type") == "short_answer")
    set_state(user_id, st)

    header = f"🧩 ({int(st['idx'])+1}/{len(st.get('order', []))})\n"
    qtext = (q.get("question") or "").strip()
    qtype = q.get("type")

    if qtype in ("mcq", "tf"):
        options: Dict[str, str] = q.get("options") or {}

        if qtype == "tf":
            keys = ["صح", "خطأ"]
            text = header + qtext + "\n\nاختر:"
        else:
            keys = sorted_mcq_keys(list(options.keys()))
            lines = [header + qtext]
            for k in keys:
                lines.append(f"{k}) {options.get(k, '')}")
            text = "\n".join(lines)

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
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if qtype == "short_answer":
        await target.reply_text(
            header + "✍️ سؤال مصطلح/إجابة قصيرة:\n" + qtext + "\n\nاكتب الإجابة في رسالة."
        )
        return

    st["idx"] = int(st.get("idx", 0) or 0) + 1
    set_state(user_id, st)
    await send_next_question(update, user_id, st)

# =========================
# أوامر
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _ = get_state(user_id)
    await update.message.reply_text(
        "هلااا 😄👋\n"
        "أنا بوت أسئلة علوم ثاني متوسط ✨\n\n"
        "🚀 /quiz ابدأ الاختبار\n"
        "📊 /stats شوف نتيجتك\n"
        "♻️ /reset بنك جديد\n\n"
        "يلا ورّنا شطارتك يا بطّطل 🔥"
    )

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    await send_next_question(update, user_id, st)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    await update.message.reply_text(
        f"📊 إحصائياتك:\n"
        f"✅ الصحيح: {st['score']}\n"
        f"🧾 المجاوب عليه: {st['answered']}\n"
        f"📍 وصلت للسؤال: {min(st['idx']+1, len(st['order']))} من {len(st['order'])}"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = new_state()
    set_state(user_id, st)
    await update.message.reply_text("♻️ تم إنشاء بنك جديد! اكتب /quiz للبدء 😄")

# =========================
# أزرار الإجابة + التخطي
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
        st["idx"] = int(st.get("idx", 0) or 0) + 1
        st["expecting_text"] = False
        st["current_qid"] = None
        set_state(user_id, st)
        await query.message.reply_text(pick(SKIP_PHRASES))
        await send_next_question(update, user_id, st)
        return

    if action != "ans" or len(parts) != 3:
        return

    _, qid, chosen_key = parts
    if st.get("current_qid") != qid:
        await query.message.reply_text("⚠️ هذا سؤال قديم. اكتب /quiz للمتابعة.")
        return

    q = QMAP.get(qid)
    if not q:
        return

    st["answered"] = int(st.get("answered", 0) or 0) + 1
    correct_key = q.get("correct_key")
    correct_text = q.get("correct", "")

    if chosen_key == correct_key:
        st["score"] = int(st.get("score", 0) or 0) + 1
        await query.message.reply_text(f"{pick(PRAISE_CORRECT)} ✅\nالإجابة: {correct_key}) {correct_text}".strip())
    else:
        await query.message.reply_text(f"{pick(ENCOURAGE_WRONG)} ❌\nالصحيح: {correct_key}) {correct_text}".strip())

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
    q = QMAP.get(qid) if qid else None
    if not q or q.get("type") != "short_answer":
        st["expecting_text"] = False
        set_state(user_id, st)
        return

    user_answer = (update.message.text or "").strip()
    correct = (q.get("correct") or "").strip()

    a = normalize_arabic(user_answer)
    b = normalize_arabic(correct)

    ok = False
    if a and a == b:
        ok = True
    elif a and b:
        ok = similarity(a, b) >= 0.85

    st["answered"] = int(st.get("answered", 0) or 0) + 1
    if ok:
        st["score"] = int(st.get("score", 0) or 0) + 1
        await update.message.reply_text(f"{pick(PRAISE_CORRECT)} ✅\nالإجابة: {correct}")
    else:
        await update.message.reply_text(f"{pick(ENCOURAGE_WRONG)} ❌\nالصحيح: {correct}")

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

    db_connect().close()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("✅ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
