import os
import json
import random
import logging
import re
import sqlite3
import asyncio
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- الإعدادات (اسحب التوكن من ريلواي للأمان) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
# رقمك الشخصي (تأكد أنه بدون أقواس مجموعة هنا لضمان الإرسال المباشر)
MY_ADMIN_ID = 290185541 

logging.basicConfig(level=logging.INFO)

# --- قاعدة البيانات ---
conn = sqlite3.connect("data.db", check_same_thread=False, isolation_level=None)
conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, approved INTEGER DEFAULT 0)")

# --- الأوامر الرئيسية ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    
    kb = [
        [InlineKeyboardButton("🎮 ابدأ الجولة", callback_data="play")],
        [InlineKeyboardButton("➕ تسجيل اسمي", callback_data="reg")],
        [InlineKeyboardButton("💬 مراسلة المشرف", callback_data="msg")]
    ]
    await update.message.reply_text("أهلاً بك! اختر من القائمة:", reply_markup=InlineKeyboardMarkup(kb))

# --- معالجة الأزرار ---
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "reg":
        context.user_data["mode"] = "reg"
        await query.message.reply_text("✍️ أرسل اسمك الثنائي الآن لاعتماده:")
    
    elif query.data == "msg":
        context.user_data["mode"] = "msg"
        await query.message.reply_text("📝 اكتب رسالتك للمشرف الآن:")

    elif query.data.startswith("ok_"):
        target_id = int(query.data.split("_")[1])
        conn.execute("UPDATE users SET approved=1 WHERE user_id=?", (target_id,))
        await query.edit_message_text(f"✅ تم اعتماد الطالب {target_id}")
        await context.bot.send_message(target_id, "🎉 مبروك! تم اعتماد اسمك من قبل المشرف.")

# --- معالجة الرسائل والردود (هنا حل مشكلتك) ---
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = update.message.text
    mode = context.user_data.get("mode")

    # 1. إذا المشرف (أنت) رد على رسالة طالب
    if uid == MY_ADMIN_ID and update.message.reply_to_message:
        try:
            # نبحث عن الـ ID في الرسالة اللي سويت لها Reply
            target = re.search(r"ID:(\d+)", update.message.reply_to_message.text).group(1)
            await context.bot.send_message(int(target), f"👨‍🏫 <b>رد من المشرف:</b>\n\n{txt}", parse_mode=ParseMode.HTML)
            await update.message.reply_text("✅ وصل ردك للطالب")
            return
        except:
            await update.message.reply_text("❌ لم أستطع تحديد ID الطالب من هذه الرسالة.")
            return

    # 2. الطالب يسجل اسمه (يرسل لك إشعار فوراً)
    if mode == "reg":
        context.user_data["mode"] = None
        conn.execute("UPDATE users SET name=? WHERE user_id=?", (txt, uid))
        await update.message.reply_text("⏳ تم إرسال طلبك للمشرف.")
        
        # زر الاعتماد يوصلك أنت
        kb = [[InlineKeyboardButton("✅ اعتماد الاسم", callback_data=f"ok_{uid}")]]
        await context.bot.send_message(MY_ADMIN_ID, f"📝 <b>طلب اسم جديد:</b>\nالاسم: {txt}\nID:{uid}", 
                                       parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

    # 3. الطالب يراسل المشرف
    elif mode == "msg":
        context.user_data["mode"] = None
        await update.message.reply_text("✅ وصلت رسالتك للمشرف.")
        
        # الرسالة توصلك أنت مع الـ ID عشان تقدر تسوي Reply
        await context.bot.send_message(MY_ADMIN_ID, f"📩 <b>رسالة من طالب:</b>\nID:{uid}\n\nالنص:\n{txt}\n\n<i>(رد على هذه الرسالة مباشرة ليوصله ردك)</i>", 
                                       parse_mode=ParseMode.HTML)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
