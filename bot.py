"""
🤖 Моніторинг Виробництва — Telegram Бот
Вносить щоденні звіти, чеки, накладні прямо з Telegram
Зберігає дані у Firebase (той самий що й веб-програма)
"""

import os
import json
import logging
import base64
from datetime import datetime, date
from io import BytesIO

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
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

# ── СТАНИ РОЗМОВИ ─────────────────────────────────────────────
(
    MAIN_MENU,
    SELECT_OBJECT, SELECT_DATE,
    DAILY_WORKERS, DAILY_MATERIALS, DAILY_TRANSPORT, DAILY_DESC, DAILY_CONFIRM,
    RECEIPT_PHOTO, RECEIPT_CONFIRM, RECEIPT_TARGET,
    STOCK_ACTION, STOCK_NAME, STOCK_QTY,
) = range(14)

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
        ['🏗️ Об\'єкти', '❓ Допомога'],
    ], resize_keyboard=True)

# ── КОМАНДА /start ────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Привіт, {name}!\n\n"
        f"🏗️ *Моніторинг виробництва + ІТР*\n\n"
        f"Що можеш робити:\n"
        f"• 📅 Вносити щоденний звіт\n"
        f"• 🧾 Фотографувати чеки та накладні\n"
        f"• 📦 Переглядати та поповнювати склад\n"
        f"• 📊 Дивитись звіти по об'єктах\n\n"
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
    elif text == '❓ Допомога':
        await show_help(update, context)
        return MAIN_MENU
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

    context.user_data['daily'] = {
        'date': today_str(),
        'workers': [], 'materials': [], 'transport': [], 'desc': ''
    }

    buttons = [[InlineKeyboardButton(o['name'], callback_data=f"obj_{o['id']}")] for o in active]
    await update.message.reply_text(
        f"📅 *Щоденний звіт*\nДата: {fmt_date(today_str())}\n\nОберіть об'єкт:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
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

    # Show workers selection
    return await ask_workers(update, context)

async def ask_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    workers = get_workers()
    context.user_data['workers_list'] = workers

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
            return await ask_workers_msg(update, context)
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
                return await ask_workers_msg(update, context)
            else:
                await update.message.reply_text("❌ Формат: Петренко I.I. 8 (або Петренко 8 280). Кілька — через кому або з нового рядка.")
                return DAILY_WORKERS

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
            return await ask_materials_msg(update, context)
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
            return await ask_transport_msg(update, context)
        except:
            await update.message.reply_text("❌ Введіть число (ціну)")
            return DAILY_TRANSPORT

    if awaiting == 'daily_desc':
        context.user_data['daily']['desc'] = text
        context.user_data.pop('awaiting', None)
        return await confirm_daily(update, context)

    return DAILY_WORKERS

async def ask_workers_msg(update, context):
    """Show workers screen via message (not callback)"""
    workers = get_workers()
    context.user_data['workers_list'] = workers
    daily = context.user_data['daily']
    added = daily['workers']

    text = f"📅 *{daily['objName']}*\n\n👷 Хто ще працював? (або Далі)\n"
    if added:
        text += "\nДодано:\n"
        for w in added:
            text += f"  • {w['name']} — {w['hrs']}год\n"

    buttons = []
    for w in workers:
        if not any(a['name'] == w.get('name') for a in added):
            buttons.append([InlineKeyboardButton(f"👷 {w['name']}", callback_data=f"wrk_{w['id']}")])
    buttons.append([InlineKeyboardButton("✅ Далі — Матеріали", callback_data="wrk_done")])

    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return DAILY_WORKERS

async def ask_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ask_materials_msg(update, context, via_callback=True)

async def ask_materials_msg(update, context, via_callback=False):
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
        await query.edit_message_text("📦 Введіть назву матеріалу:")
        context.user_data['awaiting'] = 'material_name'
        return DAILY_MATERIALS
    return DAILY_MATERIALS

async def ask_transport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ask_transport_msg(update, context, via_callback=True)

async def ask_transport_msg(update, context, via_callback=False):
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
            text += f"  • {t['name']} — {fmt_money(t['cost'])}\n"

    if daily.get('desc'):
        text += f"\n📝 {daily['desc']}\n"

    text += f"\n💰 *РАЗОМ: {fmt_money(total_all)}*"
    text += f"\n  👷 {fmt_money(total_wage)} + 📦 {fmt_money(total_mat)} + 🚛 {fmt_money(total_tr)}"

    buttons = [
        [InlineKeyboardButton("✅ Зберегти", callback_data="daily_save"),
         InlineKeyboardButton("❌ Скасувати", callback_data="daily_cancel")],
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
    await update.message.reply_text(
        "🧾 *Фото чека або накладної*\n\n"
        "Надішліть фото документу — бот розпізнає матеріали і суму автоматично.\n\n"
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

async def show_receipt_preview(update, context):
    receipt = context.user_data.get('receipt', {})
    items = receipt.get('items', [])

    text = "🧾 *Розпізнано:*\n\n"
    if receipt.get('supplier'):
        text += f"🏪 {receipt['supplier']}\n"
    if receipt.get('date'):
        text += f"📅 {fmt_date(receipt['date'])}\n"
    text += "\n"

    for item in items:
        text += f"• {item['name']} — {item['qty']} {item.get('unit','шт')} × {fmt_money(item.get('price',0))} = {fmt_money(item.get('amount',0))}\n"

    if receipt.get('total'):
        text += f"\n💰 *Разом: {fmt_money(receipt['total'])}*"

    text += "\n\nКуди записати?"

    buttons = [
        [InlineKeyboardButton("📦 На склад", callback_data="rec_stock"),
         InlineKeyboardButton("🏗️ На об'єкт", callback_data="rec_object")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="rec_cancel")],
    ]

    await update.message.reply_text(text, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons))
    return RECEIPT_TARGET

async def receipt_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'rec_cancel':
        await query.edit_message_text("❌ Скасовано")
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if data == 'rec_stock':
        # Save to matStock
        receipt = context.user_data.get('receipt', {})
        saved = 0
        for item in receipt.get('items', []):
            existing = None
            stocks = fb_get_all('matStock')
            for s in stocks:
                if s.get('name', '').lower() == item['name'].lower():
                    existing = s
                    break

            if existing:
                existing['qty'] = existing.get('qty', 0) + item.get('qty', 0)
                fb_set('matStock', existing['id'], existing)
            else:
                fb_add('matStock', {
                    'name': item['name'],
                    'qty': item.get('qty', 0),
                    'unit': item.get('unit', 'шт'),
                    'price': item.get('price', 0),
                    'supplier': receipt.get('supplier', ''),
                    'min': 0, 'cat': '', 'sku': '', 'note': ''
                })
            saved += 1

        await query.edit_message_text(f"✅ Додано на склад: {saved} позицій")
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if data == 'rec_object':
        # Select object first
        objects = get_objects()
        active = [o for o in objects if o.get('status') == 'active']
        if not active:
            await query.edit_message_text("⚠️ Немає активних об'єктів")
            await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
            return MAIN_MENU

        buttons = [[InlineKeyboardButton(o['name'], callback_data=f"recobj_{o['id']}")] for o in active]
        await query.edit_message_text("🏗️ Оберіть об'єкт:", reply_markup=InlineKeyboardMarkup(buttons))
        return RECEIPT_TARGET

    if data.startswith('recobj_'):
        obj_id = data.replace('recobj_', '')
        objects = get_objects()
        obj = next((o for o in objects if o['id'] == obj_id), None)
        receipt = context.user_data.get('receipt', {})

        # Create daily log with materials
        log = {
            'date': receipt.get('date') or today_str(),
            'objId': obj_id,
            'objName': obj['name'] if obj else '',
            'desc': f"Закупівля: {receipt.get('supplier','')}",
            'workers': [],
            'transport': [],
            'materials': [
                {
                    'name': item['name'],
                    'qty': item.get('qty', 0),
                    'unit': item.get('unit', 'шт'),
                    'price': item.get('price', 0),
                    'sku': ''
                }
                for item in receipt.get('items', [])
            ]
        }
        doc_id = fb_add('dailyLogs', log)

        if doc_id:
            await query.edit_message_text(
                f"✅ Записано на об'єкт *{obj['name'] if obj else ''}*!\n"
                f"Матеріалів: {len(log['materials'])} позицій",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Помилка збереження")

        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    return RECEIPT_TARGET

# ── СКЛАД ─────────────────────────────────────────────────────
async def show_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stocks = fb_get_all('matStock')

    if not stocks:
        await update.message.reply_text(
            "📦 Склад порожній.\nДодайте матеріали через веб-програму або фото чека.",
            reply_markup=main_keyboard()
        )
        return MAIN_MENU

    low = [s for s in stocks if s.get('qty', 0) <= s.get('min', 0)]
    ok  = [s for s in stocks if s.get('qty', 0) > s.get('min', 0)]

    text = f"📦 *Залишки складу* ({len(stocks)} позицій)\n\n"

    if low:
        text += "🔴 *Мало / Потрібно замовити:*\n"
        for s in low[:8]:
           text += f"⚠️ {s['name']} — {s.get('qty',0)} {s.get('unit','шт')} · {fmt_money(s.get('price',0))}\n"

    if ok:
        text += "\n✅ *В нормі (перші 8):*\n"
        for s in ok[:8]:
            text += f"• {s['name']} — {s.get('qty',0)} {s.get('unit','шт')} · {fmt_money(s.get('price',0))}\n"

    if len(stocks) > 16:
        text += f"\n_...ще {len(stocks)-16} позицій у веб-програмі_"

    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# ── ЗВІТ ПО ОБ'ЄКТУ ──────────────────────────────────────────
async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objects = get_objects()
    if not objects:
        await update.message.reply_text("🏗️ Об'єктів немає", reply_markup=main_keyboard())
        return MAIN_MENU

    buttons = [[InlineKeyboardButton(o['name'], callback_data=f"rep_{o['id']}")] for o in objects[:10]]
    await update.message.reply_text(
        "📊 Оберіть об'єкт для звіту:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return MAIN_MENU

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    obj_id = query.data.replace('rep_', '')

    objects = get_objects()
    obj = next((o for o in objects if o['id'] == obj_id), None)
    if not obj: return MAIN_MENU

    logs = [l for l in fb_get_all('dailyLogs') if l.get('objId') == obj_id]

    total_hrs = sum(w.get('hrs',0) for l in logs for w in l.get('workers',[]))
    total_wage = sum(w.get('hrs',0)*w.get('rate',0) for l in logs for w in l.get('workers',[]))
    total_mat = sum(m.get('qty',0)*m.get('price',0) for l in logs for m in l.get('materials',[]))
    total_tr = sum(t.get('cost',0) for l in logs for t in l.get('transport',[]))
    total = total_wage + total_mat + total_tr
    profit = (obj.get('contract',0) or 0) - total

    text = f"📊 *{obj['name']}*\n"
    text += f"👤 {obj.get('client','—')}\n\n"
    text += f"💰 Договір: {fmt_money(obj.get('contract',0))}\n"
    text += f"📉 Витрати: {fmt_money(total)}\n"
    text += f"  👷 Зарплата: {fmt_money(total_wage)} ({total_hrs:.0f} год)\n"
    text += f"  📦 Матеріали: {fmt_money(total_mat)}\n"
    text += f"  🚛 Транспорт: {fmt_money(total_tr)}\n"
    profit_emoji = "✅" if profit >= 0 else "🔴"
    text += f"\n{profit_emoji} Прибуток: {fmt_money(profit)}\n"
    text += f"📅 Записів: {len(logs)}"

    await query.edit_message_text(text, parse_mode='Markdown')
    await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
    return MAIN_MENU

# ── ОБ'ЄКТИ ──────────────────────────────────────────────────
async def show_objects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objects = get_objects()
    if not objects:
        await update.message.reply_text("🏗️ Об'єктів немає", reply_markup=main_keyboard())
        return MAIN_MENU

    text = "🏗️ *Об'єкти:*\n\n"
    for o in objects:
        status = {'active':'🟢','plan':'🔵','done':'✅','pause':'⏸️'}.get(o.get('status',''),'⚪')
        text += f"{status} *{o['name']}*\n"
        if o.get('client'): text += f"   👤 {o['client']}\n"
        if o.get('contract'): text += f"   💰 {fmt_money(o['contract'])}\n"

    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# ── ДОПОМОГА ──────────────────────────────────────────────────
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Як користуватись ботом:*\n\n"
        "📅 *Щоденний звіт* — вносити хто працював, матеріали, транспорт\n\n"
        "🧾 *Фото чека* — сфотографуйте чек або накладну, бот розпізнає і запише\n\n"
        "📦 *Склад* — перегляд залишків матеріалів\n\n"
        "📊 *Звіт* — витрати та прибуток по об'єкту\n\n"
        "💡 *Порада:* Всі дані синхронізуються з веб-програмою автоматично",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )

# ── СКАСУВАННЯ ───────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Скасовано", reply_markup=main_keyboard())
    return MAIN_MENU

# ── ГОЛОВНА ФУНКЦІЯ ───────────────────────────────────────────
def main():
    init_firebase()

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задано!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)
            ],
            SELECT_OBJECT: [
                CallbackQueryHandler(select_object, pattern='^obj_')
            ],
            DAILY_WORKERS: [
                CallbackQueryHandler(worker_callback, pattern='^wrk_'),
                CallbackQueryHandler(hours_callback, pattern='^hrs_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler),
            ],
            DAILY_MATERIALS: [
                CallbackQueryHandler(materials_callback, pattern='^mat_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler),
            ],
            DAILY_TRANSPORT: [
                CallbackQueryHandler(transport_callback, pattern='^tr_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler),
            ],
            DAILY_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler),
            ],
            DAILY_CONFIRM: [
                CallbackQueryHandler(daily_confirm_callback, pattern='^daily_'),
            ],
            RECEIPT_PHOTO: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), receipt_photo),
            ],
            RECEIPT_TARGET: [
                CallbackQueryHandler(receipt_target_callback, pattern='^rec'),
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start),
        MessageHandler(filters.TEXT & ~filters.COMMAND, start),],
    )

    app.add_handler(conv_handler)
    # Report callback (outside conversation for /start re-entry)
    app.add_handler(CallbackQueryHandler(report_callback, pattern='^rep_'))

    logger.info("Бот запущено! ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()


