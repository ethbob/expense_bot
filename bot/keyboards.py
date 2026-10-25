from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import (InlineKeyboardButton as IB, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

from . import reports

BTN_MONTH = "📊 Этот месяц"
BTN_TXS = "📋 Транзакции"
BTN_ADD = "➕ Добавить расход"
BTN_PENDING = "⏳ Неразобранные"
BTN_PERIOD = "📈 За период"
BTN_EXPORT = "📤 Экспорт"
BTN_BALANCE = "💰 Баланс"

PERIODS = [30, 60, 90, 180]


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MONTH), KeyboardButton(text=BTN_TXS)],
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_PENDING)],
            [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_PERIOD)],
            [KeyboardButton(text=BTN_EXPORT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _ym(year: int, month: int) -> str:
    return f"{year}{month:02d}"


# ---------- «назад» из карточки ----------
# back: ""                — карточка из уведомления, кнопки «назад» нет
#       "P"               — из списка неразобранных
#       "L202610p0"       — из списка транзакций (месяц 2026-10, страница 0)

def back_callback(back: str) -> str | None:
    if back == "P":
        return "pend"
    if back.startswith("L"):
        ym, page = back[1:].split("p")
        return f"txs:{ym}:{page}"
    return None


def back_button(back: str) -> list[IB]:
    cb = back_callback(back)
    return [IB(text="⬅️ Назад к списку", callback_data=cb)] if cb else []


def back_only(back: str) -> InlineKeyboardMarkup | None:
    row = back_button(back)
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


# ---------- карточка транзакции ----------

def tx_card(tx, back: str = "") -> InlineKeyboardMarkup:
    i = tx["id"]
    rows: list[list[IB]] = []
    status = tx["status"]
    if tx["source"] == "chain":
        if status == "pending":
            rows.append([IB(text="✅ Да, расход", callback_data=f"tx:e:{i}:{back}"),
                         IB(text="❌ Нет", callback_data=f"tx:n:{i}:{back}")])
        elif status == "expense":
            rows.append([IB(text="➖ Сделать «не расход»", callback_data=f"tx:n:{i}:{back}")])
        else:
            rows.append([IB(text="✅ Сделать расходом", callback_data=f"tx:e:{i}:{back}")])
    note_label = "📝 Изменить примечание" if tx["note"] else "📝 Примечание"
    rows.append([IB(text=note_label, callback_data=f"tx:note:{i}:{back}"),
                 IB(text="🗑 Удалить", callback_data=f"tx:del:{i}:{back}")])
    back_row = back_button(back)
    if back_row:
        rows.append(back_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete(tx_id: int, back: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🗑 Да, удалить", callback_data=f"tx:delok:{tx_id}:{back}"),
        IB(text="Отмена", callback_data=f"tx:show:{tx_id}:{back}"),
    ]])


# ---------- списки ----------

def tx_list(rows, tz: ZoneInfo, year: int, month: int, page: int, pages: int) -> InlineKeyboardMarkup:
    back = f"L{_ym(year, month)}p{page}"
    kb = [[IB(text=reports.tx_button_text(tx, tz), callback_data=f"tx:show:{tx['id']}:{back}")]
          for tx in rows]

    nav = []
    if page > 0:
        nav.append(IB(text="◀️", callback_data=f"txs:{_ym(year, month)}:{page - 1}"))
    if pages > 1:
        nav.append(IB(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(IB(text="▶️", callback_data=f"txs:{_ym(year, month)}:{page + 1}"))
    if nav:
        kb.append(nav)

    now = datetime.now(tz)
    py, pm = reports.shift_month(year, month, -1)
    months_row = [IB(text=f"« {reports.MONTHS[pm - 1]}", callback_data=f"txs:{_ym(py, pm)}:0")]
    if (year, month) < (now.year, now.month):
        ny, nm = reports.shift_month(year, month, 1)
        months_row.append(IB(text=f"{reports.MONTHS[nm - 1]} »", callback_data=f"txs:{_ym(ny, nm)}:0"))
    kb.append(months_row)
    return InlineKeyboardMarkup(inline_keyboard=kb)


def pending_list(rows, tz: ZoneInfo) -> InlineKeyboardMarkup:
    kb = [[IB(text=reports.tx_button_text(tx, tz), callback_data=f"tx:show:{tx['id']}:P")]
          for tx in rows]
    kb.append([IB(text="🔄 Обновить", callback_data="pend")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def open_pending() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text="⏳ Разобрать", callback_data="pend")]])


# ---------- месяц / период / экспорт ----------

def month_nav(year: int, month: int, tz: ZoneInfo) -> InlineKeyboardMarkup:
    now = datetime.now(tz)
    py, pm = reports.shift_month(year, month, -1)
    row = [IB(text=f"« {reports.MONTHS[pm - 1]}", callback_data=f"mon:{_ym(py, pm)}")]
    if (year, month) < (now.year, now.month):
        ny, nm = reports.shift_month(year, month, 1)
        row.append(IB(text=f"{reports.MONTHS[nm - 1]} »", callback_data=f"mon:{_ym(ny, nm)}"))
    else:
        row.append(IB(text="🔄 Обновить", callback_data=f"mon:{_ym(year, month)}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def balance_nav(year: int, month: int, tz: ZoneInfo, earliest: tuple[int, int] | None
                ) -> InlineKeyboardMarkup:
    now = datetime.now(tz)
    row = []
    py, pm = reports.shift_month(year, month, -1)
    if earliest is None or (py, pm) >= earliest:
        row.append(IB(text=f"« {reports.MONTHS[pm - 1]}", callback_data=f"bal:{_ym(py, pm)}"))
    if (year, month) < (now.year, now.month):
        ny, nm = reports.shift_month(year, month, 1)
        row.append(IB(text=f"{reports.MONTHS[nm - 1]} »", callback_data=f"bal:{_ym(ny, nm)}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [IB(text="📈 Вся история", callback_data="bal:all"),
         IB(text="🔄 Обновить", callback_data=f"bal:{_ym(year, month)}")],
    ])


def balance_overview() -> InlineKeyboardMarkup:
    now_cb = "bal:now"
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="📊 По месяцам", callback_data=now_cb),
        IB(text="🔄 Обновить", callback_data="bal:all"),
    ]])


def periods(selected: int | None = None) -> InlineKeyboardMarkup:
    labels = {30: "1 мес", 60: "2 мес", 90: "3 мес", 180: "6 мес"}
    row = [IB(text=("• " if d == selected else "") + labels[d], callback_data=f"per:{d}")
           for d in PERIODS]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def export_options() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="Этот месяц", callback_data="exp:month"),
         IB(text="6 месяцев", callback_data="exp:180")],
        [IB(text="Всё время", callback_data="exp:all")],
    ])


def cancel(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text="Отмена", callback_data=cb)]])


def skip_comment() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="Пропустить", callback_data="man:skip"),
        IB(text="Отмена", callback_data="man:cancel"),
    ]])
