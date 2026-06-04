import os
import json
import logging
import httpx
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
MAKE_WEBHOOK   = os.environ["MAKE_WEBHOOK_URL"]
MAKE_CHECK_URL = os.environ["MAKE_CHECK_URL"]
MAKE_UPDATE_URL = os.environ["MAKE_UPDATE_URL"]
MAKE_UPDATE_URL = os.environ["MAKE_UPDATE_URL"]

REQUIRED_FIELDS = {
    "company_name":    "Название компании",
    "inn":             "ИНН",
    "payment_purpose": "Назначение платежа",
    "amount_range":    "Сумма (от и до)",
    "bank":            "Банк",
    "vat_rate":        "Ставка НДС",
    "cash_rate":       "Ставка по кэшу (%)",
    "conditions":      "Условия (сроки, документы)",
}

ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SYSTEM_PROMPT = """Ты — эксперт по извлечению данных о компаниях из сообщений.

ГЛАВНОЕ ПРАВИЛО: Записывай ТОЛЬКО то, что ЯВНО написано в тексте.
НИКОГДА не додумывай, не предполагай, не интерпретируй расширительно.
Если данных нет или ты не уверен — ставь null.

ПРАВИЛА ПО ПОЛЯМ:
- company_name: только официальное название юрлица (ООО, ИП, АО). Если не указано — null.
- inn: строго 10 или 12 цифр. ОГРН (13-15 цифр) — НЕ ИНН. Телефон — НЕ ИНН. Сомневаешься — null.
- payment_purpose: только явно указанный товар/услуга. Не угадывай по ОКВЭД.
- amount_range: только явно указанная сумма. кк=млн, к=тыс.
- bank: только явно названный банк.
- vat_rate: ставка НДС. Приводи к одному из значений: 22%, 10%, 0%, БЕЗ НДС, БЕЗ ОТЧЕТ. Например: "с ндс 22" → "22%", "без ндс" → "БЕЗ НДС", "без отчёта" → "БЕЗ ОТЧЕТ", "нал" или "кэш без отчёта" → "БЕЗ ОТЧЕТ". Если не указано — null.
- cash_rate: только явно указанный процент комиссии/кэша.
- conditions: только явно написанные условия, сроки, документы.
- issue_date: только явно указанный срок выдачи.
- cash_location: только явно указанное место/способ выдачи.
- zsk_color: Зелёный если "принимают от зелёных" или явно указан зелёный ЗСК. Иначе null.
- company_type: только если явно написано. Иначе null.
- white_business: только если явно указано. Иначе null.
- comment: только важная информация явно присутствующая в тексте.

НЕ ДОБАВЛЯЙ поля executor и source.
В сообщении может быть НЕСКОЛЬКО компаний — извлеки ВСЕ.

Возвращай СТРОГО ТОЛЬКО валидный JSON-массив без пояснений:
[{"company_name":null,"inn":null,"payment_purpose":null,"amount_range":null,"bank":null,"vat_rate":null,"cash_rate":null,"conditions":null,"relevance":"Актуально","company_type":null,"white_business":null,"zsk_color":null,"issue_date":null,"cash_location":null,"comment":null}]"""

# ─── Make webhooks ────────────────────────────────────────────────────────────
async def check_duplicate(inn: str, company_name: str) -> dict:
    """Проверяет дубль в таблице через Make."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                MAKE_CHECK_URL,
                json={"inn": inn, "company_name": company_name},
                timeout=15
            )
            if r.status_code == 200:
                text = r.text.strip()
                logger.info(f"Check duplicate response: {text[:200]}")
                # Не найдено — Make возвращает разный мусор
                if not text or text in ("Accepted", "1", "0"):
                    return {"found": False}
                # Make иногда добавляет мусор после JSON: {"found":true}0
                # Берём только первый валидный JSON объект
                import re as _re
                m = _re.search(r'\{.*?\}', text, _re.DOTALL)
                if not m:
                    return {"found": False}
                data = json.loads(m.group(0))
                # Если found не установлен или нет данных — не дубль
                if not data.get("found"):
                    return {"found": False}
                # Если row пустой — дубль есть но номер строки неизвестен
                row = str(data.get("row", "")).strip()
                data["row"] = int(row) if row.isdigit() else None
                return data
    except Exception as e:
        logger.error(f"Check duplicate error: {e}")
    return {"found": False}

async def send_to_make(data: dict, source: str, executor: str) -> bool:
    payload = {
        "updated_at":      datetime.now().strftime("%d.%m.%Y %H:%M"),
        "relevance":       data.get("relevance") or "Актуально",
        "company_type":    data.get("company_type") or "",
        "white_business":  data.get("white_business") or "",
        "company_name":    data.get("company_name") or "",
        "inn":             data.get("inn") or "",
        "zsk_color":       data.get("zsk_color") or "",
        "amount_range":    data.get("amount_range") or "",
        "payment_purpose": data.get("payment_purpose") or "",
        "bank":            data.get("bank") or "",
        "vat_rate":        data.get("vat_rate") or "",
        "cash_rate":       data.get("cash_rate") or "",
        "issue_date":      data.get("issue_date") or "",
        "cash_location":   data.get("cash_location") or "",
        "comment":         data.get("comment") or "",
        "executor":        executor,
        "website":         data.get("website") or "",
        "source":          source,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(MAKE_WEBHOOK, json=payload, timeout=15)
        return r.status_code == 200


async def update_in_make(data: dict, source: str, executor: str, row: int) -> bool:
    payload = {
        "row":             row,
        "updated_at":      datetime.now().strftime("%d.%m.%Y %H:%M"),
        "relevance":       data.get("relevance") or "Актуально",
        "company_type":    data.get("company_type") or "",
        "white_business":  data.get("white_business") or "",
        "company_name":    data.get("company_name") or "",
        "inn":             data.get("inn") or "",
        "zsk_color":       data.get("zsk_color") or "",
        "amount_range":    data.get("amount_range") or "",
        "payment_purpose": data.get("payment_purpose") or "",
        "bank":            data.get("bank") or "",
        "vat_rate":        data.get("vat_rate") or "",
        "cash_rate":       data.get("cash_rate") or "",
        "issue_date":      data.get("issue_date") or "",
        "cash_location":   data.get("cash_location") or "",
        "comment":         data.get("comment") or "",
        "executor":        executor,
        "website":         data.get("website") or "",
        "source":          source,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(MAKE_UPDATE_URL, json=payload, timeout=15)
        logger.info(f"update_in_make response: {r.status_code} {r.text[:100]}")
        return r.status_code in (200, 201, 202, 204)

# ─── Claude парсинг ───────────────────────────────────────────────────────────
def clean_json(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r'```(?:json)?', '', raw).replace('```', '').strip()
    for m in re.finditer(r'\[', raw):
        start = m.start()
        depth = 0
        for i, ch in enumerate(raw[start:]):
            if ch == '[': depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    candidate = raw[start:start+i+1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except Exception:
                        break
    for m in re.finditer(r'\{', raw):
        start = m.start()
        depth = 0
        for i, ch in enumerate(raw[start:]):
            if ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = raw[start:start+i+1]
                    try:
                        json.loads(candidate)
                        return f"[{candidate}]"
                    except Exception:
                        break
    return raw

def parse_with_claude(text: str, extra: str = "") -> list[dict]:
    full = text + (f"\n\nДополнение от пользователя:\n{extra}" if extra else "")
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": full}],
    )
    raw = response.content[0].text
    cleaned = clean_json(raw)
    result = json.loads(cleaned)
    if isinstance(result, dict):
        return [result]
    return result

def get_missing(data: dict) -> list:
    return [k for k in REQUIRED_FIELDS if not data.get(k) or str(data.get(k)).lower() == "null"]

# ─── Форматирование ───────────────────────────────────────────────────────────
FIELD_EMOJI = {
    "company_name":    "🏢 Компания",
    "inn":             "🔢 ИНН",
    "payment_purpose": "💳 Назначение платежа",
    "amount_range":    "💰 Сумма",
    "bank":            "🏦 Банк",
    "vat_rate":        "📊 Ставка НДС",
    "cash_rate":       "📈 Ставка по кэшу",
    "conditions":      "📋 Условия",
    "relevance":       "🔔 Актуальность",
    "company_type":    "🏷 Тип компании",
    "white_business":  "✅ Белый бизнес",
    "zsk_color":       "🟢 Цвет ЗСК",
    "issue_date":      "📅 Срок выдачи",
    "cash_location":   "📍 Выдача кэша",
    "comment":         "💬 Комментарий",
}

def format_company(data: dict, index: int = None) -> str:
    prefix = f"*Компания {index}*\n" if index else ""
    lines = [prefix + "*Извлечённые данные:*\n"]
    for key, label in FIELD_EMOJI.items():
        val = data.get(key)
        if val and str(val).lower() != "null":
            lines.append(f"{label}: {val}")
    return "\n".join(lines)

def format_missing_msg(missing: list) -> str:
    lines = ["❓ *Не хватает данных:*\n"]
    for k in missing:
        lines.append(f"  • {REQUIRED_FIELDS[k]}")
    lines.append("\nДопиши или нажми кнопку.")
    return "\n".join(lines)

def format_duplicate_msg(data: dict, dup: dict) -> str:
    company = data.get("company_name", "")
    inn = data.get("inn", "")
    dup_source = dup.get("source", "неизвестно")
    dup_date = dup.get("updated_at", "неизвестно")
    dup_name = dup.get("company_name", company)
    return (
        f"⚠️ *Найден дубль!*\n\n"
        f"🏢 {dup_name}\n"
        f"🔢 ИНН: {inn}\n"
        f"📢 Источник: {dup_source}\n"
        f"📅 Дата: {dup_date}\n\n"
        f"Что делаем?"
    )

# ─── Сессии ───────────────────────────────────────────────────────────────────
sessions: dict = {}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Привет!*\n\n"
        "Пересылай мне сообщения с заявками.\n\n"
        "Я буду:\n"
        "• Извлекать данные из любого формата\n"
        "• Проверять дубли по ИНН\n"
        "• Спрашивать источник и исполнителя\n"
        "• Записывать в Google Sheets 📊",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message
    text = (msg.text or msg.caption or "").strip()

    if not text:
        await msg.reply_text("⚠️ Пустое сообщение.")
        return

    session = sessions.get(user_id)

    # ── Ввод источника ────────────────────────────────────────────────────────
    if session and session.get("stage") == "ask_source":
        session["source"] = text
        session["stage"] = "ask_executor"
        await msg.reply_text(
            "👤 *Исполнитель* (@username или имя)\nили `-` если нет:",
            parse_mode="Markdown"
        )
        return

    # ── Ввод исполнителя ──────────────────────────────────────────────────────
    if session and session.get("stage") == "ask_executor":
        session["executor"] = "" if text == "-" else text
        await _save_current(update, session, user_id)
        return

    # ── Уточнение данных ──────────────────────────────────────────────────────
    if session and session.get("stage") == "clarify":
        idx = session["current_idx"]
        session["extra"] = (session.get("extra") or "") + "\n" + text
        try:
            updated_list = parse_with_claude(session["original_text"], session["extra"])
            updated = updated_list[idx] if idx < len(updated_list) else updated_list[0]
            for k, v in updated.items():
                if v and str(v).lower() != "null":
                    session["pending"][idx][k] = v
        except Exception as e:
            logger.error(f"Clarification error: {e}")
            await msg.reply_text("⚠️ Ошибка. Попробуй ещё раз.")
            return
        await _process_current(update, session, user_id)
        return

    # ── Новое сообщение ───────────────────────────────────────────────────────
    proc = await msg.reply_text("⏳ Анализирую...")
    try:
        companies = parse_with_claude(text)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        await proc.edit_text(
            "⚠️ Не удалось распознать структуру.\n\n"
            "Попробуй переслать ещё раз или напиши:\n"
            "Компания: ...\nИНН: ...\nНазначение: ..."
        )
        return

    await proc.delete()

    if not companies:
        await msg.reply_text("⚠️ Компании не найдены.")
        return

    sessions[user_id] = {
        "pending":       companies,
        "current_idx":   0,
        "original_text": text,
        "extra":         "",
        "stage":         "clarify",
        "source":        "",
        "executor":      "",
        "saved_count":   0,
        "dup_action":    None,
    }

    total = len(companies)
    if total > 1:
        await msg.reply_text(
            f"📋 Найдено компаний: *{total}*. Обрабатываю по очереди.",
            parse_mode="Markdown"
        )

    await _process_current(update, sessions[user_id], user_id)

async def _process_current(update: Update, session: dict, user_id: int):
    idx = session["current_idx"]
    companies = session["pending"]

    if idx >= len(companies):
        return

    data = companies[idx]
    missing = get_missing(data)
    total = len(companies)
    index_label = idx + 1 if total > 1 else None
    found_text = format_company(data, index_label)

    # Проверяем недостающие поля
    if missing:
        session["stage"] = "clarify"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Записать как есть", callback_data="skip_missing"),
            InlineKeyboardButton("🗑 Пропустить", callback_data="skip_company"),
        ]])
        await update.message.reply_text(
            found_text + "\n\n" + format_missing_msg(missing),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    # Проверяем дубль
    inn = data.get("inn") or ""
    company_name = data.get("company_name") or ""
    await update.message.reply_text("🔍 Проверяю дубли...")

    dup = await check_duplicate(inn, company_name)

    if dup.get("found"):
        session["stage"] = "dup_check"
        session["dup_info"] = dup
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Внести как новую", callback_data="dup_add")],
            [InlineKeyboardButton("🔄 Обновить старую", callback_data="dup_update")],
            [InlineKeyboardButton("🗑 Пропустить", callback_data="skip_company")],
        ])
        await update.message.reply_text(
            format_duplicate_msg(data, dup),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    # Дублей нет — спрашиваем источник
    session["stage"] = "ask_source"
    await update.message.reply_text(
        found_text + "\n\n📢 *Из какого чата заявка?*\nНапиши точное название:",
        parse_mode="Markdown"
    )

async def _save_current(update, session: dict, user_id: int):
    idx = session["current_idx"]
    data = session["pending"][idx]
    reply = getattr(update, 'message', None) or update.callback_query.message

    try:
        is_update = session.get("dup_action") == "update"
        dup_row = (session.get("dup_info") or {}).get("row")
        if is_update and dup_row:
            ok = await update_in_make(data, session["source"], session["executor"], int(dup_row))
            action_text = "🔄 Обновлено в Google Sheets!"
        else:
            ok = await send_to_make(data, session["source"], session["executor"])
            action_text = "✅ Записано в Google Sheets!"
        total = len(session["pending"])
        index_label = idx + 1 if total > 1 else None
        found_text = format_company(data, index_label)

        if ok:
            session["saved_count"] += 1
            src = session["source"] or "—"
            exc = session["executor"] or "—"
            await reply.reply_text(
                f"{found_text}\n"
                f"📢 Источник: {src}\n"
                f"👤 Исполнитель: {exc}\n\n"
                f"{action_text}",
                parse_mode="Markdown"
            )
        else:
            await reply.reply_text("⚠️ Ошибка записи в Make.")
    except Exception as e:
        logger.error(f"Save error: {e}")
        await reply.reply_text(f"⚠️ Ошибка:\n`{e}`", parse_mode="Markdown")

    session["dup_action"] = None
    await _next_company(update, session, user_id)

async def _next_company(update, session: dict, user_id: int):
    session["current_idx"] += 1
    session["extra"] = ""
    session["source"] = ""
    session["executor"] = ""
    session["stage"] = "clarify"
    session["dup_info"] = None

    if session["current_idx"] < len(session["pending"]):
        await _process_current(update, session, user_id)
    else:
        if len(session["pending"]) > 1:
            reply = getattr(update, 'message', None) or update.callback_query.message
            await reply.reply_text(
                f"🎉 Все компании обработаны! Записано: *{session['saved_count']}*",
                parse_mode="Markdown"
            )
        del sessions[user_id]

# ─── Callback handlers ────────────────────────────────────────────────────────
async def handle_skip_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела.")
        return
    # Проверяем дубль даже если пропускаем уточнение
    idx = session["current_idx"]
    data = session["pending"][idx]
    inn = data.get("inn") or ""
    company_name = data.get("company_name") or ""

    await query.message.reply_text("🔍 Проверяю дубли...")
    dup = await check_duplicate(inn, company_name)

    if dup.get("found"):
        session["stage"] = "dup_check"
        session["dup_info"] = dup
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Внести как новую", callback_data="dup_add")],
            [InlineKeyboardButton("🔄 Обновить старую", callback_data="dup_update")],
            [InlineKeyboardButton("🗑 Пропустить", callback_data="skip_company")],
        ])
        await query.message.reply_text(
            format_duplicate_msg(data, dup),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    total = len(session["pending"])
    index_label = idx + 1 if total > 1 else None
    found_text = format_company(data, index_label)
    session["stage"] = "ask_source"
    await query.message.reply_text(
        found_text + "\n\n📢 *Из какого чата заявка?*\nНапиши точное название:",
        parse_mode="Markdown"
    )

async def handle_skip_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела.")
        return
    await query.message.reply_text("🗑 Компания пропущена.")
    await _next_company(update, session, user_id)

async def handle_dup_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Внести как новую строку"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела.")
        return
    idx = session["current_idx"]
    total = len(session["pending"])
    index_label = idx + 1 if total > 1 else None
    found_text = format_company(session["pending"][idx], index_label)
    session["stage"] = "ask_source"
    await query.message.reply_text(
        found_text + "\n\n📢 *Из какого чата заявка?*\nНапиши точное название:",
        parse_mode="Markdown"
    )

async def handle_dup_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновить старую запись — пишем поверх"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела.")
        return
    idx = session["current_idx"]
    total = len(session["pending"])
    index_label = idx + 1 if total > 1 else None
    found_text = format_company(session["pending"][idx], index_label)
    session["stage"] = "ask_source"
    session["dup_action"] = "update"
    await query.message.reply_text(
        found_text + "\n\n📢 *Из какого чата заявка?*\nНапиши точное название:",
        parse_mode="Markdown"
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_skip_missing, pattern="^skip_missing$"))
    app.add_handler(CallbackQueryHandler(handle_skip_company, pattern="^skip_company$"))
    app.add_handler(CallbackQueryHandler(handle_dup_add, pattern="^dup_add$"))
    app.add_handler(CallbackQueryHandler(handle_dup_update, pattern="^dup_update$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
