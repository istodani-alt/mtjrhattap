import os
import asyncio
import asyncpg
import re
import shutil
import logging
import zipfile
import io
import uuid
import hashlib
import requests
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, CopyTextButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ====== سىزنىڭ تەڭشەكلىرىڭىز تولدۇرۇلدى ======
BOT_TOKEN = "9037019918:AAHPi7VCKinoxyVlxILPPLXeO8fJ4BlaTe0"
ADMIN_IDS = [7875653665]
API_ID = 37171071
API_HASH = "b36f73444855dc87595d2757279b9409"
DATABASE_URL = "postgresql://postgres:hattap571611@db.sqnhyxwszicqibznnmsj.supabase.co:5432/postgres"
SESSIONS_DIR = "sessions"
TEMP_DIR = "temp"
NOTIFICATION_CHANNEL = "-1004358421968"
TRANSFER_FEE_PERCENT = 0.05
MANUAL_WALLET = "e340fa5d9713c087f3803ae2f6d9b9c5"

# --- 1. Rise SMM API (SMS بۆلۈمى ئۈچۈن) ---
RISE_API_URL = "https://vnvccv.shop/Ayma/api/v2"
RISE_API_TOKEN = "a4a725e2decd5670a8b33e3e59e4f13cbfae262c312b69d3d4f5d33e2fba1e7a"

def rise_smm_request(action, params=None):
    if params is None:
        params = {}
    params['key'] = RISE_API_TOKEN
    params['action'] = action
    try:
        res = requests.post(RISE_API_URL, data=params, timeout=15)
        return res.json()
    except Exception:
        return {"status": False}

# --- 2. Shark TG API ([شراء حسابات جاهز 2] ئۈچۈن) ---
SHARK_API_KEY = "6z4mu0j3638xlf7awp9j"
SHARK_BASE_URL = "https://sharktg.com/sub/api/?"

def shark_get_number(country_code):
    url = f"{SHARK_BASE_URL}apiKey={SHARK_API_KEY}&action=getNumber&country={country_code}"
    try:
        res = requests.get(url, timeout=15)
        return res.json()
    except Exception:
        return {"status": False}

def shark_get_code(hash_code):
    url = f"{SHARK_BASE_URL}apiKey={SHARK_API_KEY}&action=getCode&hash_code={hash_code}"
    try:
        res = requests.get(url, timeout=15)
        return res.json()
    except Exception:
        return {"status": False}
# ==========================================

for d in [SESSIONS_DIR, TEMP_DIR]:
    os.makedirs(d, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

pending_logouts = {}

class BuySessionStates(StatesGroup):
    select_country = State()
    enter_count = State()

class TransferStates(StatesGroup):
    target_id = State()
    amount = State()
    link_amount = State()

class ManualDepositStates(StatesGroup):
    waiting_for_txid = State()
    waiting_for_amount = State()

class AdminUploadStates(StatesGroup):
    waiting_for_zip = State()
    waiting_for_country = State()
    waiting_for_price = State()
    waiting_for_2fa_input = State()

class AdminUserStates(StatesGroup):
    search_uid = State()
    edit_balance = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

class SharkStates(StatesGroup):
    waiting_for_country = State()
    waiting_for_hash = State()

class RiseSmsStates(StatesGroup):
    waiting_for_service = State()

async def safe_edit_or_send(message: Message, text=None, reply_markup=None, parse_mode=None, photo=None, caption=None, document=None):
    try:
        if photo:
            await message.edit_caption(caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
        elif document:
            raise Exception("Cannot edit document")
        else:
            await message.edit_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await message.delete()
        except Exception:
            pass
        if photo:
            await message.answer_photo(photo=photo, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
        elif document:
            await message.answer_document(document=document, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await message.answer(text=text, reply_markup=reply_markup, parse_mode=parse_mode)

async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, balance REAL DEFAULT 0.0, is_banned INTEGER DEFAULT 0)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS accounts (id SERIAL PRIMARY KEY, phone TEXT UNIQUE, session_file TEXT, price REAL DEFAULT 2.0, country TEXT DEFAULT 'General', status TEXT DEFAULT 'available', session_2fa_pass TEXT DEFAULT NULL)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS categories (country TEXT PRIMARY KEY)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS orders (id SERIAL PRIMARY KEY, user_id BIGINT, phone TEXT, price REAL, date TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS user_transfers (id SERIAL PRIMARY KEY, sender_id BIGINT, receiver_id BIGINT, amount REAL, fee REAL, net_amount REAL, link TEXT UNIQUE, status TEXT DEFAULT 'pending')''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS manual_deposits (id SERIAL PRIMARY KEY, user_id BIGINT, txid TEXT UNIQUE, amount REAL, status TEXT DEFAULT 'pending', date TEXT)''')
    finally:
        await conn.close()

async def db_execute(query, params=()):
    idx = 1
    while '?' in query:
        query = query.replace('?', f'${idx}', 1)
        idx += 1
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        res = await conn.execute(query, *params)
        return res
    finally:
        await conn.close()

async def db_fetchone(query, params=()):
    idx = 1
    while '?' in query:
        query = query.replace('?', f'${idx}', 1)
        idx += 1
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(query, *params)
        return row
    finally:
        await conn.close()

async def db_fetchall(query, params=()):
    idx = 1
    while '?' in query:
        query = query.replace('?', f'${idx}', 1)
        idx += 1
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(query, *params)
        return rows
    finally:
        await conn.close()

def mask_phone(phone: str) -> str:
    if not phone or len(phone) < 6:
        return phone
    return phone[:-4] + "****"

def main_kb(uid):
    kb = [
        [InlineKeyboardButton(text="🛒 شراء حساب", callback_data="buy_menu", style='success')],
        [
            InlineKeyboardButton(text="📦 شراء جلسات", callback_data="buy_sessions_zip", style='primary'),
            InlineKeyboardButton(text="📨 SMS", callback_data="sms_menu", style='primary')
        ],
        [
            InlineKeyboardButton(text="💳 شحن رصيد", callback_data="charge_menu", style='primary'),
            InlineKeyboardButton(text="🛍 شراء حسابات جاهز 2", callback_data="shark_buy_menu", style='success')
        ],
        [
            InlineKeyboardButton(text="📢 قناة التفعيلات", url=f"https://t.me/{NOTIFICATION_CHANNEL.replace('@','')}", style='success'),
            InlineKeyboardButton(text="⚖️ شروط الاستخدام", callback_data="settings_menu", style='primary')
        ],
        [
            InlineKeyboardButton(text="💸 تحويل رصيد", callback_data="transfer_menu", style='primary'),
            InlineKeyboardButton(text="📞 تواصل معنا", callback_data="contact_us", style='primary')
        ]
    ]
    if uid in ADMIN_IDS:
        kb.insert(0, [InlineKeyboardButton(text="👑 لوحة الإدارة", callback_data="admin_panel", style='danger')])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_kb(callback="main_menu"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 عودة", callback_data=callback, style='primary')]])

@router.callback_query(F.data == "noop")
async def noop_cb(c: CallbackQuery):
    await c.answer()

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    uname = msg.from_user.username or "User"
    await db_execute("INSERT INTO users (user_id, username) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING", (uid, uname))

    args = msg.text.split()
    if len(args) > 1 and args[1].startswith("pay_"):
        link_code = args[1]
        transfer = await db_fetchone("SELECT sender_id, amount, status FROM user_transfers WHERE link=$1", (link_code,))
        if not transfer:
            return await msg.reply("❌ رابط الدفع غير صالح أو منتهي الصلاحية.")
        sender_id, amount, status = transfer
        if status != 'pending':
            return await msg.reply("❌ تم استخدام هذا الرابط مسبقاً.")
        if sender_id == uid:
            return await msg.reply("❌ لا يمكنك استلام الرابط الخاص بك!")
        await db_execute("UPDATE user_transfers SET status='completed', receiver_id=$1 WHERE link=$2", (uid, link_code))
        await db_execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", (amount, uid))
        await msg.reply(f"✅ تم استلام المبلغ بنجاح! تمت إضافة <b>{amount:.2f}$</b> لرصيدك.", parse_mode=ParseMode.HTML)
        try:
            await bot.send_message(sender_id, f"💰 تم استلام رابط التحويل الخاص بك بمبلغ <b>{amount:.2f}$</b> من المستخدم <code>{uid}</code>.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    row = await db_fetchone("SELECT balance FROM users WHERE user_id=$1", (uid,))
    balance = row[0] if row else 0.0
    text = (f"👋 مرحباً بك في البوت 🚀\n\n"
            f"🆔 أيدي حسابك:   <code>{uid}</code>\n"
            f"💰 رصيدك الحالي:   <b>{balance:.2f}$</b>\n\n"
            f"🔻 للتحكم بالأزرار الخاصة بالبوت من الأسفل:  ")
    await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_kb(uid))

@router.callback_query(F.data == "main_menu")
async def main_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    row = await db_fetchone("SELECT balance FROM users WHERE user_id=$1", (uid,))
    balance = row[0] if row else 0.0
    text = (f"👋 مرحباً بك في البوت 🚀\n\n"
            f"🆔 أيدي حسابك:    <code>{uid}</code>\n"
            f"💰 رصيدك الحالي:    <b>{balance:.2f}$</b>\n\n"
            f"🔻 للتحكم بالأزرار الخاصة بالبوت من الأسفل:   ")
    await safe_edit_or_send(c.message, text=text, parse_mode=ParseMode.HTML, reply_markup=main_kb(uid))

# ====== Contact Us & Support Chat System ======
@router.callback_query(F.data == "contact_us")
async def contact_us_menu(c: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="📩 تواصل مباشرة (إرسال رسالة للإدارة)", callback_data="start_support_chat", style='success')],
        [
            InlineKeyboardButton(text="👤 الأول: @hattab7657", url="https://t.me/hattab7657"),
            InlineKeyboardButton(text="📥 الثاني: @mdirbot67", url="https://t.me/mdirbot67")
        ],
        [InlineKeyboardButton(text="📥 الثالث: @hattab0033", url="https://t.me/hattab0033")],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
    ]
    text = (
        "📞 <b>تواصل معنا عبر تلجرام:</b>\n\n"
        "تواصل عبر تلجرام 👇👇\n\n"
        "اول 📥 مدير مباشرة ⏺\n@hattab7657\n\n"
        "الثاني 📥 (خادم)\n@mdirbot67\n\n"
        "الثالث 📥 (خادم)\n@hattab0033"
    )
    await safe_edit_or_send(c.message, text=text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "start_support_chat")
async def start_support_chat(c: CallbackQuery, state: FSMContext):
    await state.set_state(SupportStates.waiting_for_message)
    text = (
        "📩 <b>هل تواجهون أي مشاكل أو لديكم أي اقتراحات أو طلبات؟</b>\n\n"
        "لا تترددوا في إرسالها إلينا، فنحن نرحب بآرائكم وملاحظاتكم ونسعى دائماً إلى تحسين خدماتنا وتلبية احتياجاتكم. ❤️\n\n"
        "✍️ أرسل رسالتك الآن وسيقوم الفريق بالرد عليك:"
    )
    await safe_edit_or_send(c.message, text=text, parse_mode=ParseMode.HTML, reply_markup=back_kb("contact_us"))

@router.message(SupportStates.waiting_for_message)
async def process_user_support_message(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    uname = msg.from_user.username or "NoUsername"
    text = msg.text

    await state.clear()
    await msg.reply("✅ تم إرسال رسالتك إلى الإدارة بنجاح. سيتم الرد عليك قريباً!", reply_markup=main_kb(uid))

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 الرد على المستخدم", callback_data=f"reply_user:{uid}", style='success')]
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📩 <b>رسالة دعم جديدة من مستخدم:</b>\n\n"
                f"👤 المستخدم: <code>{uid}</code> (@{uname})\n"
                f"💬 النص:\n{text}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_kb
            )
        except Exception:
            pass

class AdminReplyStates(StatesGroup):
    waiting_for_reply_text = State()

@router.callback_query(F.data.startswith("reply_user:"))
async def admin_start_reply(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    target_uid = int(c.data.split(":")[1])
    await state.update_data(target_uid=target_uid)
    await state.set_state(AdminReplyStates.waiting_for_reply_text)
    await c.message.answer("✍️ أرسل نص الرد ليتم إرساله إلى المستخدم:")
    await c.answer()

@router.message(AdminReplyStates.waiting_for_reply_text)
async def admin_send_reply(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    target_uid = data['target_uid']
    reply_text = msg.text
    await state.clear()

    try:
        await bot.send_message(
            target_uid,
            f"💬 <b>رد من الإدارة:</b>\n\n{reply_text}",
            parse_mode=ParseMode.HTML
        )
        await msg.reply("✅ تم إرسال الرد للمستخدم بنجاح.")
    except Exception as e:
        await msg.reply(f"❌ تعذر إرسال الرد للمستخدم: {e}")

# ==========================================

@router.callback_query(F.data == "charge_menu")
async def charge_menu(c: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="🟢 شحن عبر شامكەش (يدوي)", callback_data="manual_deposit_start", style='success')],
        [InlineKeyboardButton(text="💎 شحن عبر الرقمية TON", callback_data="ton_charge_info", style='primary')],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
    ]
    await safe_edit_or_send(c.message, text="💳 اختر طريقة الشحن:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "ton_charge_info")
async def ton_charge_info(c: CallbackQuery):
    ton_text = (
        "💎 <b>شحن عبر العملات الرقمية TON</b>\n\n"
        "تأكد حسابنا قبل أن تدفع.\n\n"
        "إيداع عملات رقمية 📥\n\n"
        "💱 <b>العملة:</b> USDT\n"
        "🪩 <b>الشبكة:</b> USDT-The Open Network (TON)\n\n"
        "🔗 <b>عنوان الإيداع:</b>\n"
        "<code>UQAYw00KDTVyJYu4pKyEJUxTwczX7cQ5xDs8ipnCpOOOzi3g</code>\n\n"
        "⚠️ <b>الحد الأدنى للإيداع:</b> 0.01 USDT\n"
        "💡 تأكد من إرسال مبلغ أكبر من 0.01 USDT\n"
        "⏳ <b>وقت العملة:</b> 60:00 دقيقة\n\n"
        "⚠️ تأكد من اختيار الشبكة الصحيحة لتجنب ضياع الأموال.\n"
        "⚠️ المعاملات من محافظ خارجية قد تستغرق 5-15 دقيقة للوصول."
    )
    copy_btn = InlineKeyboardButton(text="📋 نسخ العنوان", copy_text=CopyTextButton(text="UQAYw00KDTVyJYu4pKyEJUxTwczX7cQ5xDs8ipnCpOOOzi3g"), style='primary')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [copy_btn],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="charge_menu", style='primary')]
    ])
    await safe_edit_or_send(c.message, text=ton_text, parse_mode=ParseMode.HTML, reply_markup=kb)

# ====== Manual Deposit (شامكەش) Logic ======
@router.callback_query(F.data == "manual_deposit_start")
async def manual_deposit_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(ManualDepositStates.waiting_for_txid)
    text = (
        "🟢 <b>شحن الرصيد عبر شامكەش (يدوي)</b>\n\n"
        "⚠️ <b>تنبيه هام جداً:</b>\n"
        "قبل إتمام وتحويل المبلغ، تأكد جيداً من صحة حساب الشامكەش الخاص بنا أدناه. في حال أرسلت إلى جهة أخرى فنحن غير مسؤولين تماماً.\n\n"
        f"💳 <b>حساب الشامكەش الخاص بنا:</b>\n<code>{MANUAL_WALLET}</code>\n\n"
        "📝 يرجى إرسال <b>رقم العملية (9 أرقام)</b> الآن:\n"
        "*(ملاحظة: يجب أن يكون 9 خانات בדיוק، ولا يقبل الأرقام المتطابقة مثل 111111111)*"
    )
    copy_btn = InlineKeyboardButton(text="📋 نسخ الحساب", copy_text=CopyTextButton(text=MANUAL_WALLET), style='primary')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [copy_btn],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="charge_menu", style='primary')]
    ])
    await safe_edit_or_send(c.message, text=text, parse_mode=ParseMode.HTML, reply_markup=kb)

@router.message(ManualDepositStates.waiting_for_txid)
async def manual_deposit_txid(msg: Message, state: FSMContext):
    txid = msg.text.strip()
    if not re.fullmatch(r'\d{9}', txid) or len(set(txid)) == 1:
        return await msg.reply(
            "❌ <b>رقم العملية غير صالح!</b>\n\n"
            "• يجب أن يتكون من <b>9 أرقام بالضبط</b>.\n"
            "• لا يتم قبول الأرقام المتطابقة بالكامل (مثل 111111111).\n\n"
            "يرجى إرسال رقم العملية الصحيح:",
            parse_mode=ParseMode.HTML
        )
    
    existing = await db_fetchone("SELECT id FROM manual_deposits WHERE txid=$1", (txid,))
    if existing:
        return await msg.reply("❌ رقم العملية هذا تم استخدامه مسبقاً! يرجى التحقق.")

    await state.update_data(txid=txid)
    await state.set_state(ManualDepositStates.waiting_for_amount)
    await msg.reply("💰 أدخل المبلغ الذي قمت بتحويله ($):", reply_markup=back_kb("charge_menu"))

@router.message(ManualDepositStates.waiting_for_amount)
async def manual_deposit_amount(msg: Message, state: FSMContext):
    try:
        amount = float(msg.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await msg.reply("❌ يرجى إدخال مبلغ صحيح أكبر من صفر.")

    data = await state.get_data()
    txid = data['txid']
    uid = msg.from_user.id
    uname = msg.from_user.username or "NoUsername"
    date_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    await db_execute(
        "INSERT INTO manual_deposits (user_id, txid, amount, status, date) VALUES ($1, $2, $3, 'pending', $4)",
        (uid, txid, amount, date_str)
    )
    await state.clear()

    await msg.reply(
        "✅ <b>تم إرسال طلب الشحن بنجاح!</b>\n\n"
        "⏳ سيتم مراجعة الطلب من قبل الإدارة واعتماده في أقرب وقت.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(uid)
    )

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ قوبل", callback_data=f"dep_acc:{uid}:{amount}:{txid}", style='success'),
            InlineKeyboardButton(text="❌ رُفض", callback_data=f"dep_rej:{uid}:{txid}", style='danger')
        ]
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📥 <b>طلب شحن رصيد جديد (شامكەش)</b>\n\n"
                f"👤 المستخدم: <code>{uid}</code> (@{uname})\n"
                f"💵 المبلغ: <b>{amount}$</b>\n"
                f"🔢 رقم العملية (TXID): <code>{txid}</code>\n"
                f"🕒 التاريخ: {date_str}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_kb
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("dep_acc:") | F.data.startswith("dep_rej:"))
async def admin_deposit_decision(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("غير مسموح", show_alert=True)
    
    parts = c.data.split(":")
    action = parts[0]
    uid = int(parts[1])
    txid = parts[-1]

    dep = await db_fetchone("SELECT status FROM manual_deposits WHERE txid=$1", (txid,))
    if not dep or dep[0] != 'pending':
        return await c.answer("تم معالجة هذا الطلب مسبقاً أو غير موجود.", show_alert=True)

    if action == "dep_acc":
        amount = float(parts[2])
        await db_execute("UPDATE manual_deposits SET status='completed' WHERE txid=$1", (txid,))
        await db_execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", (amount, uid))
        await c.message.edit_text(c.message.html_text + "\n\n<b>✅ تم قبول الطلب وشحن الرصيد بنجاح.</b>", parse_mode=ParseMode.HTML)
        try:
            await bot.send_message(uid, f"🎉 <b>تم قبول طلب الشحن الخاص بك!</b>\n\n💰 تمت إضافة <b>{amount}$</b> لرصيدك.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    else:
        await db_execute("UPDATE manual_deposits SET status='rejected' WHERE txid=$1", (txid,))
        await c.message.edit_text(c.message.html_text + "\n\n<b>❌ تم رفض الطلب.</b>", parse_mode=ParseMode.HTML)
        try:
            await bot.send_message(uid, f"❌ <b>عذراً، تم رفض طلب الشحن الخاص بك</b> برقم العملية <code>{txid}</code>.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    await c.answer()

@router.callback_query(F.data == "transfer_menu")
async def transfer_menu(c: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="🆔 تحويل مباشر (بالأيدي)", callback_data="transfer_direct", style='primary'),
         InlineKeyboardButton(text="🔗 إنشاء رابط دفع", callback_data="transfer_link_create", style='success')],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
    ]
    await safe_edit_or_send(c.message, text="💸 خدمات التحويل\n⚠️ ملاحظة: عند إنشاء رابط دفع، سيتم خصم المبلغ من رصيدك فوراً لضمان الجدية. ", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "transfer_direct")
async def transfer_direct_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(TransferStates.target_id)
    await safe_edit_or_send(c.message, text="🆔 أدخل الـ ID الرقمي للمستلم:", reply_markup=back_kb("transfer_menu"))

@router.message(TransferStates.target_id)
async def transfer_set_target(msg: Message, state: FSMContext):
    try:
        target = int(msg.text.strip())
        if target == msg.from_user.id:
            return await msg.reply("❌ لا يمكنك التحويل لنفسك!")
        target_user = await db_fetchone("SELECT user_id FROM users WHERE user_id=$1", (target,))
        if not target_user:
            return await msg.reply("❌ هذا المستخدم غير مسجل في البوت.")
        await state.update_data(target=target)
        await state.set_state(TransferStates.amount)
        await msg.reply("💰 أدخل المبلغ المراد تحويله ($):", reply_markup=back_kb("transfer_menu"))
    except ValueError:
        await msg.reply("❌ أدخل رقماً صحيحاً.")

@router.message(TransferStates.amount)
async def transfer_process(msg: Message, state: FSMContext):
    try:
        amount = float(msg.text.strip())
        if amount <= 0:
            return await msg.reply("❌ المبلغ يجب أن يكون أكبر من صفر.")
        sender = msg.from_user.id
        data = await state.get_data()
        receiver = data['target']
        fee = amount * TRANSFER_FEE_PERCENT
        net_amount = amount - fee
        bal = await db_fetchone("SELECT balance FROM users WHERE user_id=$1", (sender,))
        if not bal or bal[0] < amount:
            await state.clear()
            return await msg.reply(f"❌ رصيدك غير كافٍ. المطلوب: {amount}$", reply_markup=main_kb(sender))
        await db_execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", (amount, sender))
        await db_execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", (net_amount, receiver))
        await msg.reply(f"✅ تم التحويل بنجاح!\n💸 المبلغ المستلم: {net_amount:.2f}$\n📉 العمولة: {fee:.2f}$", reply_markup=main_kb(sender))
        try:
            await bot.send_message(receiver, f"🎉 وصلك تحويل من المستخدم <code>{sender}</code> بقيمة <b>{net_amount:.2f}$</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    except ValueError:
        return await msg.reply("❌ أدخل رقماً صحيحاً.")
    except Exception as e:
        await msg.reply(f"❌ حدث خطأ: {e}")
    await state.clear()

@router.callback_query(F.data == "transfer_link_create")
async def create_link_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(TransferStates.link_amount)
    await safe_edit_or_send(c.message, text="🔗 إنشاء رابط دفع\n💰 أدخل المبلغ الذي تريد وضعه في الرابط:\n⚠️ سيتم خصم هذا المبلغ من رصيدك فوراً.", reply_markup=back_kb("transfer_menu"))

@router.message(TransferStates.link_amount)
async def create_link_process(msg: Message, state: FSMContext):
    try:
        amount = float(msg.text.strip())
        if amount <= 0:
            return await msg.reply("❌ المبلغ يجب أن يكون أكبر من صفر.")
        sender = msg.from_user.id
        bal = await db_fetchone("SELECT balance FROM users WHERE user_id=$1", (sender,))
        if not bal or bal[0] < amount:
            await state.clear()
            return await msg.reply(f"❌ رصيدك غير كافٍ لإنشاء هذا الرابط. المطلوب: {amount}$", reply_markup=main_kb(sender))
        await db_execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", (amount, sender))
        unique_id = str(uuid.uuid4())[:8]
        timestamp = int(datetime.now().timestamp())
        hash_part = hashlib.md5(f"{sender}_{timestamp}_{unique_id}".encode()).hexdigest()[:6]
        link_code = f"pay_{hash_part}_{unique_id}_{timestamp}"
        bot_username = (await bot.get_me()).username
        link_url = f"https://t.me/{bot_username}?start={link_code}"
        await db_execute("INSERT INTO user_transfers (sender_id, amount, link, status) VALUES ($1,$2,$3, 'pending')", (sender, amount, link_code))
        copy_btn = InlineKeyboardButton(text=" نسخ الرابط", copy_text=CopyTextButton(text=link_url), style='primary')
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [copy_btn],
            [InlineKeyboardButton(text="🔙 العودة للقائمة", callback_data="main_menu", style='primary')]
        ])
        await msg.reply(
            f"✅ <b>تم إنشاء رابط الدفع بنجاح!</b>\n\n"
            f"💰 القيمة في الرابط: <b>{amount:.2f}$</b>\n"
            f"🔗 <b>الرابط:</b>\n<code>{link_url}</code>\n\n"
            f"⚠️ أي شخص يفتح الرابط ويستلم سيحصل على المبلغ كاملاً.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
    except ValueError:
        return await msg.reply("❌ أدخل رقماً صحيحاً.")
    except Exception as e:
        await msg.reply(f"❌ حدث خطأ: {e}")
    await state.clear()

@router.callback_query(F.data == "admin_panel")
async def admin_panel(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    users_count = (await db_fetchone("SELECT COUNT(*) FROM users"))[0]
    accs_count = (await db_fetchone("SELECT COUNT(*) FROM accounts WHERE status='available'"))[0]
    kb = [
        [InlineKeyboardButton(text="📂 رفع ZIP جماعي", callback_data="admin_bulk_zip", style='success'),
         InlineKeyboardButton(text="👥 إدارة المستخدمين", callback_data="admin_users_manage", style='primary')],
        [InlineKeyboardButton(text="🌍 إدارة الأقسام والأسعار", callback_data="admin_countries_manage", style='primary')],
        [InlineKeyboardButton(text="🔙 خروج", callback_data="main_menu", style='primary')]
    ]
    text = f"👑 لوحة التحكم\n👥 المستخدمين: {users_count}\n📦 الحسابات المتاحة: {accs_count}"
    await safe_edit_or_send(c.message, text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "admin_bulk_zip")
async def start_bulk_zip(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminUploadStates.waiting_for_zip)
    await safe_edit_or_send(c.message, text="📂 الرفع الجماعي عبر ZIP\n\nأرسل ملف .zip يحتوي على ملفات .session:", reply_markup=back_kb("admin_panel"))

@router.message(AdminUploadStates.waiting_for_zip, F.document)
async def process_zip_file(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    fname = msg.document.file_name
    if not fname or not fname.endswith('.zip'):
        return await msg.reply("❌ الملف يجب أن يكون بصيغة .zip")
    tmp_path = os.path.join(TEMP_DIR, f"{uuid.uuid4()}_{fname}")
    await bot.download(msg.document, destination=tmp_path)
    sessions = []
    extract_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(tmp_path, 'r') as zip_ref:
            for name in zip_ref.namelist():
                if name.endswith('.session') and not name.startswith('__MACOSX'):
                    zip_ref.extract(name, extract_dir)
                    sessions.append(os.path.join(extract_dir, name))
        if not sessions:
            await msg.reply("❌ لم يتم العثور على ملفات .session داخل الـ ZIP")
            shutil.rmtree(extract_dir, ignore_errors=True)
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return
        await state.update_data(sessions=sessions, zip_path=tmp_path, extract_dir=extract_dir)
        cats_rows = await db_fetchall("SELECT country FROM categories ORDER BY country")
        accs_rows = await db_fetchall("SELECT DISTINCT country FROM accounts ORDER BY country")
        all_cats = sorted(list(set([r[0] for r in cats_rows] + [r[0] for r in accs_rows])))
        kb = []
        for row_name in all_cats:
            kb.append([InlineKeyboardButton(text=f"🌍 {row_name}", callback_data=f"sel_country:{row_name}", style='primary')])
        kb.append([InlineKeyboardButton(text="➕ قسم جديد", callback_data="new_country", style='success')])
        kb.append([InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_panel", style='danger')])
        await msg.reply(f"✅ تم استخراج {len(sessions)} جلسة.\n\nاختر قسماً موجوداً أو اضغط 'قسم جديد':", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        await msg.reply(f"❌ خطأ في فك الضغط: {e}")
        shutil.rmtree(extract_dir, ignore_errors=True)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@router.callback_query(F.data.startswith("sel_country:"))
async def select_existing_country(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    country = c.data.split(":", 1)[1]
    await state.update_data(country=country)
    await state.set_state(AdminUploadStates.waiting_for_price)
    await c.message.answer(f"💰 أدخل سعر البيع الموحد لقسم '{country}':")
    await c.answer()

@router.callback_query(F.data == "new_country")
async def select_new_country(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminUploadStates.waiting_for_country)
    await c.message.answer("✏️ أدخل اسم القسم/الدولة الجديد:")
    await c.answer()

@router.message(AdminUploadStates.waiting_for_country)
async def set_country(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    country_name = msg.text.strip()
    await state.update_data(country=country_name)
    await db_execute("INSERT INTO categories (country) VALUES ($1) ON CONFLICT (country) DO NOTHING", (country_name,))
    await state.set_state(AdminUploadStates.waiting_for_price)
    await msg.reply("💰 أدخل سعر البيع الموحد لهذه الجلسات:")

@router.message(AdminUploadStates.waiting_for_price)
async def ask_for_2fa_mandatory(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        price = float(msg.text.strip())
        if price <= 0:
            raise ValueError
        await state.update_data(price=price)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭️ تخطي (بدون كلمة مرور)", callback_data="skip_2fa_confirm", style='primary')]
        ])
        await state.set_state(AdminUploadStates.waiting_for_2fa_input)
        await msg.reply(
            "🔐 <b>خطوة إلزامية:</b>\n\n"
            "هل تحتوي هذه الدفعة من الجلسات على كلمة مرور (2FA)؟\n\n"
            "• إذا كانت <b>نعم</b>: أرسل كلمة المرور الآن.\n"
            "• إذا كانت <b>لا</b>: اضغط على زر 'تخطي' أدناه.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
    except ValueError:
        await msg.reply("❌ أدخل رقماً صحيحاً للسعر.")

@router.message(AdminUploadStates.waiting_for_2fa_input)
async def save_with_provided_2fa(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    fa = msg.text.strip()
    await state.update_data(fa=fa)
    await finalize_bulk_upload(msg, state)

@router.callback_query(F.data == "skip_2fa_confirm")
async def save_without_2fa_callback(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await c.answer()
    await state.update_data(fa=None)
    await finalize_bulk_upload(c.message, state)

async def finalize_bulk_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    sessions = data.get('sessions', [])
    country = data.get('country', 'General')
    price = data.get('price', 1.0)
    fa = data.get('fa')
    zip_path = data.get('zip_path')
    extract_dir = data.get('extract_dir')

    total = len(sessions)
    success = 0
    failed = 0

    status_msg = await message.answer(f"⏳ <b>جاري فحص الجلسات...</b>\n\n0 / {total}", parse_mode=ParseMode.HTML)

    for i, sess_path in enumerate(sessions):
        client = TelegramClient(sess_path, API_ID, API_HASH)
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                if me and me.phone:
                    phone = me.phone.replace('+', '')
                    final_name = f"{phone}.session"
                    dst = os.path.join(SESSIONS_DIR, final_name)
                    await client.disconnect()
                    shutil.copy2(sess_path, dst)
                    await db_execute(
                        "INSERT INTO accounts (phone, session_file, price, country, status, session_2fa_pass) VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (phone) DO UPDATE SET session_file = EXCLUDED.session_file, price = EXCLUDED.price, country = EXCLUDED.country, status = EXCLUDED.status, session_2fa_pass = EXCLUDED.session_2fa_pass",
                        (phone, final_name, price, country, 'available', fa)
                    )
                    success += 1
                else:
                    failed += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            if os.path.exists(sess_path):
                try:
                    os.remove(sess_path)
                except Exception:
                    pass

        current = i + 1
        if current % 3 == 0 or current == total:
            try:
                await status_msg.edit_text(
                    f"⏳ <b>جاري فحص الجلسات...</b>\n\n"
                    f"✅ ناجح: {success}\n"
                    f"❌ فاشل: {failed}\n"
                    f"📊 التقدم: {current} / {total}",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    if zip_path and os.path.exists(zip_path):
        try:
            os.remove(zip_path)
        except Exception:
            pass
    if extract_dir and os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)

    final_text = (
        f"✅ <b>تم الانتهاء من الرفع!</b>\n\n"
        f"🔢 العدد الكلي: {total}\n"
        f"✅ تم إضافته: {success}\n"
        f"❌ فشل/تالف: {failed}\n"
        f"🌍 القسم: {country}\n"
        f"💰 السعر: {price}$\n"
        f"🔐 2FA: {'نعم' if fa else 'لا'}"
    )
    await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML, reply_markup=back_kb("admin_panel"))
    await state.clear()

@router.callback_query(F.data == "admin_users_manage")
async def admin_users_search(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminUserStates.search_uid)
    await safe_edit_or_send(c.message, text="🔍 أدخل الـ ID الرقمي للمستخدم:", reply_markup=back_kb("admin_panel"))

@router.message(AdminUserStates.search_uid)
async def admin_show_user(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(msg.text.strip())
        user = await db_fetchone("SELECT user_id, username, balance FROM users WHERE user_id=$1", (uid,))
        if not user:
            return await msg.reply("❌ المستخدم غير موجود.")
        u_id, u_name, u_bal = user
        kb = [
            [InlineKeyboardButton(text="➕ زيادة رصيد", callback_data=f"bal_add:{uid}", style='success'),
             InlineKeyboardButton(text="➖ إنقاص رصيد", callback_data=f"bal_sub:{uid}", style='danger')],
            [InlineKeyboardButton(text="🔄 تصفير الرصيد", callback_data=f"bal_zero:{uid}", style='danger'),
             InlineKeyboardButton(text="🔙 بحث جديد", callback_data="admin_users_manage", style='primary')]
        ]
        text = f"👤 بيانات المستخدم\n🆔 ID: <code>{u_id}</code>\n👤 الاسم: {u_name}\n💰 الرصيد: <b>{u_bal:.2f}$</b>"
        await msg.reply(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await state.clear()
    except ValueError:
        await msg.reply("❌ أدخل ID صحيح.")

@router.callback_query(F.data.startswith("bal_add:") | F.data.startswith("bal_sub:") | F.data.startswith("bal_zero:"))
async def admin_balance_action(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    action, uid_str = c.data.split(":")
    uid = int(uid_str)
    if action == "bal_zero":
        await db_execute("UPDATE users SET balance = 0 WHERE user_id=$1", (uid,))
        await c.answer("✅ تم تصفير الرصيد", show_alert=True)
        return
    await state.update_data(bal_uid=uid, bal_type=action.replace("bal_", ""))
    await state.set_state(AdminUserStates.edit_balance)
    txt = "زيادة" if action == "bal_add" else "إنقاص"
    await c.message.answer(f"💰 أدخل المبلغ المراد {txt}ه:")
    await c.answer()

@router.message(AdminUserStates.edit_balance)
async def admin_balance_process(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        amount = float(msg.text.strip())
        data = await state.get_data()
        uid, atype = data['bal_uid'], data['bal_type']
        if atype == "add":
            await db_execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", (amount, uid))
            await msg.reply(f"✅ تمت إضافة {amount}$ للمستخدم {uid}", reply_markup=back_kb("admin_panel"))
        elif atype == "sub":
            await db_execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", (amount, uid))
            await msg.reply(f"✅ تم خصم {amount}$ من المستخدم {uid}", reply_markup=back_kb("admin_panel"))
        await state.clear()
    except ValueError:
        await msg.reply("❌ رقم غير صحيح.")

@router.callback_query(F.data == "admin_countries_manage")
async def admin_countries_list(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    cats_rows = await db_fetchall("SELECT country FROM categories ORDER BY country")
    accs_rows = await db_fetchall("SELECT DISTINCT country FROM accounts ORDER BY country")
    all_cats = sorted(list(set([r[0] for r in cats_rows] + [r[0] for r in accs_rows])))
    kb = [[InlineKeyboardButton(text=f"🌍 {row}", callback_data=f"edit_country:{row}", style='primary')] for row in all_cats]
    if not kb:
        kb.append([InlineKeyboardButton(text="لا توجد أقسام", callback_data="noop")])
    kb.append([InlineKeyboardButton(text="🔙 عودة", callback_data="admin_panel", style='primary')])
    await safe_edit_or_send(c.message, text="🌍 اختر القسم للتعديل عليه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "buy_menu")
async def buy_menu(c: CallbackQuery):
    await show_countries_page(c.message, page=0)

async def show_countries_page(message: Message, page=0):
    rows_per_page = 14
    cats_rows = await db_fetchall("SELECT country FROM categories ORDER BY country")
    accs_rows = await db_fetchall("SELECT DISTINCT country FROM accounts WHERE status='available' ORDER BY country")
    all_countries_names = sorted(list(set([r['country'] for r in cats_rows] + [r['country'] for r in accs_rows])))
    if not all_countries_names:
        kb = [
            [InlineKeyboardButton(text="لا توجد أقسام متاحة حالياً", callback_data="noop")],
            [InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
        ]
        await safe_edit_or_send(message, text="🛒 لا توجد دول متاحة حالياً.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        return
    total_pages = (len(all_countries_names) + rows_per_page - 1) // rows_per_page
    start_idx = page * rows_per_page
    end_idx = start_idx + rows_per_page
    page_countries = all_countries_names[start_idx:end_idx]
    kb = []
    i = 0
    while i < len(page_countries):
        c1_name = page_countries[i]
        c1_row = await db_fetchone("SELECT COUNT(*) FROM accounts WHERE country=$1 AND status='available'", (c1_name,))
        c1_count = c1_row[0] if c1_row else 0
        if i + 1 < len(page_countries):
            c2_name = page_countries[i + 1]
            c2_row = await db_fetchone("SELECT COUNT(*) FROM accounts WHERE country=$1 AND status='available'", (c2_name,))
            c2_count = c2_row[0] if c2_row else 0
            if len(c1_name) > 15 or len(c2_name) > 15:
                kb.append([InlineKeyboardButton(text=f"{c1_name} | {c1_count}", callback_data=f"view_accs:{c1_name}", style='primary')])
                kb.append([InlineKeyboardButton(text=f"{c2_name} | {c2_count}", callback_data=f"view_accs:{c2_name}", style='primary')])
            else:
                kb.append([
                    InlineKeyboardButton(text=f"{c1_name} | {c1_count}", callback_data=f"view_accs:{c1_name}", style='primary'),
                    InlineKeyboardButton(text=f"{c2_name} | {c2_count}", callback_data=f"view_accs:{c2_name}", style='primary')
                ])
            i += 2
        else:
            kb.append([InlineKeyboardButton(text=f"{c1_name} | {c1_count}", callback_data=f"view_accs:{c1_name}", style='primary')])
            i += 1
    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton(text="<< السابق", callback_data=f"countries_page:{page-1}", style='success'))
    if page < total_pages - 1:
        nav_btns.append(InlineKeyboardButton(text="التالي >>", callback_data=f"countries_page:{page+1}", style='success'))
    if nav_btns:
        kb.append(nav_btns)
    kb.append([InlineKeyboardButton(text="🔍 بحث عن دولة", callback_data="search_country_start", style='primary')])
    kb.append([InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')])
    await safe_edit_or_send(message, text="اختــر الدولة الــتــي تــريــد شـراء حســاب منهــا:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("countries_page:"))
async def countries_pagination(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    await show_countries_page(c.message, page=page)

@router.callback_query(F.data == "search_country_start")
async def search_country_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(BuySessionStates.select_country)
    await safe_edit_or_send(c.message, text="🔍 أرسل اسم الدولة، رمزها، أو علمها للبحث:", reply_markup=back_kb("buy_menu"))

@router.message(BuySessionStates.select_country)
async def process_search(msg: Message, state: FSMContext):
    query = msg.text.strip()
    matches = await db_fetchall("SELECT DISTINCT country FROM accounts WHERE country LIKE $1", (f"%{query}%",))
    await state.clear()
    if not matches:
        await msg.reply("❌ لم يتم العثور على نتائج مطابقة.", reply_markup=back_kb("buy_menu"))
        return
    kb = []
    for row in matches:
        country_name = row['country']
        count_row = await db_fetchone("SELECT COUNT(*) FROM accounts WHERE country=$1 AND status='available'", (country_name,))
        count = count_row[0] if count_row else 0
        kb.append([InlineKeyboardButton(text=f"{country_name} | {count}", callback_data=f"view_accs:{country_name}", style='primary')])
    kb.append([InlineKeyboardButton(text="🔙 عودة للقائمة", callback_data="buy_menu", style='primary')])
    await msg.reply(f"🔍 نتائج البحث عن '{query}':", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("view_accs:"))
async def view_accounts(c: CallbackQuery):
    country = c.data.split(":", 1)[1]
    accs = await db_fetchall("SELECT id, phone, price FROM accounts WHERE country=$1 AND status='available' LIMIT 10", (country,))
    kb = []
    for acc in accs:
        masked = mask_phone(acc['phone'])
        kb.append([InlineKeyboardButton(text=f"{masked} | {acc['price']}$", callback_data=f"buy_acc:{acc['id']}", style='success')])
    if not kb:
        kb.append([InlineKeyboardButton(text="لا توجد حسابات متاحة حالياً (القسم فارغ)", callback_data="noop")])
    kb.append([InlineKeyboardButton(text="🔙 عودة", callback_data="buy_menu", style='primary')])
    await safe_edit_or_send(c.message, text=f"📱 حسابات {country}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("buy_acc:"))
async def buy_account(c: CallbackQuery):
    aid = int(c.data.split(":", 1)[1])
    acc = await db_fetchone("SELECT phone, price, country, session_2fa_pass FROM accounts WHERE id=$1 AND status='available'", (aid,))
    if not acc:
        return await c.answer("تم بيعه أو لم يعد متاحاً", show_alert=True)
    phone, price, country, fa = acc['phone'], acc['price'], acc['country'], acc['session_2fa_pass']
    uid = c.from_user.id
    bal = await db_fetchone("SELECT balance FROM users WHERE user_id=$1", (uid,))
    if not bal or bal[0] < price:
        return await c.answer("رصيدك غير كافٍ", show_alert=True)
    await db_execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", (price, uid))
    await db_execute("UPDATE accounts SET status='processing' WHERE id=$1", (aid,))
    formatted_phone = f"+{phone}" if not phone.startswith("+") else phone
    copy_phone_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=" نسخ الرقم", copy_text=CopyTextButton(text=formatted_phone), style='success')],
        [InlineKeyboardButton(text="🔑 جلب الكود", callback_data=f"req:{aid}", style='success'),
         InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
    ])
    await safe_edit_or_send(
        c.message,
        text=f"✅ تم الشراء بنجاح!\n\n"
             f"📞 الرقم: <code>{formatted_phone}</code>\n"
             f"💰 السعر: {price}$\n"
             f"🌍 القسم: {country}\n\n"
             f"🔴 اضغط على 'جلب الكود' عندما يطلب التطبيق رمز التحقق.",
        parse_mode=ParseMode.HTML,
        reply_markup=copy_phone_kb
    )

async def get_real_code(msg: Message, phone, aid, country, uid, fa):
    sess_row = await db_fetchone("SELECT session_file, status FROM accounts WHERE id=$1", (aid,))
    if not sess_row:
        return
    sess_file, current_status = sess_row['session_file'], sess_row['status']
    path = os.path.join(SESSIONS_DIR, sess_file)
    client = TelegramClient(path, API_ID, API_HASH)
    found = False
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise Exception("Invalid Session")
        try:
            await client.get_me()
        except SessionPasswordNeededError:
            if fa:
                try:
                    await client.sign_in(password=fa)
                except Exception:
                    await msg.answer("❌ خطأ في كلمة 2FA المسجلة.")
                    if current_status != 'sold':
                        await refund(aid, uid)
                    return
            else:
                await msg.answer("❌ الحساب محمي ولا توجد كلمة مرور مسجلة.")
                if current_status != 'sold':
                    await refund(aid, uid)
                return

        for _ in range(18):
            await asyncio.sleep(5)
            try:
                async for m in client.iter_messages(777000, limit=5):
                    if m.text:
                        match = re.search(r'\b(\d{5})\b', m.text)
                        if match:
                            found = True
                            code = match.group(1)
                            kb_rows = [
                                [InlineKeyboardButton(text=" نسخ الكود", copy_text=CopyTextButton(text=code), style='success')]
                            ]
                            if fa:
                                kb_rows.append([InlineKeyboardButton(text="🔐 نسخ كلمة المرور", copy_text=CopyTextButton(text=fa), style='primary')])
                            kb_rows.append([
                                InlineKeyboardButton(text="🔄 إعادة جلب الكود", callback_data=f"req:{aid}", style='primary'),
                                InlineKeyboardButton(text="🚪 تسجيل الخروج", callback_data=f"logout:{aid}", style='danger')
                            ])
                            kb_rows.append([InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')])
                            copy_code_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
                            formatted_phone = f"+{phone}" if not phone.startswith("+") else phone
                            txt = (f"✅ تم استلام رمز التحقق!\n\n"
                                   f"📞 الرقم: <code>{formatted_phone}</code>\n"
                                   f"🔢 رمز الدخول: <code>{code}</code>\n"
                                   f"🔐 كلمة المرور (2FA): <code>{fa or 'غير موجودة'}</code>\n\n"
                                   f"⚠️ اضغط تسجيل الخروج بعد الانتهاء لحماية الحساب\n"
                                   f"⏳ سيتم الخروج تلقائياً بعد 10 دقائق")
                            await safe_edit_or_send(msg, text=txt, parse_mode=ParseMode.HTML, reply_markup=copy_code_kb)
                            if current_status != 'sold':
                                acc_price_row = await db_fetchone("SELECT price FROM accounts WHERE id=$1", (aid,))
                                acc_price = acc_price_row['price']
                                conn = await asyncpg.connect(DATABASE_URL)
                                try:
                                    order_row = await conn.fetchrow(
                                        "INSERT INTO orders (user_id, phone, price, date) VALUES ($1, $2, $3, $4) RETURNING id",
                                        uid, phone, acc_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                    )
                                    order_id = order_row['id']
                                finally:
                                    await conn.close()

                                notif_text = (
                                    f"✅ تم شراء حساب جديد من البوت\n\n"
                                    f"🌍 الدولة : {country}\n"
                                    f"📱 المنصة : تليجرام\n"
                                    f"📞 الرقم : {formatted_phone[:-3]}***\n"
                                    f"💰 السعر : ${acc_price}\n"
                                    f"👤 العميل : {str(uid)[:-3]}***\n"
                                    f"🔢 كود التفعيل : {code}\n"
                                    f"🔖 رقم الطلب : {order_id}\n"
                                    f"🕒 التاريخ و الوقت : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                                )
                                channel_kb = InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="🤖 الذهاب للبوت", url=f"https://t.me/{(await bot.get_me()).username}", style='success')]
                                ])
                                try:
                                    await bot.send_message(NOTIFICATION_CHANNEL, notif_text, reply_markup=channel_kb)
                                except Exception:
                                    pass
                                await db_execute("UPDATE accounts SET status='sold' WHERE id=$1", (aid,))
                                if aid in pending_logouts:
                                    pending_logouts[aid].cancel()
                                task = asyncio.create_task(auto_logout(aid, uid, formatted_phone, sess_file))
                                pending_logouts[aid] = task
                            break
            except Exception:
                continue
            if found:
                break
        if not found:
            if current_status != 'sold':
                await msg.answer("⏰ لم يصل رمز التحقق خلال الوقت المحدد. تم استرجاع الرصيد.")
                await refund(aid, uid)
            else:
                await msg.answer("⏰ لم يصل رمز تحقق جديد.")
    except Exception as e:
        logger.error(f"Login error: {e}")
        if current_status != 'sold':
            await msg.answer("❌ حدث خطأ أثناء الدخول للجلسة. تم استرجاع الرصيد.")
            await refund(aid, uid)
        else:
            await msg.answer("❌ تعذر الاتصال بالجلسة.")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

@router.callback_query(F.data.startswith("req:"))
async def req_code(c: CallbackQuery):
    aid = int(c.data.split(":", 1)[1])
    acc = await db_fetchone("SELECT phone, country, session_2fa_pass FROM accounts WHERE id=$1", (aid,))
    if not acc:
        return await c.answer("الحساب غير موجود", show_alert=True)
    phone, country, fa = acc['phone'], acc['country'], acc['session_2fa_pass']
    await safe_edit_or_send(c.message, text=f"⏳ جاري جلب رمز التحقق لـ +{phone.lstrip('+')}...\nقم بطلب الكود في تطبيق تيليجرام الآن.")
    asyncio.create_task(get_real_code(c.message, phone, aid, country, c.from_user.id, fa))

@router.callback_query(F.data.startswith("logout:"))
async def logout_session(c: CallbackQuery):
    aid = int(c.data.split(":", 1)[1])
    if aid in pending_logouts:
        pending_logouts[aid].cancel()
        del pending_logouts[aid]
    acc = await db_fetchone("SELECT phone, session_file FROM accounts WHERE id=$1", (aid,))
    if not acc:
        return await c.answer("الحساب غير موجود", show_alert=True)
    path = os.path.join(SESSIONS_DIR, acc['session_file'])
    client = TelegramClient(path, API_ID, API_HASH)
    try:
        await client.connect()
        await client.log_out()
        await safe_edit_or_send(c.message, text=f"📞 +{acc['phone'].lstrip('+')}\n\n✅ تم تسجيل الخروج بنجاح وحماية الحساب", reply_markup=back_kb("main_menu"))
    except Exception:
        await safe_edit_or_send(c.message, text=f"📞 +{acc['phone'].lstrip('+')}\n\n❌ حدث خطأ أثناء تسجيل الخروج أو تم الخروج مسبقاً", reply_markup=back_kb("main_menu"))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

async def auto_logout(aid, uid, phone, sess):
    await asyncio.sleep(600)
    if aid not in pending_logouts:
        return
    del pending_logouts[aid]
    path = os.path.join(SESSIONS_DIR, sess)
    client = TelegramClient(path, API_ID, API_HASH)
    try:
        await client.connect()
        await client.log_out()
        try:
            await bot.send_message(uid, f"⏰ تسجيل خروج تلقائي\n\n📞 {phone}\n✅ تم تسجيل الخروج من البوت بعد 10 دقائق لحماية حسابك.")
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

async def refund(aid, uid):
    try:
        p_row = await db_fetchone("SELECT price, status FROM accounts WHERE id=$1", (aid,))
        if p_row and p_row['status'] == 'processing':
            await db_execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", (p_row['price'], uid))
            await db_execute("UPDATE accounts SET status='corrupted' WHERE id=$1", (aid,))
    except Exception:
        pass

@router.callback_query(F.data == "settings_menu")
async def settings_menu(c: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="⚖️ شروط الاستخدام", callback_data="terms_of_use", style='primary'),
         InlineKeyboardButton(text="🛠️ الدعم الفني", url="https://t.me/hattab7657", style='success')],
        [InlineKeyboardButton(text="👨‍💻 مطور البوت", url="https://t.me/hattab7657", style='primary')],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
    ]
    await safe_edit_or_send(c.message, text="⚙️ الإعدادات والمعلومات\n\nاختر الخيار المطلوب: ", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "terms_of_use")
async def show_terms(c: CallbackQuery):
    terms_text = ("📜 شروط الاستخدام\n\n"
                  "1️⃣ الاستخدام المشروع: جميع الحسابات المباعة عبر هذا البوت مخصصة للاستخدام في ما يرضي الله تعالى فقط.\n\n"
                  "2️⃣ إخلاء المسؤولية: البوت ومطوروه يتبرؤون تماماً من أي استخدام مسيء أو غير قانوني للحسابات.\n\n"
                  "3️⃣ مكافحة الاحتيال: أي محاولة للاحتيال على البوت ستؤدي إلى حظر حسابك نهائياً ومصادرة رصيدك.")
    kb = [[InlineKeyboardButton(text="🔙 عودة", callback_data="settings_menu", style='primary')]]
    await safe_edit_or_send(c.message, text=terms_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# ====== 1. Rise SMM API (SMS بۆلۈمى) ======
@router.callback_query(F.data == "sms_menu")
async def sms_menu(c: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="📨 طلب خدمة SMS جديدة", callback_data="rise_sms_services", style='success')],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
    ]
    text = (
        "📨 <b>خدمة SMS (عبر Rise SMM)</b>\n\n"
        "التوكن النشط والمربوط:\n"
        "<code>a4a725e2decd5670a8b33e3e59e4f13cbfae262c312b69d3d4f5d33e2fba1e7a</code>\n\n"
        "رابط API:\n"
        "<code>https://vnvccv.shop/Ayma/api/v2</code>\n\n"
        "اختر العملية المطلوبة:"
    )
    await safe_edit_or_send(c.message, text=text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "rise_sms_services")
async def rise_sms_services(c: CallbackQuery):
    # Rise SMM API dan xizmatلارنى سوراش ياكى كۆرسىتىش
    res = rise_smm_request("services")
    text = f"📋 <b>خدمات Rise SMM النشطة:</b>\n\n<pre>{res}</pre>"
    kb = [[InlineKeyboardButton(text="🔙 عودة", callback_data="sms_menu", style='primary')]]
    await safe_edit_or_send(c.message, text=text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


# ====== 2. Shark TG API ([شراء حسابات جاهز 2]) ======
@router.callback_query(F.data == "shark_buy_menu")
async def shark_buy_menu(c: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="📱 طلب نومۇر (Get Number)", callback_data="shark_get_number_prompt", style='success')],
        [InlineKeyboardButton(text="🔑 جلب الكود (Get Code)", callback_data="shark_get_code_prompt", style='primary')],
        [InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')]
    ]
    await safe_edit_or_send(c.message, text="🛍 <b>شراء حسابات جاهز 2 (Shark TG)</b>\n\nاختر العملية المطلوبة:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "shark_get_number_prompt")
async def shark_get_number_prompt(c: CallbackQuery, state: FSMContext):
    await state.set_state(SharkStates.waiting_for_country)
    await safe_edit_or_send(c.message, text="🌍 أدخل رمز الدولة المطلوبة لطلب الرقم عبر Shark TG (مثال: ru, us, kaz):", reply_markup=back_kb("shark_buy_menu"))

@router.message(SharkStates.waiting_for_country)
async def shark_process_get_number(msg: Message, state: FSMContext):
    country_code = msg.text.strip()
    await state.clear()
    
    res = shark_get_number(country_code)
    await msg.reply(f"📥 <b>نتيجة طلب الرقم (Shark TG):</b>\n<pre>{res}</pre>", parse_mode=ParseMode.HTML, reply_markup=main_kb(msg.from_user.id))

@router.callback_query(F.data == "shark_get_code_prompt")
async def shark_get_code_prompt(c: CallbackQuery, state: FSMContext):
    await state.set_state(SharkStates.waiting_for_hash)
    await safe_edit_or_send(c.message, text="🔑 أدخل الـ (hash_code) الخاص بالرقم لجلب كود التفعيل:", reply_markup=back_kb("shark_buy_menu"))

@router.message(SharkStates.waiting_for_hash)
async def shark_process_get_code(msg: Message, state: FSMContext):
    hash_code = msg.text.strip()
    await state.clear()
    
    res = shark_get_code(hash_code)
    await msg.reply(f"📥 <b>نتيجة جلب الكود (Shark TG):</b>\n<pre>{res}</pre>", parse_mode=ParseMode.HTML, reply_markup=main_kb(msg.from_user.id))

# ==========================================

@router.callback_query(F.data == "buy_sessions_zip")
async def buy_sessions_zip_start(c: CallbackQuery, state: FSMContext):
    cats_rows = await db_fetchall("SELECT country FROM categories ORDER BY country")
    accs_rows = await db_fetchall("SELECT country, COUNT(*) FROM accounts WHERE status='available' GROUP BY country")
    all_cats_dict = {r['country']: 0 for r in cats_rows}
    for r in accs_rows:
        all_cats_dict[r['country']] = r['count'] if 'count' in r else r[1]
    rows = sorted(all_cats_dict.items())
    kb = []
    if rows:
        for name, count in rows:
            kb.append([InlineKeyboardButton(text=f"{name} ({count} متاح)", callback_data=f"sess_cat:{name}", style='primary')])
    else:
        kb.append([InlineKeyboardButton(text="لا يوجد جلسات متاحة حالياً", callback_data="noop")])
    kb.append([InlineKeyboardButton(text="🔙 عودة", callback_data="main_menu", style='primary')])
    await safe_edit_or_send(c.message, text="📦 شراء جلسات تيليجرام (ZIP)\n\nاختر القسم المطلوب:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("sess_cat:"))
async def sess_cat_select(c: CallbackQuery, state: FSMContext):
    country = c.data.split(":", 1)[1]
    count_row = await db_fetchone("SELECT COUNT(*) FROM accounts WHERE country=$1 AND status='available'", (country,))
    avail = count_row[0] if count_row else 0
    if avail < 2:
        await c.answer("❌ عذراً، يجب توفر جلستين على الأقل للشراء", show_alert=True)
        return
    await state.update_data(sess_country=country, max_avail=avail)
    await state.set_state(BuySessionStates.enter_count)
    await safe_edit_or_send(
        c.message,
        text=f"🌍 القسم: {country}\n✅ المتاح: {avail}\n\n"
             f"⚠️ الحد الأدنى للطلب: 2 جلسات\n"
             f"⚠️ الحد الأقصى: {avail} جلسات\n\n"
             f"🔢 أرسل العدد المطلوب:",
        reply_markup=back_kb("buy_sessions_zip")
    )

@router.message(BuySessionStates.enter_count)
async def process_session_count(msg: Message, state: FSMContext):
    try:
        count = int(msg.text.strip())
    except ValueError:
        await msg.reply("❌ أرسل رقماً صحيحاً.")
        return
    data = await state.get_data()
    country = data.get('sess_country')
    max_avail = data.get('max_avail', 0)
    uid = msg.from_user.id
    if count < 2:
        return await msg.reply("❌ الحد الأدنى للطلب هو 2 جلسات.")
    if count > max_avail:
        return await msg.reply(f"❌ العدد المطلوب ({count}) أكبر من المتاح ({max_avail}).")
    accs = await db_fetchall("SELECT id, phone, session_file, price FROM accounts WHERE country=$1 AND status='available' LIMIT $2", (country, count))
    actual_count = len(accs)
    if actual_count < count:
        await state.clear()
        return await msg.reply(f"❌ المتاح حالياً {actual_count} فقط. حاول مرة أخرى.", reply_markup=main_kb(uid))
    total_price = sum(a['price'] for a in accs)
    bal_row = await db_fetchone("SELECT balance FROM users WHERE user_id=$1", (uid,))
    bal = bal_row[0] if bal_row else 0
    if bal < total_price:
        await state.clear()
        return await msg.reply(f"❌ رصيد غير كافٍ.\nالمطلوب: {total_price:.2f}$\nرصيدك: {bal:.2f}$", reply_markup=main_kb(uid))
    await state.clear()
    await db_execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", (total_price, uid))
    ids = [a['id'] for a in accs]
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("UPDATE accounts SET status='processing' WHERE id = ANY($1::int[])", ids)
    finally:
        await conn.close()

    processing_msg = await msg.reply("⏳ جاري فحص الجلسات وتجهيز الملف... يرجى الانتظار.")
    asyncio.create_task(prepare_and_send_sessions(msg, uid, accs, country, processing_msg))

async def prepare_and_send_sessions(msg: Message, uid, accs, country, processing_msg: Message):
    valid_sessions = []
    passwords_content = "=== بيانات الجلسات ===\n\n"
    caption_passwords = []
    actual_paid = 0.0

    for acc in accs:
        aid, phone, sess_file, price = acc['id'], acc['phone'], acc['session_file'], acc['price']
        path = os.path.join(SESSIONS_DIR, sess_file)
        if not os.path.exists(path):
            await db_execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", (price, uid))
            await db_execute("UPDATE accounts SET status='corrupted' WHERE id=$1", (aid,))
            continue
        is_valid = False
        client = TelegramClient(path, API_ID, API_HASH)
        try:
            await client.connect()
            if await client.is_user_authorized():
                is_valid = True
        except Exception:
            pass
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        if is_valid:
            fa_row = await db_fetchone("SELECT session_2fa_pass FROM accounts WHERE id=$1", (aid,))
            fa = fa_row[0] if fa_row else None
            valid_sessions.append((aid, path, sess_file, phone, fa, price))
            actual_paid += price
            pw_line = f"Phone: +{phone.lstrip('+')}\n2FA: {fa if fa else 'None'}\n{'-'*30}\n"
            passwords_content += pw_line
            caption_passwords.append(f"📞 +{phone.lstrip('+')} | 🔐 <code>{fa if fa else 'بدون'}</code>")
        else:
            await db_execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", (price, uid))
            await db_execute("UPDATE accounts SET status='corrupted' WHERE id=$1", (aid,))

    if not valid_sessions:
        await processing_msg.edit_text("❌ للأسف، جميع الجلسات المختارة كانت غير صالحة. تم إرجاع المبلغ بالكامل.")
        return

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for _, path, sess_file, _, _, _ in valid_sessions:
            zipf.write(path, arcname=sess_file)
        zipf.writestr("passwords.txt", passwords_content)
    zip_buffer.seek(0)

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    zip_filename = f"Sessions_{country}_{timestamp}.zip"

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        for aid, _, _, phone, _, price in valid_sessions:
            await conn.execute("UPDATE accounts SET status='sold' WHERE id=$1", aid)
            await conn.execute(
                "INSERT INTO orders (user_id, phone, price, date) VALUES ($1, $2, $3, $4)",
                uid, phone, price, datetime.now().strftime("%Y/%m/%d %H:%M")
            )
    finally:
        await conn.close()

    passwords_text = "\n".join(caption_passwords[:15])
    if len(caption_passwords) > 15:
        passwords_text += "\n... (باقي التفاصيل داخل ملف passwords.txt)"

    caption = (
        f"✅ تم شراء الجلسات بنجاح!\n\n"
        f"🌍 القسم: {country}\n"
        f"🔢 العدد الصالح: {len(valid_sessions)}\n"
        f"💰 المبلغ المخصوم: {actual_paid:.2f}$\n\n"
        f"🔐 <b>كلمات المرور (2FA):</b>\n{passwords_text}\n\n"
        f"📎 تم إرفاق ملف passwords.txt داخل الأرشيف."
    )
    try:
        await msg.answer_document(
            document=BufferedInputFile(zip_buffer.read(), filename=zip_filename),
            caption=caption,
            parse_mode=ParseMode.HTML
        )
        await processing_msg.delete()
    except Exception as e:
        logger.error(f"Error sending ZIP: {e}")
        await processing_msg.edit_text("❌ حدث خطأ أثناء إرسال الملف. تواصل مع الدعم.")

async def web_server():
    async def handle(request):
        return web.Response(text="Bot is running!")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")

async def main():
    await init_db()
    await web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
