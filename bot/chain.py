"""Чтение исходящих переводов USDT (BEP-20) напрямую из BNB Smart Chain через публичный RPC.

Никаких API-ключей и приватных ключей не нужно — только публичный адрес кошелька.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

from .config import TRANSFER_TOPIC, USDT_CONTRACT, USDT_DECIMALS

log = logging.getLogger(__name__)


class RpcError(Exception):
    pass


@dataclass
class Transfer:
    tx_hash: str
    log_index: int
    block_number: int
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
        to_address="0x" + topics[2][-40:].lower(),
        amount=Decimal(int(entry["data"], 16)) / (Decimal(10) ** USDT_DECIMALS),
    )


class BscClient:
    def __init__(self, session: aiohttp.ClientSession, urls: list[str]):
        self.session = session
        self.urls = urls
        self._current = 0
        self._req_id = 0

    async def _call(self, method: str, params: list):
        last_error: Exception | None = None
        for shift in range(len(self.urls)):
            idx = (self._current + shift) % len(self.urls)
            url = self.urls[idx]
            self._req_id += 1
            payload = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
            try:
                async with self.session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    data = await resp.json(content_type=None)
                if "error" in data:
                    raise RpcError(f"{url}: {data['error']}")
                self._current = idx
                return data["result"]
            except Exception as e:  # noqa: BLE001 — пробуем следующий узел
                last_error = e
                log.warning("RPC %s %s не ответил: %s", url, method, e)
        raise RpcError(f"Все RPC-узлы недоступны: {last_error}")

    async def block_number(self) -> int:
        return int(await self._call("eth_blockNumber", []), 16)

    async def block_time(self, number: int) -> datetime:
        block = await self._call("eth_getBlockByNumber", [hex(number), False])
        return datetime.fromtimestamp(int(block["timestamp"], 16), tz=timezone.utc)

    async def outgoing_usdt(self, wallet: str, from_block: int, to_block: int) -> list[Transfer]:
        logs = await self._call("eth_getLogs", [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": USDT_CONTRACT,
            "topics": [TRANSFER_TOPIC, address_topic(wallet)],
        }])
        return [parse_transfer_log(e) for e in logs if not e.get("removed")]
