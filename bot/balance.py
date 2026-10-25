"""История баланса USDT: расчёт по дням и графики."""
import io
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from . import reports  # noqa: E402
from .db import Database  # noqa: E402

MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн",
                "июл", "авг", "сен", "окт", "ноя", "дек"]
CURRENT_COLOR = "#2563EB"
PREV_COLOR = "#9CA3AF"


# ---------- состояние в kv ----------

async def get_anchor(db: Database) -> tuple[int, Decimal, datetime] | None:
    """Последний проверенный баланс: (блок, баланс, время)."""
    raw = await db.get_kv("balance_anchor")
    if not raw:
        return None
    d = json.loads(raw)
    return d["block"], Decimal(d["balance"]), datetime.fromisoformat(d["at"])


async def set_anchor(db: Database, block: int, balance: Decimal, at: datetime) -> None:
    await db.set_kv("balance_anchor", json.dumps(
        {"block": block, "balance": str(balance), "at": at.isoformat()}))


async def get_backfill(db: Database) -> dict | None:
    raw = await db.get_kv("backfill")
    return json.loads(raw) if raw else None


async def set_backfill(db: Database, state: dict) -> None:
    await db.set_kv("backfill", json.dumps(state))


def backfill_progress(state: dict) -> int:
    total = max(1, state["to"] - state["from"])
    return min(100, int((state["cursor"] - state["from"]) * 100 / total))


# ---------- расчёт ----------

def _day_start(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz)


async def daily_balances(db: Database, tz: ZoneInfo, first: date, last: date) -> dict[date, Decimal]:
    """Баланс на конец каждого дня [first, last]. Сегодняшний день — текущий баланс.

    Считается назад от последнего проверенного баланса: вычитаем все переводы,
    которые случились после конца нужного дня.
    """
    anchor = await get_anchor(db)
    state = await get_backfill(db)
    if anchor is None or not state or not state.get("done"):
        return {}
    anchor_block, running, _ = anchor
    since = datetime.fromisoformat(state["since"]).astimezone(tz).date()

    today = datetime.now(tz).date()
    last = min(last, today)
    flows = await db.flows_since(_day_start(first + timedelta(days=1), tz), anchor_block)

    result: dict[date, Decimal] = {}
    i = 0
    d = last
    while d >= first:
        if d < today:
            boundary = _day_start(d + timedelta(days=1), tz)
            while i < len(flows) and flows[i]["occurred_at"] >= boundary:
                f = flows[i]
                running -= f["amount"] if f["direction"] == "in" else -f["amount"]
                i += 1
        if d >= since - timedelta(days=1):
            result[d] = running
        d -= timedelta(days=1)
    return result


def _month_days(year: int, month: int) -> tuple[date, date]:
    ny, nm = reports.shift_month(year, month, 1)
    return date(year, month, 1), date(ny, nm, 1) - timedelta(days=1)


def _signed(x: Decimal) -> str:
    return ("+" if x >= 0 else "−") + reports.fmt(abs(x))


# ---------- графики ----------

def _thousands(x, _pos) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _style(ax) -> None:
    ax.grid(True, axis="y", color="#E5E7EB", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#D1D5DB")
    ax.tick_params(colors="#4B5563", labelsize=9)
    ax.yaxis.set_major_formatter(FuncFormatter(_thousands))


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def month_chart(title: str, cur_label: str, cur: dict[date, Decimal],
                prev_label: str | None, prev: dict[date, Decimal]) -> bytes:
    fig, ax = plt.subplots(figsize=(9, 4.8))
    if prev:
        xs = sorted(prev)
        ax.plot([d.day for d in xs], [float(prev[d]) for d in xs], color=PREV_COLOR,
                linestyle="--", linewidth=1.8, label=prev_label)
    if cur:
        xs = sorted(cur)
        ys = [float(cur[d]) for d in xs]
        ax.plot([d.day for d in xs], ys, color=CURRENT_COLOR, linewidth=2.4,
                marker="o", markersize=3, label=cur_label)
        ax.annotate(_thousands(ys[-1], None), (xs[-1].day, ys[-1]), textcoords="offset points",
                    xytext=(6, 6), fontsize=10, color=CURRENT_COLOR, fontweight="bold")
    ax.set_xlim(0.5, 31.5)
    ax.set_xticks([1, 5, 10, 15, 20, 25, 31])
    ax.set_xlabel("день месяца", color="#6B7280", fontsize=9)
    ax.set_title(title, loc="left", fontsize=13, color="#111827", pad=12)
    _style(ax)
    ax.legend(frameon=False, fontsize=9, loc="best")
    return _png(fig)


def overview_chart(series: dict[date, Decimal]) -> bytes:
    xs = sorted(series)
    ys = [float(series[d]) for d in xs]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(xs, ys, color=CURRENT_COLOR, linewidth=2)
    ax.fill_between(xs, ys, min(ys + [0]), color=CURRENT_COLOR, alpha=0.08)
    month_starts = [d for d in xs if d.day == 1]
    for d in month_starts:
        ax.axvline(d, color="#E5E7EB", linewidth=0.8, zorder=0)
    ax.set_xticks(month_starts)
    ax.set_xticklabels([f"{MONTHS_SHORT[d.month - 1]}\n{d.year}" if d.month == 1 or d == month_starts[0]
                        else MONTHS_SHORT[d.month - 1] for d in month_starts])
    ax.annotate(_thousands(ys[-1], None), (xs[-1], ys[-1]), textcoords="offset points",
                xytext=(-10, 8), ha="right", fontsize=10, color=CURRENT_COLOR, fontweight="bold")
    ax.set_title("Баланс USDT за 6 месяцев", loc="left", fontsize=13, color="#111827", pad=12)
    _style(ax)
    return _png(fig)


# ---------- то, что отдаём в бота ----------

async def status_text(db: Database) -> str | None:
    """Сообщение, если история ещё не готова; None — если всё готово."""
    state = await get_backfill(db)
    if not state:
        return "⏳ История баланса ещё не начала загружаться. Загляни через пару минут."
    if not state.get("done"):
        return (f"⏳ Восстанавливаю историю баланса за 6 месяцев: {backfill_progress(state)}%.\n"
                "Как закончу — пришлю сообщение.")
    if await get_anchor(db) is None:
        return "⏳ Ещё не успел проверить текущий баланс. Загляни через минуту."
    return None


async def month_view(db: Database, tz: ZoneInfo, year: int, month: int) -> tuple[str, bytes | None]:
    wait = await status_text(db)
    if wait:
        anchor = await get_anchor(db)
        if anchor:
            wait = f"💰 Баланс сейчас: <b>{reports.fmt(anchor[1])} USDT</b>\n\n" + wait
        return wait, None

    py, pm = reports.shift_month(year, month, -1)
    first_prev, _ = _month_days(py, pm)
    first, last = _month_days(year, month)
    series = await daily_balances(db, tz, first_prev - timedelta(days=1), last)

    cur = {d: v for d, v in series.items() if d.month == month and d.year == year}
    prev = {d: v for d, v in series.items() if d.month == pm and d.year == py}
    anchor = await get_anchor(db)

    lines = [f"💰 Баланс сейчас: <b>{reports.fmt(anchor[1])} USDT</b>", ""]
    for label, (y, m), data, start_key in (
        (reports.month_title(year, month), (year, month), cur, first - timedelta(days=1)),
        (reports.month_title(py, pm), (py, pm), prev, first_prev - timedelta(days=1)),
    ):
        if not data:
            continue
        start = series.get(start_key, data[min(data)])
        end = data[max(data)]
        end_label = "сейчас" if max(data) == datetime.now(tz).date() else f"{max(data):%d.%m}"
        lines.append(f"<b>{label}</b>: {reports.fmt(start)} → {reports.fmt(end)} ({end_label})  "
                     f"<b>{_signed(end - start)}</b>")

    if not cur:
        return "\n".join(lines + ["", "За этот месяц истории нет."]), None
    chart = month_chart(f"Баланс USDT — {reports.month_title(year, month).lower()}",
                        reports.month_title(year, month), cur,
                        reports.month_title(py, pm), prev)
    return "\n".join(lines), chart


async def overview_view(db: Database, tz: ZoneInfo) -> tuple[str, bytes | None]:
    wait = await status_text(db)
    if wait:
        return wait, None
    state = await get_backfill(db)
    since = datetime.fromisoformat(state["since"]).astimezone(tz).date()
    today = datetime.now(tz).date()
    series = await daily_balances(db, tz, since - timedelta(days=1), today)
    start_value = series.pop(since - timedelta(days=1), None)
    if len(series) < 2:
        return "Пока мало данных для графика.", None
    if start_value is None:
        start_value = series[min(series)]

    lines = [f"💰 Баланс сейчас: <b>{reports.fmt(series[today])} USDT</b>",
             f"На {since:%d.%m.%Y}: {reports.fmt(start_value)} → сейчас: "
             f"<b>{_signed(series[today] - start_value)}</b>", ""]
    # баланс на конец каждого месяца
    for d in sorted(series):
        nxt = d + timedelta(days=1)
        if nxt.day == 1 or d == today:
            lines.append(f"• {reports.month_title(d.year, d.month)}: {reports.fmt(series[d])}"
                         + (" (сейчас)" if d == today else ""))
    return "\n".join(lines), overview_chart(series)
