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

SYSTEM_PROMPT = """Ты — эксперт по извлечению данных о компаниях из сообщений любого формата.
Сообщения могут содержать одну или несколько компаний, быть написаны в свободной форме, с эмодзи, сокращениями, на русском языке.

ВАЖНО: В сообщении может быть НЕСКОЛЬКО компаний. Извлеки ВСЕ компании.

Возвращай ТОЛЬКО валидный JSON-массив (даже если компания одна):
[
  {
    "company_name":    "Название компании (ООО, ИП и т.д.)",
    "inn":             "ИНН (только цифры, без пробелов, null если нет)",
    "payment_purpose": "Назначение платежа — за что платят (удобрения, стройматериалы и т.д.)",
    "amount_range":    "Сумма от и до (например: 12-13 млн, до 3кк, 500к-10кк)",
    "bank":            "Банк или банки через запятую",
    "vat_rate":        "Ставка НДС (20%, 22%, без НДС, 0%, null если не указано)",
    "cash_rate":       "Ставка по кэшу/комиссия в процентах (например: 10%, 17, null если нет)",
    "conditions":      "Условия: сроки выдачи, документы (УПД, счёт-фактура, ЭДО), требования к контрагентам",
    "relevance":       "Актуально",
    "company_type":    "Тип: Белый бизнес / Флагман / другое (null если не указано)",
    "white_business":  "Да или Нет (null если не указано)",
    "zsk_color":       "Зелёный / Жёлтый / Красный (null если не указано)",
    "issue_date":      "Срок выдачи (например: след день, 4-5 дней, null если нет)",
    "cash_location":   "Где и как выдают (город, карты, USDT, null если нет)",
    "comment":         "Важные дополнительные условия, ограничения по ОКВЭД, требования",
    "executor":        "username или имя исполнителя если есть",
    "website":         "Сайт если упомянут"
  }
]

Правила:
- Всегда возвращай МАССИВ [...] даже для одной компании
- Если поле не найдено — ставь null (не пустую строку)
- ИНН: только цифры, проверь что это именно ИНН (10 или 12 цифр)
- кк = миллион, к = тысяча, млн = миллион
- Возвращай ТОЛЬКО JSON-массив, никакого другого текста"""

async def send_to_make(data: dict, source: str) -> bool:
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
        "executor":        data.get("executor") or "",
        "website":         data.get("website") or "",
        "source":          source,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(MAKE_WEBHOOK, json=payload, timeout=15)
        return r.status_code == 200

def clean_json(raw: str) -> str:
    """Очищаем ответ Claude от лишнего текста и markdown."""
    raw = raw.strip()
    # Убираем markdown блоки
    raw = re.sub(r'```(?:json)?', '', raw).replace('```', '').strip()
    # Ищем JSON массив
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if match:
        return match.group(0)
    # Ищем одиночный объект и оборачиваем в массив
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return f"[{match.group(0)}]"
    return raw

def parse_with_claude(text: str, extra: str = "") -> list[dict]:
    full = text + (f"\n\nДополнение пользователя: {extra}" if extra else "")
    response = ai.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": full}],
    )
    raw = response.content[0].text
    cleaned = clean_json(raw)
    result = json.loads(cleaned)
    # Гарантируем что возвращаем список
    if isinstance(result, dict):
        return [result]
    return result

def get_missing(data: dict) -> list:
    return [k for k in REQUIRED_FIELDS if not data.get(k) or data.get(k) == "null"]

def get_source(msg) -> str:
    try:
        origin = msg.forward_origin
        if origin is None:
            return "личный чат"
        if hasattr(origin, 'chat') and origin.chat:
            return origin.chat.title or origin.chat.username or "канал"
        if hasattr(origin, 'sender_user') and origin.sender_user:
            u = origin.sender_user
            return f"@{u.username}" if u.username else u.full_name
        if hasattr(origin, 'sender_user_name') and origin.sender_user_name:
            return origin.sender_user_name
        return "неизвестный источник"
    except Exception:
        return "личный чат"

def is_forwarded(msg) -> bool:
    try:
        return msg.forward_origin is not None
    except Exception:
        return False

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
    "executor":        "👤 Исполнитель",
    "website":         "🌐 Сайт",
}

def format_company(data: dict, index: int = None) -> str:
    prefix = f"*Компания {index}*\n" if index else ""
    lines = [prefix + "*Извлечённые данные:*\n"]
    for key, label in FIELD_EMOJI.items():
        val = data.get(key)
        if val and val != "null":
            lines.append(f"{label}: {val}")
    return "\n".join(lines)

def format_missing_msg(missing: list) -> str:
    labels = "\n".join(f"  • {REQUIRED_FIELDS[k]}" for k in missing)
    return f"❓ *Не хватает данных:*\n{labels}\n\nДопиши или нажми кнопку чтобы записать как есть."

# Сессии: { user_id: { pending: [компании на уточнение], current_idx, source, original_text, extra } }
sessions: dict = {}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Привет!*\n\n"
        "Пересылай мне сообщения с заявками из Telegram-групп.\n\n"
        "Я умею:\n"
        "• Извлекать данные из текста любого формата\n"
        "• Обрабатывать несколько компаний из одного сообщения\n"
        "• Уточнять недостающие данные\n"
        "• Записывать всё в Google Sheets 📊",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message
    text = (msg.text or msg.caption or "").strip()

    if not text:
        await msg.reply_text("⚠️ Пустое сообщение. Перешли заявку с текстом.")
        return

    source = get_source(msg)
    forwarded = is_forwarded(msg)
    session = sessions.get(user_id)

    # Ответ на уточняющий вопрос
    if session and session.get("waiting_clarification") and not forwarded:
        idx = session["current_idx"]
        session["extra"] = (session.get("extra") or "") + "\n" + text
        try:
            updated_list = parse_with_claude(session["original_text"], session["extra"])
            if idx < len(updated_list):
                updated = updated_list[idx]
            else:
                updated = updated_list[0]
            for k, v in updated.items():
                if v and v != "null":
                    session["pending"][idx][k] = v
        except Exception as e:
            logger.error(f"Claude clarification error: {e}")
            await msg.reply_text("⚠️ Ошибка обработки. Попробуй написать данные ещё раз.")
            return
        await _process_current(update, session, user_id)
        return

    # Новое сообщение — парсим
    proc = await msg.reply_text("⏳ Анализирую сообщение...")
    try:
        companies = parse_with_claude(text)
    except Exception as e:
        logger.error(f"Parse error: {e}\nRaw text: {text[:200]}")
        await proc.edit_text(
            "⚠️ Не удалось распознать структуру.\n\n"
            "Попробуй переслать ещё раз или добавь текст в формате:\n"
            "Компания: ...\nИНН: ...\nНазначение: ..."
        )
        return

    await proc.delete()

    if not companies:
        await msg.reply_text("⚠️ Компании не найдены в сообщении.")
        return

    sessions[user_id] = {
        "pending": companies,
        "current_idx": 0,
        "source": source,
        "original_text": text,
        "extra": "",
        "waiting_clarification": False,
        "saved_count": 0,
    }

    total = len(companies)
    if total > 1:
        await msg.reply_text(f"📋 Найдено компаний: *{total}*. Обрабатываю по очереди.", parse_mode="Markdown")

    await _process_current(update, sessions[user_id], user_id)

async def _process_current(update: Update, session: dict, user_id: int):
    idx = session["current_idx"]
    companies = session["pending"]

    if idx >= len(companies):
        # Всё обработано
        total = session["saved_count"]
        await update.message.reply_text(f"✅ Готово! Записано компаний: *{total}*", parse_mode="Markdown")
        del sessions[user_id]
        return

    data = companies[idx]
    missing = get_missing(data)
    total = len(companies)
    index_label = idx + 1 if total > 1 else None
    found_text = format_company(data, index_label)

    if missing:
        session["waiting_clarification"] = True
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Записать как есть", callback_data="skip_missing"),
            InlineKeyboardButton("🗑 Пропустить компанию", callback_data="skip_company"),
        ]])
        await update.message.reply_text(
            found_text + "\n\n" + format_missing_msg(missing),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    session["waiting_clarification"] = False
    await _save_current(update, session, user_id)

async def _save_current(update, session: dict, user_id: int):
    idx = session["current_idx"]
    data = session["pending"][idx]
    reply = getattr(update, 'message', None) or update.callback_query.message

    try:
        ok = await send_to_make(data, session["source"])
        total = len(session["pending"])
        index_label = idx + 1 if total > 1 else None
        found_text = format_company(data, index_label)

        if ok:
            session["saved_count"] += 1
            await reply.reply_text(f"{found_text}\n\n✅ Записано в Google Sheets!", parse_mode="Markdown")
        else:
            await reply.reply_text("⚠️ Make webhook вернул ошибку. Проверь сценарий в Make.com")
    except Exception as e:
        logger.error(f"Make error: {e}")
        await reply.reply_text(f"⚠️ Ошибка отправки:\n`{e}`", parse_mode="Markdown")

    # Переходим к следующей компании
    session["current_idx"] += 1
    session["extra"] = ""
    session["waiting_clarification"] = False

    if session["current_idx"] < len(session["pending"]):
        await _process_current(update, session, user_id)
    else:
        total = session["saved_count"]
        if len(session["pending"]) > 1:
            reply2 = getattr(update, 'message', None) or update.callback_query.message
            await reply2.reply_text(f"🎉 Все компании обработаны! Записано: *{total}*", parse_mode="Markdown")
        del sessions[user_id]

async def handle_skip_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела. Перешли заявку заново.")
        return
    session["waiting_clarification"] = False
    await _save_current(update, session, user_id)

async def handle_skip_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела.")
        return
    await query.message.reply_text("🗑 Компания пропущена.")
    session["current_idx"] += 1
    session["extra"] = ""
    session["waiting_clarification"] = False
    if session["current_idx"] < len(session["pending"]):
        await _process_current(update, session, user_id)
    else:
        del sessions[user_id]

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_skip_missing, pattern="^skip_missing$"))
    app.add_handler(CallbackQueryHandler(handle_skip_company, pattern="^skip_company$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
