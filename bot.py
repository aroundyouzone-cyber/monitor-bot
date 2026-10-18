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
import asyncio
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta, time as dtime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
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

def catalog_items(kind, limit=12):
    """Довідник (колекція catalog): найчастіші назви спершу"""
    items = [c for c in fb_get_all('catalog') if c.get('kind') == kind and c.get('name')]
    items.sort(key=lambda c: (-(c.get('uses') or 0), c.get('name', '')))
    return items[:limit]

def catalog_learn(kind, name, unit='', price=0):
    """Запам'ятати назву: якщо її ще немає — додати, якщо є — оновити одиницю/ціну і лічильник"""
    name = (name or '').strip()
    if not name:
        return
    all_c = fb_get_all('catalog')
    ex = next((c for c in all_c if c.get('kind') == kind
               and (c.get('name') or '').strip().lower() == name.lower()), None)
    if ex:
        data = {k: v for k, v in ex.items() if k != 'id'}
        data['uses'] = (ex.get('uses') or 0) + 1
        if unit:
            data['unit'] = unit
        if price:
            data['price'] = price
        fb_set('catalog', ex['id'], data)
    else:
        fb_add('catalog', {'kind': kind, 'name': name, 'unit': unit or '',
                           'price': price or 0, 'uses': 1})

def get_workers():
    return fb_get_all('workers')

def main_keyboard():
    return ReplyKeyboardMarkup([
        ['📅 Щоденний звіт', '🧾 Чек / накладна / рахунок'],
        ['📦 Склад', '📊 Переглянути звіт'],
        ['🏗️ Об\'єкти', '📜 Мої записи'],
        ['📆 Планування'],
        ['❓ Допомога'],
    ], resize_keyboard=True)

# ── КОМАНДА /start ────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.effective_message.reply_text("⛔ У вас немає доступу до цього бота.\nЗверніться до адміністратора.")
        return ConversationHandler.END

    name = update.effective_user.first_name
    nav_reset(context)
    if is_owner(uid):
        fb_set('settings', 'admin', {'chat_id': update.effective_chat.id})
    # ── заставка: відео splash.mp4 (лежить поруч із bot.py) ──
    try:
        splash_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'splash.mp4')
        if os.path.exists(splash_path):
            with open(splash_path, 'rb') as f:
                await update.effective_message.reply_animation(animation=f, caption="🏗️ Моніторинг виробництва + ІТР")
    except Exception as e:
        logging.warning(f"Заставку не надіслано: {e}")
    await update.effective_message.reply_text(
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
    nav_reset(context)
    context.user_data.pop('daily', None)

    if text == '📅 Щоденний звіт':
        return await start_daily(update, context)
    elif text == '🧾 Чек / накладна / рахунок':
        return await start_receipt(update, context)
    elif text == '📦 Склад':
        return await show_stock(update, context)
    elif text == '📊 Переглянути звіт':
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

# ── НАВІГАЦІЯ «НАЗАД» (стек екранів) ─────────────────────────
# Кожен екран, який бот показує, кладе свою назву в context.user_data['nav'].
# «⬅️ Назад» знімає поточний екран і перерисовує попередній.
NAV_MAIN = 'main'

def nav_push(context, screen):
    stack = context.user_data.setdefault('nav', [])
    if not stack or stack[-1] != screen:
        stack.append(screen)

def nav_reset(context):
    context.user_data['nav'] = []

async def show_or_edit(update, text, reply_markup=None, parse_mode=None):
    # Змінює поточне повідомлення, якщо це натискання кнопки; інакше надсилає нове
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        except BadRequest as e:
            logger.warning(f"edit_message_text не вдалося, надсилаю нове: {e}")
    await update.effective_message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

async def go_main_menu(update, context):
    for k in ('daily', 'receipt', 'awaiting'):
        context.user_data.pop(k, None)
    nav_reset(context)
    query = update.callback_query
    if query:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
    # ReplyKeyboard не можна прикріпити до edit_message_text — тому нове повідомлення
    await update.effective_message.reply_text(
        "👋 Повернулися на головне меню. Обери дію:",
        reply_markup=main_keyboard()
    )
    return MAIN_MENU

def back_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]])

def prompt_markup():
    # Кнопки під кожним текстовим запитанням бота
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu"),
        InlineKeyboardButton("❌ Скасувати", callback_data="flow_cancel"),
    ]])

def nav_row(back_data="back_to_menu"):
    # Ряд кнопок під будь-яким екраном звіту: повернутись або скасувати
    return [InlineKeyboardButton("⬅️ Назад", callback_data=back_data),
            InlineKeyboardButton("❌ Скасувати", callback_data="flow_cancel")]

def undo_markup(ms):
    if not ms:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Відмінити цю дію", callback_data=f"undo_{ms}")]])

# ── ЖУРНАЛ ВІДМІНИ (Firebase: settings/undo_<user_id>) ────────
UNDO_LIMIT = 30

def undo_push(uid, desc, ops):
    """Записати дію в журнал. ops — список змін, які треба зробити для відміни.
    Повертає мітку дії (для кнопки) або None."""
    if not db or not ops or not uid:
        return None
    try:
        ref = db.collection('settings').document(f'undo_{uid}')
        snap = ref.get()
        items = (snap.to_dict() or {}).get('items', []) if snap.exists else []
        ms = int(time.time() * 1000)
        if items and ms <= items[-1].get('ms', 0):
            ms = items[-1]['ms'] + 1          # мітки завжди зростають, щоб кнопка знала свою дію
        items.append({'desc': desc, 'ops': ops, 'ms': ms})
        ref.set({'items': items[-UNDO_LIMIT:]})
        return ms
    except Exception as e:
        logger.error(f"undo_push error: {e}")
        return None

def undo_pop(uid, expect_ms=None):
    """Зняти останню дію з журналу. Якщо expect_ms заданий — знімає лише тоді, коли це та сама дія."""
    if not db:
        return None
    try:
        ref = db.collection('settings').document(f'undo_{uid}')
        snap = ref.get()
        items = (snap.to_dict() or {}).get('items', []) if snap.exists else []
        if not items:
            return None
        if expect_ms is not None and items[-1].get('ms') != expect_ms:
            return 'mismatch'
        last = items.pop()
        ref.set({'items': items})
        return last
    except Exception as e:
        logger.error(f"undo_pop error: {e}")
        return None

def undo_execute(entry):
    ok = True
    for op in reversed(entry.get('ops', [])):
        if op.get('op') == 'delete':
            ok = fb_delete(op['col'], op['id']) and ok
        elif op.get('op') == 'restore':
            ok = fb_set(op['col'], op['id'], op['data']) and ok
    return ok

async def do_undo(update, context, expect_ms=None):
    uid = update.effective_user.id
    entry = undo_pop(uid, expect_ms)
    if entry == 'mismatch':
        return "ℹ️ Цю дію вже не можна відмінити: після неї були інші дії. Використайте /undo для останньої."
    if not entry:
        return "ℹ️ Немає дій, які можна відмінити."
    ok = undo_execute(entry)
    text = f"↩️ Відмінено: {entry.get('desc', 'дію')}"
    if not ok:
        text += "\n⚠️ Частину змін не вдалося відмінити. Перевірте зв'язок з Firebase."
    return text

async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await do_undo(update, context)
    await update.effective_message.reply_text(text)

async def undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ms = int(query.data.replace('undo_', ''))
    except ValueError:
        ms = None
    text = await do_undo(update, context, expect_ms=ms)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    await query.message.reply_text(text)

async def flow_cancel(update, context):
    for k in ('awaiting', 'pending_material', 'pending_transport', 'pending_worker', 'pending_receipt', 'wiz'):
        context.user_data.pop(k, None)
    return await go_main_menu(update, context)

async def flow_cancel_callback(update, context):
    query = update.callback_query
    await query.answer()
    return await flow_cancel(update, context)

async def flow_cancel_text(update, context):
    return await flow_cancel(update, context)

async def render_history(update, context):
    return await show_history(update, context, via_callback=True)

async def render_objects_screen(update, context):
    date_str = (context.user_data.get('daily') or {}).get('date') or today_str()
    return await show_object_buttons(update, context, date_str, via_callback=True)

async def render_desc_screen(update, context):
    nav_push(context, 'desc')
    context.user_data['awaiting'] = 'daily_desc'
    await show_or_edit(update, "📝 Опишіть що зробили за день:", reply_markup=prompt_markup())
    return DAILY_DESC

def _nav_renderers():
    # Ключ екрана → функція, яка його знову показує
    return {
        'date': start_daily,
        'objects': render_objects_screen,
        'workers': ask_workers,
        'materials': ask_materials,
        'transport': ask_transport,
        'desc': render_desc_screen,
        'confirm': confirm_daily,
        'receipt': show_receipt_preview,
        'stock': show_stock,
        'stock_list': show_stock_list,
        'report': show_report,
        'history': render_history,
        'objects_list': show_objects,
        'obj_card': lambda u, c: show_object_card(u, c, c.user_data.get('cur_obj_id')),
        'report_detail': lambda u, c: show_object_report(u, c, c.user_data.get('cur_obj_id')),
        'plan': start_plan,
        'reminder': lambda u, c: render_reminder(u, c),
    }

async def back_to_menu(update, context):
    # «⬅️ Назад» — повертає на ПОПЕРЕДНІЙ крок
    query = update.callback_query
    if query:
        await query.answer()
    for _k in ('awaiting', 'pending_material', 'pending_transport', 'pending_worker'):
        context.user_data.pop(_k, None)
    stack = context.user_data.setdefault('nav', [])
    if stack:
        stack.pop()                                   # поточний екран
    prev = stack.pop() if stack else NAV_MAIN         # попередній (його renderer знову покладе в стек)
    renderer = _nav_renderers().get(prev)
    if renderer is None:
        return await go_main_menu(update, context)
    return await renderer(update, context)

async def back_text_handler(update, context):
    # Текстова кнопка «🔙 Назад» з reply-клавіатури (екран фото чека)
    return await go_main_menu(update, context)

def _with_back(states):
    # «Назад» ставимо ПЕРШИМ у кожному стані, інакше загальні CallbackQueryHandler
    # (без pattern) перехоплюють back_to_menu і він не спрацьовує
    back_cb = CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$')
    back_txt = MessageHandler(filters.Regex('^🔙 Назад$'), back_text_handler)
    cancel_cb = CallbackQueryHandler(flow_cancel_callback, pattern='^flow_cancel$')
    cancel_txt = MessageHandler(filters.Regex('^❌ Скасувати$'), flow_cancel_text)
    cancel_cmd = CommandHandler('cancel', flow_cancel_text)
    # Кнопки головного меню (reply-клавіатура) працюють з будь-якого кроку
    menu_re = filters.Regex('^(📅 Щоденний звіт|🧾 Чек / накладна / рахунок|📦 Склад|📊 Переглянути звіт|🏗️ Об\'єкти|📜 Мої записи|📆 Планування|❓ Допомога)$')
    menu_h = MessageHandler(menu_re, main_menu_handler)
    fallback = MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback_handler)
    return {k: [back_cb, cancel_cb, back_txt, cancel_txt, cancel_cmd, menu_h, *v, fallback] for k, v in states.items()}

# ── ЩОДЕННИЙ ЗВІТ ────────────────────────────────────────────
async def start_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objects = get_objects()
    active = [o for o in objects if o.get('status') == 'active']

    if not active:
        await update.effective_message.reply_text(
            "⚠️ Немає активних об'єктів.\n"
            "Спочатку додайте об'єкт у веб-програмі.",
            reply_markup=main_keyboard()
        )
        return MAIN_MENU

    nav_push(context, 'date')
    buttons = [
        [InlineKeyboardButton("📅 Сьогодні", callback_data="date_today"),
         InlineKeyboardButton("📆 Вчора", callback_data="date_yesterday")],
        [InlineKeyboardButton("✏️ Інша дата", callback_data="date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]
    await show_or_edit(update, "🗓 За яку дату вносимо звіт?", reply_markup=InlineKeyboardMarkup(buttons))
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
        await query.edit_message_text("✏️ Введіть дату у форматі ДД.ММ.РРРР (наприклад: 28.08.2026):",
            reply_markup=prompt_markup())
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
            await update.effective_message.reply_text(msg, reply_markup=main_keyboard())
        return MAIN_MENU

    daily = context.user_data.get('daily') or {}
    if daily.get('date') != chosen_date:
        daily = {'date': chosen_date, 'workers': [], 'materials': [], 'transport': [], 'desc': ''}
    context.user_data['daily'] = daily
    nav_push(context, 'objects')

    buttons = [[InlineKeyboardButton(o['name'], callback_data=f"obj_{o['id']}")] for o in active]
    buttons.append([InlineKeyboardButton("➕ Новий об'єкт", callback_data="obj_new")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
    text = f"📅 *Щоденний звіт*\nДата: {fmt_date(chosen_date)}\n\nОберіть об'єкт:"
    if via_callback and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.effective_message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return SELECT_OBJECT

async def select_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'obj_new':
        context.user_data['awaiting'] = 'new_object_name'
        await show_or_edit(update,
            "🏗️ Новий об'єкт.\n\nНапишіть назву об'єкта, наприклад: СЕС Губа 1\n"
            "Після цього він одразу стане обраним для звіту.",
            reply_markup=prompt_markup())
        return SELECT_OBJECT
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
    nav_push(context, 'workers')
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
    buttons.append(nav_row())

    msg_text = text
    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.effective_message.reply_text(msg_text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
    return DAILY_WORKERS

async def worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data.startswith('hrs_'):
        return await hours_callback(update, context)
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
            "`Петренко І.І. 8`\n\nАбо: `Петренко 8 280` (ПІБ години ставка)",
            parse_mode='Markdown',
            reply_markup=prompt_markup()
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
            nav_row("hrs_back"),
        ])
    )
    return DAILY_WORKERS

async def hours_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'hrs_back':
        context.user_data.pop('pending_worker', None)
        context.user_data.pop('awaiting', None)
        return await ask_workers(update, context)

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
        await query.edit_message_text("Введіть кількість годин (наприклад: 8 або 7.5):",
            reply_markup=InlineKeyboardMarkup([nav_row("hrs_back")]))
        context.user_data['awaiting'] = 'worker_hrs_input'
        return DAILY_WORKERS

    return DAILY_WORKERS

def make_daily_photo_handler(state):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        daily = context.user_data.get('daily')
        if not daily or not daily.get('objId'):
            await update.effective_message.reply_text(
                "Спочатку почніть звіт: «📅 Щоденний звіт», оберіть об'єкт.", reply_markup=main_keyboard())
            return MAIN_MENU
        photos = daily.setdefault('photos', [])
        photos.append(update.effective_message.photo[-1].file_id)
        await update.effective_message.reply_text(
            f"📷 Фото додано ({len(photos)}). Можна додати ще або продовжити звіт.")
        return state
    return handler

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
            await update.effective_message.reply_text("❌ Формат дати: ДД.ММ.РРРР (наприклад: 28.08.2026)", reply_markup=prompt_markup())
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
            await update.effective_message.reply_text("❌ Введіть число, наприклад: 8 або 7.5", reply_markup=prompt_markup())
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
                await update.effective_message.reply_text(f"⚠️ Не розпізнано: {', '.join(failed)}", reply_markup=prompt_markup())
            if added:
                return await ask_workers(update, context)
            else:
                await update.effective_message.reply_text("❌ Формат: Петренко I.I. 8 (або Петренко 8 280). Кілька — через кому або з нового рядка.", reply_markup=prompt_markup())
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
            undo_ops = []
            if mat.get('stock_id') and stock_data:
                prev_stock = dict(stock_data); prev_stock['qty'] = available
                stock_data['qty'] = new_qty
                fb_set('matStock', mat['stock_id'], stock_data)
                undo_ops.append({'op': 'restore', 'col': 'matStock', 'id': mat['stock_id'], 'data': prev_stock})
                daily = context.user_data.get('daily', {})
                move_id = fb_add('stockMoves', {
                    'matId': mat['stock_id'],
                    'type': 'out',
                    'qty': qty,
                    'price': mat.get('price', 0),
                    'date': daily.get('date') or today_str(),
                    'note': f"Бот · щоденний звіт · {daily.get('objName','')}".strip(' ·')
                })
                if move_id:
                    undo_ops.append({'op': 'delete', 'col': 'stockMoves', 'id': move_id})
            context.user_data.pop('pending_material', None)
            context.user_data.pop('awaiting', None)
            warn = "\n⚠️ Залишок став від'ємним!" if new_qty < 0 else ""
            ms = undo_push(update.effective_user.id,
                           f"Списання: {mat.get('name', '')} {qty} {mat.get('unit', 'шт')}", undo_ops)
            await update.effective_message.reply_text(f"✅ Списано {qty} {mat.get('unit','шт')}. Залишок: {new_qty}{warn}", reply_markup=undo_markup(ms))
            return await ask_materials(update, context)
        except:
            await update.effective_message.reply_text("❌ Введіть число, наприклад: 10", reply_markup=prompt_markup())
            return DAILY_MATERIALS

    if awaiting == 'material_name':

        context.user_data['pending_material'] = {'name': text}
        await update.effective_message.reply_text(
            f"📦 *{text}*\n\nВведіть кількість та одиницю (наприклад: 50 м або 10 шт):",
            parse_mode='Markdown', reply_markup=prompt_markup()
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
            await update.effective_message.reply_text(
                f"💰 Ціна за {unit} (введіть 0 якщо невідома):", reply_markup=prompt_markup()
            )
            context.user_data['awaiting'] = 'material_price'
            return DAILY_MATERIALS
        except:
            await update.effective_message.reply_text("❌ Формат: 50 м або 10", reply_markup=prompt_markup())
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
            await update.effective_message.reply_text("❌ Введіть число (ціну)", reply_markup=prompt_markup())
            return DAILY_MATERIALS

    if awaiting == 'transport_name':
        context.user_data['pending_transport'] = {'name': text}
        await update.effective_message.reply_text("🛣 Введіть пробіг (км) або мотогодини (наприклад: 120):", reply_markup=prompt_markup())
        context.user_data['awaiting'] = 'transport_qty'
        return DAILY_TRANSPORT

    if awaiting == 'transport_qty':
        try:
            qty = float(text.replace(',', '.'))
            tr = context.user_data.get('pending_transport', {})
            tr['qty'] = qty
            context.user_data['pending_transport'] = tr
            await update.effective_message.reply_text("💰 Ціна за одиницю (₴/км або ₴/год), введіть 0 якщо невідома:", reply_markup=prompt_markup())
            context.user_data['awaiting'] = 'transport_price'
            return DAILY_TRANSPORT
        except:
            await update.effective_message.reply_text("❌ Введіть число, наприклад: 120", reply_markup=prompt_markup())
            return DAILY_TRANSPORT

    if awaiting == 'transport_price':
        try:
            price = float(text.replace(',', '.').replace(' ', ''))
            tr = context.user_data.get('pending_transport', {})
            tr['price'] = price
            tr['cost'] = tr.get('qty', 0) * price
            context.user_data['daily']['transport'].append(tr)
            catalog_learn('transport', tr.get('name'), '', price)
            context.user_data.pop('pending_transport', None)
            context.user_data.pop('awaiting', None)
            return await ask_transport(update, context)
        except:
            await update.effective_message.reply_text("❌ Введіть число (ціну)", reply_markup=prompt_markup())
            return DAILY_TRANSPORT

    if awaiting == 'daily_desc':
        context.user_data['daily']['desc'] = text
        context.user_data.pop('awaiting', None)
        return await confirm_daily(update, context)

    # Fallback for unexpected text
    phase = context.user_data.get('daily_phase', 'workers')
    await update.effective_message.reply_text("⚠️ Скористайтесь кнопками вище, або натисніть відповідну кнопку зі списку.", reply_markup=prompt_markup())
    if phase == 'materials':
        return DAILY_MATERIALS
    if phase == 'transport':
        return DAILY_TRANSPORT
    return DAILY_WORKERS

async def ask_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ask_materials_msg(update, context, via_callback=True)

async def ask_materials_msg(update, context, via_callback=False):
    nav_push(context, 'materials')
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
                text += f" × {fmt_money(m['price'])} = {fmt_money(m['qty']*m['price'])}"
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
    if data == 'mat_back':
        context.user_data.pop('awaiting', None)
        return await ask_materials_msg(update, context, via_callback=True)
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
        buttons.append(nav_row("mat_back"))
        await query.edit_message_text("📦 Оберіть матеріал зі складу:", reply_markup=InlineKeyboardMarkup(buttons))
        return DAILY_MATERIALS
    if data == 'mat_manual':
        await query.edit_message_text("📦 Введіть назву матеріалу:", reply_markup=prompt_markup())
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
            parse_mode='Markdown',
            reply_markup=prompt_markup()
        )
        context.user_data['awaiting'] = 'material_stock_qty'
        return DAILY_MATERIALS
    return DAILY_MATERIALS

async def ask_transport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ask_transport_msg(update, context, via_callback=True)

async def ask_transport_msg(update, context, via_callback=False):
    nav_push(context, 'transport')
    context.user_data['daily_phase'] = 'transport'
    context.user_data['previous_state'] = DAILY_TRANSPORT
    daily = context.user_data['daily']
    added = daily['transport']

    text = "🚛 *Транспорт*\n"
    if added:
        text += "\nДодано:\n"
        for t in added:
            if t.get('qty'):
                text += f"  • {t['name']} — {t['qty']:g} × {fmt_money(t.get('price', 0))} = {fmt_money(t['cost'])}\n"
            else:
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
        nav_push(context, 'desc')
        await query.edit_message_text("📝 Коротко опишіть що зробили за день (або натисніть /skip):", reply_markup=prompt_markup())
        context.user_data['awaiting'] = 'daily_desc'
        return DAILY_DESC
    if data == 'tr_save':
        return await confirm_daily(update, context)
    if data == 'tr_list':
        context.user_data.pop('awaiting', None)
        return await ask_transport(update, context)
    if data.startswith('trcat_'):
        cid = data[len('trcat_'):]
        c = next((x for x in fb_get_all('catalog') if x.get('id') == cid), None)
        if not c:
            return await ask_transport(update, context)
        context.user_data['pending_transport'] = {'name': c['name']}
        context.user_data['awaiting'] = 'transport_qty'
        await query.edit_message_text(
            f"🛣 {c['name']}\n\nВведіть пробіг (км) або мотогодини (наприклад: 120):",
            reply_markup=prompt_markup())
        return DAILY_TRANSPORT
    if data == 'tr_add':
        rows = [[InlineKeyboardButton(f"🚛 {c['name']}", callback_data=f"trcat_{c['id']}")]
                for c in catalog_items('transport', 8)]
        text = "🚛 Введіть назву транспорту (наприклад: Газель)."
        if rows:
            text += "\n\nАбо оберіть із довідника 👇"
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="tr_list")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        context.user_data['awaiting'] = 'transport_name'
        return DAILY_TRANSPORT

async def confirm_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nav_push(context, 'confirm')
    daily = context.user_data['daily']
    context.user_data['previous_state'] = DAILY_CONFIRM

    total_hrs = sum(w['hrs'] for w in daily['workers'])
    total_wage = sum(w['hrs'] * w.get('rate', 0) for w in daily['workers'])
    total_mat = sum(m['qty'] * m.get('price', 0) for m in daily['materials'])
    total_tr = sum(t.get('cost', 0) for t in daily['transport'])
    total_all = total_wage + total_mat + total_tr

    text = f"📋 *Підтвердіть запис*\n"
    text += f"📅 {fmt_date(daily['date'])} | {daily.get('objName', '—')}\n"
    if daily.get('photos'):
        text += f"📷 Фото: {len(daily['photos'])}\n"
    text += "\n"

    if daily['workers']:
        text += "👷 *Робітники:*\n"
        for w in daily['workers']:
            text += f"  • {w['name']} — {w['hrs']} год"
            if w.get('rate'):
                text += f" × {fmt_money(w['rate'])} = {fmt_money(w['hrs']*w['rate'])}"
            text += "\n"

    if daily['materials']:
        text += "\n📦 *Матеріали:*\n"
        for m in daily['materials']:
            text += f"  • {m['name']} — {m['qty']} {m['unit']}"
            if m.get('price'):
                text += f" × {fmt_money(m['price'])} = {fmt_money(m['qty']*m['price'])}"
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
        await update.effective_message.reply_text(text, parse_mode='Markdown',
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
        # Запис без об'єкта не зберігаємо: перевіряємо, що об'єкт обрано і він існує
        obj_ok = bool(daily.get('objId')) and any(o.get('id') == daily.get('objId') for o in get_objects())
        if not obj_ok:
            await query.edit_message_text(
                "⚠️ Запис не збережено: не вибрано об'єкт (або він видалений).\n\n"
                "Натисніть «📅 Щоденний звіт», оберіть об'єкт і внесіть запис знову.")
            context.user_data.pop('daily', None)
            await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
            return MAIN_MENU
        daily['submittedBy'] = update.effective_user.first_name or update.effective_user.username or 'Невідомо'
        daily['submittedById'] = update.effective_user.id
        doc_id = fb_add('dailyLogs', daily)

        if doc_id:
            ms = undo_push(update.effective_user.id,
                           f"Щоденний звіт {fmt_date(daily['date'])} · {daily.get('objName', '')}",
                           [{'op': 'delete', 'col': 'dailyLogs', 'id': doc_id}])
            await query.edit_message_text(
                f"✅ *Збережено!*\n\n"
                f"📅 {fmt_date(daily['date'])} — {daily.get('objName', '')}\n"
                f"Запис у Firebase ✅",
                parse_mode='Markdown',
                reply_markup=undo_markup(ms)
            )
        else:
            await query.edit_message_text("❌ Помилка збереження. Перевірте Firebase.")

        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    return DAILY_CONFIRM

# ── ФОТО ЧЕКА / НАКЛАДНОЇ ────────────────────────────────────
async def start_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['previous_state'] = MAIN_MENU
    await update.effective_message.reply_text(
        "🧾 *Внесення чеку, накладної або рахунку*\n\n"
        "Тут можна внести покупку або документ у склад, закупівлі чи на об'єкт.\n\n"
        "📷 *З фото:* надішліть фото документа — бот сам розпізнає матеріали, суму і номер. "
        "Номер можна виправити кнопкою «🔖 Додати/Змінити № документа».\n\n"
        "⌨️ *З клавіатури:* напишіть назву матеріалу, наприклад `Кабель ВВГ`. "
        "Далі бот по одному запитає характеристику, одиницю, кількість і ціну.\n\n"
        "Або одним рядком з рисками:\n"
        "`Кабель ВВГ | 5×16 | 50 м | 56`",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu"),
            InlineKeyboardButton("❌ Скасувати", callback_data="flow_cancel"),
        ]])
    )
    return RECEIPT_PHOTO

# ── ПОКРОКОВЕ ВВЕДЕННЯ МАТЕРІАЛУ (чек / накладна) ─────────────────
WIZ_UNITS = [["м", "шт"], ["л", "кг"], ["інше"]]

def _wiz_nav():
    return nav_row("wiz_back")

def _num(t):
    try:
        return float(str(t).replace(',', '.').strip())
    except Exception:
        return None

def parse_quick_line(line):
    """'Кабель ВВГ | 5×16 | 50 м | 56' або 'Кабель ВВГ | 50 м | 56' → item або None"""
    parts = [p.strip() for p in line.split('|')]
    if len(parts) == 3:
        name, spec, qu, price_s = parts[0], '', parts[1], parts[2]
    elif len(parts) == 4:
        name, spec, qu, price_s = parts[0], parts[1], parts[2], parts[3]
    else:
        return None
    m = re.match(r'^(\d+(?:[.,]\d+)?)\s*(.*)$', qu)
    price = _num(price_s)
    if not name or not m or price is None:
        return None
    qty = _num(m.group(1))
    unit = (m.group(2) or 'шт').strip()
    full_name = f"{name} {spec}".strip()
    return {'name': full_name, 'qty': qty, 'unit': unit, 'price': price, 'amount': qty * price}

def _wiz_render(update, text, buttons):
    return show_or_edit(update, text, reply_markup=InlineKeyboardMarkup(buttons + [_wiz_nav()]))

async def wiz_ask_name(update, context):
    context.user_data['wiz'] = {'step': 'name', 'item': {}}
    text = ("✍️ Введення матеріалу.\n\n"
            "1️⃣ Напишіть назву, наприклад: Кабель ВВГ\n\n"
            "Або одним рядком з рисками:\n"
            "Кабель ВВГ | 5×16 | 50 м | 56\n"
            "(назва | характеристика | кількість одиниця | ціна)")
    rows = [[InlineKeyboardButton(f"📦 {c['name']}", callback_data=f"wizcat_{c['id']}")]
            for c in catalog_items('material', 8)]
    if rows:
        text += "\n\nАбо оберіть із довідника 👇"
    rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="flow_cancel")])
    await show_or_edit(update, text, reply_markup=InlineKeyboardMarkup(rows))
    return RECEIPT_PHOTO

async def wiz_ask_spec(update, context):
    context.user_data['wiz']['step'] = 'spec'
    text = ("2️⃣ Характеристика (необов'язково).\n"
            "Для кабелю: жили × перетин, наприклад 5×16.\n"
            "Для інших матеріалів можна пропустити.")
    await _wiz_render(update, text, [[InlineKeyboardButton("⏭ Пропустити", callback_data="wiz_skip_spec")]])
    return RECEIPT_PHOTO

async def wiz_ask_unit(update, context):
    context.user_data['wiz']['step'] = 'unit'
    buttons = [[InlineKeyboardButton(u, callback_data=f"wiz_unit_{u}") for u in row] for row in WIZ_UNITS]
    await _wiz_render(update, "3️⃣ Одиниця виміру:\n(м — погонний метр, шт — штуки, л — літри, кг — кілограми)", buttons)
    return RECEIPT_PHOTO

async def wiz_ask_unit_text(update, context):
    context.user_data['wiz']['step'] = 'unit_text'
    await _wiz_render(update, "Напишіть одиницю виміру, наприклад: пач, рулон, пог.м", [])
    return RECEIPT_PHOTO

async def wiz_ask_qty(update, context):
    wiz = context.user_data['wiz']
    wiz['step'] = 'qty'
    await _wiz_render(update, f"4️⃣ Скільки? Напишіть число, наприклад 50.\nОдиниця: {wiz['item'].get('unit', 'шт')}", [])
    return RECEIPT_PHOTO

async def wiz_ask_price(update, context):
    wiz = context.user_data['wiz']
    wiz['step'] = 'price'
    unit = wiz['item'].get('unit', 'шт')
    await _wiz_render(update, f"5️⃣ Ціна за одну одиницю ({unit}), грн.\nНаприклад 56. Якщо невідома — напишіть 0.", [])
    return RECEIPT_PHOTO

async def wiz_confirm(update, context):
    wiz = context.user_data['wiz']
    wiz['step'] = 'confirm'
    it = wiz['item']
    amount = it['qty'] * it['price']
    text = (f"📋 Перевірте:\n\n"
            f"{it['name']}\n"
            f"Кількість: {it['qty']:g} {it['unit']}\n"
            f"Ціна: {fmt_money(it['price'])} за {it['unit']}\n"
            f"Сума: {fmt_money(amount)}")
    buttons = [[InlineKeyboardButton("✅ Записати", callback_data="wiz_save")],
               [InlineKeyboardButton("✏️ Почати спочатку", callback_data="wiz_restart")]]
    await _wiz_render(update, text, buttons)
    return RECEIPT_PHOTO

async def wiz_save(update, context):
    item = dict(context.user_data['wiz']['item'])
    item['amount'] = item['qty'] * item['price']
    context.user_data.pop('wiz', None)
    catalog_learn('material', item.get('name'), item.get('unit', ''), item.get('price', 0))
    receipt = context.user_data.get('receipt') or {'supplier': '', 'items': []}
    items = list(receipt.get('items', [])) + [item]
    receipt['items'] = items
    receipt['total'] = sum(i.get('amount', 0) for i in items)
    context.user_data['receipt'] = receipt
    return await show_receipt_preview(update, context)

async def wiz_text(update, context):
    wiz = context.user_data['wiz']
    step = wiz['step']
    t = (update.message.text or '').strip()
    it = wiz['item']
    if step == 'name':
        if not t:
            return await wiz_ask_name(update, context)
        it['name'] = t
        return await wiz_ask_spec(update, context)
    if step == 'spec':
        it['spec'] = '' if t in ('-', '') else t
        it['name'] = f"{it.get('name', '')} {it['spec']}".strip() if it['spec'] else it.get('name', '')
        return await wiz_ask_unit(update, context)
    if step == 'unit_text':
        if not t:
            return await wiz_ask_unit_text(update, context)
        it['unit'] = t
        return await wiz_ask_qty(update, context)
    if step == 'qty':
        v = _num(t)
        if v is None or v <= 0:
            await update.effective_message.reply_text("❌ Напишіть число більше 0, наприклад 50")
            return RECEIPT_PHOTO
        it['qty'] = v
        if it.get('price'):
            return await wiz_confirm(update, context)
        return await wiz_ask_price(update, context)
    if step == 'price':
        v = _num(t)
        if v is None or v < 0:
            await update.effective_message.reply_text("❌ Напишіть ціну числом, наприклад 56 (або 0)")
            return RECEIPT_PHOTO
        it['price'] = v
        return await wiz_confirm(update, context)
    return await wiz_ask_name(update, context)

async def wiz_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith('wizcat_'):
        cid = data[len('wizcat_'):]
        c = next((x for x in fb_get_all('catalog') if x.get('id') == cid), None)
        if not c:
            return await wiz_ask_name(update, context)
        # з довідника назва, одиниця і ціна вже відомі: одразу питаємо кількість
        context.user_data['wiz'] = {'step': 'qty', 'item': {
            'name': c['name'], 'unit': c.get('unit') or 'шт', 'price': c.get('price') or 0}}
        return await wiz_ask_qty(update, context)
    if data == 'wiz_restart':
        return await wiz_ask_name(update, context)
    if data == 'wiz_save':
        return await wiz_save(update, context)
    if data == 'wiz_skip_spec':
        return await wiz_ask_unit(update, context)
    if data == 'wiz_unit_інше' or data == 'wiz_unit_other':
        return await wiz_ask_unit_text(update, context)
    if data.startswith('wiz_unit_'):
        context.user_data['wiz']['item']['unit'] = data.replace('wiz_unit_', '')
        return await wiz_ask_qty(update, context)
    if data == 'wiz_back':
        wiz = context.user_data.get('wiz')
        if not wiz:
            return await wiz_ask_name(update, context)
        prev = {'spec': wiz_ask_name, 'unit': wiz_ask_spec, 'unit_text': wiz_ask_unit,
                'qty': wiz_ask_unit, 'price': wiz_ask_qty, 'confirm': wiz_ask_price}
        step = wiz['step']
        if step == 'name':
            context.user_data.pop('wiz', None)
            return await wiz_ask_name(update, context)
        fn = prev.get(step, wiz_ask_name)
        return await fn(update, context)
    return RECEIPT_PHOTO

async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo of receipt"""
    if update.message.text == '🔙 Назад':
        await update.effective_message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if update.message.photo:
        await update.effective_message.reply_text("🔍 Розпізнаю чек...")

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
                    await update.effective_message.reply_text(
                        f"⚠️ Не вдалось розпізнати: {parsed['error']}\n\nВведіть вручну:",
                        reply_markup=prompt_markup()
                    )
                    return RECEIPT_PHOTO

                context.user_data['receipt'] = parsed
                return await show_receipt_preview(update, context)

            except Exception as e:
                logger.error(f"Claude receipt error: {e}")
                await update.effective_message.reply_text(
                    "⚠️ Помилка розпізнавання. Введіть вручну:\n"
                    "назву матеріалу — далі бот запитає решту по кроках",
                    parse_mode='Markdown',
                    reply_markup=prompt_markup()
                )
                return RECEIPT_PHOTO
        else:
            await update.effective_message.reply_text(
                "⚠️ Розпізнавання недоступне (немає API ключа Claude).\n"
                "Напишіть назву матеріалу — далі бот запитає решту по кроках",
                parse_mode='Markdown',
                reply_markup=prompt_markup()
            )
            return RECEIPT_PHOTO

    elif update.message.text:
        if context.user_data.get('wiz'):
            return await wiz_text(update, context)
        text = update.message.text.strip()
        if '|' in text:
            items = [parse_quick_line(l) for l in text.splitlines() if l.strip()]
            if items and all(items):
                receipt = context.user_data.get('receipt') or {'supplier': '', 'items': []}
                receipt['items'] = list(receipt.get('items', [])) + items
                receipt['total'] = sum(i.get('amount', 0) for i in receipt['items'])
                context.user_data['receipt'] = receipt
                return await show_receipt_preview(update, context)
            await update.effective_message.reply_text(
                "❌ Не вийшло розібрати рядок.\nПриклад: Кабель ВВГ | 5×16 | 50 м | 56")
            return RECEIPT_PHOTO
        # Звичайний текст — починаємо покрокове введення з цією назвою
        context.user_data['wiz'] = {'step': 'spec', 'item': {'name': text}}
        return await wiz_ask_spec(update, context)

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
        await update.effective_message.reply_text(text, parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logger.error(f"Receipt preview Markdown error, resending as plain text: {e}")
        await update.effective_message.reply_text(text, reply_markup=markup)

async def show_receipt_preview(update, context):
    nav_push(context, 'receipt')
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
        await query.edit_message_text("🔖 Введіть номер накладної / рахунку / чека (текстом):", reply_markup=prompt_markup())
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
        undo_ops = []
        for item in receipt.get('items', []):
            existing = None
            stocks = fb_get_all('matStock')
            for s in stocks:
                if s.get('name', '').lower() == item['name'].lower():
                    existing = s
                    break

            prev_stock = dict(existing) if existing else None
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
                move_id = fb_add('stockMoves', {
                    'matId': mat_id,
                    'type': 'in',
                    'qty': item.get('qty', 0),
                    'price': item.get('price', 0),
                    'date': receipt.get('date') or today_str(),
                    'note': f"Бот · чек{(' · ' + receipt.get('supplier')) if receipt.get('supplier') else ''}{doc_number_suffix(receipt)}"
                })
                if move_id:
                    undo_ops.append({'op': 'delete', 'col': 'stockMoves', 'id': move_id})
                if prev_stock is not None:
                    undo_ops.append({'op': 'restore', 'col': 'matStock', 'id': mat_id, 'data': prev_stock})
                else:
                    undo_ops.append({'op': 'delete', 'col': 'matStock', 'id': mat_id})
                saved += 1
            else:
                failed.append(item.get('name', '?'))

        text = f"✅ Додано на склад ({'🛠️ інструмент/спецодяг' if cat else '📦 матеріали'}): {saved} позицій"
        if failed:
            text += f"\n❌ Не вдалося зберегти ({len(failed)}): {', '.join(md_safe(n) for n in failed)}\nСпробуйте ще раз через кілька хвилин або перевірте зв'язок з Firebase."
        ms = undo_push(update.effective_user.id, f"Чек → склад: {saved} поз.", undo_ops)
        await query.edit_message_text(text, reply_markup=undo_markup(ms))
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
        undo_ops = []
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
                undo_ops.append({'op': 'delete', 'col': 'purchases', 'id': purch_id})
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
        ms = undo_push(update.effective_user.id, f"Закупівлі ({cat_labels.get(category, category)}): {saved} поз.", undo_ops)
        await query.edit_message_text(text, reply_markup=undo_markup(ms))
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
        undo_ops = []
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
                undo_ops.append({'op': 'delete', 'col': 'dailyLogs', 'id': log_id})
                saved += 1
            else:
                failed.append(item.get('name', '?'))

        text = f"✅ Додано до щоденних звітів: {saved} записів"
        ms = undo_push(update.effective_user.id, f"Чек → щоденні звіти: {saved} зап.", undo_ops)
        if failed:
            text += f"\n❌ Не вдалось ({len(failed)}): {', '.join(md_safe(n) for n in failed)}"
        await query.edit_message_text(text, reply_markup=undo_markup(ms))
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    return RECEIPT_TARGET

# ── СКЛАД ────────────────────────────────────────────────────
async def show_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nav_push(context, 'stock')
    context.user_data['previous_state'] = MAIN_MENU
    buttons = [[InlineKeyboardButton("🔍 Знайти матеріал", callback_data="stock_search")],
               [InlineKeyboardButton("📋 Показати весь склад", callback_data="stock_all")],
               [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]]
    await show_or_edit(update,
        "📦 Склад матеріалів\n\nЩо зробити?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return STOCK_ACTION

def search_back_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Знайти інший матеріал", callback_data="stock_search")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ])

def _stock_lines(items):
    # Таблиця складу; довга назва переноситься на кілька рядків, а не обрізається
    rows = [(str(i), st.get('name') or '—', f"{_fmt_num(st.get('qty', 0))} {st.get('unit') or 'шт'}")
            for i, st in enumerate(items, 1)]
    return make_table(["№", "Назва", "Кільк."], rows, [3, 22, 8])

async def show_stock_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nav_push(context, 'stock_list')
    stocks = fb_get_all('matStock')
    if not stocks:
        await show_or_edit(update, "📦 Склад поки порожній", reply_markup=search_back_markup())
        return STOCK_ACTION
    note = ""
    if len(stocks) > 50:
        note = "\nПоказано перші 50. Для пошуку натисніть «Знайти матеріал»."
    text = "<b>📦 Склад</b>\n" + _stock_lines(stocks[:50]) + note
    await show_or_edit(update, text, reply_markup=search_back_markup(), parse_mode='HTML')
    return STOCK_ACTION

async def stock_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'stock_search':
        context.user_data['awaiting'] = 'stock_search'
        await show_or_edit(update,
            "🔍 Напишіть назву матеріалу або її частину.\nНаприклад: кабель",
            reply_markup=prompt_markup())
        return STOCK_ACTION
    if query.data == 'stock_all':
        return await show_stock_list(update, context)
    return STOCK_ACTION

async def stock_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.effective_message.text or '').strip().lower()
    context.user_data.pop('awaiting', None)
    words = [w for w in q.split() if w]
    stocks = fb_get_all('matStock')
    found = [st for st in stocks if words and all(w in (st.get('name') or '').lower() for w in words)]
    if not found:
        await update.effective_message.reply_text(
            f"Нічого не знайдено за словом «{q}».\nСпробуйте коротше, наприклад: кабель або ВВГ",
            reply_markup=search_back_markup())
    else:
        text = f"<b>🔍 Знайдено: {len(found)}</b>\n" + _stock_lines(found[:30])
        await update.effective_message.reply_text(text, reply_markup=search_back_markup(), parse_mode='HTML')
    return STOCK_ACTION

async def new_object_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.effective_message.text or '').strip()
    if len(name) < 2:
        await update.effective_message.reply_text(
            "❌ Назва занадто коротка. Напишіть назву об'єкта, наприклад: СЕС Губа 1",
            reply_markup=prompt_markup())
        return SELECT_OBJECT
    context.user_data.pop('awaiting', None)
    uid = update.effective_user.id
    # Якщо такий об'єкт уже є — не дублюємо, а обираємо його
    existing = next((o for o in get_objects()
                     if (o.get('name') or '').strip().lower() == name.lower()), None)
    if existing and existing.get('status') == 'active':
        obj_id, obj_name = existing['id'], existing['name']
        await update.effective_message.reply_text(f"ℹ️ Об'єкт «{obj_name}» вже є — обрано його.")
    else:
        obj_id = fb_add('objects', {'name': name, 'status': 'active', 'note': 'Додано з бота'})
        if not obj_id:
            await update.effective_message.reply_text("❌ Не вдалося зберегти об'єкт. Спробуйте ще раз.", reply_markup=main_keyboard())
            return MAIN_MENU
        obj_name = name
        ms = undo_push(uid, f"Створено об'єкт «{name}»", [{'op': 'delete', 'col': 'objects', 'id': obj_id}])
        await update.effective_message.reply_text(f"✅ Об'єкт «{name}» додано.", reply_markup=undo_markup(ms))
    daily = context.user_data.get('daily') or {}
    daily['objId'] = obj_id
    daily['objName'] = obj_name
    context.user_data['daily'] = daily
    context.user_data['objects'] = get_objects()
    context.user_data['previous_state'] = SELECT_OBJECT
    return await ask_workers(update, context)

async def text_fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Текст, якого бот не чекав: не мовчимо, а пояснюємо що робити
    if str(context.user_data.get('awaiting', '')).startswith('hedit_'):
        return await hist_edit_text(update, context)
    if context.user_data.get('awaiting') == 'new_object_name':
        return await new_object_text(update, context)
    if str(context.user_data.get('awaiting', '')).startswith('obj_rename:'):
        return await obj_rename_text(update, context)
    if context.user_data.get('awaiting') == 'stock_search':
        return await stock_search_text(update, context)
    await update.effective_message.reply_text(
        "🤔 Я не зрозумів. Натисніть кнопку внизу, щоб обрати дію.\nЩоб почати спочатку, натисніть /start.",
        reply_markup=main_keyboard())
    return MAIN_MENU

import html as _html

def _h(v):
    return _html.escape(str(v))

def _cell(v, w):
    v = str(v)
    if len(v) > w:
        v = v[:w - 1] + "…"
    return v.ljust(w)

def _fmt_num(x):
    """Число без валюти: 2640 → '2 640', 25.5 → '25.5'."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if x == int(x):
        return f"{int(x):,}".replace(',', ' ')
    return f"{x:,.2f}".replace(',', ' ').rstrip('0').rstrip('.')

def _wrap(v, w):
    """Розбиває текст на рядки завширшки до w (слова цілі, де можна), нічого не обрізає."""
    lines, cur = [], ''
    for word in str(v).split():
        while len(word) > w:
            if cur:
                lines.append(cur)
                cur = ''
            lines.append(word[:w])
            word = word[w:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= w:
            cur += ' ' + word
        else:
            lines.append(cur)
            cur = word
    if cur or not lines:
        lines.append(cur)
    return lines

def make_table(headers, rows, widths):
    """Таблиця з заголовками у моноширинному блоці (Telegram <pre>).
    Довгий текст переноситься на наступні рядки всередині своєї колонки."""
    sep = " | "

    def block(cells):
        wrapped = [_wrap(c, w) for c, w in zip(cells, widths)]
        n = max(len(x) for x in wrapped)
        return [sep.join((x[i] if i < len(x) else '').ljust(w) for x, w in zip(wrapped, widths)).rstrip()
                for i in range(n)]

    lines = block(headers)
    lines.append("-" * (sum(widths) + len(sep) * (len(widths) - 1)))
    for r in rows:
        lines += block(r)
    return "<pre>" + _h("\n".join(lines)) + "</pre>"

# ── ДЕТАЛІ ЗАПИСУ ДЛЯ ЗВІТУ ─────────────────────────────────────
def log_obj_name(log):
    """Назва об'єкта для запису: якщо в записі її немає — шукаємо за id, інакше «без об'єкта»."""
    name = (log.get('objName') or '').strip()
    if name:
        return name
    oid = log.get('objId')
    if oid:
        o = next((o for o in get_objects() if o.get('id') == oid), None)
        if o and o.get('name'):
            return o['name']
        # запис посилається на об'єкт, якого вже немає (видалений або перейменований)
        return f"об'єкт не знайдено (id {oid[:6]})"
    return "без об'єкта (запис створено без об'єкта)"

def log_total(log):
    wage = sum((w.get('hrs') or 0) * (w.get('rate') or 0) for w in (log.get('workers') or []))
    mat = sum((m.get('qty') or 0) * (m.get('price') or 0) for m in (log.get('materials') or []))
    tr = sum((t.get('cost') or 0) for t in (log.get('transport') or []))
    return wage + mat + tr

def format_log_details(log):
    """Картка запису: кожен розділ — таблиця з заголовками. Повертає HTML."""
    out = [f"<b>📅 {_h(fmt_date(log.get('date')))} · {_h(log_obj_name(log))}</b>",
           f"✍️ Подав: {_h(log.get('submittedBy') or 'не вказано')}"]
    workers = log.get('workers') or []
    materials = log.get('materials') or []
    transport = log.get('transport') or []
    # Кожна таблиця: кількість × ціна за одиницю = сума
    if workers:
        rows = []
        for w in workers:
            hrs, rate = w.get('hrs') or 0, w.get('rate')
            rows.append((w.get('name', ''),
                         _fmt_num(hrs),
                         _fmt_num(rate) if rate else '—',
                         _fmt_num(hrs * rate) if rate else '—'))
        out += ["", "<b>👷 Робітники</b>",
                make_table(["Працівник", "Годин", "Ставка", "Сума"], rows, [11, 6, 6, 6])]
    if materials:
        rows = []
        for m in materials:
            qty, price = m.get('qty'), m.get('price')
            rows.append((m.get('name', ''),
                         f"{_fmt_num(qty)} {m.get('unit') or ''}".strip() if qty not in (None, '') else '—',
                         _fmt_num(price) if price else '—',
                         _fmt_num((qty or 0) * price) if price else '—'))
        out += ["", "<b>📦 Матеріали</b>",
                make_table(["Матеріал", "К-сть", "Ціна/од", "Сума"], rows, [11, 6, 7, 6])]
    if transport:
        rows = []
        for x in transport:
            qty, price = x.get('qty'), x.get('price')
            rows.append((x.get('name', ''),
                         _fmt_num(qty) if qty else '—',
                         _fmt_num(price) if price else '—',
                         _fmt_num(x.get('cost') or 0)))
        out += ["", "<b>🚛 Транспорт</b>",
                make_table(["Транспорт", "К-сть", "Ціна/од", "Сума"], rows, [11, 6, 7, 6])]
    if log.get('desc'):
        out += ["", f"📝 {_h(log['desc'])}"]
    if log.get('photos'):
        out += ["", f"📷 Фото: {len(log['photos'])} (натисніть «Показати фото»)"]
    out += ["", f"💰 <b>Разом за запис: {_h(fmt_money(log_total(log)))}</b>"]
    hist = log.get('history') or []
    if hist:
        out += ["", "<b>🕘 Зміни</b>"]
        for h in hist[-3:]:
            out.append(f"• {_h(h.get('ts', ''))} — {_h(h.get('by', ''))} змінив {_h(h.get('what', ''))}")
    return "\n".join(out)

PERIOD_LABELS = {'w': 'за тиждень', 'm': 'за місяць', '': 'за весь час'}

def filter_logs_period(logs, kind):
    t = date.today()
    if kind == 'w':
        start = (t - timedelta(days=6)).isoformat()
    elif kind == 'm':
        start = t.replace(day=1).isoformat()
    else:
        start = ''
    return [l for l in logs if not start or str(l.get('date') or '') >= start]

def rep_markup(obj_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Тиждень", callback_data=f"rep_w_{obj_id}"),
         InlineKeyboardButton("📆 Місяць", callback_data=f"rep_m_{obj_id}")],
        [InlineKeyboardButton("🗂 Весь час", callback_data=f"rep_{obj_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ])

def render_object_report(obj_logs, label=''):
    obj_logs = sorted(obj_logs, key=lambda l: str(l.get('date') or ''), reverse=True)
    name = log_obj_name(obj_logs[0])
    grand = sum(log_total(l) for l in obj_logs)
    rows = [(fmt_date(l.get('date')), l.get('submittedBy') or 'не вказано', fmt_money(log_total(l)))
            for l in obj_logs[:15]]
    title = f"<b>📊 Звіт по об'єкту: {_h(name)}</b>" + (f" <i>({_h(label)})</i>" if label else "")
    parts = [title,
             f"Записів: {len(obj_logs)} | Разом: <b>{_h(fmt_money(grand))}</b>",
             "", make_table(["Дата", "Подав", "Сума"], rows, [10, 10, 9])]
    if len(obj_logs) > 15:
        parts.append(f"… показано 15 з {len(obj_logs)}")
    parts.append("\n<b>Деталі останніх записів:</b>")
    shown = 0
    for l in obj_logs[:5]:
        card = format_log_details(l)
        if len("\n".join(parts)) + len(card) > 3500:
            break
        parts.append("\n" + card)
        shown += 1
    if shown < len(obj_logs):
        parts.append(f"\n… ще {len(obj_logs) - shown} записів: натисніть «Мої записи»")
    return "\n".join(parts)

# ── ЗВІТ ПО ОБ'ЄКТУ ───────────────────────────────────────────
async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nav_push(context, 'report')
    context.user_data['previous_state'] = MAIN_MENU
    objects = get_objects()
    buttons = [[InlineKeyboardButton(o['name'], callback_data=f"rep_{o['id']}")] for o in objects[:10]]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
    await show_or_edit(update,
        "📊 *Звіт по об'єкту*\n\nОберіть об'єкт:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return HISTORY_LIST

def parse_date_ddmmyyyy(t):
    try:
        parts = t.replace('/', '.').replace('-', '.').split('.')
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        return date(y, m, d).isoformat()
    except Exception:
        return None

def log_card_markup(log_id, extra_rows=None, has_photos=False):
    rows = list(extra_rows or [])
    if has_photos:
        rows.append([InlineKeyboardButton("📷 Показати фото", callback_data=f"hphoto_{log_id}")])
    rows += [
        [InlineKeyboardButton("✏️ Змінити опис", callback_data=f"hedit_desc_{log_id}"),
         InlineKeyboardButton("📅 Змінити дату", callback_data=f"hedit_date_{log_id}")],
        [InlineKeyboardButton("🗑 Видалити запис", callback_data=f"hdel_{log_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="hist_back")],
    ]
    return InlineKeyboardMarkup(rows)

def now_stamp():
    return datetime.now(kyiv_tz()).strftime('%d.%m %H:%M')

def kyiv_tz():
    try:
        return ZoneInfo('Europe/Kyiv') if ZoneInfo else timezone(timedelta(hours=3))
    except Exception:
        return timezone(timedelta(hours=3))

def _log_without_id(log):
    return {k: v for k, v in log.items() if k != 'id'}

async def hist_edit_text(update, context):
    """Текст для зміни опису або дати запису (стан awaiting = hedit_<поле>:<id>)"""
    aw = context.user_data.get('awaiting', '')
    kind, log_id = aw.split(':', 1)
    kind = kind.replace('hedit_', '')
    text = (update.effective_message.text or '').strip()
    if kind == 'desc':
        if not text:
            await update.effective_message.reply_text("❌ Напишіть опис роботи текстом.", reply_markup=prompt_markup())
            return HISTORY_LIST
        changes = {'desc': text}
    else:
        iso = parse_date_ddmmyyyy(text)
        if not iso:
            await update.effective_message.reply_text(
                "❌ Невірна дата. Напишіть так: 17.10.2026", reply_markup=prompt_markup())
            return HISTORY_LIST
        changes = {'date': iso}
    logs = fb_get_all('dailyLogs')
    old = next((l for l in logs if l['id'] == log_id), None)
    if not old:
        context.user_data.pop('awaiting', None)
        await update.effective_message.reply_text("❌ Запис не знайдено.", reply_markup=main_keyboard())
        return MAIN_MENU
    old_wo = _log_without_id(old)
    new = dict(old_wo)
    new.update(changes)
    new['history'] = list(old_wo.get('history') or []) + [{
        'ts': now_stamp(),
        'by': update.effective_user.first_name or 'Невідомо',
        'what': 'опис' if kind == 'desc' else 'дату',
    }]
    if not fb_set('dailyLogs', log_id, new):
        await update.effective_message.reply_text("❌ Не вдалося зберегти зміну. Спробуйте ще раз.")
        return HISTORY_LIST
    context.user_data.pop('awaiting', None)
    ms = undo_push(update.effective_user.id,
                   f"Змінено запис {fmt_date(new.get('date'))} · {log_obj_name(new)}",
                   [{'op': 'restore', 'col': 'dailyLogs', 'id': log_id, 'data': old_wo}])
    undo_rows = undo_markup(ms).inline_keyboard if ms else []
    markup = log_card_markup(log_id, extra_rows=undo_rows)
    await update.effective_message.reply_text("✅ Зміну збережено.\n\n" + format_log_details(new),
                                              parse_mode='HTML', reply_markup=log_card_markup(log_id, extra_rows=undo_rows, has_photos=bool(new.get('photos'))))
    return HISTORY_LIST

async def history_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith('rep_'):
        rest = data[len('rep_'):]
        kind = ''
        if rest.startswith('w_'):
            kind, rest = 'w', rest[2:]
        elif rest.startswith('m_'):
            kind, rest = 'm', rest[2:]
        obj_id = rest
        logs = fb_get_all('dailyLogs')
        obj_logs = [l for l in logs if l.get('objId') == obj_id]

        if not obj_logs:
            await query.edit_message_text("❌ Записів немає", reply_markup=back_markup())
            return HISTORY_LIST

        label = PERIOD_LABELS[kind]
        sel = filter_logs_period(obj_logs, kind)
        nav_push(context, 'report_detail')
        if not sel:
            await query.edit_message_text(f"ℹ️ {label[0].upper() + label[1:]} записів немає.", reply_markup=rep_markup(obj_id))
            return HISTORY_LIST
        text = render_object_report(sel, label)
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=rep_markup(obj_id))
        return HISTORY_LIST

    if data == 'hist_back':
        context.user_data.pop('awaiting', None)
        return await show_history(update, context, via_callback=True)

    if data.startswith('hphoto_'):
        log_id = data[len('hphoto_'):]
        log = next((l for l in fb_get_all('dailyLogs') if l['id'] == log_id), None)
        photos = (log or {}).get('photos') or []
        if not photos:
            await query.message.reply_text("📷 Фото до цього запису немає.")
            return HISTORY_LIST
        for fid in photos[:10]:
            try:
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=fid)
            except Exception as e:
                logger.error(f"send_photo error: {e}")
        return HISTORY_LIST

    if data.startswith('hedit_desc_') or data.startswith('hedit_date_'):
        kind = 'desc' if data.startswith('hedit_desc_') else 'date'
        log_id = data.split('_', 2)[2]
        context.user_data['awaiting'] = f"hedit_{kind}:{log_id}"
        text = ("✍️ Напишіть новий опис роботи:" if kind == 'desc'
                else "📅 Напишіть нову дату у форматі ДД.ММ.РРРР, наприклад 17.10.2026")
        await show_or_edit(update, text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Назад", callback_data=f"hist_{log_id}"),
            InlineKeyboardButton("❌ Скасувати", callback_data="flow_cancel"),
        ]]))
        return HISTORY_LIST

    if data.startswith('hdel_') and not data.startswith('hdelok_'):
        log_id = data[len('hdel_'):]
        log = next((l for l in fb_get_all('dailyLogs') if l['id'] == log_id), None)
        if not log:
            await show_or_edit(update, "❌ Запис не знайдено.", reply_markup=back_markup())
            return HISTORY_LIST
        await show_or_edit(update,
            f"🗑 Видалити запис?\n📅 {fmt_date(log.get('date'))} · {log_obj_name(log)}\n\n"
            "Після видалення можна відмінити кнопкою.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Так, видалити", callback_data=f"hdelok_{log_id}")],
                [InlineKeyboardButton("⬅️ Ні, назад", callback_data=f"hist_{log_id}")],
            ]))
        return HISTORY_LIST

    if data.startswith('hdelok_'):
        log_id = data[len('hdelok_'):]
        log = next((l for l in fb_get_all('dailyLogs') if l['id'] == log_id), None)
        if not log:
            await query.edit_message_text("❌ Запис уже не існує.")
            return HISTORY_LIST
        old_wo = _log_without_id(log)
        fb_delete('dailyLogs', log_id)
        ms = undo_push(update.effective_user.id,
                       f"Видалено запис {fmt_date(log.get('date'))} · {log_obj_name(log)}",
                       [{'op': 'restore', 'col': 'dailyLogs', 'id': log_id, 'data': old_wo}])
        await query.edit_message_text("🗑 Запис видалено.", reply_markup=undo_markup(ms))
        return HISTORY_LIST

    if data.startswith('hist_'):
        log_id = data.replace('hist_', '')
        context.user_data.pop('awaiting', None)
        logs = fb_get_all('dailyLogs')
        log = next((l for l in logs if l['id'] == log_id), None)

        if log:
            nav_push(context, 'history_detail')
            await query.edit_message_text(format_log_details(log), parse_mode='HTML',
                                          reply_markup=log_card_markup(log_id, has_photos=bool(log.get('photos'))))
        else:
            await query.edit_message_text("❌ Запис не знайдено.", reply_markup=back_markup())

        return HISTORY_LIST

    return HISTORY_LIST

# ── ОБ'ЄКТИ ───────────────────────────────────────────────────
async def show_objects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nav_push(context, 'objects_list')
    context.user_data['previous_state'] = MAIN_MENU
    objects = get_objects()
    buttons = [[InlineKeyboardButton(f"🏗️ {o['name']}", callback_data=f"obj_detail_{o['id']}")] for o in objects]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
    await show_or_edit(update,
        "🏗️ *Об'єкти*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return OBJECTS_ACTION

async def show_object_card(update, context, obj_id, undo_ms=None):
    # Картка об'єкта: стан і зміни. Внесення й перегляд звітів — у головному меню.
    obj = next((o for o in get_objects() if o['id'] == obj_id), None)
    if not obj:
        await show_or_edit(update, "❌ Такого об'єкта немає. Оберіть інший.", reply_markup=back_markup())
        return OBJECTS_ACTION
    context.user_data['cur_obj_id'] = obj_id
    nav_push(context, 'obj_card')
    active = obj.get('status') == 'active'
    status = 'активний' if active else 'призупинено'
    logs = [l for l in fb_get_all('dailyLogs') if l.get('objId') == obj_id]
    wk = filter_logs_period(logs, 'w')
    text = f"🏗️ {obj.get('name', '—')}\nСтатус: {status}\n\n"
    text += f"Записів за тиждень: {len(wk)} · {fmt_money(sum(log_total(l) for l in wk))}\n"
    if logs:
        last = max(logs, key=lambda l: str(l.get('date') or ''))
        text += f"Останній запис: {fmt_date(last.get('date'))} · {fmt_money(log_total(last))}"
    else:
        text += "Записів ще немає"
    buttons = []
    if undo_ms:
        buttons.append(undo_markup(undo_ms).inline_keyboard[0])
    buttons += [
        [InlineKeyboardButton("▶️ Активувати" if not active else "⏸ Призупинити",
                              callback_data=f"obj_toggle_{obj_id}")],
        [InlineKeyboardButton("✏️ Змінити назву", callback_data=f"obj_rename_{obj_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]
    await show_or_edit(update, text, reply_markup=InlineKeyboardMarkup(buttons))
    return OBJECTS_ACTION

async def obj_rename_text(update, context):
    name = (update.effective_message.text or '').strip()
    awaiting = str(context.user_data.get('awaiting', ''))
    obj_id = awaiting.split(':', 1)[1] if ':' in awaiting else ''
    if len(name) < 2:
        await update.effective_message.reply_text(
            "❌ Назва занадто коротка. Напишіть нову назву, наприклад: СЕС Губа 1",
            reply_markup=prompt_markup())
        return OBJECTS_ACTION
    obj = next((o for o in get_objects() if o['id'] == obj_id), None)
    context.user_data.pop('awaiting', None)
    if not obj:
        await update.effective_message.reply_text("❌ Такого об'єкта немає.", reply_markup=back_markup())
        return OBJECTS_ACTION
    old = {k: v for k, v in obj.items() if k != 'id'}
    fb_set('objects', obj_id, {**old, 'name': name})
    ms = undo_push(update.effective_user.id, f"Перейменовано об'єкт «{old.get('name', '')}» → «{name}»",
                   [{'op': 'restore', 'col': 'objects', 'id': obj_id, 'data': old}])
    return await show_object_card(update, context, obj_id, undo_ms=ms)

async def show_object_report(update, context, obj_id):
    logs = fb_get_all('dailyLogs')
    obj_logs = [l for l in logs if l.get('objId') == obj_id]
    nav_push(context, 'report_detail')
    if not obj_logs:
        await show_or_edit(update, "ℹ️ По цьому об'єкту ще немає звітів.", reply_markup=obj_back_markup(obj_id))
    else:
        await show_or_edit(update, render_object_report(obj_logs), reply_markup=obj_back_markup(obj_id), parse_mode='HTML')
    return OBJECTS_ACTION

def obj_back_markup(obj_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Внести звіт за сьогодні", callback_data=f"obj_daily_{obj_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ])

async def objects_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'back_to_menu':
        return await back_to_menu(update, context)

    if data.startswith('obj_detail_'):
        return await show_object_card(update, context, data.replace('obj_detail_', ''))

    if data.startswith('obj_toggle_'):
        obj_id = data.replace('obj_toggle_', '')
        obj = next((o for o in get_objects() if o['id'] == obj_id), None)
        if not obj:
            await show_or_edit(update, "❌ Такого об'єкта немає.", reply_markup=back_markup())
            return OBJECTS_ACTION
        old = {k: v for k, v in obj.items() if k != 'id'}
        now_active = obj.get('status') != 'active'
        fb_set('objects', obj_id, {**old, 'status': 'active' if now_active else 'paused'})
        ms = undo_push(update.effective_user.id,
                       f"Об'єкт «{old.get('name', '')}»: {'активовано' if now_active else 'призупинено'}",
                       [{'op': 'restore', 'col': 'objects', 'id': obj_id, 'data': old}])
        return await show_object_card(update, context, obj_id, undo_ms=ms)

    if data.startswith('obj_rename_'):
        obj_id = data.replace('obj_rename_', '')
        obj = next((o for o in get_objects() if o['id'] == obj_id), None)
        if not obj:
            await show_or_edit(update, "❌ Такого об'єкта немає.", reply_markup=back_markup())
            return OBJECTS_ACTION
        context.user_data['awaiting'] = f'obj_rename:{obj_id}'
        await show_or_edit(update,
            f"✏️ Напишіть нову назву для «{obj.get('name', '')}»:",
            reply_markup=prompt_markup())
        return OBJECTS_ACTION

    if data.startswith('obj_daily_'):
        obj_id = data.replace('obj_daily_', '')
        obj = next((o for o in get_objects() if o['id'] == obj_id), None)
        if not obj:
            await show_or_edit(update, "❌ Такого об'єкта немає.", reply_markup=back_markup())
            return OBJECTS_ACTION
        context.user_data['daily'] = {
            'date': today_str(), 'workers': [], 'materials': [], 'transport': [], 'desc': '',
            'objId': obj_id, 'objName': obj.get('name', ''),
        }
        context.user_data['objects'] = get_objects()
        context.user_data['previous_state'] = SELECT_OBJECT
        nav_reset(context)
        return await ask_workers(update, context)

    if data.startswith('obj_report_'):
        obj_id = data.replace('obj_report_', '')
        context.user_data['cur_obj_id'] = obj_id
        nav_push(context, 'obj_card')
        return await show_object_report(update, context, obj_id)

    return OBJECTS_ACTION

# ── ІСТОРІЯ ────────────────────────────────────────────────────
async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE, via_callback=False):
    nav_push(context, 'history')
    context.user_data['previous_state'] = MAIN_MENU
    logs = fb_get_all('dailyLogs')

    if not logs:
        msg = "📜 Записів немає"
        if via_callback:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg, reply_markup=main_keyboard())
        return MAIN_MENU

    buttons = [[InlineKeyboardButton(f"📅 {fmt_date(l.get('date'))} — {log_obj_name(l)}", callback_data=f"hist_{l['id']}")] for l in logs[:10]]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])

    msg = "📜 *Мої записи:*"
    if via_callback:
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.effective_message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return HISTORY_LIST

# ── ПЛАНУВАННЯ ────────────────────────────────────────────────
async def start_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nav_push(context, 'plan')
    context.user_data['previous_state'] = MAIN_MENU
    buttons = [
        [InlineKeyboardButton("➕ Додати нагадування", callback_data="plan_add")],
        [InlineKeyboardButton("📋 Найближчі", callback_data="plan_list")],
        [InlineKeyboardButton("🔔 Нагадування про звіт", callback_data="rem_menu")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ]
    await show_or_edit(update,
        "📆 *Планування*\n\nЩо робити?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return PLAN_MENU

async def plan_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith('rem_'):
        return await reminder_callback(update, context)

    if data == 'plan_add':
        nav_push(context, 'plan_date')
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
        nav_push(context, 'plan_list')
        plans = fb_get_all('plans')
        if not plans:
            await query.edit_message_text("📋 Нагадувань немає", reply_markup=back_markup())
        else:
            text = "📋 *Найближчі нагадування:*\n\n"
            for p in plans[:10]:
                text += f"📅 {fmt_date(p.get('date'))} — {p.get('text')}\n"
            await query.edit_message_text(text, parse_mode='Markdown', reply_markup=back_markup())
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
            await update.effective_message.reply_text(f"📝 Введіть текст нагадування:", reply_markup=prompt_markup())
            context.user_data['awaiting'] = 'plan_text'
            return PLAN_TEXT
        except:
            await update.effective_message.reply_text("❌ Формат дати: ДД.ММ.РРРР (наприклад: 28.08.2026)", reply_markup=prompt_markup())
            return PLAN_DATE

    if data == 'plandate_today':
        chosen_date = today_str()
    elif data == 'plandate_tomorrow':
        chosen_date = (date.today() + timedelta(days=1)).isoformat()
    elif data == 'plandate_custom':
        context.user_data['previous_state'] = PLAN_DATE
        await query.edit_message_text("✏️ Введіть дату (ДД.ММ.РРРР):", reply_markup=prompt_markup())
        context.user_data['awaiting'] = 'plan_date'
        return PLAN_DATE
    else:
        return PLAN_DATE

    context.user_data['plan_date'] = chosen_date
    context.user_data['previous_state'] = PLAN_DATE
    await query.edit_message_text(f"📝 Введіть текст нагадування:", reply_markup=prompt_markup())
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

    await update.effective_message.reply_text(
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
        plan_id = fb_add('plans', plan)
        ms = undo_push(update.effective_user.id, f"Нагадування: {plan.get('text') or ''}",
                       [{'op': 'delete', 'col': 'plans', 'id': plan_id}] if plan_id else [])
        await query.edit_message_text("✅ Нагадування додано", reply_markup=undo_markup(ms))
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    return PLAN_TIME

# ── ДОПОМОГА ──────────────────────────────────────────────────
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 *Моніторинг виробництва — Бот*

📅 *Щоденний звіт* — вносьте робітників, матеріали та транспорт
🧾 *Чек / накладна / рахунок* — внести покупку з фото або вручну
📦 *Склад* — перегляд матеріалів
📊 *Звіт* — звіти по об'єктам
📆 *Планування* — нагадування
❓ *Допомога* — ця інформація

⬅️ *Назад* — повернутись на попередній крок (далі — головне меню)
❌ *Скасувати* — перервати введення і повернутись у головне меню
↩️ /undo — відмінити останню дію (або кнопка «Відмінити» під повідомленням)
"""
    await update.effective_message.reply_text(text, parse_mode='Markdown')

# ── ОСНОВНИЙ ЦИКЛ ─────────────────────────────────────────────
def kyiv_now():
    return datetime.now(kyiv_tz())

def reminder_due(now, last_sent_day, hour):
    # Нагадування надсилається в межах 2 годин після свого часу, щоб уночі після перезапуску не шуміти
    return hour <= now.hour < hour + 2 and last_sent_day != now.date().isoformat()

def missing_reports(objects, logs, day_iso):
    have = {l.get('objId') for l in logs if str(l.get('date') or '') == day_iso}
    return [o for o in objects if o.get('status') == 'active' and o.get('id') not in have]

# ── НАГАДУВАННЯ ПРО ЗВІТ (персональні: дні, час, відпустка) ──────
WEEKDAY_NAMES = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']
ABSENCE_LABELS = {'vac': '🏖 Відпустка', 'sick': '🤒 Лікарняний', 'other': '📝 Інше'}
REASONS = {'vac': '🏖 Відпустка', 'sick': '🤒 Лікарняний', 'nowork': '🌧 Не було роботи', 'other': '📝 Інше'}
DEFAULT_EARLY = int(os.environ.get('REMIND_EARLY_HOUR', '17') or 17)
DEFAULT_LATE = int(os.environ.get('REMIND_HOUR', '18') or 18)

def _prefs_from(doc):
    return {'weekdays': list(doc.get('weekdays', [0, 1, 2, 3, 4])),
            'absentType': doc.get('absentType', ''), 'absentUntil': doc.get('absentUntil', ''),
            'early_h': int(doc.get('early_h', DEFAULT_EARLY)), 'late_h': int(doc.get('late_h', DEFAULT_LATE)),
            'earlySent': doc.get('earlySent', ''), 'lateSent': doc.get('lateSent', '')}

def get_user_prefs(uid):
    doc = next((x for x in fb_get_all('reminderPrefs') if str(x.get('id')) == str(uid)), None) or {}
    return _prefs_from(doc)

def _save_prefs(uid, prefs):
    fb_set('reminderPrefs', str(uid), dict(prefs))

def is_absent(prefs, day_iso):
    return bool(prefs.get('absentType')) and str(prefs.get('absentUntil') or '') >= day_iso

def should_remind(prefs, now):
    return now.weekday() in prefs['weekdays'] and not is_absent(prefs, now.date().isoformat())

def pending_objects(day):
    # Активні об'єкти без звіту за день, яким ще не пояснено причину
    explained = {x.get('objId') for x in fb_get_all('noReportReasons') if x.get('date') == day}
    return [o for o in missing_reports(get_objects(), fb_get_all('dailyLogs'), day) if o.get('id') not in explained]

def _render_text(p, today):
    days = ", ".join(WEEKDAY_NAMES[i] for i in sorted(p['weekdays'])) or "немає"
    if p['absentType'] and str(p['absentUntil']) >= today.isoformat():
        absence = f"Зараз: {ABSENCE_LABELS.get(p['absentType'], '')} до {fmt_date(p['absentUntil'])}"
    else:
        absence = "Зараз: на роботі"
    return ("🔔 Нагадування про звіт\n\n"
            f"Дні: {days}\n"
            f"Перше нагадування: {p['early_h']:02d}:00\n"
            f"Друге нагадування: {p['late_h']:02d}:00\n"
            f"{absence}\n\n"
            "Натисніть день, щоб вмикати або вимикати нагадування.")

def _render_kb(p, undo_ms=None):
    rows = []
    if undo_ms:
        rows.append(undo_markup(undo_ms).inline_keyboard[0])
    for chunk in (range(0, 4), range(4, 7)):
        rows.append([InlineKeyboardButton(("✅ " if i in p['weekdays'] else "⬜ ") + WEEKDAY_NAMES[i],
                                          callback_data=f"rem_day_{i}") for i in chunk])
    rows.append([InlineKeyboardButton(f"⏰ Перше: {p['early_h']:02d}:00", callback_data="rem_tm_e"),
                 InlineKeyboardButton(f"⏰ Друге: {p['late_h']:02d}:00", callback_data="rem_tm_l")])
    rows.append([InlineKeyboardButton("🏖 Відпустка", callback_data="rem_abs_vac"),
                 InlineKeyboardButton("🤒 Лікарняний", callback_data="rem_abs_sick")])
    rows.append([InlineKeyboardButton("📝 Інше", callback_data="rem_abs_other")])
    if p['absentType']:
        rows.append([InlineKeyboardButton("✅ Я вже на роботі", callback_data="rem_clear")])
    rows.append([InlineKeyboardButton("📝 Пояснити, чому немає звіту", callback_data="why_menu")])
    rows.append(nav_row())
    return InlineKeyboardMarkup(rows)

async def render_reminder(update, context, undo_ms=None):
    p = get_user_prefs(update.effective_user.id)
    await show_or_edit(update, _render_text(p, kyiv_now().date()), reply_markup=_render_kb(p, undo_ms))
    return PLAN_MENU

async def show_reminder_settings(update, context):
    stack = context.user_data.setdefault('nav', [])
    if not stack or stack[-1] != 'reminder':
        nav_push(context, 'reminder')
    return await render_reminder(update, context)

def _change_prefs(uid, new, old, desc):
    _save_prefs(uid, new)
    return undo_push(uid, desc, [{'op': 'restore', 'col': 'reminderPrefs', 'id': str(uid), 'data': dict(old)}])

async def reminder_callback(update, context):
    data = update.callback_query.data
    uid = update.effective_user.id
    if data == 'rem_menu':
        return await show_reminder_settings(update, context)
    p = get_user_prefs(uid)
    if data.startswith('rem_day_'):
        i = int(data[len('rem_day_'):])
        new = dict(p, weekdays=sorted(set(p['weekdays']) ^ {i}))
        ms = _change_prefs(uid, new, p, f"Нагадування: дні {', '.join(WEEKDAY_NAMES[d] for d in new['weekdays']) or 'немає'}")
        return await render_reminder(update, context, undo_ms=ms)
    if data.startswith('rem_tm_'):
        kind = data[len('rem_tm_'):]
        hours = [15, 16, 17, 18] if kind == 'e' else [17, 18, 19, 20]
        title = "першого" if kind == 'e' else "другого"
        buttons = [[InlineKeyboardButton(f"{h:02d}:00", callback_data=f"rem_seth_{kind}_{h}") for h in hours]]
        buttons.append(nav_row("rem_menu"))
        await show_or_edit(update, f"Оберіть час {title} нагадування:", reply_markup=InlineKeyboardMarkup(buttons))
        return PLAN_MENU
    if data.startswith('rem_seth_'):
        kind, h = data[len('rem_seth_'):].split('_')
        key = 'early_h' if kind == 'e' else 'late_h'
        new = dict(p, **{key: int(h)})
        ms = _change_prefs(uid, new, p, f"Нагадування: {'перше' if kind == 'e' else 'друге'} о {int(h):02d}:00")
        return await render_reminder(update, context, undo_ms=ms)
    if data.startswith('rem_abs_'):
        t = data[len('rem_abs_'):]
        buttons = [[InlineKeyboardButton(f"{n} дн.", callback_data=f"rem_set_{t}_{n}") for n in (1, 3, 7, 14)]]
        buttons.append(nav_row("rem_menu"))
        await show_or_edit(update, f"{ABSENCE_LABELS.get(t, '')}\n\nНа скільки днів, починаючи з сьогодні?",
                           reply_markup=InlineKeyboardMarkup(buttons))
        return PLAN_MENU
    if data.startswith('rem_set_'):
        t, n = data[len('rem_set_'):].rsplit('_', 1)
        until = (kyiv_now().date() + timedelta(days=int(n) - 1)).isoformat()
        new = dict(p, absentType=t, absentUntil=until)
        ms = _change_prefs(uid, new, p, f"Нагадування: {ABSENCE_LABELS.get(t, '')} до {fmt_date(until)}")
        return await render_reminder(update, context, undo_ms=ms)
    if data == 'rem_clear':
        new = dict(p, absentType='', absentUntil='')
        ms = _change_prefs(uid, new, p, "Нагадування: знову на роботі")
        return await render_reminder(update, context, undo_ms=ms)
    return PLAN_MENU

# ── ПРИЧИНА НЕВНЕСЕННЯ ЗВІТУ (кнопки в нагадуванні) ──────────────
def why_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📝 Пояснити, чому немає звіту", callback_data="why_menu")]])

async def why_callback(update, context):
    q = update.callback_query
    if not is_allowed(update.effective_user.id):
        await q.answer("Немає доступу", show_alert=True)
        return
    await q.answer()
    uid = update.effective_user.id
    day = kyiv_now().date().isoformat()
    data = q.data
    if data == 'why_menu':
        left = pending_objects(day)
        if not left:
            await q.edit_message_text("✅ Усі звіти за сьогодні внесено або пояснено.")
            return
        rows = [[InlineKeyboardButton(o.get('name', '—'), callback_data=f"why_obj_{o['id']}")] for o in left]
        await q.edit_message_text("Оберіть об'єкт, по якому немає звіту:", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith('why_obj_'):
        oid = data[len('why_obj_'):]
        rows = [[InlineKeyboardButton(label, callback_data=f"why_r_{oid}_{code}")] for code, label in REASONS.items()]
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="why_menu")])
        await q.edit_message_text("Оберіть причину:", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith('why_r_'):
        oid, code = data[len('why_r_'):].rsplit('_', 1)
        obj = next((o for o in get_objects() if o.get('id') == oid), None)
        name = (obj or {}).get('name', '—')
        doc_id = f"{day}_{oid}"
        fb_set('noReportReasons', doc_id, {'date': day, 'objId': oid, 'objName': name, 'reason': code,
                                           'by': update.effective_user.first_name or '', 'ts': now_stamp()})
        ms = undo_push(uid, f"Причина: {name} — {REASONS.get(code, code)}",
                       [{'op': 'delete', 'col': 'noReportReasons', 'id': doc_id}])
        await q.edit_message_text(f"✅ Записано: {name} — {REASONS.get(code, code)}", reply_markup=undo_markup(ms))

# ── НАДСИЛАННЯ НАГАДУВАНЬ ─────────────────────────────────────
async def _send(bot, uid, text, markup=None):
    try:
        await bot.send_message(chat_id=int(uid), text=text, reply_markup=markup)
    except Exception as e:
        logger.error(f"reminder send error: {e}")

async def reminder_loop(bot):
    # Кожному користувачу — у його часи (типово 17:00 і 18:00), лише в його робочі дні
    # і не під час відпустки/лікарняного. Друге нагадування — тільки якщо є незвітовані об'єкти.
    while True:
        try:
            now = kyiv_now()
            day = now.date().isoformat()
            docs = {str(x.get('id')): x for x in fb_get_all('reminderPrefs')}
            for uid in ALLOWED_IDS:
                p = _prefs_from(docs.get(str(uid), {}))
                if not should_remind(p, now):
                    continue
                if now.hour < p['late_h'] and reminder_due(now, p['earlySent'], p['early_h']):
                    await _send(bot, uid, f"⏰ Час внести щоденний звіт за {fmt_date(day)}.\nНатисніть «📅 Щоденний звіт».")
                    p['earlySent'] = day
                    _save_prefs(uid, p)
                if reminder_due(now, p['lateSent'], p['late_h']):
                    if pending_objects(day):
                        await _send(bot, uid, f"🔔 Звіт за {fmt_date(day)} ще не внесено.\nНатисніть «📅 Щоденний звіт» або вкажіть причину.",
                                    why_markup())
                    p['lateSent'] = day
                    _save_prefs(uid, p)
        except Exception as e:
            logger.error(f"reminder loop error: {e}")
        await asyncio.sleep(600)

async def _post_init(app):
    if os.environ.get('REMIND_ENABLED', '1') != '0':
        asyncio.create_task(reminder_loop(app.bot))

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    init_firebase()
    _patch_markdown_safety_net()

    # Conversation handler
    app.add_handler(CommandHandler('undo', undo_command))
    app.add_handler(CallbackQueryHandler(undo_callback, pattern=r'^undo_\d+$'))
    app.add_handler(CallbackQueryHandler(why_callback, pattern=r'^why_'), group=-1)
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("cancel", flow_cancel_text),
            MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)
        ],
        states=_with_back({
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
                MessageHandler(filters.PHOTO, make_daily_photo_handler(DAILY_DESC)),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler)
            ],
            DAILY_CONFIRM: [
                CallbackQueryHandler(daily_confirm_callback),
                MessageHandler(filters.PHOTO, make_daily_photo_handler(DAILY_CONFIRM)),
            ],
            RECEIPT_PHOTO: [
                CallbackQueryHandler(wiz_callback, pattern=r'^wiz_'),
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
        }),
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
