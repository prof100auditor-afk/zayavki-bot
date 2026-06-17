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

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
MAKE_WEBHOOK    = os.environ["MAKE_WEBHOOK_URL"]
MAKE_CHECK_URL  = os.environ["MAKE_CHECK_URL"]
MAKE_UPDATE_URL = os.environ["MAKE_UPDATE_URL"]

ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

FIELDS = {
    "company_name":    {"label": "Название компании",     "required": True},
    "inn":             {"label": "ИНН",                   "required": True},
    "website":         {"label": "Сайт компании",         "required": False},
    "zsk_color":       {"label": "Цвет ЗСК",              "required": True,  "buttons": ["🟢 Зелёный", "🟡 Жёлтый", "🟠 Жёлтый с типологией", "🔴 Красный"]},
    "accepts_from":    {"label": "Принимает от",          "required": True,  "buttons": ["🟢 Зелёный", "🟡 Жёлтый", "🟠 Жёлтый с типологией", "🔴 Красный"]},
    "amount_range":    {"label": "Сумма ОТ и ДО",         "required": True},
    "payment_purpose": {"label": "Назначение платежа",    "required": True},
    "bank":            {"label": "Банк",                  "required": True},
    "vat_rate":        {"label": "Ставка НДС",            "required": True,  "buttons": ["22%", "10%", "0%", "БЕЗ НДС", "БЕЗ ОТЧЕТ"]},
    "cash_rate":       {"label": "Ставка по кэшу",        "required": True},
    "issue_date":      {"label": "Дата выдачи/срок",      "required": True,  "buttons": ["Т+1", "Т+2", "Т+3", "Т+4", "Т+5"]},
    "comment":         {"label": "Комментарии",           "required": False},
    "source":          {"label": "Источник (группа/чат)", "required": True},
}

REQUIRED_FIELDS = [k for k, v in FIELDS.items() if v["required"]]

SYSTEM_PROMPT = """Ты — эксперт по извлечению данных о компаниях из сообщений.

ПРАВИЛО: Записывай ТОЛЬКО то что ЯВНО написано. Если не уверен — null.

- company_name: официальное название юрлица. Если нет — null.
- inn: строго 10 или 12 цифр. ОГРН — НЕ ИНН. Сомневаешься — null.
- website: сайт если упомянут, иначе null.
- zsk_color: Зелёный/Жёлтый/Жёлтый с типологией/Красный. Сленг: зелень=Зелёный, желтяк=Жёлтый, типология/тиположка/коричневый/оранжевый=Жёлтый с типологией.
- accepts_from: от каких принимает. Те же варианты. "Принимает от зелёных"=Зелёный.
- amount_range: сумма. кк=млн, к=тыс. Если нет — null.
- payment_purpose: назначение. Только явно указанное.
- bank: явно названный банк. Нормализуй: сбер=Сбербанк, альфа=Альфа-Банк, псб=ПСБ.
- vat_rate: 22%/10%/0%/БЕЗ НДС/БЕЗ ОТЧЕТ. "без отчёта/нал"=БЕЗ ОТЧЕТ.
- cash_rate: конкретный % (17%). Без диапазонов.
- issue_date: Т+N. "след день"=Т+1, "3 дня"=Т+3.
- comment: важная доп. информация.

НЕ ДОБАВЛЯЙ source.

Возвращай ТОЛЬКО JSON:
{"company_name":null,"inn":null,"website":null,"zsk_color":null,"accepts_from":null,"amount_range":null,"payment_purpose":null,"bank":null,"vat_rate":null,"cash_rate":null,"issue_date":null,"comment":null}"""

async def check_duplicate(inn: str, company_name: str) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(MAKE_CHECK_URL, json={"inn": inn, "company_name": company_name}, timeout=15)
            if r.status_code == 200:
                text = r.text.strip()
                logger.info(f"Dup check: {text[:200]}")
                if not text or text in ("Accepted", "1", "0"):
                    return {"found": False}
                m = re.search(r'\{.*?\}', text, re.DOTALL)
                if not m:
                    return {"found": False}
                data = json.loads(m.group(0))
                if not data.get("found"):
                    return {"found": False}
                row = str(data.get("row", "")).strip()
                data["row"] = int(row) if row.isdigit() else None
                return data
    except Exception as e:
        logger.error(f"Dup check error: {e}")
    return {"found": False}

def make_payload(data: dict, source: str) -> dict:
    return {
        "updated_at":      datetime.now().strftime("%d.%m.%Y %H:%M"),
        "company_name":    data.get("company_name") or "",
        "inn":             data.get("inn") or "",
        "website":         data.get("website") or "",
        "zsk_color":       data.get("zsk_color") or "",
        "accepts_from":    data.get("accepts_from") or "",
        "amount_range":    data.get("amount_range") or "",
        "payment_purpose": data.get("payment_purpose") or "",
        "bank":            data.get("bank") or "",
        "vat_rate":        data.get("vat_rate") or "",
        "cash_rate":       data.get("cash_rate") or "",
        "issue_date":      data.get("issue_date") or "",
        "comment":         data.get("comment") or "",
        "source":          source,
    }

async def send_to_make(data: dict, source: str) -> bool:
    async with httpx.AsyncClient() as client:
        r = await client.post(MAKE_WEBHOOK, json=make_payload(data, source), timeout=15)
        return r.status_code in (200, 201, 202, 204)

async def update_in_make(data: dict, source: str, row: int) -> bool:
    payload = make_payload(data, source)
    payload["row"] = row
    async with httpx.AsyncClient() as client:
        r = await client.post(MAKE_UPDATE_URL, json=payload, timeout=15)
        return r.status_code in (200, 201, 202, 204)

def parse_with_claude(text: str, extra: str = "") -> dict:
    full = text + (f"\n\nДополнение: {extra}" if extra else "")
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": full}],
    )
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    return json.loads(m.group(0)) if m else {}

def get_missing(data: dict) -> list:
    return [k for k in REQUIRED_FIELDS if not data.get(k) or str(data.get(k)).lower() == "null"]

FIELD_EMOJI = {
    "company_name":    "🏢 Компания",
    "inn":             "🔢 ИНН",
    "website":         "🌐 Сайт",
    "zsk_color":       "🎨 Цвет ЗСК",
    "accepts_from":    "✅ Принимает от",
    "amount_range":    "💰 Сумма",
    "payment_purpose": "💳 Назначение",
    "bank":            "🏦 Банк",
    "vat_rate":        "📊 НДС",
    "cash_rate":       "📈 Ставка кэша",
    "issue_date":      "📅 Срок",
    "comment":         "💬 Комментарий",
}

def format_found(data: dict) -> str:
    lines = ["*Данные:*\n"]
    for key, label in FIELD_EMOJI.items():
        val = data.get(key)
        if val and str(val).lower() != "null":
            lines.append(f"{label}: {val}")
    return "\n".join(lines)

def make_field_keyboard(field: str):
    buttons = FIELDS[field].get("buttons")
    if not buttons:
        return None
    rows = []
    row = []
    for btn in buttons:
        val = btn.split(" ", 1)[-1] if btn[0] in "🟢🟡🟠🔴" else btn
        row.append(InlineKeyboardButton(btn, callback_data=f"fld_{field}_{val}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"fld_{field}_manual")])
    return InlineKeyboardMarkup(rows)

sessions: dict = {}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Пересылай заявки — извлеку данные и запишу в таблицу 📊",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    session = sessions.get(user_id)

    if session and session.get("stage") == "ask_source":
        session["source"] = text
        await _check_dup(update, session, user_id)
        return

    if session and session.get("stage") == "manual_input":
        field = session.get("current_field")
        if field:
            session["data"][field] = text
            session["stage"] = "clarify"
            session["current_field"] = None
            await _next_field(update, session, user_id)
        return

    if session and session.get("stage") == "clarify":
        session["extra"] = (session.get("extra") or "") + "\n" + text
        try:
            updated = parse_with_claude(session["original_text"], session["extra"])
            for k, v in updated.items():
                if v and str(v).lower() != "null":
                    session["data"][k] = v
        except Exception as e:
            logger.error(f"Clarify error: {e}")
        await _next_field(update, session, user_id)
        return

    proc = await msg.reply_text("⏳ Анализирую...")
    try:
        data = parse_with_claude(text)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        await proc.edit_text("⚠️ Не удалось распознать. Попробуй ещё раз.")
        return
    await proc.delete()

    source = "личный чат"
    try:
        if msg.forward_origin:
            origin = msg.forward_origin
            if hasattr(origin, 'chat') and origin.chat:
                source = origin.chat.title or "чат"
            elif hasattr(origin, 'sender_user') and origin.sender_user:
                u = origin.sender_user
                source = f"@{u.username}" if u.username else u.full_name
    except Exception:
        pass

    sessions[user_id] = {
        "data": data, "source": source,
        "original_text": text, "extra": "",
        "stage": "clarify", "dup_action": None,
        "dup_info": None, "current_field": None,
    }
    await _next_field(update, sessions[user_id], user_id)

async def _next_field(update, session: dict, user_id: int):
    msg = update.message
    missing = get_missing(session["data"])

    if not missing:
        session["stage"] = "ask_source"
        await msg.reply_text(
            format_found(session["data"]) + "\n\n📢 *Из какого чата заявка?*",
            parse_mode="Markdown"
        )
        return

    field = missing[0]
    session["current_field"] = field
    label = FIELDS[field]["label"]
    found_text = format_found(session["data"])

    keyboard = make_field_keyboard(field)
    skip_btn = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_fld")]]) if not FIELDS[field]["required"] else None

    await msg.reply_text(
        found_text + f"\n\n❓ *{label}:*",
        parse_mode="Markdown",
        reply_markup=keyboard or skip_btn
    )

async def _check_dup(update, session: dict, user_id: int):
    msg = update.message or update.callback_query.message
    data = session["data"]
    await msg.reply_text("🔍 Проверяю дубли...")
    dup = await check_duplicate(data.get("inn") or "", data.get("company_name") or "")

    if dup.get("found"):
        session["stage"] = "dup_check"
        session["dup_info"] = dup
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Новая запись", callback_data="dup_add")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="dup_update")],
            [InlineKeyboardButton("🗑 Пропустить", callback_data="skip_company")],
        ])
        company = data.get("company_name", "")
        inn = data.get("inn", "")
        await msg.reply_text(
            f"⚠️ *Найден дубль!*\n\n🏢 {dup.get('company_name', company)}\n🔢 ИНН: {inn}\n📢 Источник: {dup.get('source','—')}\n📅 Дата: {dup.get('updated_at','—')}\n\nЧто делаем?",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return
    await _save(update, session, user_id)

async def _save(update, session: dict, user_id: int):
    reply = getattr(update, 'message', None) or update.callback_query.message
    data = session["data"]
    source = session["source"]
    is_update = session.get("dup_action") == "update"
    row = (session.get("dup_info") or {}).get("row")
    try:
        if is_update and row:
            ok = await update_in_make(data, source, int(row))
            action = "🔄 Обновлено"
        else:
            ok = await send_to_make(data, source)
            action = "✅ Записано"
        if ok:
            await reply.reply_text(
                f"{format_found(data)}\n📢 Источник: {source}\n\n{action} в Google Sheets!",
                parse_mode="Markdown"
            )
        else:
            await reply.reply_text("⚠️ Ошибка записи.")
    except Exception as e:
        logger.error(f"Save error: {e}")
        await reply.reply_text(f"⚠️ Ошибка: `{e}`", parse_mode="Markdown")
    if user_id in sessions:
        del sessions[user_id]

async def handle_field_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела.")
        return
    parts = query.data.split("_", 2)
    field = parts[1]
    value = parts[2]
    if value == "manual":
        session["stage"] = "manual_input"
        session["current_field"] = field
        await query.message.reply_text(f"✏️ Введи *{FIELDS[field]['label']}*:", parse_mode="Markdown")
        return
    session["data"][field] = value
    session["stage"] = "clarify"
    session["current_field"] = None
    class FU:
        message = query.message
    await _next_field(FU(), session, user_id)

async def handle_skip_fld(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        return
    field = session.get("current_field")
    if field:
        session["data"][field] = ""
    session["stage"] = "clarify"
    session["current_field"] = None
    class FU:
        message = query.message
    await _next_field(FU(), session, user_id)

async def handle_dup_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        return
    session["dup_action"] = "add"
    await _save(update, session, user_id)

async def handle_dup_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        return
    session["dup_action"] = "update"
    await _save(update, session, user_id)

async def handle_skip_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id in sessions:
        del sessions[user_id]
    await query.message.reply_text("🗑 Пропущено.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_field_btn, pattern="^fld_"))
    app.add_handler(CallbackQueryHandler(handle_skip_fld, pattern="^skip_fld$"))
    app.add_handler(CallbackQueryHandler(handle_dup_add, pattern="^dup_add$"))
    app.add_handler(CallbackQueryHandler(handle_dup_update, pattern="^dup_update$"))
    app.add_handler(CallbackQueryHandler(handle_skip_company, pattern="^skip_company$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
