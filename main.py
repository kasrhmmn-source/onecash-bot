import os
import sqlite3
import uuid
import logging
import threading
import requests
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# -------------------------------------------------------------
# 1. إعدادات البيئة والتسجيل (Config & Logging)
# -------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ONECASH_MERCHANT_ID = os.getenv("ONECASH_MERCHANT_ID", "N/A")
ONECASH_API_KEY = os.getenv("ONECASH_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_secret")
FLASK_PORT = int(os.getenv("FLASK_PORT", 5000))
DB_NAME = "shop.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# حالات محادثة إضافة المنتج للأدمن
ADD_NAME, ADD_DESC, ADD_PRICE, ADD_CONTENT = range(4)

# -------------------------------------------------------------
# 2. إعداد قاعدة البيانات (Database Setup)
# -------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        # جدول المنتجات
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                price REAL NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # جدول الطلبات المعلقة والمكتملة
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id)
            )
        """)
        # جدول العمليات المكتملة لمنع التكرار (Anti-Replay Attack)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id TEXT PRIMARY KEY,
                order_id TEXT UNIQUE NOT NULL,
                amount REAL NOT NULL,
                sender_phone TEXT,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
        """)
        conn.commit()

init_db()

# -------------------------------------------------------------
# 3. دوال مساعدة لإرسال الرسائل عبر Telegram API من خارج الـ Loop
# -------------------------------------------------------------
def send_telegram_message(chat_id: int, text: str, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Error sending telegram message: {e}")

# -------------------------------------------------------------
# 4. خادم Flask لاستقبال إشعارات الدفع (OneCash Webhook Server)
# -------------------------------------------------------------
app = Flask(__name__)

@app.route("/api/onecash/webhook", methods=["POST"])
def onecash_webhook():
    """
    استقبال إشعار الدفع من ون كاش ومعالجته آلياً:
    JSON المتوقع:
    {
        "secret": "WEBHOOK_SECRET",
        "tx_id": "TXN_123456789",
        "order_id": "ORD-123456",
        "amount": 5000.0,
        "merchant_id": "770000000",
        "sender_phone": "777xxxxxx",
        "status": "PAID"
    }
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400

    # 1. التحقق من مفتاح الحماية / التوكن المخصص
    provided_secret = request.headers.get("X-OneCash-Secret") or data.get("secret")
    if provided_secret != WEBHOOK_SECRET:
        logger.warning("Unauthorized webhook access attempt!")
        return jsonify({"status": "unauthorized"}), 401

    tx_id = str(data.get("tx_id", "")).strip()
    order_id = str(data.get("order_id", "")).strip()
    paid_amount = float(data.get("amount", 0.0))
    status = data.get("status", "").upper()
    sender_phone = str(data.get("sender_phone", "غير متوفر"))

    if not tx_id or not order_id or status != "PAID":
        return jsonify({"status": "ignored", "message": "Transaction not valid or not completed"}), 200

    with get_db() as conn:
        cursor = conn.cursor()

        # 2. الحماية من إعادة استخدام رقم العملية (Anti-Replay Check)
        cursor.execute("SELECT tx_id FROM transactions WHERE tx_id = ?", (tx_id,))
        if cursor.fetchone():
            logger.warning(f"Replay attack detected for tx_id: {tx_id}")
            return jsonify({"status": "error", "message": "Transaction ID already used"}), 409

        # 3. التحقق من الطلب ومطابقة السعر
        cursor.execute("""
            SELECT o.user_id, o.amount, o.status, p.name, p.content 
            FROM orders o
            JOIN products p ON o.product_id = p.id
            WHERE o.order_id = ?
        """, (order_id,))
        order = cursor.fetchone()

        if not order:
            return jsonify({"status": "error", "message": "Order not found"}), 404

        if order["status"] == "COMPLETED":
            return jsonify({"status": "already_processed"}), 200

        # التحقق من أن المبلغ المدفوع يطابق سعر الطلب المطلوب تماماً أو أكبر
        if paid_amount < order["amount"]:
            logger.warning(f"Underpaid order {order_id}: expected {order['amount']}, received {paid_amount}")
            return jsonify({"status": "error", "message": "Insufficient amount"}), 400

        # 4. تحديث حالة الطلب وتسجيل العملية في جدول المعاملات
        try:
            cursor.execute("UPDATE orders SET status = 'COMPLETED' WHERE order_id = ?", (order_id,))
            cursor.execute("""
                INSERT INTO transactions (tx_id, order_id, amount, sender_phone)
                VALUES (?, ?, ?, ?)
            """, (tx_id, order_id, paid_amount, sender_phone))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return jsonify({"status": "error", "message": "Database conflict / duplicate"}), 409

    # 5. تسليم المنتج فوراً للمشتري تلقائياً بدون تدخل بشري
    user_id = order["user_id"]
    product_name = order["name"]
    product_content = order["content"]

    delivery_message = (
        f"✅ <b>تم استلام المبلغ وتأكيد الدفع بنجاح!</b>\n\n"
        f"📦 <b>المنتج:</b> {product_name}\n"
        f"💰 <b>المبلغ المدفوع:</b> {paid_amount} ريال\n"
        f"🧾 <b>رقم العملية:</b> <code>{tx_id}</code>\n"
        f"🆔 <b>رقم الطلب:</b> <code>{order_id}</code>\n\n"
        f"🎁 <b>بيانات المنتج / رابط التحميل:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{product_content}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🙏 شكراً لتعاملك معنا!"
    )
    send_telegram_message(user_id, delivery_message)

    # 6. إشعار الأدمن بالعملية
    admin_alert = (
        f"🔔 <b>عملية بيع آلية جديدة!</b>\n\n"
        f"📦 <b>المنتج:</b> {product_name}\n"
        f"💵 <b>المبلغ:</b> {paid_amount} ريال\n"
        f"👤 <b>المشتري (ID):</b> <code>{user_id}</code>\n"
        f"📱 <b>هاتف المحول:</b> <code>{sender_phone}</code>\n"
        f"🧾 <b>رقم العملية:</b> <code>{tx_id}</code>"
    )
    send_telegram_message(ADMIN_ID, admin_alert)

    return jsonify({"status": "success", "message": "Delivered"}), 200


# -------------------------------------------------------------
# 5. واجهات ولوحة تحكم البوت (Telegram Bot Handlers)
# -------------------------------------------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    keyboard = [
        [InlineKeyboardButton("🛍️ تصفح المنتجات والتصاميم", callback_data="list_products")],
        [InlineKeyboardButton("📜 سجل طلباتي", callback_data="my_orders")],
        [InlineKeyboardButton("ℹ️ معلومات الدفع والتسليم", callback_data="info")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"مرحباً بك <b>{user_name}</b> في متجر التصاميم والمنتجات الرقمية! 🚀\n\n"
        "⚡ <b>النظام مؤتمت بالكامل 100%:</b>\n"
        "عند الدفع عبر ون كاش، يتم التحقق من عملية الدفع وتسليمك المنتج/الملف في نفس اللحظة تلقائياً.\n\n"
        "اختر من القائمة أدناه للبدء:",
        parse_mode="HTML",
        reply_markup=reply_markup
    )

async def info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    info_text = (
        "ℹ️ <b>كيف تتم عملية الشراء والتسليم؟</b>\n\n"
        "1️⃣ اختر المنتج الرقمي المطلوب.\n"
        "2️⃣ سينشئ البوت لك <b>رقم طلب فريد (Order ID)</b>.\n"
        "3️⃣ قم بتحويل المبلغ المحدد إلى حساب ون كاش التاجر وضع <b>رقم الطلب في البيان/الملاحظات</b>.\n"
        "4️⃣ يقوم النظام المالي بالتحقق التلقائي وتسليمك الرابط/المحتوى مباشرة في الشات دون انتظار الأدمن."
    )
    keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_home")]]
    await query.edit_message_text(info_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def list_products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, price FROM products ORDER BY id DESC")
        products = cursor.fetchall()

    if not products:
        keyboard = [[InlineKeyboardButton("🔙 العودة", callback_data="back_home")]]
        await query.edit_message_text("⚠️ لا توجد منتجات متوفرة حالياً، يرجى العودة لاحقاً.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = []
    for p in products:
        keyboard.append([InlineKeyboardButton(f"📦 {p['name']} - {p['price']} ريال", callback_data=f"view_prod_{p['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_home")])

    await query.edit_message_text("🛒 <b>قائمة المنتجات والتصاميم المتاحة:</b>\nاختر المنتج لمعاينة التفاصيل والشراء:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def view_product_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[2])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        p = cursor.fetchone()

    if not p:
        await query.edit_message_text("❌ هذا المنتج لم يعد متوفراً.")
        return

    text = (
        f"📦 <b>{p['name']}</b>\n\n"
        f"📝 <b>الوصف:</b>\n{p['description']}\n\n"
        f"💰 <b>السعر:</b> <code>{p['price']}</code> ريال يمني\n"
        f"⚡ <b>نوع التسليم:</b> فوري وتلقائي عبر ون كاش"
    )

    keyboard = [
        [InlineKeyboardButton("💳 شراء الآن والدفع عبر ون كاش", callback_data=f"buy_prod_{p['id']}")],
        [InlineKeyboardButton("🔙 رجوع للمنتجات", callback_data="list_products")]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_product_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[2])
    user_id = update.effective_user.id

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        p = cursor.fetchone()

        if not p:
            await query.edit_message_text("❌ حدث خطأ، المنتج غير موجود.")
            return

        # توليد رقم طلب فريد
        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"

        cursor.execute("""
            INSERT INTO orders (order_id, user_id, product_id, amount, status)
            VALUES (?, ?, ?, ?, 'PENDING')
        """, (order_id, user_id, product_id, p["price"]))
        conn.commit()

    instructions = (
        f"🧾 <b>فاتورة دفع ون كاش المخصصة</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>المنتج:</b> {p['name']}\n"
        f"💰 <b>المبلغ المطلوب بالضبط:</b> <code>{p['price']}</code> ريال\n"
        f"🆔 <b>رقم الطلب الخاص بك:</b> <code>{order_id}</code>\n"
        f"🏦 <b>رقم حساب التاجر (ون كاش):</b> <code>{ONECASH_MERCHANT_ID}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚠️ <b>تعليمات هامة جداً لإتمام التحقق التلقائي:</b>\n"
        f"1. افتح تطبيق <b>OneCash</b> وقم بالتحويل إلى رقم التاجر أعلاه.\n"
        f"2. <b>اكتب رقم الطلب:</b> <code>{order_id}</code> في خانة (البيان / ملاحظات التحويل).\n"
        f"3. بمجرد تأكيد البنك للعملية، سيرسل لك البوت ملفك/رابطك هنا فوراً دون أي تأخير!\n"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 فحص حالة الطلب", callback_data=f"check_order_{order_id}")],
        [InlineKeyboardButton("🔙 العودة للمتجر", callback_data="list_products")]
    ]
    await query.edit_message_text(instructions, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def check_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = query.data.split("_")[2]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
        order = cursor.fetchone()

    if order and order["status"] == "COMPLETED":
        await query.answer("✅ تم تأكيد الدفع وتسليم المنتج بالأعلى!", show_alert=True)
    else:
        await query.answer("⏳ بانتظار إشعار الدفع من ون كاش... تأكد من إتمام التحويل.", show_alert=True)

async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT o.order_id, o.amount, o.status, p.name, o.created_at
            FROM orders o
            JOIN products p ON o.product_id = p.id
            WHERE o.user_id = ?
            ORDER BY o.created_at DESC LIMIT 5
        """, (user_id,))
        orders = cursor.fetchall()

    if not orders:
        keyboard = [[InlineKeyboardButton("🔙 العودة", callback_data="back_home")]]
        await query.edit_message_text("📂 ليس لديك طلبات سابقة حتى الآن.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    text = "📜 <b>سجل آخر 5 طلبات لك:</b>\n\n"
    for o in orders:
        status_icon = "✅ مكتمل" if o["status"] == "COMPLETED" else "⏳ بانتظار الدفع"
        text += f"• <b>{o['name']}</b> ({o['amount']} ريال)\n  الطلب: <code>{o['order_id']}</code> | الحالة: {status_icon}\n\n"

    keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_home")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def back_home_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_name = update.effective_user.first_name
    keyboard = [
        [InlineKeyboardButton("🛍️ تصفح المنتجات والتصاميم", callback_data="list_products")],
        [InlineKeyboardButton("📜 سجل طلباتي", callback_data="my_orders")],
        [InlineKeyboardButton("ℹ️ معلومات الدفع والتسليم", callback_data="info")]
    ]
    await query.edit_message_text(
        f"مرحباً بك مجدداً <b>{user_name}</b> في المتجر الآلي!\nاختر ما تريد من القائمة:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# -------------------------------------------------------------
# 6. لوحة تحكم الأدمن (/admin) وإدارة المنتجات
# -------------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ غير مصرح لك بالدخول لهذه اللوحة.")
        return

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), IFNULL(SUM(amount), 0) FROM transactions")
        total_sales, total_revenue = cursor.fetchone()

        cursor.execute("SELECT COUNT(*) FROM products")
        total_products = cursor.fetchone()[0]

    admin_text = (
        f"👑 <b>لوحة تحكم الأدمن</b>\n\n"
        f"📊 <b>إجمالي المبيعات المؤكدة:</b> {total_sales} عملية\n"
        f"💰 <b>إجمالي الأرباح المستلمة:</b> {total_revenue:,.2f} ريال يمني\n"
        f"📦 <b>عدد المنتجات المعروضة:</b> {total_products}\n"
    )

    keyboard = [
        [InlineKeyboardButton("➕ إضافة منتج جديد", callback_data="admin_add_product")],
        [InlineKeyboardButton("📦 إدارة وحذف المنتجات", callback_data="admin_manage_products")]
    ]
    await update.message.reply_text(admin_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# Conversation: إضافة منتج
async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    await query.edit_message_text("📝 <b>أرسل اسم المنتج أو التصميم الرقمي:</b>", parse_mode="HTML")
    return ADD_NAME

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["prod_name"] = update.message.text.strip()
    await update.message.reply_text("📋 <b>أرسل وصفاً مختصراً للمنتج:</b>", parse_mode="HTML")
    return ADD_DESC

async def add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["prod_desc"] = update.message.text.strip()
    await update.message.reply_text("💰 <b>أدخل سعر المنتج بالأرقام (مثلاً 2500):</b>", parse_mode="HTML")
    return ADD_PRICE

async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError()
        context.user_data["prod_price"] = price
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح وموجب للسعر:")
        return ADD_PRICE

    await update.message.reply_text(
        "🔗 <b>أرسل محتوى التسليم التلقائي:</b>\n"
        "(قد يكون رابط تحميل Google Drive أو Mega، أو مفتاح ترخيص، أو كود التفعيل الذي يستلمه العميل فور الدفع):",
        parse_mode="HTML"
    )
    return ADD_CONTENT

async def add_product_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = update.message.text.strip()
    name = context.user_data["prod_name"]
    desc = context.user_data["prod_desc"]
    price = context.user_data["prod_price"]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO products (name, description, price, content)
            VALUES (?, ?, ?, ?)
        """, (name, desc, price, content))
        conn.commit()

    await update.message.reply_text(
        f"✅ <b>تمت إضافة المنتج بنجاح!</b>\n\n"
        f"📦 الاسم: {name}\n"
        f"💰 السعر: {price} ريال",
        parse_mode="HTML"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await u
