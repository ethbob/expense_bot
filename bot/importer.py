"""Разовый импорт истории переводов из CSV-выгрузки BscScan (Token Transfers BEP-20)."""
import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from . import balance, reports
from .config import USDT_CONTRACT
from .db import Database

REQUIRED = {"Transaction Hash", "Blockno", "UnixTimestamp", "From", "To", "TokenValue", "ContractAddress"}


class ImportErrorText(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass
class ImportResult:
    imported: int
    incoming: Decimal
    outgoing: Decimal
    skipped: int
    first_at: datetime
    start_balance: Decimal
    check_diff: Decimal | None  # расхождение с балансом на момент старта учёта, если его можно проверить
    min_balance: Decimal        # минимальный баланс по всей истории (минус = в файле нет начала)


def parse_csv(data: bytes, wallet: str) -> tuple[list[tuple], int]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportErrorText("Не получилось прочитать файл — это точно CSV из BscScan?")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
        raise ImportErrorText(
            "Это не похоже на выгрузку BscScan. Нужен CSV со вкладки «Token Transfers (BEP-20)».")

    usdt = USDT_CONTRACT.lower()
    seen_in_tx: dict[str, int] = defaultdict(int)
    flows, skipped = [], 0
    for r in reader:
        frm, to = r["From"].strip().lower(), r["To"].strip().lower()
        try:
            amount = Decimal(r["TokenValue"].replace(",", "").strip())
        except InvalidOperation:
            skipped += 1
            continue
        if (r["ContractAddress"].strip().lower() != usdt or frm == to
                or wallet not in (frm, to) or amount <= 0):
            skipped += 1  # другие (в т.ч. поддельные) токены, нули, переводы себе
            continue
        tx_hash = r["Transaction Hash"].strip().lower()
        seen_in_tx[tx_hash] += 1
        # в выгрузке нет номера события в транзакции — используем отрицательный порядковый
        log_index = -seen_in_tx[tx_hash]
        direction = "out" if frm == wallet else "in"
        flows.append((tx_hash, log_index, int(r["Blockno"]), direction,
                      to if direction == "out" else frm, amount,
                      datetime.fromtimestamp(int(r["UnixTimestamp"]), tz=timezone.utc)))
    if not flows:
        raise ImportErrorText("В файле нет переводов USDT с этого кошелька. Проверь адрес и вкладку.")
    return flows, skipped


async def import_csv(db: Database, wallet: str, data: bytes) -> ImportResult:
    anchor = await balance.get_anchor(db)
    if anchor is None:
        raise ImportErrorText("Я ещё ни разу не проверил текущий баланс. Подожди минуту и отправь файл снова.")
    anchor_block, anchor_balance, _ = anchor

    flows, skipped = parse_csv(data, wallet)
    max_block = max(f[2] for f in flows)
    first_at = min(f[6] for f in flows)
    if max_block > anchor_block:
        # файл новее последней проверки баланса — переводы после неё бот запишет сам
        flows = [f for f in flows if f[2] <= anchor_block]
        max_block = anchor_block

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            # всё, что покрывает файл, заменяем данными из файла
            await conn.execute("DELETE FROM usdt_flows WHERE block_number <= $1", max_block)
            await conn.executemany(
                """
                INSERT INTO usdt_flows (tx_hash, log_index, block_number, direction,
                                        counterparty, amount, occurred_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (tx_hash, log_index) DO NOTHING
                """,
                flows,
            )
            net_all = await conn.fetchval(
                """SELECT COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE -amount END), 0)
                   FROM usdt_flows WHERE block_number <= $1""", anchor_block)

    start_balance = anchor_balance - net_all

    running = min_balance = start_balance
    for row in await db.pool.fetch(
            "SELECT direction, amount FROM usdt_flows WHERE block_number <= $1 "
            "ORDER BY block_number, log_index", anchor_block):
        running += row["amount"] if row["direction"] == "in" else -row["amount"]
        min_balance = min(min_balance, running)

    # Проверка: баланс на момент старта учёта, посчитанный по файлу, должен совпасть с реальным
    check_diff = None
    old_since = await balance.get_since(db)
    if old_since and old_since[0] > first_at:
        after = await db.pool.fetchval(
            """SELECT COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE -amount END), 0)
               FROM usdt_flows WHERE occurred_at > $1 AND block_number <= $2""",
            old_since[0], anchor_block)
        check_diff = (anchor_balance - after) - old_since[1]

    since_at = first_at - timedelta(seconds=1)
    await db.set_kv("balance_since", json.dumps(
        {"at": since_at.isoformat(), "balance": str(start_balance)}))

    return ImportResult(
        imported=len(flows),
        incoming=sum((f[5] for f in flows if f[3] == "in"), Decimal(0)),
        outgoing=sum((f[5] for f in flows if f[3] == "out"), Decimal(0)),
        skipped=skipped,
        first_at=first_at,
        start_balance=start_balance,
        check_diff=check_diff,
        min_balance=min_balance,
    )


def result_text(res: ImportResult, tz: ZoneInfo) -> str:
    lines = [
        "✅ <b>История загружена</b>",
        f"Переводов USDT: {res.imported} (пришло {reports.fmt(res.incoming)}, "
        f"ушло {reports.fmt(res.outgoing)})",
        f"Пропущено строк: {res.skipped} — другие токены, спам и нулевые переводы",
        f"История баланса теперь с {res.first_at.astimezone(tz):%d.%m.%Y}",
        "",
        f"Баланс до первого перевода в файле: {reports.fmt(res.start_balance)} USDT",
    ]
    if res.min_balance < Decimal("-0.01"):
        lines.append("⚠️ По файлу баланс где-то уходит в минус — значит, в выгрузке не хватает "
                     "части входящих переводов. Выгрузи период с самого начала и отправь файл ещё раз.")
    elif abs(res.start_balance) < Decimal("0.01"):
        lines.append("✓ Кошелёк начинался с нуля — история полная.")
    else:
        lines.append("ℹ️ На кошельке уже были деньги до начала выгрузки — график начнётся с этой суммы.")
    if res.check_diff is not None:
        if abs(res.check_diff) < Decimal("0.01"):
            lines.append("✓ Сверка с реальным балансом пройдена.")
        else:
            lines.append(f"⚠️ Расхождение с реальным балансом: {reports.fmt(res.check_diff)} USDT. "
                         "Похоже, файл выгружен не до сегодняшнего дня — выгрузи заново и отправь ещё раз.")
    lines.append("\nЖми «💰 Баланс».")
    return "\n".join(lines)
