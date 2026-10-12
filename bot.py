"""
🤖 Моніторинг Виробництва — Telegram Бот
Вносить щоденні звіти, чеки, накладні прямо з Telegram
Зберігає дані у Firebase (той самий що й веб-програма)
"""

import os
import re
import json
import logging
import base64
import csv
import io
import traceback
import time
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta, time as dtime
from io import BytesIO

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, ExtBot
)
from telegram.error import BadRequest, Conflict
import firebase_admin
from firebase_admin import credentials, firestore

# ── Anthropic для розпізнавання чеків ────────────────────────
try:
    import google.generativeai as genai
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── КОНФІГУРАЦІЯ (задається через змінні середовища) ─────────
BOT_TOKEN    = os.environ.get('BOT_TOKEN', '')
FIREBASE_KEY = os.environ.get('FIREBASE_KEY', '')   # JSON рядок з ключем Firebase
CLAUDE_KEY   = os.environ.get('GEMINI_API_KEY', '')

# ── СИНХРОНІЗАЦІЯ З GOOGLE-ТАБЛИЦЕЮ "Облік_продажу" ───────────
SHEET_ID          = os.environ.get('SHEET_ID', '1LUB5jakppvWBo9MQN2ouRM7HKao4dv9MpDkKWsRWh0Y')
SHEET_GID_PURCHASES = os.environ.get('SHEET_GID_PURCHASES', '416162921')   # аркуш "Закупки"
SHEET_GID_SALES      = os.environ.get('SHEET_GID_SALES', '1456202155')     # аркуш "Продажі"

# ── ДОСТУП (задається через змінні середовища) ────────────────
# ALLOWED_USER_IDS: через кому, напр. "123456789,987654321"
# OWNER_ID: власник (може видаляти записи). Якщо не задано — перший з ALLOWED_USER_IDS
ALLOWED_IDS = [i.strip() for i in os.environ.get('ALLOWED_USER_IDS', '').split(',') if i.strip()]
OWNER_ID    = os.environ.get('OWNER_ID', '') or (ALLOWED_IDS[0] if ALLOWED_IDS else '')

def is_allowed(user_id):
    """Якщо ALLOWED_USER_IDS не задано — доступ відкритий (для першого налаштування)"""
    if not ALLOWED_IDS:
        return True
    return str(user_id) in ALLOWED_IDS

def is_owner(user_id):
    return bool(OWNER_ID) and str(user_id) == str(OWNER_ID)

# ── СТАНИ РОЗМОВИ ─────────────────────────────────────────────
(
    MAIN_MENU,
    SELECT_OBJECT, SELECT_DATE,
    DAILY_WORKERS, DAILY_MATERIALS, DAILY_TRANSPORT, DAILY_DESC, DAILY_CONFIRM,
    RECEIPT_PHOTO, RECEIPT_CONFIRM, RECEIPT_TARGET,
    STOCK_ACTION, STOCK_NAME, STOCK_QTY,
    HISTORY_LIST,
    OBJECTS_ACTION, OBJ_INPUT,
    PLAN_MENU, PLAN_TEXT, PLAN_DATE, PLAN_TIME,
) = range(21)

# ── FIREBASE ──────────────────────────────────────────────────
db = None

def init_firebase():
    global db
    try:
        if FIREBASE_KEY:
            cred_dict = json.loads(FIREBASE_KEY)
            cred = credentials.Certificate(cred_dict)
        else:
            cred = credentials.Certificate('firebase-key.json')
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase підключено ✅")
    except Exception as e:
        logger.error(f"Firebase помилка: {e}")

def fb_get_all(collection):
    """Отримати всі документи з колекції"""
    if not db: return []
    try:
        docs = db.collection(collection).get()
        return [{'id': d.id, **d.to_dict()} for d in docs]
    except Exception as e:
        logger.error(f"Firebase read error {collection}: {e}")
        return []

def fb_set(collection, doc_id, data):
    """Зберегти документ"""
    if not db: return False
    try:
        db.collection(collection).document(doc_id).set(data)
        return True
    except Exception as e:
        logger.error(f"Firebase write error: {e}")
        return False

def fb_add(collection, data):
    """Додати новий документ"""
    if not db: return None
    try:
        import random, string
        doc_id = '_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        data['id'] = doc_id
        db.collection(collection).document(doc_id).set(data)
        return doc_id
    except Exception as e:
        logger.error(f"Firebase add error: {e}")
        return None

def fb_delete(collection, doc_id):
    """Видалити документ"""
    if not db: return False
    try:
        db.collection(collection).document(doc_id).delete()
        return True
    except Exception as e:
        logger.error(f"Firebase delete error: {e}")
        return False

# ── ДОПОМІЖНІ ФУНКЦІЇ ─────────────────────────────────────────
def today_str():
    return date.today().isoformat()

def fmt_date(d):
    if not d: return '—'
    parts = d.split('-')
    if len(parts) == 3:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return d

def fmt_money(n):
    try:
        return f"{float(n):,.0f} ₴".replace(',', ' ')
    except:
        return f"{n} ₴"

def get_objects():
    return fb_get_all('objects')

def get_workers():
    return fb_get_all('workers')

def main_keyboard():
    return ReplyKeyboardMarkup([
        ['📅 Щоденний звіт', '🧾 Фото чека/накладної'],
        ['📦 Склад', '📊 Звіт по об\'єкту'],
        ['🏗️ Об\'єкти', '📜 Мої записи'],
        ['📆 Планування'],
        ['❓ Допомога'],
    ], resize_keyboard=True)

# ── КОМАНДА /start ────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ У вас немає доступу до цього бота.\nЗверніться до адміністратора.")
        return ConversationHandler.END

    name = update.effective_user.first_name
    if is_owner(uid):
        fb_set('settings', 'admin', {'chat_id': update.effective_chat.id})
    await update.message.reply_text(
        f"👋 Привіт, {name}!\n\n"
        f"🏗️ *Моніторинг виробництва + ІТР*\n\n"
        f"Що можеш робити:\n"
        f"• 📅 Вносити щоденний звіт\n"
        f"• 🧾 Фотографувати чеки та накладні\n"
        f"• 📦 Переглядати та поповнювати склад\n"
        f"• 📊 Дивитись звіти по об'єктах\n"
        f"• 📆 Додавати нагадування та плани\n\n"
        f"Обери дію:",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )
    return MAIN_MENU

# ── ГОЛОВНЕ МЕНЮ ──────────────────────────────────────────────
async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == '📅 Щоденний звіт':
        return await start_daily(update, context)
    elif text == '🧾 Фото чека/накладної':
        return await start_receipt(update, context)
    elif text == '📦 Склад':
        return await show_stock(update, context)
    elif text == '📊 Звіт по об\'єкту':
        return await show_report(update, context)
    elif text == '🏗️ Об\'єкти':
        return await show_objects(update, context)
    elif text == '📜 Мої записи':
        return await show_history(update, context)
    elif text == '📆 Планування':
        return await start_plan(update, context)
    elif text == '❓ Допомога':
        await show_help(update, context)
        return MAIN_MENU
    return MAIN_MENU

# ── КНОПКА НАЗАД ──────────────────────────────────────────────
async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляч для кнопки 'Назад' — повертає на попередній стан"""
    query = update.callback_query
    await query.answer()

    # Перевіряємо, чи є збережений попередній стан
    previous_state = context.user_data.get('previous_state')

    if previous_state == SELECT_DATE:
        return await start_daily(update, context)
    elif previous_state == SELECT_OBJECT:
        # Повертаємось до вибору об'єкту для щоденного звіту
        daily = context.user_data.get('daily', {})
        chosen_date = daily.get('date', today_str())
        context.user_data['previous_state'] = SELECT_DATE
        return await show_object_buttons(update, context, chosen_date, via_callback=True)
    elif previous_state == DAILY_WORKERS:
        context.user_data['previous_state'] = SELECT_OBJECT
        return await ask_workers(update, context)
    elif previous_state == DAILY_MATERIALS:
        context.user_data['previous_state'] = DAILY_WORKERS
        return await ask_materials(update, context)
    elif previous_state == DAILY_TRANSPORT:
        context.user_data['previous_state'] = DAILY_MATERIALS
        return await ask_transport(update, context)
    elif previous_state == DAILY_DESC:
        context.user_data['previous_state'] = DAILY_TRANSPORT
        return await ask_transport(update, context)
    elif previous_state == DAILY_CONFIRM:
        context.user_data['previous_state'] = DAILY_DESC
        await query.edit_message_text("📝 Опишіть що зробили за день:")
        context.user_data['awaiting'] = 'daily_desc'
        return DAILY_DESC
    elif previous_state == RECEIPT_PHOTO:
        return await start_receipt(update, context)
    elif previous_state == RECEIPT_TARGET:
        receipt = context.user_data.get('receipt', {})
        context.user_data['previous_state'] = RECEIPT_PHOTO
        await send_receipt_preview_message(update, receipt)
        return RECEIPT_TARGET
    elif previous_state == STOCK_ACTION:
        return await show_stock(update, context)
    elif previous_state == HISTORY_LIST:
        return await show_history(update, context, via_callback=True)
    elif previous_state == OBJECTS_ACTION:
        return await show_objects(update, context)
    elif previous_state == PLAN_MENU:
        return await start_plan(update, context)
    elif previous_state == PLAN_DATE:
        context.user_data['previous_state'] = PLAN_MENU
        buttons = [
            [InlineKeyboardButton("📅 Сьогодні", callback_data="plandate_today"),
             InlineKeyboardButton("📆 Завтра", callback_data="plandate_tomorrow")],
            [InlineKeyboardButton("✏️ Інша дата", callback_data="plandate_custom")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ]
        await query.edit_message_text(
            "📅 На яку дату додати нагадування?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return PLAN_DATE
    elif previous_state == PLAN_TEXT:
        context.user_data['previous_state'] = PLAN_DATE
        buttons = [
            [InlineKeyboardButton("📅 Сьогодні", callback_data="plandate_today"),
             InlineKeyboardButton("📆 Завтра", callback_data="plandate_tomorrow")],
            [InlineKeyboardButton("✏️ Інша дата", callback_data="plandate_custom")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ]
        await query.edit_message_text(
            "📅 На яку дату додати нагадування?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return PLAN_DATE
    elif previous_state == PLAN_TIME:
        context.user_data['previous_state'] = PLAN_TEXT
        await query.edit_message_text(f"📝 Введіть текст нагадування:")
        context.user_data['awaiting'] = 'plan_text'
        return PLAN_TEXT

    # За замовчуванням повертаємось на головне меню
    context.user_data.clear()
    await query.edit_message_text(
        "👋 Повернулися на головне меню. Обери дію:",
        reply_markup=main_keyboard()
    )
    return MAIN_MENU

# ── ЩОДЕННИЙ ЗВІТ ────────────────────────────────────────────
async def start_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objects = get_objects()
    active = [o for o in objects if o.get('status') == 'active']

    if not active:
        await update.message.reply_text(
            "⚠️ Немає активних об'єктів.\n"
            "Спочатку додайте об'єкт у веб-програмі.",
            reply_markup=main_keyboard()
        )
        return MAIN_MENU

    context.user_data['previous_state'] = MAIN_MENU
    buttons = [
        [InlineKeyboardButton("📅 Сьогодні", callback_data="date_today"),
         InlineKeyboardButton("📆 Вчора", callback_data="date_yesterday")],
        [InlineKeyboardButton("✏️ Інша дата", callback_data="date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]
    await update.message.reply_text(
        "🗓 За яку дату вносимо звіт?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return SELECT_DATE

async def date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'date_today':
        return await show_object_buttons(update, context, today_str(), via_callback=True)
    if data == 'date_yesterday':
        yst = (date.today() - timedelta(days=1)).isoformat()
        return await show_object_buttons(update, context, yst, via_callback=True)
    if data == 'date_custom':
        await query.edit_message_text("✏️ Введіть дату у форматі ДД.ММ.РРРР (наприклад: 28.08.2026):")
        context.user_data['awaiting'] = 'custom_date'
        return SELECT_DATE
    return SELECT_DATE

async def show_object_buttons(update, context, chosen_date, via_callback=False):
    objects = get_objects()
    active = [o for o in objects if o.get('status') == 'active']
    if not active:
        msg = "⚠️ Немає активних об'єктів.\nСпочатку додайте об'єкт у веб-програмі."
        if via_callback and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg, reply_markup=main_keyboard())
        return MAIN_MENU

    context.user_data['daily'] = {
        'date': chosen_date,
        'workers': [], 'materials': [], 'transport': [], 'desc': ''
    }
    context.user_data['previous_state'] = SELECT_DATE

    buttons = [[InlineKeyboardButton(o['name'], callback_data=f"obj_{o['id']}")] for o in active]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
    text = f"📅 *Щоденний звіт*\nДата: {fmt_date(chosen_date)}\n\nОберіть об'єкт:"
    if via_callback and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return SELECT_OBJECT

async def select_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    obj_id = query.data.replace('obj_', '')

    objects = get_objects()
    obj = next((o for o in objects if o['id'] == obj_id), None)
    if not obj:
        await query.edit_message_text("❌ Об'єкт не знайдено")
        return MAIN_MENU

    context.user_data['daily']['objId'] = obj_id
    context.user_data['daily']['objName'] = obj['name']
    context.user_data['objects'] = objects
    context.user_data['previous_state'] = SELECT_OBJECT

    # Show workers selection
    return await ask_workers(update, context)

async def ask_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    workers = get_workers()
    context.user_data['workers_list'] = workers
    context.user_data['previous_state'] = DAILY_WORKERS

    daily = context.user_data['daily']
    added = daily['workers']

    text = f"📅 *{daily['objName']}* — {fmt_date(daily['date'])}\n\n"
    text += "👷 *Хто працював сьогодні?*\n"
    if added:
        text += "\nВже додано:\n"
        for w in added:
            text += f"  • {w['name']} — {w['hrs']} год ({fmt_money(w['hrs']*w['rate'])})\n"

    buttons = []
    for w in workers:
        name = w.get('name', '')
        if not any(a['name'] == name for a in added):
            buttons.append([InlineKeyboardButton(
                f"👷 {name} ({w.get('spec','')})",
                callback_data=f"wrk_{w['id']}"
            )])

    buttons.append([InlineKeyboardButton("➕ Ввести вручну", callback_data="wrk_manual")])
    buttons.append([InlineKeyboardButton("✅ Далі — Матеріали", callback_data="wrk_done")])
    if added:
        buttons.append([InlineKeyboardButton("🔙 Видалити останнього", callback_data="wrk_undo")])

    msg_text = text
    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(msg_text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    return DAILY_WORKERS

async def worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'wrk_done':
        return await ask_materials(update, context)

    if data == 'wrk_undo':
        daily = context.user_data['daily']
        if daily['workers']:
            daily['workers'].pop()
        return await ask_workers(update, context)

    if data == 'wrk_manual':
        await query.edit_message_text(
            "✏️ Введіть ПІБ робітника та години у форматі:\n"
            "`Петренко І.І. 8`\n\nАбо: `Петренко 8 280` (ПІБ години ставка)"
        )
        context.user_data['awaiting'] = 'worker_manual'
        return DAILY_WORKERS

    # Worker from list
    wrk_id = data.replace('wrk_', '')
    workers = context.user_data.get('workers_list', get_workers())
    wrk = next((w for w in workers if w['id'] == wrk_id), None)
    if not wrk:
        return await ask_workers(update, context)

    context.user_data['pending_worker'] = wrk
    context.user_data['awaiting'] = 'worker_hrs'
    shift = wrk.get('shift', 8)
    await query.edit_message_text(
        f"👷 *{wrk['name']}*\n"
        f"Ставка: {fmt_money(wrk.get('rate', 0))}/год\n\n"
        f"Скільки годин відпрацював? (натисніть або введіть):\n",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("4 год", callback_data="hrs_4"),
             InlineKeyboardButton("8 год", callback_data="hrs_8"),
             InlineKeyboardButton("10 год", callback_data="hrs_10")],
            [InlineKeyboardButton("6 год", callback_data="hrs_6"),
             InlineKeyboardButton("12 год", callback_data="hrs_12"),
             InlineKeyboardButton("Ввести", callback_data="hrs_custom")],
        ])
    )
    return DAILY_WORKERS

async def hours_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith('hrs_') and data != 'hrs_custom':
        hrs = float(data.replace('hrs_', ''))
        wrk = context.user_data.get('pending_worker', {})
        context.user_data['daily']['workers'].append({
            'name': wrk.get('name', ''),
            'spec': wrk.get('spec', ''),
            'hrs': hrs,
            'rate': wrk.get('rate', 0)
        })
        context.user_data.pop('pending_worker', None)
        return await ask_workers(update, context)

    if data == 'hrs_custom':
        await query.edit_message_text("Введіть кількість годин (наприклад: 8 або 7.5):")
        context.user_data['awaiting'] = 'worker_hrs_input'
        return DAILY_WORKERS

    return DAILY_WORKERS

async def daily_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input during daily log creation"""
    text = update.message.text.strip()
    awaiting = context.user_data.get('awaiting', '')

    if awaiting == 'custom_date':
        try:
            parts = text.replace('/', '.').replace('-', '.').split('.')
            if len(parts) != 3:
                raise ValueError
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            chosen_date = date(y, m, d).isoformat()
            context.user_data.pop('awaiting', None)
            return await show_object_buttons(update, context, chosen_date, via_callback=False)
        except:
            await update.message.reply_text("❌ Формат дати: ДД.ММ.РРРР (наприклад: 28.08.2026)")
            return SELECT_DATE

    if awaiting == 'worker_hrs_input':
        try:
            hrs = float(text.replace(',', '.'))
            wrk = context.user_data.get('pending_worker', {})
            context.user_data['daily']['workers'].append({
                'name': wrk.get('name', ''),
                'spec': wrk.get('spec', ''),
                'hrs': hrs,
                'rate': wrk.get('rate', 0)
            })
            context.user_data.pop('pending_worker', None)
            context.user_data.pop('awaiting', None)
            return await ask_workers(update, context)
        except:
            await update.message.reply_text("❌ Введіть число, наприклад: 8 або 7.5")
            return DAILY_WORKERS

    if awaiting == 'worker_manual':
            raw_entries = text.replace('\n', ',').split(',')
            entries = [e.strip() for e in raw_entries if e.strip()]
            added = 0
            failed = []
            for entry in entries:
                parts = entry.split()
                nums_idx = [i for i, p in enumerate(parts) if p.replace('.','').isdigit()]
                if not nums_idx:
                    failed.append(entry)
                    continue
                name = ' '.join(p for i, p in enumerate(parts) if i not in nums_idx)
                nums = [float(parts[i]) for i in nums_idx]
                hrs = nums[0] if nums else 8
                rate = nums[1] if len(nums) > 1 else 0
                if name:
                    context.user_data['daily']['workers'].append({'name': name, 'spec': '', 'hrs': hrs, 'rate': rate})
                    added += 1
                else:
                    failed.append(entry)
            context.user_data.pop('awaiting', None)
            if failed:
                await update.message.reply_text(f"⚠️ Не розпізнано: {', '.join(failed)}")
            if added:
                return await ask_workers(update, context)
            else:
                await update.message.reply_text("❌ Формат: Петренко I.I. 8 (або Петренко 8 280). Кілька — через кому або з нового рядка.")
                return DAILY_WORKERS

    if awaiting == 'material_stock_qty':
        try:
            qty = float(text.replace(',', '.'))
            mat = context.user_data.get('pending_material', {})
            available = mat.get('available', 0)
            new_qty = available - qty
            context.user_data['daily']['materials'].append({
                'name': mat.get('name',''),
                'qty': qty,
                'unit': mat.get('unit','шт'),
                'price': mat.get('price', 0),
                'sku': ''
            })
            stock_data = dict(mat.get('stock_data', {}))
            if mat.get('stock_id') and stock_data:
                stock_data['qty'] = new_qty
                fb_set('matStock', mat['stock_id'], stock_data)
                daily = context.user_data.get('daily', {})
                fb_add('stockMoves', {
                    'matId': mat['stock_id'],
                    'type': 'out',
                    'qty': qty,
                    'price': mat.get('price', 0),
                    'date': daily.get('date') or today_str(),
                    'note': f"Бот · щоденний звіт · {daily.get('objName','')}".strip(' ·')
                })
            context.user_data.pop('pending_material', None)
            context.user_data.pop('awaiting', None)
            warn = "\n⚠️ Залишок став від'ємним!" if new_qty < 0 else ""
            await update.message.reply_text(f"✅ Списано {qty} {mat.get('unit','шт')}. Залишок: {new_qty}{warn}")
            return await ask_materials(update, context)
        except:
            await update.message.reply_text("❌ Введіть число, наприклад: 10")
            return DAILY_MATERIALS

    if awaiting == 'material_name':

        context.user_data['pending_material'] = {'name': text}
        await update.message.reply_text(
            f"📦 *{text}*\n\nВведіть кількість та одиницю (наприклад: 50 м або 10 шт):",
            parse_mode='Markdown'
        )
        context.user_data['awaiting'] = 'material_qty'
        return DAILY_MATERIALS

    if awaiting == 'material_qty':
        parts = text.split()
        try:
            qty = float(parts[0].replace(',', '.'))
            unit = parts[1] if len(parts) > 1 else 'шт'
            mat = context.user_data.get('pending_material', {})
            mat['qty'] = qty
            mat['unit'] = unit
            context.user_data['pending_material'] = mat
            await update.message.reply_text(
                f"💰 Ціна за {unit} (введіть 0 якщо невідома):"
            )
            context.user_data['awaiting'] = 'material_price'
            return DAILY_MATERIALS
        except:
            await update.message.reply_text("❌ Формат: 50 м або 10")
            return DAILY_MATERIALS

    if awaiting == 'material_price':
        try:
            price = float(text.replace(',', '.').replace(' ', ''))
            mat = context.user_data.get('pending_material', {})
            mat['price'] = price
            mat['sku'] = ''
            context.user_data['daily']['materials'].append(mat)
            context.user_data.pop('pending_material', None)
            context.user_data.pop('awaiting', None)
            return await ask_materials(update, context)
        except:
            await update.message.reply_text("❌ Введіть число (ціну)")
            return DAILY_MATERIALS

    if awaiting == 'transport_name':
        context.user_data['pending_transport'] = {'name': text}
        await update.message.reply_text("🛣 Введіть пробіг (км) або мотогодини (наприклад: 120):")
        context.user_data['awaiting'] = 'transport_qty'
        return DAILY_TRANSPORT

    if awaiting == 'transport_qty':
        try:
            qty = float(text.replace(',', '.'))
            tr = context.user_data.get('pending_transport', {})
            tr['qty'] = qty
            context.user_data['pending_transport'] = tr
            await update.message.reply_text("💰 Ціна за одиницю (₴/км або ₴/год), введіть 0 якщо невідома:")
            context.user_data['awaiting'] = 'transport_price'
            return DAILY_TRANSPORT
        except:
            await update.message.reply_text("❌ Введіть число, наприклад: 120")
            return DAILY_TRANSPORT

    if awaiting == 'transport_price':
        try:
            price = float(text.replace(',', '.').replace(' ', ''))
            tr = context.user_data.get('pending_transport', {})
            tr['price'] = price
            tr['cost'] = tr.get('qty', 0) * price
            context.user_data['daily']['transport'].append(tr)
            context.user_data.pop('pending_transport', None)
            context.user_data.pop('awaiting', None)
            return await ask_transport(update, context)
        except:
            await update.message.reply_text("❌ Введіть число (ціну)")
            return DAILY_TRANSPORT

    if awaiting == 'daily_desc':
        context.user_data['daily']['desc'] = text
        context.user_data.pop('awaiting', None)
        return await confirm_daily(update, context)

    # Fallback for unexpected text
    phase = context.user_data.get('daily_phase', 'workers')
    await update.message.reply_text("⚠️ Скористайтесь кнопками вище, або натисніть відповідну кнопку зі списку.")
    if phase == 'materials':
        return DAILY_MATERIALS
    if phase == 'transport':
        return DAILY_TRANSPORT
    return DAILY_WORKERS

async def ask_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ask_materials_msg(update, context, via_callback=True)

async def ask_materials_msg(update, context, via_callback=False):
    context.user_data['daily_phase'] = 'materials'
    context.user_data['previous_state'] = DAILY_MATERIALS
    daily = context.user_data['daily']
    added = daily['materials']

    text = f"📦 *Витрачені матеріали*\n"
    if added:
        text += "\nВже додано:\n"
        for m in added:
            text += f"  • {m['name']} — {m['qty']} {m['unit']}"
            if m.get('price'):
                text += f" × {fmt_money(m['price'])}"
            text += "\n"

    buttons = [
        [InlineKeyboardButton("➕ Додати матеріал", callback_data="mat_add")],
        [InlineKeyboardButton("✅ Далі — Транспорт", callback_data="mat_done")],
        [InlineKeyboardButton("⏭ Пропустити транспорт", callback_data="mat_skip_tr")],
    ]
    if added:
        buttons.append([InlineKeyboardButton("🔙 Видалити останній", callback_data="mat_undo")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])

    msg = text
    if via_callback and update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await (update.message or update.callback_query.message).reply_text(
            msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return DAILY_MATERIALS

async def materials_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'mat_done':
        return await ask_transport(update, context)
    if data == 'mat_skip_tr':
        return await confirm_daily(update, context)
    if data == 'mat_undo':
        if context.user_data['daily']['materials']:
            context.user_data['daily']['materials'].pop()
        return await ask_materials_msg(update, context, via_callback=True)
    if data == 'mat_add':
        stocks = fb_get_all('matStock')
        buttons = []
        for s in stocks:
            buttons.append([InlineKeyboardButton(
                f"📦 {s.get('name','')} ({s.get('qty',0)} {s.get('unit','шт')})",
                callback_data=f"matsel_{s['id']}"
            )])
        buttons.append([InlineKeyboardButton("✏️ Немає в списку — вручну", callback_data="mat_manual")])
        await query.edit_message_text("📦 Оберіть матеріал зі складу:", reply_markup=InlineKeyboardMarkup(buttons))
        return DAILY_MATERIALS
    if data == 'mat_manual':
        await query.edit_message_text("📦 Введіть назву матеріалу:")
        context.user_data['awaiting'] = 'material_name'
        return DAILY_MATERIALS
    if data.startswith('matsel_'):
        stock_id = data.replace('matsel_', '')
        stocks = fb_get_all('matStock')
        stock = next((s for s in stocks if s['id'] == stock_id), None)
        if not stock:
            return await ask_materials_msg(update, context, via_callback=True)
        context.user_data['pending_material'] = {
            'name': stock.get('name',''),
            'unit': stock.get('unit','шт'),
            'price': stock.get('price', 0),
            'stock_id': stock_id,
            'available': stock.get('qty', 0),
            'stock_data': stock
        }
        await query.edit_message_text(
            f"📦 *{stock.get('name','')}*\nНаявно: {stock.get('qty',0)} {stock.get('unit','шт')}\n\nВведіть кількість, яку використали:",
            parse_mode='Markdown'
        )
        context.user_data['awaiting'] = 'material_stock_qty'
        return DAILY_MATERIALS
    return DAILY_MATERIALS

async def ask_transport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ask_transport_msg(update, context, via_callback=True)

async def ask_transport_msg(update, context, via_callback=False):
    context.user_data['daily_phase'] = 'transport'
    context.user_data['previous_state'] = DAILY_TRANSPORT
    daily = context.user_data['daily']
    added = daily['transport']

    text = "🚛 *Транспорт*\n"
    if added:
        text += "\nДодано:\n"
        for t in added:
            text += f"  • {t['name']} — {fmt_money(t['cost'])}\n"

    buttons = [
        [InlineKeyboardButton("➕ Додати транспорт", callback_data="tr_add")],
        [InlineKeyboardButton("✅ Далі — Опис", callback_data="tr_done")],
        [InlineKeyboardButton("⏭ Без опису — Зберегти", callback_data="tr_save")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]

    msg = text
    if via_callback and update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await (update.message or update.callback_query.message).reply_text(
            msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return DAILY_TRANSPORT

async def transport_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'tr_done':
        await query.edit_message_text("📝 Коротко опишіть що зробили за день (або натисніть /skip):")
        context.user_data['awaiting'] = 'daily_desc'
        return DAILY_DESC
    if data == 'tr_save':
        return await confirm_daily(update, context)
    if data == 'tr_add':
        await query.edit_message_text("🚛 Введіть назву транспорту (наприклад: Газель):")
        context.user_data['awaiting'] = 'transport_name'
        return DAILY_TRANSPORT

async def confirm_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    daily = context.user_data['daily']
    context.user_data['previous_state'] = DAILY_CONFIRM

    total_hrs = sum(w['hrs'] for w in daily['workers'])
    total_wage = sum(w['hrs'] * w.get('rate', 0) for w in daily['workers'])
    total_mat = sum(m['qty'] * m.get('price', 0) for m in daily['materials'])
    total_tr = sum(t.get('cost', 0) for t in daily['transport'])
    total_all = total_wage + total_mat + total_tr

    text = f"📋 *Підтвердіть запис*\n"
    text += f"📅 {fmt_date(daily['date'])} | {daily.get('objName', '—')}\n\n"

    if daily['workers']:
        text += "👷 *Робітники:*\n"
        for w in daily['workers']:
            text += f"  • {w['name']} — {w['hrs']}год"
            if w.get('rate'):
                text += f" = {fmt_money(w['hrs']*w['rate'])}"
            text += "\n"

    if daily['materials']:
        text += "\n📦 *Матеріали:*\n"
        for m in daily['materials']:
            text += f"  • {m['name']} {m['qty']} {m['unit']}"
            if m.get('price'):
                text += f" = {fmt_money(m['qty']*m['price'])}"
            text += "\n"

    if daily['transport']:
        text += "\n🚛 *Транспорт:*\n"
        for t in daily['transport']:
            if t.get('qty'):
                text += f"  • {t['name']} — {t['qty']} x {fmt_money(t.get('price',0))} = {fmt_money(t.get('cost',0))}\n"
            else:
                text += f"  • {t['name']} — {fmt_money(t.get('cost',0))}\n"

    if daily.get('desc'):
        text += f"\n📝 {daily['desc']}\n"

    text += f"\n💰 *РАЗОМ: {fmt_money(total_all)}*"
    text += f"\n  👷 {fmt_money(total_wage)} + 📦 {fmt_money(total_mat)} + 🚛 {fmt_money(total_tr)}"

    buttons = [
        [InlineKeyboardButton("✅ Зберегти", callback_data="daily_save"),
         InlineKeyboardButton("❌ Скасувати", callback_data="daily_cancel")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    return DAILY_CONFIRM

async def daily_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'daily_cancel':
        await query.edit_message_text("❌ Скасовано")
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if query.data == 'daily_save':
        daily = context.user_data['daily']
        daily['submittedBy'] = update.effective_user.first_name or update.effective_user.username or 'Невідомо'
        daily['submittedById'] = update.effective_user.id
        doc_id = fb_add('dailyLogs', daily)

        if doc_id:
            await query.edit_message_text(
                f"✅ *Збережено!*\n\n"
                f"📅 {fmt_date(daily['date'])} — {daily.get('objName', '')}\n"
                f"Запис у Firebase ✅",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Помилка збереження. Перевірте Firebase.")

        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    return DAILY_CONFIRM

# ── ФОТО ЧЕКА / НАКЛАДНОЇ ────────────────────────────────────
async def start_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['previous_state'] = MAIN_MENU
    await update.message.reply_text(
        "🧾 *Фото чека або накладної*\n\n"
        "Надішліть фото документу — бот розпізнає матеріали, суму і номер документа (чека/накладної/рахунка) автоматично. "
        "Якщо номер не розпізнався або його треба виправити — на наступному кроці буде кнопка \"🔖 Додати/Змінити № документа\".\n\n"
        "Або введіть вручну у форматі:\n"
        "`Назва матеріалу кількість одиниця ціна`\n"
        "Наприклад: `Кабель ВВГ 50 м 38`",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup([['🔙 Назад']], resize_keyboard=True)
    )
    return RECEIPT_PHOTO

async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo of receipt"""
    if update.message.text == '🔙 Назад':
        await update.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if update.message.photo:
        await update.message.reply_text("🔍 Розпізнаю чек...")

        if CLAUDE_AVAILABLE and CLAUDE_KEY:
            try:
                # Get photo
                photo = update.message.photo[-1]
                file = await photo.get_file()
                bio = BytesIO()
                await file.download_to_memory(bio)
                img_bytes = bio.getvalue()
                img_b64 = base64.standard_b64encode(img_bytes).decode()

                # Ask Claude to parse receipt
                genai.configure(api_key=CLAUDE_KEY)
                model = genai.GenerativeModel("gemini-3.6-flash")
                response = model.generate_content([
                    """Розпізнай цей чек або накладну. Поверни JSON у форматі:
{
  "supplier": "назва магазину/постачальника",
  "date": "дата YYYY-MM-DD або null",
  "docNumber": "номер документу (номер чека, номер або серія накладної, номер рахунку/фактури) або null, якщо не вказано",
  "total": 1234.50,
  "items": [
    {"name": "назва товару", "qty": 2, "unit": "шт", "price": 150.0, "amount": 300.0}
  ]
}
Якщо не можеш розпізнати – поверни {"error": "причина"}
Відповідай ТІЛЬКИ JSON без пояснень.""",
                    {"mime_type": "image/jpeg", "data": img_bytes}
                ])

                result_text = response.text.strip()
                # Clean JSON
                if '```' in result_text:
                    result_text = result_text.split('```')[1]
                    if result_text.startswith('json'):
                        result_text = result_text[4:]

                parsed = json.loads(result_text)

                if 'error' in parsed:
                    await update.message.reply_text(
                        f"⚠️ Не вдалось розпізнати: {parsed['error']}\n\nВведіть вручну:"
                    )
                    return RECEIPT_PHOTO

                context.user_data['receipt'] = parsed
                return await show_receipt_preview(update, context)

            except Exception as e:
                logger.error(f"Claude receipt error: {e}")
                await update.message.reply_text(
                    "⚠️ Помилка розпізнавання. Введіть вручну:\n"
                    "`Назва к-сть одиниця ціна`",
                    parse_mode='Markdown'
                )
                return RECEIPT_PHOTO
        else:
            await update.message.reply_text(
                "⚠️ Розпізнавання недоступне (немає API ключа Claude).\n"
                "Введіть вручну:\n`Назва к-сть одиниця ціна`",
                parse_mode='Markdown'
            )
            return RECEIPT_PHOTO

    elif update.message.text:
        # Manual input
        text = update.message.text.strip()
        parts = text.split()
        try:
            nums = [i for i, p in enumerate(parts) if p.replace(',','').replace('.','').isdigit()]
            if len(nums) >= 1:
                # Last numbers are qty and price
                price_idx = nums[-1]
                qty_idx = nums[-2] if len(nums) >= 2 else nums[-1]
                price = float(parts[price_idx].replace(',','.'))
                qty = float(parts[qty_idx].replace(',','.')) if len(nums) >= 2 else 1
                unit = parts[qty_idx+1] if qty_idx+1 < price_idx else 'шт'
                name = ' '.join(parts[:qty_idx])

                context.user_data['receipt'] = {
                    'supplier': '',
                    'items': [{'name': name, 'qty': qty, 'unit': unit, 'price': price, 'amount': qty*price}],
                    'total': qty * price
                }
                return await show_receipt_preview(update, context)
        except Exception as e:
            pass

        await update.message.reply_text(
            "❌ Не зрозумів формат.\nСпробуйте: `Кабель ВВГ 50 м 38`",
            parse_mode='Markdown'
        )
        return RECEIPT_PHOTO

    return RECEIPT_PHOTO

def doc_number_suffix(receipt):
    """' · № 12345' if the receipt/invoice number was recognized or entered, else ''"""
    num = (receipt.get('docNumber') or '').strip()
    return f" · № {num}" if num else ""

def _patch_markdown_safety_net():
    """Any message sent with parse_mode='Markdown' can contain arbitrary user/OCR-derived
    text (material names, supplier names, notes...) with an unpaired *, _, ` or [ that
    Telegram's legacy Markdown parser can't handle — it then rejects the WHOLE message with
    BadRequest ("Can't parse entities"), and that one action looks like the bot "doesn't
    respond". md_safe() covers the couple of spots it's applied to, but this file sends
    Markdown from 30+ places and it's easy to miss one — confirmed 23.09.2026, when the exact
    same class of bug hit "📦 Склад" (show_stock), a spot md_safe() was never added to.
    Instead of chasing every call site by hand, patch Telegram's send/edit methods ONCE:
    on this specific error, retry the same call as plain text instead of failing outright.
    This is a safety net, not a replacement for md_safe() — plain text still isn't as nice
    as formatted text, but it always gets delivered."""
    orig_send_message = ExtBot.send_message
    orig_edit_message_text = ExtBot.edit_message_text

    async def safe_send_message(self, *args, **kwargs):
        try:
            return await orig_send_message(self, *args, **kwargs)
        except BadRequest as e:
            if kwargs.get('parse_mode') and "Can't parse entities" in str(e):
                logger.error(f"Markdown parse error on send_message, retrying as plain text: {e}")
                kwargs['parse_mode'] = None
                return await orig_send_message(self, *args, **kwargs)
            raise

    async def safe_edit_message_text(self, *args, **kwargs):
        try:
            return await orig_edit_message_text(self, *args, **kwargs)
        except BadRequest as e:
            if kwargs.get('parse_mode') and "Can't parse entities" in str(e):
                logger.error(f"Markdown parse error on edit_message_text, retrying as plain text: {e}")
                kwargs['parse_mode'] = None
                return await orig_edit_message_text(self, *args, **kwargs)
            raise

    ExtBot.send_message = safe_send_message
    ExtBot.edit_message_text = safe_edit_message_text

def md_safe(text):
    """Strip characters that break Telegram's legacy Markdown parser (*_`[) when interpolating
    arbitrary OCR-recognized text (item names, supplier, doc numbers) into a Markdown message.
    Without this, a product name like 'D65*45' crashes the whole preview with
    'Can't parse entities' and the bot looks unresponsive."""
    if not text:
        return text
    return re.sub(r'[*_`\[\]]', '', str(text))

def ensure_supplier(name):
    """Make sure a supplier with this name exists in the 'suppliers' collection (used by the
    web app's 'Постачальники' page), so a name recognized from a receipt shows up there too —
    not just as free text on the stock-move/purchase/daily-log record. Case-insensitive match
    by name; creates a minimal record if none exists yet. Returns the supplier id, or '' if
    the name is blank or the write failed."""
    name = (name or '').strip()
    if not name:
        return ''
    try:
        existing = fb_get_all('suppliers')
        for s in existing:
            if (s.get('name') or '').strip().lower() == name.lower():
                return s.get('id', '')
        return fb_add('suppliers', {
            'name': name, 'contact': '', 'phone': '',
            'note': 'Додано автоматично з чека (бот)'
        }) or ''
    except Exception as e:
        logger.error(f"ensure_supplier error: {e}")
        return ''

def build_receipt_preview_text(receipt):
    items = receipt.get('items', [])

    text = "🧾 *Розпізнано:*\n\n"
    if receipt.get('supplier'):
        text += f"🏪 {md_safe(receipt['supplier'])}\n"
    if receipt.get('date'):
        text += f"📅 {fmt_date(receipt['date'])}\n"
    if receipt.get('docNumber'):
        text += f"🔖 № {md_safe(receipt['docNumber'])}\n"
    text += "\n"

    for item in items:
        text += f"• {md_safe(item['name'])} — {item['qty']} {item.get('unit','шт')} × {fmt_money(item.get('price',0))} = {fmt_money(item.get('amount',0))}\n"

    if receipt.get('total'):
        text += f"\n💰 *Разом: {fmt_money(receipt['total'])}*"

    text += "\n\nКуди записати?"
    return text

def receipt_preview_buttons(receipt):
    docnum_label = "🔖 Змінити № документа" if receipt.get('docNumber') else "🔖 Додати № документа"
    return [
        [InlineKeyboardButton("📦 На склад", callback_data="rec_stock"),
         InlineKeyboardButton("🏗️ На об'єкт", callback_data="rec_object")],
        [InlineKeyboardButton("🧾 В Закупки", callback_data="rec_purchase")],
        [InlineKeyboardButton(docnum_label, callback_data="rec_docnum")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]

async def send_receipt_preview_message(update, receipt):
    """Send the receipt preview, falling back to plain text if Markdown parsing still fails
    for some unforeseen character in OCR'd text — better a plain message than a silent crash."""
    text = build_receipt_preview_text(receipt)
    markup = InlineKeyboardMarkup(receipt_preview_buttons(receipt))
    try:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logger.error(f"Receipt preview Markdown error, resending as plain text: {e}")
        await update.message.reply_text(text, reply_markup=markup)

async def show_receipt_preview(update, context):
    receipt = context.user_data.get('receipt', {})
    context.user_data['previous_state'] = RECEIPT_PHOTO
    await send_receipt_preview_message(update, receipt)
    return RECEIPT_TARGET

async def receipt_docnum_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User typed the invoice/receipt/check number after tapping '🔖 Додати № документа'"""
    text = (update.message.text or '').strip()
    receipt = context.user_data.get('receipt', {})
    receipt['docNumber'] = text
    context.user_data['receipt'] = receipt
    await send_receipt_preview_message(update, receipt)
    return RECEIPT_TARGET

async def receipt_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'rec_cancel':
        await query.edit_message_text("❌ Скасовано")
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if data == 'rec_docnum':
        await query.edit_message_text("🔖 Введіть номер накладної / рахунку / чека (текстом):")
        return RECEIPT_CONFIRM

    if data == 'rec_stock':
        buttons = [
            [InlineKeyboardButton("📦 Матеріали", callback_data="recstock_materials"),
             InlineKeyboardButton("🛠️ Інструмент/спецодяг", callback_data="recstock_tool")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ]
        await query.edit_message_text("Це звичайні матеріали чи інструмент/спецодяг?", reply_markup=InlineKeyboardMarkup(buttons))
        return RECEIPT_TARGET

    if data.startswith('recstock_'):
        cat = 'tool' if data == 'recstock_tool' else ''
        receipt = context.user_data.get('receipt', {})
        supplier_id = ensure_supplier(receipt.get('supplier', ''))
        saved = 0
        failed = []
        for item in receipt.get('items', []):
            existing = None
            stocks = fb_get_all('matStock')
            for s in stocks:
                if s.get('name', '').lower() == item['name'].lower():
                    existing = s
                    break

            if existing:
                existing['qty'] = existing.get('qty', 0) + item.get('qty', 0)
                if supplier_id and not existing.get('supplierId'):
                    existing['supplierId'] = supplier_id
                if cat:
                    existing['cat'] = cat
                ok = fb_set('matStock', existing['id'], existing)
                mat_id = existing['id'] if ok else None
            else:
                mat_id = fb_add('matStock', {
                    'name': item['name'],
                    'qty': item.get('qty', 0),
                    'unit': item.get('unit', 'шт'),
                    'price': item.get('price', 0),
                    'supplier': receipt.get('supplier', ''),
                    'supplierId': supplier_id,
                    'min': 0, 'cat': cat, 'sku': '', 'note': ''
                })
            if mat_id:
                fb_add('stockMoves', {
                    'matId': mat_id,
                    'type': 'in',
                    'qty': item.get('qty', 0),
                    'price': item.get('price', 0),
                    'date': receipt.get('date') or today_str(),
                    'note': f"Бот · чек{(' · ' + receipt.get('supplier')) if receipt.get('supplier') else ''}{doc_number_suffix(receipt)}"
                })
                saved += 1
            else:
                failed.append(item.get('name', '?'))

        text = f"✅ Додано на склад ({'🛠️ інструмент/спецодяг' if cat else '📦 матеріали'}): {saved} позицій"
        if failed:
            text += f"\n❌ Не вдалося зберегти ({len(failed)}): {', '.join(md_safe(n) for n in failed)}\nСпробуйте ще раз через кілька хвилин або перевірте зв'язок з Firebase."
        await query.edit_message_text(text)
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if data == 'rec_purchase':
        buttons = [
            [InlineKeyboardButton("📦 Матеріали", callback_data="reccat_materials"),
             InlineKeyboardButton("🚚 Доставка", callback_data="reccat_delivery")],
            [InlineKeyboardButton("🚗 Транспорт", callback_data="reccat_transport"),
             InlineKeyboardButton("📎 Інше", callback_data="reccat_other")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ]
        await query.edit_message_text("Оберіть категорію закупівлі:", reply_markup=InlineKeyboardMarkup(buttons))
        return RECEIPT_TARGET

    if data == 'reccat_materials':
        buttons = [
            [InlineKeyboardButton("📦 Матеріали", callback_data="recpurmat_materials"),
             InlineKeyboardButton("🛠️ Інструмент/спецодяг", callback_data="recpurmat_tool")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ]
        await query.edit_message_text("Це звичайні матеріали чи інструмент/спецодяг?", reply_markup=InlineKeyboardMarkup(buttons))
        return RECEIPT_TARGET

    if data.startswith('reccat_') or data.startswith('recpurmat_'):
        if data.startswith('recpurmat_'):
            category = 'materials'
            cat = 'tool' if data == 'recpurmat_tool' else ''
        else:
            category = data.replace('reccat_', '')
            cat = ''
        cat_labels = {'materials': '📦 Матеріали', 'delivery': '🚚 Доставка', 'transport': '🚗 Транспорт', 'other': '📎 Інше'}
        receipt = context.user_data.get('receipt', {})
        supplier_id = ensure_supplier(receipt.get('supplier', ''))
        saved = 0
        failed = []
        stock_note = 0
        for item in receipt.get('items', []):
            purchase = {
                'name': item['name'],
                'category': category,
                'qty': item.get('qty', 0),
                'unit': item.get('unit', 'шт'),
                'price': item.get('price', 0),
                'amount': item.get('amount', 0),
                'supplier': receipt.get('supplier', ''),
                'supplierId': supplier_id,
                'date': receipt.get('date') or today_str(),
                'note': f"Бот · {cat_labels.get(category, category)}{doc_number_suffix(receipt)}"
            }
            purch_id = fb_add('purchases', purchase)
            if purch_id:
                saved += 1
                if category == 'materials':
                    stock_note += 1
            else:
                failed.append(item.get('name', '?'))

        text = f"✅ Додано в Закупки ({cat_labels.get(category, category)}): {saved} позицій"
        if stock_note:
            text += f"\n💡 {stock_note} з них — матеріалів. Не забудьте їх додати в Склад!"
        if failed:
            text += f"\n❌ Не вдалося зберегти ({len(failed)}): {', '.join(md_safe(n) for n in failed)}"
        await query.edit_message_text(text)
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if data == 'rec_object':
        objects = get_objects()
        buttons = [[InlineKeyboardButton(o['name'], callback_data=f"recobj_{o['id']}")] for o in objects[:10]]
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
        await query.edit_message_text("🏗️ Оберіть об'єкт:", reply_markup=InlineKeyboardMarkup(buttons))
        return RECEIPT_TARGET

    if data.startswith('recobj_'):
        obj_id = data.replace('recobj_', '')
        objects = context.user_data.get('objects') or get_objects()
        obj = next((o for o in objects if o['id'] == obj_id), None)
        if not obj:
            return RECEIPT_TARGET

        receipt = context.user_data.get('receipt', {})
        saved = 0
        failed = []
        for item in receipt.get('items', []):
            log = {
                'date': receipt.get('date') or today_str(),
                'objId': obj_id,
                'objName': obj['name'],
                'workers': [],
                'materials': [{
                    'name': item['name'],
                    'qty': item.get('qty', 0),
                    'unit': item.get('unit', 'шт'),
                    'price': item.get('price', 0),
                    'sku': ''
                }],
                'transport': [],
                'desc': f"Бот · з чека{doc_number_suffix(receipt)}",
                'submittedBy': update.effective_user.first_name or 'Бот',
                'submittedById': update.effective_user.id
            }
            log_id = fb_add('dailyLogs', log)
            if log_id:
                saved += 1
            else:
                failed.append(item.get('name', '?'))

        text = f"✅ Додано до щоденних звітів: {saved} записів"
        if failed:
            text += f"\n❌ Не вдалось ({len(failed)}): {', '.join(md_safe(n) for n in failed)}"
        await query.edit_message_text(text)
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    return RECEIPT_TARGET

# ── СКЛАД ────────────────────────────────────────────────────
async def show_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['previous_state'] = MAIN_MENU
    buttons = [[InlineKeyboardButton("🔍 Пошук на складі", callback_data="stock_search")],
               [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]]
    await update.message.reply_text(
        "📦 *Склад матеріалів*\n\nЩо робити?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return STOCK_ACTION

async def stock_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'stock_search':
        stocks = fb_get_all('matStock')
        if not stocks:
            await query.edit_message_text("📦 Склад порожній")
            return STOCK_ACTION

        text = "📦 *Склад:*\n\n"
        for s in stocks:
            name = s.get('name', '—')
            qty = s.get('qty', 0)
            unit = s.get('unit', 'шт')
            price = s.get('price', 0)
            text += f"• {name} — {qty} {unit}"
            if price:
                text += f" × {fmt_money(price)}"
            text += "\n"

        await query.edit_message_text(text, parse_mode='Markdown')
        return STOCK_ACTION

    return STOCK_ACTION

# ── ЗВІТ ПО ОБ'ЄКТУ ───────────────────────────────────────────
async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['previous_state'] = MAIN_MENU
    objects = get_objects()
    buttons = [[InlineKeyboardButton(o['name'], callback_data=f"rep_{o['id']}")] for o in objects[:10]]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
    await update.message.reply_text(
        "📊 *Звіт по об'єкту*\n\nОберіть об'єкт:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return HISTORY_LIST

async def history_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith('rep_'):
        obj_id = data.replace('rep_', '')
        logs = fb_get_all('dailyLogs')
        obj_logs = [l for l in logs if l.get('objId') == obj_id]

        if not obj_logs:
            await query.edit_message_text("❌ Записів немає")
            return HISTORY_LIST

        text = f"📊 Звіт по об'єкту\n\n"
        for log in obj_logs[:10]:
            text += f"📅 {fmt_date(log.get('date'))} — {log.get('submittedBy')}\n"

        await query.edit_message_text(text)
        return HISTORY_LIST

    if data.startswith('hist_'):
        log_id = data.replace('hist_', '')
        logs = fb_get_all('dailyLogs')
        log = next((l for l in logs if l['id'] == log_id), None)

        if log:
            text = f"📋 {fmt_date(log.get('date'))} — {log.get('objName')}\n\n"
            text += f"Введено: {log.get('submittedBy')}\n"
            await query.edit_message_text(text)

        return HISTORY_LIST

    if query.data == 'hist_back':
        return await show_history(update, context, via_callback=True)

    return HISTORY_LIST

# ── ОБ'ЄКТИ ───────────────────────────────────────────────────
async def show_objects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['previous_state'] = MAIN_MENU
    objects = get_objects()
    buttons = [[InlineKeyboardButton(f"🏗️ {o['name']}", callback_data=f"obj_detail_{o['id']}")] for o in objects]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
    await update.message.reply_text(
        "🏗️ *Об'єкти*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return OBJECTS_ACTION

async def objects_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'back_to_menu':
        return await back_to_menu(update, context)

    return OBJECTS_ACTION

# ── ІСТОРІЯ ────────────────────────────────────────────────────
async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE, via_callback=False):
    context.user_data['previous_state'] = MAIN_MENU
    logs = fb_get_all('dailyLogs')

    if not logs:
        msg = "📜 Записів немає"
        if via_callback:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg, reply_markup=main_keyboard())
        return MAIN_MENU

    buttons = [[InlineKeyboardButton(f"📅 {fmt_date(l.get('date'))} — {l.get('objName')}", callback_data=f"hist_{l['id']}")] for l in logs[:10]]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])

    msg = "📜 *Мої записи:*"
    if via_callback:
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return HISTORY_LIST

# ── ПЛАНУВАННЯ ────────────────────────────────────────────────
async def start_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['previous_state'] = MAIN_MENU
    buttons = [
        [InlineKeyboardButton("➕ Додати нагадування", callback_data="plan_add")],
        [InlineKeyboardButton("📋 Найближчі", callback_data="plan_list")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]
    await update.message.reply_text(
        "📆 *Планування*\n\nЩо робити?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return PLAN_MENU

async def plan_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'plan_add':
        context.user_data['previous_state'] = PLAN_MENU
        buttons = [
            [InlineKeyboardButton("📅 Сьогодні", callback_data="plandate_today"),
             InlineKeyboardButton("📆 Завтра", callback_data="plandate_tomorrow")],
            [InlineKeyboardButton("✏️ Інша дата", callback_data="plandate_custom")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ]
        await query.edit_message_text(
            "📅 На яку дату додати нагадування?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return PLAN_DATE

    if data == 'plan_list':
        plans = fb_get_all('plans')
        if not plans:
            await query.edit_message_text("📋 Нагадувань немає")
        else:
            text = "📋 *Найближчі нагадування:*\n\n"
            for p in plans[:10]:
                text += f"📅 {fmt_date(p.get('date'))} — {p.get('text')}\n"
            await query.edit_message_text(text, parse_mode='Markdown')
        return PLAN_MENU

    return PLAN_MENU

async def plan_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle both CallbackQuery and Message
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data
    else:
        # Text input for custom date
        text = update.message.text.strip()
        try:
            parts = text.replace('/', '.').replace('-', '.').split('.')
            if len(parts) != 3:
                raise ValueError
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            chosen_date = date(y, m, d).isoformat()
            context.user_data['plan_date'] = chosen_date
            context.user_data['previous_state'] = PLAN_DATE
            await update.message.reply_text(f"📝 Введіть текст нагадування:")
            context.user_data['awaiting'] = 'plan_text'
            return PLAN_TEXT
        except:
            await update.message.reply_text("❌ Формат дати: ДД.ММ.РРРР (наприклад: 28.08.2026)")
            return PLAN_DATE

    if data == 'plandate_today':
        chosen_date = today_str()
    elif data == 'plandate_tomorrow':
        chosen_date = (date.today() + timedelta(days=1)).isoformat()
    elif data == 'plandate_custom':
        context.user_data['previous_state'] = PLAN_DATE
        await query.edit_message_text("✏️ Введіть дату (ДД.ММ.РРРР):")
        context.user_data['awaiting'] = 'plan_date'
        return PLAN_DATE
    else:
        return PLAN_DATE

    context.user_data['plan_date'] = chosen_date
    context.user_data['previous_state'] = PLAN_DATE
    await query.edit_message_text(f"📝 Введіть текст нагадування:")
    context.user_data['awaiting'] = 'plan_text'
    return PLAN_TEXT

async def plan_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle text input for plan text
    text = update.message.text.strip()
    context.user_data['plan_text'] = text
    context.user_data['previous_state'] = PLAN_TEXT

    buttons = [
        [InlineKeyboardButton("⏭ Без часу", callback_data="plantime_skip")],
    ]

    await update.message.reply_text(
        "⏰ Час нагадування? (можна пропустити)",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    context.user_data['awaiting'] = 'plan_time'
    return PLAN_TIME

async def plan_time_final_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'plantime_skip':
        plan = {
            'date': context.user_data.get('plan_date'),
            'text': context.user_data.get('plan_text'),
            'time': None,
            'done': False
        }
        fb_add('plans', plan)
        await query.edit_message_text("✅ Нагадування додано")
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    return PLAN_TIME

# ── ДОПОМОГА ──────────────────────────────────────────────────
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 *Моніторинг виробництва — Бот*

📅 *Щоденний звіт* — вносьте робітників, матеріали та транспорт
🧾 *Фото чека* — розпізнавання матеріалів з фотографій
📦 *Склад* — перегляд матеріалів
📊 *Звіт* — звіти по об'єктам
📆 *Планування* — нагадування
❓ *Допомога* — ця інформація

⬅️ *Назад* — повернутись на головне меню з будь-якого місця
"""
    await update.message.reply_text(text, parse_mode='Markdown')

# ── ОСНОВНИЙ ЦИКЛ ─────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    init_firebase()
    _patch_markdown_safety_net()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)
        ],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT, main_menu_handler)],
            SELECT_DATE: [
                CallbackQueryHandler(date_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler)
            ],
            SELECT_OBJECT: [CallbackQueryHandler(select_object)],
            DAILY_WORKERS: [
                CallbackQueryHandler(worker_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler)
            ],
            DAILY_MATERIALS: [
                CallbackQueryHandler(materials_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler)
            ],
            DAILY_TRANSPORT: [
                CallbackQueryHandler(transport_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler)
            ],
            DAILY_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler)
            ],
            DAILY_CONFIRM: [CallbackQueryHandler(daily_confirm_callback)],
            RECEIPT_PHOTO: [
                CallbackQueryHandler(receipt_target_callback),
                MessageHandler(filters.PHOTO | filters.TEXT, receipt_photo)
            ],
            RECEIPT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_docnum_handler)],
            RECEIPT_TARGET: [CallbackQueryHandler(receipt_target_callback)],
            STOCK_ACTION: [CallbackQueryHandler(stock_action_callback)],
            HISTORY_LIST: [CallbackQueryHandler(history_detail_callback)],
            OBJECTS_ACTION: [CallbackQueryHandler(objects_action_callback)],
            PLAN_MENU: [CallbackQueryHandler(plan_menu_callback)],
            PLAN_DATE: [
                CallbackQueryHandler(plan_date_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_date_callback)
            ],
            PLAN_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_time_callback)],
            PLAN_TIME: [CallbackQueryHandler(plan_time_final_callback)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(back_to_menu, pattern='^back_to_menu'),
        ],
    )

    app.add_handler(conv_handler)

    logger.info("🤖 Бот запущено")
    app.run_polling()

if __name__ == '__main__':
    main()
