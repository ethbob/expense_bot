from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id           SERIAL PRIMARY KEY,
    source       TEXT NOT NULL CHECK (source IN ('chain', 'manual')),
    tx_hash      TEXT,
    log_index    INTEGER,
    block_number BIGINT,
    to_address   TEXT,
    amount       NUMERIC(38, 18) NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'expense', 'not_expense')),
    note         TEXT,
    occurred_at  TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS ix_transactions_occurred ON transactions (occurred_at);
CREATE INDEX IF NOT EXISTS ix_transactions_status ON transactions (status);

-- Все входящие и исходящие переводы USDT (для истории баланса)
CREATE TABLE IF NOT EXISTS usdt_flows (
    tx_hash      TEXT NOT NULL,
    log_index    INTEGER NOT NULL,
    block_number BIGINT NOT NULL,
    direction    TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    counterparty TEXT NOT NULL,
    amount       NUMERIC(38, 18) NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS ix_usdt_flows_block ON usdt_flows (block_number);
CREATE INDEX IF NOT EXISTS ix_usdt_flows_occurred ON usdt_flows (occurred_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SUMMARY_SQL = """
SELECT
    COALESCE(SUM(amount) FILTER (WHERE status = 'expense'), 0)                        AS exp_total,
    COUNT(*)             FILTER (WHERE status = 'expense')                            AS exp_count,
    COALESCE(SUM(amount) FILTER (WHERE status = 'expense' AND source = 'chain'), 0)  AS chain_total,
    COUNT(*)             FILTER (WHERE status = 'expense' AND source = 'chain')      AS chain_count,
    COALESCE(SUM(amount) FILTER (WHERE status = 'expense' AND source = 'manual'), 0) AS manual_total,
    COUNT(*)             FILTER (WHERE status = 'expense' AND source = 'manual')     AS manual_count,
    COALESCE(SUM(amount) FILTER (WHERE status = 'not_expense'), 0)                    AS not_total,
    COUNT(*)             FILTER (WHERE status = 'not_expense')                        AS not_count,
    COALESCE(SUM(amount) FILTER (WHERE status = 'pending'), 0)                        AS pend_total,
    COUNT(*)             FILTER (WHERE status = 'pending')                            AS pend_count
FROM transactions
WHERE occurred_at >= $1 AND occurred_at < $2
"""


class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Database":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        return cls(pool)

    async def init(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
            await conn.execute(
                "INSERT INTO kv (key, value) VALUES ('started_at', $1) ON CONFLICT (key) DO NOTHING",
                datetime.now(timezone.utc).isoformat(),
            )

    async def close(self) -> None:
        await self.pool.close()

    # --- key/value ---
    async def get_kv(self, key: str) -> str | None:
        return await self.pool.fetchval("SELECT value FROM kv WHERE key = $1", key)

    async def set_kv(self, key: str, value: str) -> None:
        await self.pool.execute(
            "INSERT INTO kv (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value,
        )

    # --- transactions ---
    async def insert_chain_tx(self, tx_hash: str, log_index: int, block_number: int,
                              to_address: str, amount: Decimal, occurred_at: datetime) -> int | None:
        """Возвращает id новой записи или None, если такая транзакция уже есть."""
        return await self.pool.fetchval(
            """
            INSERT INTO transactions (source, tx_hash, log_index, block_number, to_address,
                                      amount, status, occurred_at)
            VALUES ('chain', $1, $2, $3, $4, $5, 'pending', $6)
            ON CONFLICT (tx_hash, log_index) DO NOTHING
            RETURNING id
            """,
            tx_hash, log_index, block_number, to_address, amount, occurred_at,
        )

    async def add_manual(self, amount: Decimal, note: str | None, occurred_at: datetime) -> int:
        return await self.pool.fetchval(
            """
            INSERT INTO transactions (source, amount, status, note, occurred_at)
            VALUES ('manual', $1, 'expense', $2, $3)
            RETURNING id
            """,
            amount, note, occurred_at,
        )

    async def get_tx(self, tx_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM transactions WHERE id = $1", tx_id)

    async def set_status(self, tx_id: int, status: str) -> None:
        await self.pool.execute("UPDATE transactions SET status = $2 WHERE id = $1", tx_id, status)

    async def set_note(self, tx_id: int, note: str | None) -> None:
        await self.pool.execute("UPDATE transactions SET note = $2 WHERE id = $1", tx_id, note)

    async def delete_tx(self, tx_id: int) -> None:
        await self.pool.execute("DELETE FROM transactions WHERE id = $1", tx_id)

    async def count_txs(self, start: datetime, end: datetime) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE occurred_at >= $1 AND occurred_at < $2",
            start, end,
        )

    async def list_txs(self, start: datetime, end: datetime, offset: int, limit: int):
        return await self.pool.fetch(
            """
            SELECT * FROM transactions
            WHERE occurred_at >= $1 AND occurred_at < $2
            ORDER BY occurred_at DESC, id DESC
            OFFSET $3 LIMIT $4
            """,
            start, end, offset, limit,
        )

    async def list_pending(self, limit: int = 40):
        return await self.pool.fetch(
            "SELECT * FROM transactions WHERE status = 'pending' ORDER BY occurred_at LIMIT $1",
            limit,
        )

    async def count_pending(self) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM transactions WHERE status = 'pending'")

    async def count_pending_older_than(self, moment: datetime) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE status = 'pending' AND created_at <= $1",
            moment,
        )

    async def summary(self, start: datetime, end: datetime) -> asyncpg.Record:
        return await self.pool.fetchrow(SUMMARY_SQL, start, end)

    # --- поток USDT для баланса ---
    async def insert_flows(self, flows: list[tuple]) -> None:
        """flows: (tx_hash, log_index, block_number, direction, counterparty, amount, occurred_at)"""
        if not flows:
            return
        await self.pool.executemany(
            """
            INSERT INTO usdt_flows (tx_hash, log_index, block_number, direction,
                                    counterparty, amount, occurred_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (tx_hash, log_index) DO NOTHING
            """,
            flows,
        )

    async def net_flow_between_blocks(self, after_block: int, upto_block: int) -> Decimal:
        return await self.pool.fetchval(
            """
            SELECT COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE -amount END), 0)
            FROM usdt_flows WHERE block_number > $1 AND block_number <= $2
            """,
            after_block, upto_block,
        )

    async def flows_since(self, start: datetime, upto_block: int):
        return await self.pool.fetch(
            """
            SELECT direction, amount, occurred_at FROM usdt_flows
            WHERE occurred_at >= $1 AND block_number <= $2
            ORDER BY occurred_at DESC
            """,
            start, upto_block,
        )

    async def export_rows(self, start: datetime | None):
        if start is None:
            return await self.pool.fetch("SELECT * FROM transactions ORDER BY occurred_at, id")
        return await self.pool.fetch(
            "SELECT * FROM transactions WHERE occurred_at >= $1 ORDER BY occurred_at, id", start
        )
