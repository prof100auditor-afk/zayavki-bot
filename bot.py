import os
import json
import logging
import httpx
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

SYSTEM_PROMPT = """Ты — ассистент, который извлекает структурированные данные о компаниях из сообщений любого формата.
Сообщения могут быть написаны в свободной форме, с сокращениями, на русском языке.

Извлекай поля и возвращай ТОЛЬКО валидный JSON без пояснений и markdown:
{
  "company_name":    "Название компании (ООО, ИП и т.д.)",
  "inn":             "ИНН (только цифры, без пробелов)",
  "payment_purpose": "Назначение платежа — за что платят",
  "amount_range":    "Сумма от и до (например: до 3кк, 500к-10кк, от 100к)",
  "bank":            "Банк или банки (Альфа, Сбербанк, РРБ и т.д.)",
  "vat_rate":        "Ставка НДС (20%, без НДС, 0%)",
  "cash_rate":       "Ставка по кэшу в процентах (число или диапазон)",
  "conditions":      "Условия: сроки, документы (ЭДО, УПД, договор), требования",
  "relevance":       "Актуальность (если не указано — Актуально)",
  "company_type":    "Тип компании: Белый бизнес, Флагман, или другое",
  "white_business":  "Проверка белого бизнеса: Да или Нет",
  "zsk_color":       "Цвет ЗСК: Зелёный, Жёлтый, Красный (если не указано — null)",
  "issue_date":      "Дата выдачи или срок (например: 4-5 дней)",
  "cash_location":   "Где выдают кэш и каким способом (город, карты, USDT, кэш)",
  "comment":         "Дополнительные условия, особенности, ограничения",
  "executor":        "Исполнитель — username или имя если упомянуто",
  "website":         "Сайт компании если есть"
}

Правила:
- Если поле не найдено — ставь null
- ИНН только цифры
- кк = миллион, к = тысяча
- Возвращай ТОЛЬКО JSON"""

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
        r = await client.post(MAKE_WEBHOOK, json=payload, timeout=10)
        return r.status_code == 200

def parse_with_claude(text: str, extra: str = "") -> dict:
    full = text + (f"\n\nДополнение: {extra}" if extra else "")
    response = ai.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": full}],
    )
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def get_missing(data: dict) -> list:
    return [k for k in REQUIRED_FIELDS if not data.get(k) or data.get(k) == "null"]

def get_source(msg) -> str:
    """Определяем источник сообщения совместимо с PTB v21+"""
    try:
        origin = msg.forward_origin
        if origin is None:
            return "личный чат"
        # MessageOriginChannel
        if hasattr(origin, 'chat') and origin.chat:
            return origin.chat.title or origin.chat.username or "неизвестный канал"
        # MessageOriginUser
        if hasattr(origin, 'sender_user') and origin.sender_user:
            u = origin.sender_user
            return f"@{u.username}" if u.username else u.full_name
        # MessageOriginHiddenUser
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

def format_found(data: dict) -> str:
    lines = ["*Извлечённые данные:*\n"]
    for key, label in FIELD_EMOJI.items():
        val = data.get(key)
        if val and val != "null":
            lines.append(f"{label}: {val}")
    return "\n".join(lines)

def format_missing_msg(missing: list) -> str:
    labels = "\n".join(f"  • {REQUIRED_FIELDS[k]}" for k in missing)
    return f"❓ *Не хватает данных:*\n{labels}\n\nДопиши или нажми кнопку чтобы записать как есть."

sessions: dict = {}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Привет!*\n\nПересылай мне сообщения с заявками из Telegram-групп.\n\nЯ извлеку данные и запишу в Google Sheets 📊",
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

    # Ответ на уточняющий вопрос (не пересланное сообщение)
    if session and not forwarded:
        session["extra"] = (session.get("extra") or "") + "\n" + text
        try:
            updated = parse_with_claude(session["original_text"], session["extra"])
            for k, v in updated.items():
                if v and v != "null":
                    session["data"][k] = v
        except Exception as e:
            logger.error(f"Claude error: {e}")
            await msg.reply_text("⚠️ Ошибка обработки. Попробуй ещё раз.")
            return
        await _check_and_save(update, session, user_id)
        return

    # Новая заявка
    proc = await msg.reply_text("⏳ Анализирую...")
    try:
        data = parse_with_claude(text)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        await proc.edit_text("⚠️ Не удалось распознать. Попробуй ещё раз.")
        return

    await proc.delete()
    sessions[user_id] = {"data": data, "source": source, "original_text": text, "extra": ""}
    await _check_and_save(update, sessions[user_id], user_id)

async def _check_and_save(update: Update, session: dict, user_id: int):
    missing = get_missing(session["data"])
    found_text = format_found(session["data"])
    if missing:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Записать как есть", callback_data="skip_missing")
        ]])
        await update.message.reply_text(
            found_text + "\n\n" + format_missing_msg(missing),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return
    await _save(update, session, user_id)

async def _save(update, session: dict, user_id: int):
    reply = getattr(update, 'message', None) or update.callback_query.message
    try:
        ok = await send_to_make(session["data"], session["source"])
        found_text = format_found(session["data"])
        if ok:
            await reply.reply_text(f"{found_text}\n\n✅ Записано в Google Sheets!", parse_mode="Markdown")
        else:
            await reply.reply_text("⚠️ Make webhook вернул ошибку. Проверь сценарий в Make.com")
        if user_id in sessions:
            del sessions[user_id]
    except Exception as e:
        logger.error(f"Make error: {e}")
        await reply.reply_text(f"⚠️ Ошибка отправки в Make:\n`{e}`", parse_mode="Markdown")

async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела. Перешли заявку заново.")
        return
    await _save(update, session, user_id)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_skip, pattern="^skip_missing$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
