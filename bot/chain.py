"""Чтение переводов и баланса USDT (BEP-20) напрямую из BNB Smart Chain через RPC.

Никаких API-ключей и приватных ключей не нужно — только публичный адрес кошелька.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

from .config import TRANSFER_TOPIC, USDT_CONTRACT, USDT_DECIMALS

log = logging.getLogger(__name__)

UNIT = Decimal(10) ** USDT_DECIMALS
BALANCE_OF = "0x70a08231"  # balanceOf(address)


class RpcError(Exception):
    pass


@dataclass
class Transfer:
    tx_hash: str
    log_index: int
    block_number: int
    from_address: str
    to_address: str
    amount: Decimal


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower()[2:]


def parse_transfer_log(entry: dict) -> Transfer:
    topics = entry["topics"]
    return Transfer(
        tx_hash=entry["transactionHash"].lower(),
        log_index=int(entry["logIndex"], 16),
        block_number=int(entry["blockNumber"], 16),
        from_address="0x" + topics[1][-40:].lower(),
        to_address="0x" + topics[2][-40:].lower(),
        amount=Decimal(int(entry["data"], 16)) / UNIT,
    )


class BscClient:
    def __init__(self, session: aiohttp.ClientSession, urls: list[str]):
        self.session = session
        self.urls = urls
        self._current = 0
        self._req_id = 0
        self._block_time_cache: dict[int, datetime] = {}

    async def _call(self, method: str, params: list):
        last_error: Exception | None = None
        for shift in range(len(self.urls)):
            idx = (self._current + shift) % len(self.urls)
            url = self.urls[idx]
            self._req_id += 1
            payload = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
            try:
                async with self.session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    data = await resp.json(content_type=None)
                if "error" in data:
                    raise RpcError(f"{url}: {data['error']}")
                if data.get("result") is None and method != "eth_getLogs":
                    raise RpcError(f"{url}: пустой ответ на {method}")
                self._current = idx
                return data["result"]
            except Exception as e:  # noqa: BLE001 — пробуем следующий узел
                last_error = e
                log.debug("RPC %s %s: %s", url, method, e)
        raise RpcError(f"Все RPC-узлы не ответили на {method}: {last_error}")

    async def block_number(self) -> int:
        return int(await self._call("eth_blockNumber", []), 16)

    async def block_time(self, number: int) -> datetime:
        if number not in self._block_time_cache:
            block = await self._call("eth_getBlockByNumber", [hex(number), False])
            if len(self._block_time_cache) > 5000:
                self._block_time_cache.clear()
            self._block_time_cache[number] = datetime.fromtimestamp(
                int(block["timestamp"], 16), tz=timezone.utc)
        return self._block_time_cache[number]

    async def find_block_at(self, moment: datetime) -> int:
        """Первый блок с временем >= moment (бинарный поиск)."""
        lo, hi = 1, await self.block_number()
        while lo < hi:
            mid = (lo + hi) // 2
            if await self.block_time(mid) < moment:
                lo = mid + 1
            else:
                hi = mid
        return lo

    async def usdt_balance(self, wallet: str, block: int) -> Decimal:
        data = BALANCE_OF + "0" * 24 + wallet.lower()[2:]
        raw = await self._call("eth_call", [{"to": USDT_CONTRACT, "data": data}, hex(block)])
        return Decimal(int(raw, 16)) / UNIT

    async def _logs(self, topics: list, from_block: int, to_block: int) -> list[Transfer]:
        logs = await self._call("eth_getLogs", [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": USDT_CONTRACT,
            "topics": topics,
        }])
        return [parse_transfer_log(e) for e in logs or [] if not e.get("removed")]

    async def outgoing_usdt(self, wallet: str, from_block: int, to_block: int) -> list[Transfer]:
        return await self._logs([TRANSFER_TOPIC, address_topic(wallet)], from_block, to_block)

    async def incoming_usdt(self, wallet: str, from_block: int, to_block: int) -> list[Transfer]:
        return await self._logs([TRANSFER_TOPIC, None, address_topic(wallet)], from_block, to_block)
