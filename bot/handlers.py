import math
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

from . import balance
from . import keyboards as kb
from . import reports
from .config import Config
from .db import Database

router = Router()
PAGE_SIZE = 8

HELP = (
    "Я слежу за исходящими переводами USDT (BEP-20) с твоего кошелька и веду учёт расходов.\n\n"
    "• При каждом переводе пришлю уведомление и спрошу, расход это или нет.\n"
    "• Неразобранные напомню, если они висят больше суток.\n"
    "• 1-го числа пришлю итоги прошлого месяца.\n\n"
    f"{kb.BTN_MONTH} — итог с 1-го числа\n"
    f"{kb.BTN_TXS} — список, примечания, удаление\n"
    f"{kb.BTN_ADD} — неучтённый расход вручную\n"
    f"{kb.BTN_PENDING} — то, что ещё не разобрано\n"
    f"{kb.BTN_BALANCE} — график баланса USDT по месяцам\n"
    f"{kb.BTN_PERIOD} — 1, 2, 3 или 6 месяцев (скользящий период)\n"
    f"{kb.BTN_EXPORT} — выгрузка в Excel"
)


class ManualExpense(StatesGroup):
    amount = State()
    comment = State()


class NoteInput(StatesGroup):
    text = State()


async def safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def parse_amount(text: str) -> Decimal | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text.replace(" ", ""))
    if not m:
        return None
    try:
        value = Decimal(m.group().replace(",", "."))
    except InvalidOperation:
        return None
    if value <= 0 or value >= Decimal("1e12"):
        return None
    return value


def parse_ym(s: str) -> tuple[int, int]:
    return int(s[:4]), int(s[4:6])


# ================= команды и главное меню =================

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP, reply_markup=kb.main_menu())


@router.message(F.text == kb.BTN_MONTH)
async def show_month(message: Message, state: FSMContext, db: Database, cfg: Config):
    await state.clear()
    now = datetime.now(cfg.tz)
    text = await reports.month_summary(db, cfg.tz, now.year, now.month)
    await message.answer(text, reply_markup=kb.month_nav(now.year, now.month, cfg.tz))


@router.callback_query(F.data.startswith("mon:"))
async def cb_month(cq: CallbackQuery, db: Database, cfg: Config):
    year, month = parse_ym(cq.data[4:])
    text = await reports.month_summary(db, cfg.tz, year, month)
    await safe_edit(cq.message, text, kb.month_nav(year, month, cfg.tz))
    await cq.answer()


# ================= список транзакций =================

async def render_tx_list(db: Database, cfg: Config, year: int, month: int, page: int):
    start, end = reports.month_bounds(year, month, cfg.tz)
    total = await db.count_txs(start, end)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    rows = await db.list_txs(start, end, page * PAGE_SIZE, PAGE_SIZE)

    text = f"📋 <b>Транзакции — {reports.month_title(year, month)}</b>\n"
    if total == 0:
        text += "\nЗа этот месяц транзакций нет."
    else:
        text += (f"Всего: {total}\n\n"
                 "✅ расход · ➖ не расход · ⏳ не разобрана · ✍️ вручную\n"
                 "Нажми на транзакцию, чтобы открыть её.")
    return text, kb.tx_list(rows, cfg.tz, year, month, page, pages)


@router.message(F.text == kb.BTN_TXS)
async def show_txs(message: Message, state: FSMContext, db: Database, cfg: Config):
    await state.clear()
    now = datetime.now(cfg.tz)
    text, markup = await render_tx_list(db, cfg, now.year, now.month, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("txs:"))
async def cb_txs(cq: CallbackQuery, db: Database, cfg: Config):
    _, ym, page = cq.data.split(":")
    year, month = parse_ym(ym)
    text, markup = await render_tx_list(db, cfg, year, month, int(page))
    await safe_edit(cq.message, text, markup)
    await cq.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


# ================= неразобранные =================

async def render_pending(db: Database, cfg: Config):
    rows = await db.list_pending()
    if not rows:
        return "👌 Все транзакции разобраны.", None
    total = await db.count_pending()
    text = f"⏳ <b>Неразобранные транзакции: {total}</b>\nНажми, чтобы отметить расход или нет."
    return text, kb.pending_list(rows, cfg.tz)


@router.message(F.text == kb.BTN_PENDING)
async def show_pending(message: Message, state: FSMContext, db: Database, cfg: Config):
    await state.clear()
    text, markup = await render_pending(db, cfg)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "pend")
async def cb_pending(cq: CallbackQuery, db: Database, cfg: Config):
    text, markup = await render_pending(db, cfg)
    try:
        await safe_edit(cq.message, text, markup)
    except TelegramBadRequest:
        await cq.message.answer(text, reply_markup=markup)
    await cq.answer()


# ================= карточка транзакции =================

@router.callback_query(F.data.startswith("tx:"))
async def cb_tx(cq: CallbackQuery, state: FSMContext, db: Database, cfg: Config):
    _, action, sid, back = cq.data.split(":", 3)
    tx_id = int(sid)
    tx = await db.get_tx(tx_id)
    if tx is None:
        await safe_edit(cq.message, "Транзакция не найдена — возможно, уже удалена.", kb.back_only(back))
        await cq.answer()
        return

    if action in ("e", "n"):
        status = "expense" if action == "e" else "not_expense"
        await db.set_status(tx_id, status)
        tx = await db.get_tx(tx_id)
        await safe_edit(cq.message, reports.tx_card(tx, cfg.tz), kb.tx_card(tx, back))
        await cq.answer("Записал как расход ✅" if status == "expense" else "Не расход — в итог не пойдёт")

    elif action == "show":
        await safe_edit(cq.message, reports.tx_card(tx, cfg.tz), kb.tx_card(tx, back))
        await cq.answer()

    elif action == "note":
        prompt = await cq.message.answer(
            f"Напиши примечание к транзакции на {reports.fmt(tx['amount'])} USDT"
            + (" (отправь «-», чтобы удалить текущее)." if tx["note"] else "."),
            reply_markup=kb.cancel("note:cancel"),
        )
        await state.set_state(NoteInput.text)
        await state.update_data(tx_id=tx_id, back=back, card_chat=cq.message.chat.id,
                                card_msg=cq.message.message_id, prompt_msg=prompt.message_id)
        await cq.answer()

    elif action == "del":
        await safe_edit(cq.message,
                        reports.tx_card(tx, cfg.tz) + "\n\n❗ <b>Удалить эту транзакцию?</b>",
                        kb.confirm_delete(tx_id, back))
        await cq.answer()

    elif action == "delok":
        await db.delete_tx(tx_id)
        await safe_edit(cq.message, f"🗑 Транзакция на {reports.fmt(tx['amount'])} USDT удалена.",
                        kb.back_only(back))
        await cq.answer("Удалено")

    else:
        await cq.answer()


@router.callback_query(F.data == "note:cancel")
async def cb_note_cancel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cq.message, "Отменено.")
    await cq.answer()


# ================= ручной расход =================

@router.message(F.text == kb.BTN_ADD)
async def add_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ManualExpense.amount)
    await message.answer("Введи сумму расхода в USDT (например: 25 или 12.5)",
                         reply_markup=kb.cancel("man:cancel"))


@router.callback_query(F.data == "man:cancel")
async def cb_add_cancel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cq.message, "Отменено.")
    await cq.answer()


async def _save_manual(message: Message, state: FSMContext, db: Database, cfg: Config,
                       note: str | None) -> None:
    data = await state.get_data()
    await state.clear()
    amount = Decimal(data["amount"])
    tx_id = await db.add_manual(amount, note, datetime.now(cfg.tz))
    tx = await db.get_tx(tx_id)
    await message.answer("✅ Расход добавлен\n\n" + reports.tx_card(tx, cfg.tz),
                         reply_markup=kb.tx_card(tx))


@router.callback_query(F.data == "man:skip", StateFilter(ManualExpense.comment))
async def cb_add_skip(cq: CallbackQuery, state: FSMContext, db: Database, cfg: Config):
    await cq.message.edit_reply_markup(reply_markup=None)
    await _save_manual(cq.message, state, db, cfg, None)
    await cq.answer()


# ================= баланс =================

async def _balance_earliest(db: Database, cfg: Config) -> tuple[int, int] | None:
    started = await balance.get_since(db)
    if not started:
        return None
    since = started[0].astimezone(cfg.tz)
    return since.year, since.month


async def _show_balance(message: Message, text: str, chart: bytes | None,
                        markup: InlineKeyboardMarkup | None, edit: bool) -> None:
    if edit and chart and message.photo:
        await message.edit_media(
            InputMediaPhoto(media=BufferedInputFile(chart, "balance.png"), caption=text),
            reply_markup=markup)
    elif edit and not chart and not message.photo:
        await safe_edit(message, text, markup)
    elif chart:
        await message.answer_photo(BufferedInputFile(chart, "balance.png"), caption=text,
                                   reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _balance_month(message: Message, db: Database, cfg: Config, year: int, month: int,
                         edit: bool) -> None:
    text, chart = await balance.month_view(db, cfg.tz, year, month)
    markup = kb.balance_nav(year, month, cfg.tz, await _balance_earliest(db, cfg)) if chart else None
    await _show_balance(message, text, chart, markup, edit)


@router.message(F.text == kb.BTN_BALANCE)
async def show_balance(message: Message, state: FSMContext, db: Database, cfg: Config):
    await state.clear()
    now = datetime.now(cfg.tz)
    await message.bot.send_chat_action(message.chat.id, "upload_photo")
    await _balance_month(message, db, cfg, now.year, now.month, edit=False)


@router.callback_query(F.data.startswith("bal:"))
async def cb_balance(cq: CallbackQuery, db: Database, cfg: Config):
    arg = cq.data[4:]
    await cq.answer()
    if arg == "all":
        text, chart = await balance.overview_view(db, cfg.tz)
        await _show_balance(cq.message, text, chart, kb.balance_overview() if chart else None, edit=True)
        return
    if arg == "now":
        now = datetime.now(cfg.tz)
        year, month = now.year, now.month
    else:
        year, month = parse_ym(arg)
    try:
        await _balance_month(cq.message, db, cfg, year, month, edit=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ================= период и экспорт =================

@router.message(F.text == kb.BTN_PERIOD)
async def show_periods(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("За какой период посчитать расходы?", reply_markup=kb.periods())


@router.callback_query(F.data.startswith("per:"))
async def cb_period(cq: CallbackQuery, db: Database, cfg: Config):
    days = int(cq.data[4:])
    text = await reports.period_summary(db, cfg.tz, days)
    await safe_edit(cq.message, text, kb.periods(days))
    await cq.answer()


@router.message(F.text == kb.BTN_EXPORT)
async def show_export(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Что выгрузить в Excel?", reply_markup=kb.export_options())


@router.callback_query(F.data.startswith("exp:"))
async def cb_export(cq: CallbackQuery, db: Database, cfg: Config):
    kind = cq.data[4:]
    now = datetime.now(cfg.tz)
    if kind == "month":
        start, _ = reports.month_bounds(now.year, now.month, cfg.tz)
        label = f"{now:%Y-%m}"
    elif kind == "180":
        start = now - timedelta(days=180)
        label = "6m"
    else:
        start = None
        label = "all"
    rows = await db.export_rows(start)
    if not rows:
        await cq.answer("За этот период транзакций нет", show_alert=True)
        return
    data = reports.build_xlsx(rows, cfg.tz)
    await cq.message.answer_document(
        BufferedInputFile(data, filename=f"expenses_{label}_{now:%Y%m%d}.xlsx"),
        caption=f"Транзакций в выгрузке: {len(rows)}",
    )
    await cq.answer()


# ================= ввод текста в состояниях =================
# Эти обработчики стоят последними: кнопки меню выше перехватываются раньше и сбрасывают ввод.

@router.message(ManualExpense.amount, F.text)
async def add_amount(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Не понял сумму. Отправь число, например 25 или 12.5")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(ManualExpense.comment)
    await message.answer(f"Сумма: <b>{reports.fmt(amount)} USDT</b>\n"
                         "Напиши комментарий (на что потрачено) или нажми «Пропустить».",
                         reply_markup=kb.skip_comment())


@router.message(ManualExpense.comment, F.text)
async def add_comment(message: Message, state: FSMContext, db: Database, cfg: Config):
    await _save_manual(message, state, db, cfg, message.text.strip()[:500])


@router.message(NoteInput.text, F.text)
async def note_text(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot):
    data = await state.get_data()
    await state.clear()
    raw = message.text.strip()
    note = None if raw == "-" else raw[:500]
    tx = await db.get_tx(data["tx_id"])
    if tx is None:
        await message.answer("Транзакция не найдена — возможно, уже удалена.")
        return
    await db.set_note(tx["id"], note)
    tx = await db.get_tx(tx["id"])

    try:
        await bot.delete_message(message.chat.id, data["prompt_msg"])
    except TelegramBadRequest:
        pass
    try:
        await bot.edit_message_text(reports.tx_card(tx, cfg.tz), chat_id=data["card_chat"],
                                    message_id=data["card_msg"],
                                    reply_markup=kb.tx_card(tx, data["back"]))
        await message.answer("📝 Примечание сохранено" if note else "Примечание удалено")
    except TelegramBadRequest:
        await message.answer(reports.tx_card(tx, cfg.tz), reply_markup=kb.tx_card(tx))


@router.message()
async def fallback(message: Message):
    await message.answer("Выбери действие в меню 👇", reply_markup=kb.main_menu())
