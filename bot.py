 f is_allowed(user_id):
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

    buttons = [
        [InlineKeyboardButton("📅 Сьогодні", callback_data="date_today"),
         InlineKeyboardButton("📆 Вчора", callback_data="date_yesterday")],
        [InlineKeyboardButton("✏️ Інша дата", callback_data="date_custom")],
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

    buttons = [[InlineKeyboardButton(o['name'], callback_data=f"obj_{o['id']}")] for o in active]
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
            return await ask_materials_msg(update, context)
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

    phase = context.user_data.get('daily_phase', 'workers')
    await update.message.reply_text("⚠️ Скористайтесь кнопками вище, або натисніть відповідну кнопку зі списку.")
    if phase == 'materials':
        return DAILY_MATERIALS
    if phase == 'transport':
        return DAILY_TRANSPORT
    return DAILY_WORKERS

async def ask_workers_msg(update, context, via_callback=False):
    """Show workers screen via message (not callback)"""
    context.user_data['daily_phase'] = 'workers'
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

    if via_callback and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return DAILY_WORKERS

async def ask_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ask_materials_msg(update, context, via_callback=True)

async def ask_materials_msg(update, context, via_callback=False):
    context.user_data['daily_phase'] = 'materials'
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
    buttons.append([InlineKeyboardButton("↩️ Назад до робітників", callback_data="mat_back")])

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
        return await ask_workers_msg(update, context, via_callback=True)
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
        [InlineKeyboardButton("↩️ Назад до матеріалів", callback_data="tr_back")],
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
    if data == 'tr_back':
        return await ask_materials_msg(update, context, via_callback=True)
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
        [InlineKeyboardButton("↩️ Назад до транспорту", callback_data="daily_back")],
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

    if query.data == 'daily_back':
        return await ask_transport_msg(update, context, via_callback=True)

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

def build_receipt_preview_text(receipt):
    items = receipt.get('items', [])

    text = "🧾 *Розпізнано:*\n\n"
    if receipt.get('supplier'):
        text += f"🏪 {receipt['supplier']}\n"
    if receipt.get('date'):
        text += f"📅 {fmt_date(receipt['date'])}\n"
    if receipt.get('docNumber'):
        text += f"🔖 № {receipt['docNumber']}\n"
    text += "\n"

    for item in items:
        text += f"• {item['name']} — {item['qty']} {item.get('unit','шт')} × {fmt_money(item.get('price',0))} = {fmt_money(item.get('amount',0))}\n"

    if receipt.get('total'):
        text += f"\n💰 *Разом: {fmt_money(receipt['total'])}*"

    text += "\n\nКуди записати?"
    return text

def receipt_preview_buttons(receipt):
    docnum_label = "🔖 Змінити № документа" if receipt.get('docNumber') else "🔖 Додати № документа"
    return [
        [InlineKeyboardButton("📦 На склад", callback_data="rec_stock"),
         InlineKeyboardButton("🏗️ На об'єкт", callback_data="rec_object")],
        [InlineKeyboardButton("🧾 В Закупки (без складу)", callback_data="rec_purchase")],
        [InlineKeyboardButton(docnum_label, callback_data="rec_docnum")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="rec_cancel")],
    ]

async def show_receipt_preview(update, context):
    receipt = context.user_data.get('receipt', {})
    text = build_receipt_preview_text(receipt)
    await update.message.reply_text(text, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(receipt_preview_buttons(receipt)))
    return RECEIPT_TARGET

async def receipt_docnum_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User typed the invoice/receipt/check number after tapping '🔖 Додати № документа'"""
    text = (update.message.text or '').strip()
    receipt = context.user_data.get('receipt', {})
    receipt['docNumber'] = text
    context.user_data['receipt'] = receipt
    await update.message.reply_text(build_receipt_preview_text(receipt), parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(receipt_preview_buttons(receipt)))
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
                mat_id = existing['id']
            else:
                mat_id = fb_add('matStock', {
                    'name': item['name'],
                    'qty': item.get('qty', 0),
                    'unit': item.get('unit', 'шт'),
                    'price': item.get('price', 0),
                    'supplier': receipt.get('supplier', ''),
                    'min': 0, 'cat': '', 'sku': '', 'note': ''
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

        await query.edit_message_text(f"✅ Додано на склад: {saved} позицій")
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    if data == 'rec_purchase':
        # Ask which category — doesn't touch matStock/Склад at all
        buttons = [
            [InlineKeyboardButton("📦 Матеріали", callback_data="reccat_materials"),
             InlineKeyboardButton("🚚 Доставка", callback_data="reccat_delivery")],
            [InlineKeyboardButton("🚗 Транспорт", callback_data="reccat_transport"),
             InlineKeyboardButton("📎 Інше", callback_data="reccat_other")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="rec_cancel")],
        ]
        await query.edit_message_text("Оберіть категорію закупівлі:", reply_markup=InlineKeyboardMarkup(buttons))
        return RECEIPT_TARGET

    if data.startswith('reccat_'):
        category = data.replace('reccat_', '')
        cat_labels = {'materials': '📦 Матеріали', 'delivery': '🚚 Доставка', 'transport': '🚗 Транспорт', 'other': '📎 Інше'}
        receipt = context.user_data.get('receipt', {})
        saved = 0
        for item in receipt.get('items', []):
            doc_id = fb_add('purchases', {
                'name': item['name'],
                'category': category,
                'enterpriseId': '',
                'supplier': receipt.get('supplier', ''),
                'supplierPhone': '',
                'date': receipt.get('date') or today_str(),
                'qty': item.get('qty', 0),
                'unit': item.get('unit', 'шт'),
                'price': item.get('price', 0),
                'currency': 'UAH',
                'rate': 1,
                'serial': '',
                'note': f"Бот · чек{(' · ' + receipt.get('supplier')) if receipt.get('supplier') else ''}{doc_number_suffix(receipt)}",
            })
            if doc_id:
                saved += 1

        await query.edit_message_text(f"✅ Додано в Закупки ({cat_labels.get(category, category)}): {saved} позицій")
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
            'desc': f"Закупівля: {receipt.get('supplier','')}{doc_number_suffix(receipt)}",
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
        for s in low[:20]:
           text += f"⚠️ {s['name']} — {s.get('qty',0)} {s.get('unit','шт')} · {fmt_money(s.get('price',0))}\n"

    if ok:
        text += "\n✅ *В нормі:*\n"
        for s in ok[:20]:
            text += f"• {s['name']} — {s.get('qty',0)} {s.get('unit','шт')} · {fmt_money(s.get('price',0))}\n"

    if len(stocks) > 40:
        text += f"\n_...ще {len(stocks)-40} позицій, скористайтесь пошуком або веб-програмою_"

    buttons = [[InlineKeyboardButton("🔍 Пошук на складі", callback_data="stock_search")]]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return STOCK_ACTION

async def stock_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'stock_search':
        await query.edit_message_text("🔍 Введіть частину назви матеріалу:")
        return STOCK_NAME
    return MAIN_MENU

async def stock_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip().lower()
    stocks = fb_get_all('matStock')
    found = [s for s in stocks if q in s.get('name','').lower()]

    if not found:
        await update.message.reply_text(f"❌ Нічого не знайдено за «{update.message.text}»", reply_markup=main_keyboard())
        return MAIN_MENU

    text = f"🔍 *Знайдено ({len(found)}):*\n\n"
    for s in found[:20]:
        text += f"• {s['name']} — {s.get('qty',0)} {s.get('unit','шт')} · {fmt_money(s.get('price',0))}\n"

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

# ── МОЇ ЗАПИСИ (ІСТОРІЯ) ──────────────────────────────────────
def _log_total(l):
    total_wage = sum(w.get('hrs',0)*w.get('rate',0) for w in l.get('workers',[]))
    total_mat = sum(m.get('qty',0)*m.get('price',0) for m in l.get('materials',[]))
    total_tr = sum(t.get('cost',0) for t in l.get('transport',[]))
    return total_wage + total_mat + total_tr

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE, via_callback=False):
    logs = fb_get_all('dailyLogs')
    logs.sort(key=lambda l: l.get('date',''), reverse=True)
    logs = logs[:10]

    if not logs:
        msg = "📜 Записів ще немає"
        if via_callback and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg, reply_markup=main_keyboard())
        return MAIN_MENU

    buttons = []
    for l in logs:
        total = _log_total(l)
        label = f"{fmt_date(l.get('date','—'))} | {l.get('objName','—')} · {fmt_money(total)}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"hist_{l['id']}")])

    text = "📜 *Останні записи* (натисніть для деталей):"
    if via_callback and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return HISTORY_LIST

async def history_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'hist_back':
        return await show_history(update, context, via_callback=True)

    if data.startswith('histdel_'):
        if not is_owner(update.effective_user.id):
            await query.edit_message_text("⛔ Тільки власник може видаляти записи.")
            await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
            return MAIN_MENU
        log_id = data.replace('histdel_', '')
        fb_delete('dailyLogs', log_id)
        await query.edit_message_text("✅ Запис видалено")
        await query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    log_id = data.replace('hist_', '')
    logs = fb_get_all('dailyLogs')
    log = next((l for l in logs if l['id'] == log_id), None)
    if not log:
        await query.edit_message_text("❌ Запис не знайдено")
        return MAIN_MENU

    total = _log_total(log)
    text = f"📅 *{fmt_date(log.get('date','—'))} | {log.get('objName','—')}*\n"
    if log.get('submittedBy'):
        text += f"✍️ Вніс: {log['submittedBy']}\n"
    text += "\n"
    if log.get('workers'):
        text += "👷 *Робітники:*\n"
        for w in log['workers']:
            text += f"  • {w.get('name','')} — {w.get('hrs',0)}год"
            if w.get('rate'):
                text += f" = {fmt_money(w['hrs']*w['rate'])}"
            text += "\n"
    if log.get('materials'):
        text += "\n📦 *Матеріали:*\n"
        for m in log['materials']:
            text += f"  • {m.get('name','')} — {m.get('qty',0)} {m.get('unit','шт')}"
            if m.get('price'):
                text += f" x {fmt_money(m['price'])} = {fmt_money(m['qty']*m['price'])}"
            text += "\n"
    if log.get('transport'):
        text += "\n🚛 *Транспорт:*\n"
        for t in log['transport']:
            text += f"  • {t.get('name','')} — {fmt_money(t.get('cost',0))}\n"
    if log.get('desc'):
        text += f"\n📝 {log['desc']}\n"
    text += f"\n💰 *РАЗОМ: {fmt_money(total)}*"

    buttons = [[InlineKeyboardButton("🔙 До списку", callback_data="hist_back")]]
    if is_owner(update.effective_user.id):
        buttons.insert(0, [InlineKeyboardButton("🗑 Видалити цей запис", callback_data=f"histdel_{log_id}")])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return HISTORY_LIST

# ── ОБ'ЄКТИ ──────────────────────────────────────────────────
async def show_objects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objects = get_objects()

    text = "🏗️ *Об'єкти:*\n\n"
    if not objects:
        text = "🏗️ Об'єктів немає\n\n"
    for o in objects:
        status = {'active':'🟢','plan':'🔵','done':'✅','pause':'⏸️'}.get(o.get('status',''),'⚪')
        text += f"{status} *{o['name']}*\n"
        if o.get('client'): text += f"   👤 {o['client']}\n"
        if o.get('contract'): text += f"   💰 {fmt_money(o['contract'])}\n"

    buttons = [[InlineKeyboardButton("➕ Додати об'єкт", callback_data="addobj_start")]]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return OBJECTS_ACTION

async def objects_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'addobj_start':
        await query.edit_message_text("✏️ Введіть назву нового об'єкта:")
        context.user_data['awaiting'] = 'new_obj_name'
        context.user_data['new_obj'] = {}
        return OBJ_INPUT
    return MAIN_MENU

async def skip_new_obj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get('awaiting', '')
    if awaiting == 'new_obj_client':
        context.user_data['awaiting'] = 'new_obj_contract'
        await update.message.reply_text("💰 Сума договору, ₴ (число, або /skip якщо невідомо):")
        return OBJ_INPUT
    if awaiting == 'new_obj_contract':
        return await save_new_object(update, context)
    return OBJ_INPUT

async def save_new_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = context.user_data.get('new_obj', {})
    obj.setdefault('status', 'active')
    obj.setdefault('client', '')
    obj.setdefault('contract', 0)
    doc_id = fb_add('objects', obj)
    context.user_data.pop('new_obj', None)
    context.user_data.pop('awaiting', None)
    if doc_id:
        await update.message.reply_text(f"✅ Об'єкт «{obj['name']}» додано!", reply_markup=main_keyboard())
    else:
        await update.message.reply_text("❌ Помилка збереження. Перевірте Firebase.", reply_markup=main_keyboard())
    return MAIN_MENU

async def new_object_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    awaiting = context.user_data.get('awaiting', '')

    if awaiting == 'new_obj_name':
        if not text:
            await update.message.reply_text("❌ Назва не може бути порожньою. Введіть назву об'єкта:")
            return OBJ_INPUT
        context.user_data['new_obj']['name'] = text
        context.user_data['awaiting'] = 'new_obj_client'
        await update.message.reply_text("👤 Ім'я клієнта/контактної особи (або /skip):")
        return OBJ_INPUT

    if awaiting == 'new_obj_client':
        context.user_data['new_obj']['client'] = text
        context.user_data['awaiting'] = 'new_obj_contract'
        await update.message.reply_text("💰 Сума договору, ₴ (число, або /skip якщо невідомо):")
        return OBJ_INPUT

    if awaiting == 'new_obj_contract':
        try:
            contract = float(text.replace(',', '.').replace(' ', ''))
        except:
            contract = 0
        context.user_data['new_obj']['contract'] = contract
        return await save_new_object(update, context)

    return OBJ_INPUT

# ── ДОПОМОГА ──────────────────────────────────────────────────
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Як користуватись ботом:*\n\n"
        "📅 *Щоденний звіт* — оберіть дату (сьогодні/вчора/іншу), потім вносьте хто працював, матеріали, транспорт. На кожному кроці можна натиснути «↩️ Назад», якщо треба щось виправити\n\n"
        "🧾 *Фото чека* — сфотографуйте чек або накладну, бот розпізнає і запише\n\n"
        "📦 *Склад* — перегляд залишків матеріалів, є пошук за назвою\n\n"
        "📊 *Звіт* — витрати та прибуток по об'єкту\n\n"
        "📜 *Мої записи* — останні 10 записів, можна переглянути деталі або видалити\n\n"
        "📆 *Планування* — додайте нагадування чи план на дату, і бот сам надішле список о 8:00 ранку в день події\n\n"
        "💡 *Порада:* Всі дані синхронізуються з веб-програмою автоматично",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )

# ── СКАСУВАННЯ ───────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Скасовано", reply_markup=main_keyboard())
    return MAIN_MENU

# ── ГОЛОВНА ФУНКЦІЯ ───────────────────────────────────────────
# ── ПЛАНУВАННЯ (НАГАДУВАННЯ ТА ПЛАНИ) ─────────────────────────
async def start_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton("➕ Додати нагадування", callback_data="plan_add")],
        [InlineKeyboardButton("📋 Найближчі", callback_data="plan_list")],
    ]
    await update.message.reply_text(
        "📆 *Планування*\n\nДодайте нагадування чи план на дату — вранці о 8:00 в день події бот надішле список.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return PLAN_MENU

async def plan_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'plan_add':
        await query.edit_message_text("✏️ Введіть текст нагадування чи плану:")
        return PLAN_TEXT
    if query.data == 'plan_list':
        return await show_plan_list(update, context, via_callback=True)
    return PLAN_MENU

async def plan_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("✏️ Введіть текст нагадування чи плану:")
        return PLAN_TEXT
    context.user_data['pending_plan'] = {'name': text}
    buttons = [
        [InlineKeyboardButton("📅 Сьогодні", callback_data="plandate_today"),
         InlineKeyboardButton("📆 Завтра", callback_data="plandate_tomorrow")],
        [InlineKeyboardButton("✏️ Інша дата", callback_data="plandate_custom")],
    ]
    await update.message.reply_text("🗓 На яку дату?", reply_markup=InlineKeyboardMarkup(buttons))
    return PLAN_DATE

async def plan_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == 'plandate_today':
        return await ask_plan_time(update, context, today_str(), via_callback=True)
    if data == 'plandate_tomorrow':
        d = (date.today() + timedelta(days=1)).isoformat()
        return await ask_plan_time(update, context, d, via_callback=True)
    if data == 'plandate_custom':
        await query.edit_message_text("✏️ Введіть дату у форматі ДД.ММ.РРРР (наприклад: 28.09.2026):")
        context.user_data['awaiting_plan_date'] = True
        return PLAN_DATE
    return PLAN_DATE

async def plan_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_plan_date'):
        await update.message.reply_text("⚠️ Скористайтесь кнопками вище.")
        return PLAN_DATE
    text = update.message.text.strip()
    try:
        parts = text.replace('/', '.').replace('-', '.').split('.')
        if len(parts) != 3:
            raise ValueError
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        chosen = date(y, m, d).isoformat()
        context.user_data.pop('awaiting_plan_date', None)
        return await ask_plan_time(update, context, chosen, via_callback=False)
    except Exception:
        await update.message.reply_text("❌ Формат дати: ДД.ММ.РРРР (наприклад: 28.09.2026)")
        return PLAN_DATE

async def ask_plan_time(update, context, chosen_date, via_callback):
    context.user_data.setdefault('pending_plan', {})['date'] = chosen_date
    buttons = [[InlineKeyboardButton("⏭ Без часу", callback_data="plantime_skip")]]
    msg = "🕐 Час (наприклад 14:30) — або натисніть «Без часу»:"
    if via_callback:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
    return PLAN_TIME

async def plan_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'plantime_skip':
        return await save_plan_event(update, context, '', via_callback=True)
    return PLAN_TIME

async def plan_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    text = update.message.text.strip()
    if re.match(r'^\d{1,2}:\d{2}$', text):
        return await save_plan_event(update, context, text, via_callback=False)
    await update.message.reply_text("❌ Формат часу: ГГ:ХХ, наприклад 14:30 — або натисніть «Без часу» вище")
    return PLAN_TIME

async def save_plan_event(update, context, time_str, via_callback):
    plan = context.user_data.pop('pending_plan', {})
    if not plan.get('name') or not plan.get('date'):
        text = "❌ Щось пішло не так, спробуйте додати нагадування ще раз."
    else:
        plan['time'] = time_str
        plan.setdefault('type', 'other')
        plan.setdefault('objId', '')
        plan.setdefault('priority', 'normal')
        plan.setdefault('note', '')
        fb_add('planEvents', plan)
        text = f"✅ Додано: {plan.get('name')}\n📅 {fmt_date(plan.get('date',''))}" + (f" о {time_str}" if time_str else "")
    if via_callback:
        await update.callback_query.edit_message_text(text)
        await update.callback_query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=main_keyboard())
    return MAIN_MENU

async def show_plan_list(update: Update, context: ContextTypes.DEFAULT_TYPE, via_callback=False):
    events = fb_get_all('planEvents')
    todaystr = today_str()
    upcoming = sorted(
        [e for e in events if e.get('date', '') >= todaystr],
        key=lambda e: (e.get('date', ''), e.get('time', '') or '99:99')
    )[:15]
    if not upcoming:
        text = "📋 Немає запланованих нагадувань чи планів."
    else:
        lines = ["📋 *Найближчі плани:*\n"]
        for e in upcoming:
            t = f" о {e['time']}" if e.get('time') else ""
            lines.append(f"• {fmt_date(e.get('date',''))}{t} — {e.get('name','')}")
        text = "\n".join(lines)
    if via_callback:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown')
        await update.callback_query.message.reply_text("Головне меню:", reply_markup=main_keyboard())
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

async def morning_plan_digest(context: ContextTypes.DEFAULT_TYPE):
    """Щоранку о 8:00 надсилає список нагадувань/планів, зустрічей на сьогодні
    та попередження про матеріали, яких бракує на складі"""
    settings = fb_get_all('settings')
    admin = next((s for s in settings if s['id'] == 'admin'), None)
    if not admin or not admin.get('chat_id'):
        return
    todaystr = today_str()
    events = [e for e in fb_get_all('planEvents') if e.get('date') == todaystr]
    meets = [m for m in fb_get_all('meetings') if m.get('date') == todaystr]
    low_stock = [m for m in fb_get_all('matStock') if (m.get('qty') or 0) < (m.get('min') or 0)]

    if not events and not meets and not low_stock:
        return

    lines = [f"☀️ Доброго ранку! ({fmt_date(todaystr)})\n"]

    if events or meets:
        items = sorted(
            [{'time': e.get('time', ''), 'name': e.get('name', ''), 'icon': '📌'} for e in events] +
            [{'time': m.get('time', ''), 'name': m.get('name', ''), 'icon': '🤝'} for m in meets],
            key=lambda x: x['time'] or '99:99'
        )
        lines.append("📆 *На сьогодні:*")
        for it in items:
            t = f"{it['time']} — " if it['time'] else ""
            lines.append(f"{it['icon']} {t}{it['name']}")

    if low_stock:
        if events or meets:
            lines.append("")
        lines.append("⚠️ *Закінчується на складі:*")
        for m in sorted(low_stock, key=lambda x: x.get('name', '')):
            need = max((m.get('min') or 0) - (m.get('qty') or 0), 0)
            lines.append(f"📦 {m.get('name','')} — залишок {m.get('qty',0)} {m.get('unit','')} (докупити ~{need} {m.get('unit','')})")

    try:
        await context.bot.send_message(chat_id=admin['chat_id'], text="\n".join(lines), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Morning digest send error: {e}")

async def meeting_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Двічі на день (о 9:00 і о 18:00) нагадує про заплановані зустрічі на
    найближчі 2 дні. Нагадування само зупиняється, щойно дата зустрічі мине
    або зустріч видалять (скасують) у програмі — окремого статусу 'скасовано'
    не потрібно, досить прибрати зустріч зі "Зустрічі" у веб-додатку."""
    settings = fb_get_all('settings')
    admin = next((s for s in settings if s['id'] == 'admin'), None)
    if not admin or not admin.get('chat_id'):
        return
    today = date.today()
    upcoming = []
    for m in fb_get_all('meetings'):
        d = m.get('date')
        if not d:
            continue
        try:
            md = datetime.strptime(d, '%Y-%m-%d').date()
        except ValueError:
            continue
        delta = (md - today).days
        if 0 <= delta <= 2:
            upcoming.append((delta, m))
    if not upcoming:
        return
    upcoming.sort(key=lambda x: (x[0], x[1].get('time') or '99:99'))
    lines = ["🔔 *Нагадування про заплановані зустрічі:*\n"]
    for delta, m in upcoming:
        when = "сьогодні" if delta == 0 else ("завтра" if delta == 1 else fmt_date(m.get('date', '')))
        t = f" о {m['time']}" if m.get('time') else ""
        place = f" · {m['place']}" if m.get('place') else ""
        lines.append(f"🤝 {when}{t} — {m.get('name', '')}{place}")
    lines.append("\n_Якщо зустріч скасована — видаліть її в програмі (розділ «Зустрічі»), і нагадування припиняться._")
    try:
        await context.bot.send_message(chat_id=admin['chat_id'], text="\n".join(lines), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Meeting reminder send error: {e}")

async def send_backup(context: ContextTypes.DEFAULT_TYPE, chat_id=None):
    """Формує JSON-резервну копію всіх колекцій Firestore і надсилає документом у Telegram."""
    if not db:
        return
    if chat_id is None:
        settings = fb_get_all('settings')
        admin = next((s for s in settings if s['id'] == 'admin'), None)
        if not admin or not admin.get('chat_id'):
            return
        chat_id = admin['chat_id']
    try:
        backup = {}
        total_docs = 0
        for col_ref in db.collections():
            col_data = {}
            for d in col_ref.stream():
                col_data[d.id] = d.to_dict()
                total_docs += 1
            backup[col_ref.id] = col_data
        payload = json.dumps(backup, ensure_ascii=False, indent=2, default=str)
        bio = BytesIO(payload.encode('utf-8'))
        fname = f"backup_{today_str()}.json"
        bio.name = fname
        await context.bot.send_document(
            chat_id=chat_id,
            document=bio,
            filename=fname,
            caption=f"🗄️ Резервна копія бази даних · {fmt_date(today_str())}\nКолекцій: {len(backup)} · Документів: {total_docs}"
        )
    except Exception as e:
        logger.error(f"Backup send error: {e}")

async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Щоденна автоматична резервна копія (о 02:00) — надсилається адміну."""
    await send_backup(context)

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /backup — миттєва резервна копія на вимогу."""
    await update.message.reply_text("🗄️ Формую резервну копію...")
    await send_backup(context, chat_id=update.effective_chat.id)

# ── СИНХРОНІЗАЦІЯ З GOOGLE-ТАБЛИЦЕЮ "Облік_продажу" ───────────
def _parse_ua_number(s):
    """Перетворює число у форматі '10 454,40' або '96,80' на float."""
    if s is None:
        return 0.0
    s = str(s).strip().replace('\xa0', '').replace(' ', '').replace(',', '.')
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

def _parse_ua_date(s):
    """Перетворює дату 'ДД.ММ.РРРР' на 'РРРР-ММ-ДД'. Порожнє або незрозуміле — повертає ''."""
    s = (s or '').strip()
    if not s:
        return ''
    parts = s.split('.')
    if len(parts) == 3:
        d, m, y = parts
        if len(y) == 2:
            y = '20' + y
        try:
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            return ''
    return ''

def _currency_from_sheet(s):
    s = (s or '').strip()
    return 'USD' if '$' in s else 'UAH'

def _fetch_sheet_rows(gid):
    """Завантажує аркуш Google-таблиці (за gid) як список словників (csv.DictReader)."""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    text = raw.decode('utf-8-sig')
    return list(csv.DictReader(io.StringIO(text)))

async def sync_from_sheets(context: ContextTypes.DEFAULT_TYPE, chat_id=None, manual=False):
    """Підтягує нові закупівлі/продажі з Google-таблиці "Облік_продажу" в Firestore.
    Записи, які вже перенесені раніше (за excelId), пропускаються — тому безпечно
    викликати повторно скільки завгодно."""
    if not db:
        return
    try:
        purchase_rows = _fetch_sheet_rows(SHEET_GID_PURCHASES)
        sale_rows = _fetch_sheet_rows(SHEET_GID_SALES)
    except Exception as e:
        logger.error(f"Sync fetch error: {e}")
        if manual and chat_id:
            await context.bot.send_message(chat_id, f"❌ Не вдалося завантажити таблицю: {e}")
        return

    existing_purchases = fb_get_all('purchases')
    existing_sales = fb_get_all('sales')
    excel_to_fsid = {p.get('excelId'): p['id'] for p in existing_purchases if p.get('excelId')}
    existing_purchase_eids = set(excel_to_fsid.keys())
    existing_sale_eids = {s.get('excelId') for s in existing_sales if s.get('excelId')}
    # eid -> {fsid, rate, currency, price} — джерело правди для собівартості продажу,
    # бо в аркуші "Продажі" курс/валюта закупівлі не завжди продубльовані коректно
    purchase_info = {
        p.get('excelId'): {'fsid': p['id'], 'rate': p.get('rate') or 1,
                            'currency': p.get('currency') or 'UAH', 'price': p.get('price') or 0}
        for p in existing_purchases if p.get('excelId')
    }

    added_purchases = 0
    for row in purchase_rows:
        eid = (row.get('№ закупки') or '').strip()
        name = (row.get('Найменування') or '').strip()
        if not eid or not name or eid in existing_purchase_eids:
            continue
        qty = _parse_ua_number(row.get('К-ть закуплено'))
        if qty <= 0:
            continue
        p_price = _parse_ua_number(row.get('Ціна закупки (за од.)'))
        p_currency = _currency_from_sheet(row.get('Валюта закупки'))
        p_rate = _parse_ua_number(row.get('Курс на дату закупки')) or 1
        doc_id = fb_add('purchases', {
            'name': name,
            'supplier': (row.get('Постачальник') or '').strip(),
            'supplierPhone': (row.get('Контакт постачальника') or '').strip(),
            'date': _parse_ua_date(row.get('Дата закупки')) or today_str(),
            'qty': qty,
            'unit': (row.get('Од. вим.') or 'шт').strip() or 'шт',
            'price': p_price,
            'currency': p_currency,
            'rate': p_rate,
            'note': (row.get('№ накладної/чека') or '').strip(),
            'excelId': eid,
            'source': 'excel-sync',
        })
        if doc_id:
            excel_to_fsid[eid] = doc_id
            existing_purchase_eids.add(eid)
            purchase_info[eid] = {'fsid': doc_id, 'rate': p_rate, 'currency': p_currency, 'price': p_price}
            added_purchases += 1

    added_sales = 0
    for row in sale_rows:
        eid = (row.get('№ продажу') or '').strip()
        name = (row.get('Найменування') or '').strip()
        if not eid or not name or eid in existing_sale_eids:
            continue
        qty = _parse_ua_number(row.get('К-ть продано'))
        if qty <= 0:
            continue
        purchase_eid = (row.get('№ закупки') or '').strip()
        pinfo = purchase_info.get(purchase_eid)
        purchase_fsid = pinfo['fsid'] if pinfo else ''
        sale_currency = _currency_from_sheet(row.get('Валюта продажу'))
        sale_rate = _parse_ua_number(row.get('Курс на дату продажу'))
        if sale_rate <= 0:
            # У рядку продажу курс не заповнено — якщо валюта та сама, що й у партії
            # закупівлі, беремо курс звідти (найімовірніше та сама угода), інакше 1
            sale_rate = pinfo['rate'] if (pinfo and pinfo['currency'] == sale_currency) else 1
        # Собівартість завжди беремо з самої партії закупівлі (джерело правди),
        # а не з можливо порожніх/неточних дубльованих колонок в аркуші "Продажі"
        if pinfo:
            cost_price, cost_currency, cost_rate = pinfo['price'], pinfo['currency'], pinfo['rate']
        else:
            cost_price = _parse_ua_number(row.get('Ціна закупки (за од.)'))
            cost_currency = _currency_from_sheet(row.get('Валюта закупки'))
            cost_rate = sale_rate
        paid_raw = (row.get('Статус оплати') or '').strip().lower()
        paid = paid_raw in ('так', 'оплачено', 'paid', '✓', '+', 'оплата')
        note_bits = []
        inv_ref = (row.get('№ накладної/чека') or '').strip()
        if inv_ref:
            note_bits.append(f"Накладна (Excel): {inv_ref}")
        doc_id = fb_add('sales', {
            'purchaseId': purchase_fsid,
            'name': name,
            'buyer': (row.get('Покупець') or '').strip(),
            'buyerContact': (row.get('Контакт покупця') or '').strip(),
            'siteAddress': (row.get("Об'єкт/адреса монтажу") or '').strip(),
            'objId': '',
            'date': _parse_ua_date(row.get('Дата продажу')) or today_str(),
            'warrantyUntil': _parse_ua_date(row.get('Гарантія до')),
            'qty': qty,
            'price': _parse_ua_number(row.get('Ціна продажна (за од.)')),
            'currency': sale_currency,
            'rate': sale_rate,
            'costPrice': cost_price,
            'costCurrency': cost_currency,
            'costRate': cost_rate,
            'paid': paid,
            'invoiceId': '',
            'note': ' · '.join(note_bits),
            'excelId': eid,
            'source': 'excel-sync',
        })
        if doc_id:
            existing_sale_eids.add(eid)
            added_sales += 1

    if added_purchases or added_sales or manual:
        msg = f"🔄 Синхронізація Excel → програма: +{added_purchases} закупівель, +{added_sales} продажів"
        logger.info(msg)
        if chat_id:
            await context.bot.send_message(chat_id, msg)
        elif OWNER_ID and (added_purchases or added_sales):
            try:
                await context.bot.send_message(int(OWNER_ID), msg)
            except Exception as e:
                logger.error(f"Sync notify error: {e}")

async def sync_sheets_job(context: ContextTypes.DEFAULT_TYPE):
    """Періодична автоматична синхронізація (кожні 15 хв)."""
    await sync_from_sheets(context)

async def sync_sheets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /syncsheets — синхронізація на вимогу."""
    await update.message.reply_text("🔄 Перевіряю таблицю на нові записи...")
    await sync_from_sheets(context, chat_id=update.effective_chat.id, manual=True)

async def check_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Щовечора перевіряє, чи внесено хоч один звіт сьогодні, і нагадує, якщо ні"""
    settings = fb_get_all('settings')
    admin = next((s for s in settings if s['id'] == 'admin'), None)
    if not admin or not admin.get('chat_id'):
        return
    logs = fb_get_all('dailyLogs')
    today_logs = [l for l in logs if l.get('date') == today_str()]
    if today_logs:
        return
    try:
        await context.bot.send_message(
            chat_id=admin['chat_id'],
            text="⏰ Нагадування: сьогодні ще не внесено жодного щоденного звіту!"
        )
    except Exception as e:
        logger.error(f"Reminder send error: {e}")

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
            SELECT_DATE: [
                CallbackQueryHandler(date_callback, pattern='^date_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, daily_text_handler),
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
            RECEIPT_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_docnum_handler),
            ],
            RECEIPT_TARGET: [
                CallbackQueryHandler(receipt_target_callback, pattern='^rec'),
            ],
            STOCK_ACTION: [
                CallbackQueryHandler(stock_action_callback, pattern='^stock_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            ],
            STOCK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stock_search_handler),
            ],
            HISTORY_LIST: [
                CallbackQueryHandler(history_detail_callback, pattern='^hist'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            ],
            OBJECTS_ACTION: [
                CallbackQueryHandler(objects_action_callback, pattern='^addobj'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            ],
            OBJ_INPUT: [
                CommandHandler('skip', skip_new_obj),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_object_text_handler),
            ],
            PLAN_MENU: [
                CallbackQueryHandler(plan_menu_callback, pattern='^plan_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            ],
            PLAN_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_text_input),
            ],
            PLAN_DATE: [
                CallbackQueryHandler(plan_date_callback, pattern='^plandate_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_date_text),
            ],
            PLAN_TIME: [
                CallbackQueryHandler(plan_time_callback, pattern='^plantime_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_time_text),
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
    # Ручна резервна копія на вимогу
    app.add_handler(CommandHandler('backup', backup_command))
    # Ручна синхронізація з Google-таблицею на вимогу
    app.add_handler(CommandHandler('syncsheets', sync_sheets_command))

    if app.job_queue:
        app.job_queue.run_daily(check_daily_reminder, time=dtime(hour=20, minute=0))
        app.job_queue.run_daily(morning_plan_digest, time=dtime(hour=8, minute=0))
        app.job_queue.run_daily(meeting_reminder_job, time=dtime(hour=9, minute=0))
        app.job_queue.run_daily(meeting_reminder_job, time=dtime(hour=18, minute=0))
        app.job_queue.run_daily(daily_backup_job, time=dtime(hour=2, minute=0))
        app.job_queue.run_repeating(sync_sheets_job, interval=900, first=60)
    else:
        logger.error("JobQueue недоступний — нагадування вимкнено (потрібен пакет python-telegram-bot[job-queue])")

    logger.info("Бот запущено! ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()


