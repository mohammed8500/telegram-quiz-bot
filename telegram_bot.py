import json
import os
import re
import random
import sqlite3
import html
from difflib import SequenceMatcher
from typing import Dict, Any, Optional, List, Set, Tuple

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
# أدوات تنسيق + مطابقة مصطلحات
# =========================
_AR_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652]")
_NON_TEXT_RE = re.compile(r"[^\u0600-\u06FF0-9A-Za-z\s]")

def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = _AR_DIACRITICS_RE.sub("", text)  # تشكيل
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    text = text.replace("ـ", "")
    text = _NON_TEXT_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text

def _strip_al_prefix_token(token: str) -> str:
    if token.startswith("ال") and len(token) > 2:
        stripped = token[2:]
        return stripped if stripped else token
    return token

def normalize_term_variants(text: str) -> Set[str]:
    base = normalize_arabic(text)
    if not base:
        return set()
    tokens = base.split()
    no_al_tokens = [_strip_al_prefix_token(t) for t in tokens]
    v1 = " ".join(tokens).strip()
    v2 = " ".join(no_al_tokens).strip()
    variants = {v1}
    if v2:
        variants.add(v2)
    if len(tokens) == 1:
        variants.add(_strip_al_prefix_token(tokens[0]))
    return {v for v in variants if v}

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

def smart_term_match(user_answer: str, correct_answer: str) -> Tuple[bool, float]:
    user_vars = normalize_term_variants(user_answer)
    corr_vars = normalize_term_variants(correct_answer)

    if not user_vars or not corr_vars:
        return False, 0.0

    # 1) تطابق مباشر
    for ua in user_vars:
        for ca in corr_vars:
            if ua == ca:
                return True, 1.0

    # 2) احتواء بسيط
    for ua in user_vars:
        for ca in corr_vars:
            if ua in ca or ca in ua:
                if abs(len(ua) - len(ca)) <= 2:
                    return True, 0.95

    # 3) Fuzzy
    best = 0.0
    for ua in user_vars:
        for ca in corr_vars:
            best = max(best, similarity(ua, ca))

    max_len = max(max(len(x) for x in user_vars), max(len(x) for x in corr_vars))
    if max_len <= 4:
        thr = 0.95
    elif max_len <= 7:
        thr = 0.90
    else:
        thr = 0.85

    return best >= thr, best

def sorted_mcq_keys(keys: List[str]) -> List[str]:
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    return sorted(keys, key=lambda k: order.get(k, 999))

def progress_bar(current_index_1based: int, total: int, length: int = 10) -> str:
    if total <= 0:
        return ""
    ratio = current_index_1based / total
    filled = int(round(ratio * length))
    filled = max(0, min(length, filled))
    return "▰" * filled + "▱" * (length - filled)

def esc(s: str) -> str:
    return html.escape(s or "")

def help_text_for(q: Dict[str, Any]) -> str:
    qtype = q.get("type")
    if qtype == "mcq":
        return (
            "ℹ️ <b>مساعدة</b>\n"
            "• اضغط على حرف الإجابة من الأزرار.\n"
            "• تقدر تضغط <b>⏭️ تخطي</b> لو تبغى.\n"
            "• اكتب <code>/stats</code> تشوف نتيجتك.\n"
            "• اكتب <code>/reset</code> عشان بنك جديد."
        )
    if qtype == "tf":
        return (
            "ℹ️ <b>مساعدة</b>\n"
            "• اختر: <b>صح</b> أو <b>خطأ</b> من الأزرار.\n"
            "• تقدر تضغط <b>⏭️ تخطي</b>.\n"
            "• <code>/stats</code> للنتيجة."
        )
    # short_answer
    return (
        "ℹ️ <b>مساعدة</b>\n"
        "• اكتب المصطلح في رسالة وحدة.\n"
        "• إذا كتبت <b>تقنية</b> بدل <b>التقنية</b> تُحسب صح ✅\n"
        "• لا تشيل هم الهمزات/التشكيل—البوت يتساهل فيها.\n"
        "• تقدر تضغط <b>⏭️ تخطي</b>."
    )

def main_menu_text() -> str:
    return (
        "هلااا 😄👋\n"
        "أنا بوت أسئلة علوم ثاني متوسط ✨\n\n"
        "🚀 <b>/quiz</b> ابدأ الاختبار\n"
        "📊 <b>/stats</b> شوف نتيجتك\n"
        "♻️ <b>/reset</b> بنك جديد\n\n"
        "يلا ورّنا شطارتك يا بطّطل 🔥"
    )

# =========================
# تحميل الأسئلة من JSON
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

def build_common_buttons(qid: str) -> List[List[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton("ℹ️ مساعدة", callback_data=f"help|{qid}"),
            InlineKeyboardButton("⏭️ تخطي", callback_data=f"skip|{qid}"),
        ]
    ]

async def send_next_question(update: Update, user_id: int, st: Dict[str, Any]):
    q = get_current_q(st)
    target = update.message if update.message else update.callback_query.message

    total = len(st.get("order", [])) or 0
    current_1based = min(int(st.get("idx", 0) or 0) + 1, total if total else 1)
    bar = progress_bar(current_1based, total, length=10)

    if not q:
        await target.reply_text(
            f"🎉 <b>خلصت الاختبار!</b>\n\n"
            f"📊 <b>نتيجتك:</b> {st['score']} / {st['answered']}\n"
            f"♻️ اكتب <code>/reset</code> لو تبغى بنك جديد",
            parse_mode="HTML",
        )
        return

    st["current_qid"] = q["id"]
    st["expecting_text"] = (q.get("type") == "short_answer")
    set_state(user_id, st)

    header = (
        f"🧠 <b>سؤال {current_1based} من {total}</b>\n"
        f"{bar}\n\n"
    )

    qtext = esc((q.get("question") or "").strip())
    qtype = q.get("type")

    if qtype in ("mcq", "tf"):
        options: Dict[str, str] = q.get("options") or {}

        if qtype == "tf":
            keys = ["صح", "خطأ"]
            text = header + f"❓ <b>{qtext}</b>\n\nاختر الإجابة:"
        else:
            keys = sorted_mcq_keys(list(options.keys()))
            lines = [header + f"❓ <b>{qtext}</b>\n"]
            for k in keys:
                lines.append(f"• <b>{esc(k)})</b> {esc(options.get(k, ''))}")
            lines.append("\nاختر الإجابة من الأزرار 👇")
            text = "\n".join(lines)

        keyboard: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for k in keys:
            row.append(InlineKeyboardButton(text=k, callback_data=f"ans|{q['id']}|{k}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard += build_common_buttons(q["id"])

        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    if qtype == "short_answer":
        text = (
            header
            + "✍️ <b>سؤال مصطلح/إجابة قصيرة</b>\n"
            + f"❓ <b>{qtext}</b>\n\n"
            + "اكتب الإجابة في رسالة وحدة 👇"
        )
        keyboard = build_common_buttons(q["id"])
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
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
    await update.message.reply_text(main_menu_text(), parse_mode="HTML")

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    await send_next_question(update, user_id, st)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_state(user_id)
    await update.message.reply_text(
        f"📊 <b>إحصائياتك</b>\n\n"
        f"✅ <b>الصحيح:</b> {st['score']}\n"
        f"🧾 <b>المجاوب عليه:</b> {st['answered']}\n"
        f"📍 <b>وصلت للسؤال:</b> {min(st['idx']+1, len(st['order']))} من {len(st['order'])}",
        parse_mode="HTML",
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = new_state()
    set_state(user_id, st)
    await update.message.reply_text("♻️ <b>تم إنشاء بنك جديد!</b>\nاكتب <code>/quiz</code> للبدء 😄", parse_mode="HTML")

# =========================
# أزرار: إجابة/تخطي/مساعدة
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    st = get_state(user_id)

    data = query.data or ""
    parts = data.split("|")
    action = parts[0] if parts else ""

    if action == "help":
        if len(parts) != 2:
            return
        qid = parts[1]
        q = QMAP.get(qid)
        if not q:
            await query.message.reply_text("ℹ️ ما لقيت السؤال. اكتب /quiz للمتابعة.", parse_mode="HTML")
            return
        await query.message.reply_text(help_text_for(q), parse_mode="HTML")
        return

    if action == "skip":
        st["idx"] = int(st.get("idx", 0) or 0) + 1
        st["expecting_text"] = False
        st["current_qid"] = None
        set_state(user_id, st)
        await query.message.reply_text(pick(SKIP_PHRASES), parse_mode="HTML")
        await send_next_question(update, user_id, st)
        return

    if action != "ans" or len(parts) != 3:
        return

    _, qid, chosen_key = parts
    if st.get("current_qid") != qid:
        await query.message.reply_text("⚠️ هذا سؤال قديم. اكتب <code>/quiz</code> للمتابعة.", parse_mode="HTML")
        return

    q = QMAP.get(qid)
    if not q:
        return

    st["answered"] = int(st.get("answered", 0) or 0) + 1
    correct_key = q.get("correct_key")
    correct_text = esc(q.get("correct", ""))

    if chosen_key == correct_key:
        st["score"] = int(st.get("score", 0) or 0) + 1
        await query.message.reply_text(
            f"{pick(PRAISE_CORRECT)} ✅\n"
            f"🎯 <b>الإجابة:</b> {esc(correct_key)}) {correct_text}",
            parse_mode="HTML",
        )
    else:
        await query.message.reply_text(
            f"{pick(ENCOURAGE_WRONG)} ❌\n"
            f"✅ <b>الصحيح:</b> {esc(correct_key)}) {correct_text}",
            parse_mode="HTML",
        )

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

    ok, sim = smart_term_match(user_answer, correct)

    st["answered"] = int(st.get("answered", 0) or 0) + 1
    if ok:
        st["score"] = int(st.get("score", 0) or 0) + 1
        await update.message.reply_text(
            f"{pick(PRAISE_CORRECT)} ✅\n"
            f"🎯 <b>الإجابة الصحيحة:</b> {esc(correct)}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"{pick(ENCOURAGE_WRONG)} ❌\n"
            f"📝 <b>إجابتك:</b> {esc(user_answer)}\n"
            f"✅ <b>الصحيح:</b> {esc(correct)}",
            parse_mode="HTML",
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

    db_connect().close()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("✅ Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()