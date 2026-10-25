"""Фоновые задачи: сканер блокчейна, напоминания о неразобранных, итоги месяца."""
import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from aiogram import Bot

from . import balance
from . import keyboards as kb
from . import reports
from .chain import BscClient, Transfer
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

REMIND_AFTER = timedelta(hours=24)
REMIND_HOURS = range(10, 22)  # напоминания только днём, с 10:00 до 21:59 по МСК


# ---------- сканер ----------

async def scan_once(bot: Bot, db: Database, cfg: Config, client: BscClient) -> None:
    head = await client.block_number()
    safe_head = head - cfg.confirmations

    last = await db.get_kv("last_block")
    if last is None:
        # Первый запуск: учёт начинается с текущего момента, историю не тянем
        await db.set_kv("last_block", str(safe_head))
        log.info("Первый запуск, начинаю отслеживание с блока %s", safe_head)
        return

    start = int(last) + 1
    while start <= safe_head:
        end = min(start + cfg.max_block_range - 1, safe_head)
        transfers = await client.outgoing_usdt(cfg.wallet, start, end)
        incoming = await client.incoming_usdt(cfg.wallet, start, end)

        # все переводы — в историю баланса (тихо)
        await db.insert_flows(await flows_from(client, cfg.wallet, transfers + incoming))

        for t in transfers:
            if t.to_address == cfg.wallet:
                continue  # перевод самому себе
            if t.amount < cfg.min_amount:
                # нулевые/копеечные «переводы» — типичный спам address poisoning
                log.info("Пропущен мелкий перевод %s USDT (%s)", t.amount, t.tx_hash)
                continue

            tx_id = await db.insert_chain_tx(t.tx_hash, t.log_index, t.block_number,
                                             t.to_address, t.amount,
                                             await client.block_time(t.block_number))
            if tx_id is None:
                continue
            log.info("Новый перевод %s USDT → %s", t.amount, t.to_address)
            tx = await db.get_tx(tx_id)
            try:
                await bot.send_message(cfg.owner_id, reports.tx_card(tx, cfg.tz),
                                       reply_markup=kb.tx_card(tx))
            except Exception:  # noqa: BLE001 — транзакция уже в базе, напоминание всё равно придёт
                log.exception("Не удалось отправить уведомление о транзакции %s", tx_id)

        await db.set_kv("last_block", str(end))
        start = end + 1

    await update_balance(db, cfg, client, int(await db.get_kv("last_block")))


async def flows_from(client: BscClient, wallet: str, transfers: list[Transfer]) -> list[tuple]:
    rows = []
    for t in transfers:
        if t.amount <= 0 or t.from_address == t.to_address:
            continue
        direction = "out" if t.from_address == wallet else "in"
        counterparty = t.to_address if direction == "out" else t.from_address
        rows.append((t.tx_hash, t.log_index, t.block_number, direction, counterparty,
                     t.amount, await client.block_time(t.block_number)))
    return rows


async def update_balance(db: Database, cfg: Config, client: BscClient, block: int) -> None:
    """Запоминает реальный баланс на блоке и сверяет его с посчитанным по переводам."""
    try:
        actual = await client.usdt_balance(cfg.wallet, block)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось получить баланс: %s", e)
        return
    prev = await balance.get_anchor(db)
    if prev and prev[0] < block:
        expected = prev[1] + await db.net_flow_between_blocks(prev[0], block)
        if abs(expected - actual) > Decimal("0.000001"):
            log.warning("Баланс не сходится: ожидалось %s, в блокчейне %s (блоки %s–%s)",
                        expected, actual, prev[0], block)
    now = datetime.now(cfg.tz)
    await balance.set_anchor(db, block, actual, now)
    await balance.ensure_since(db, actual, now)


async def scanner_loop(bot: Bot, db: Database, cfg: Config, client: BscClient) -> None:
    while True:
        try:
            await scan_once(bot, db, cfg, client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка сканирования блокчейна")
        await asyncio.sleep(cfg.poll_interval)


# ---------- напоминания и итоги месяца ----------

async def monthly_report(bot: Bot, db: Database, cfg: Config) -> None:
    now = datetime.now(cfg.tz)
    current = f"{now:%Y-%m}"
    last = await db.get_kv("last_month_report")
    if last == current:
        return
    await db.set_kv("last_month_report", current)
    if last is None:
        return  # первый запуск — отчитываться пока не за что

    py, pm = reports.shift_month(now.year, now.month, -1)
    text = await reports.month_summary(db, cfg.tz, py, pm)
    await bot.send_message(cfg.owner_id, "🗓 <b>Итоги прошлого месяца</b>\n\n" + text,
                           reply_markup=kb.month_nav(py, pm, cfg.tz))

    pending = await db.count_pending()
    if pending:
        await bot.send_message(cfg.owner_id,
                               f"⏳ Ещё не разобрано транзакций: {pending}. "
                               "После разбора итог прошлого месяца можно пересчитать кнопкой выше.",
                               reply_markup=kb.open_pending())


async def pending_reminder(bot: Bot, db: Database, cfg: Config) -> None:
    now = datetime.now(cfg.tz)
    if now.hour not in REMIND_HOURS:
        return
    last = await db.get_kv("last_reminder")
    if last and now - datetime.fromisoformat(last) < REMIND_AFTER:
        return
    old = await db.count_pending_older_than(now - REMIND_AFTER)
    if not old:
        return
    total = await db.count_pending()
    await bot.send_message(cfg.owner_id,
                           f"⏰ У тебя {total} неразобранн{'ая транзакция' if total == 1 else 'ых транзакций'}. "
                           "Отметь, расход это или нет — иначе они не попадут в отчёт.",
                           reply_markup=kb.open_pending())
    await db.set_kv("last_reminder", now.isoformat())


async def housekeeping_loop(bot: Bot, db: Database, cfg: Config) -> None:
    while True:
        for job in (monthly_report, pending_reminder):
            try:
                await job(bot, db, cfg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в задаче %s", job.__name__)
        await asyncio.sleep(300)
