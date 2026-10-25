import os
import re
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

# USDT (BEP-20) в BNB Smart Chain
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
USDT_DECIMALS = 18
# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

DEFAULT_RPC_URLS = [
    "https://bsc-rpc.publicnode.com",
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc.drpc.org",
]


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    wallet: str  # lowercase 0x...
    database_url: str
    rpc_urls: list[str]
    tz: ZoneInfo
    poll_interval: int
    confirmations: int
    max_block_range: int
    min_amount: Decimal


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def load_config() -> Config:
    wallet = _required("WALLET_ADDRESS").lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", wallet):
        raise RuntimeError("WALLET_ADDRESS должен быть адресом вида 0x + 40 hex-символов")

    rpc_env = os.getenv("BSC_RPC_URLS", "").strip()
    rpc_urls = [u.strip() for u in rpc_env.split(",") if u.strip()] or DEFAULT_RPC_URLS

    return Config(
        bot_token=_required("BOT_TOKEN"),
        owner_id=int(_required("OWNER_ID")),
        wallet=wallet,
        database_url=_required("DATABASE_URL"),
        rpc_urls=rpc_urls,
        tz=ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow")),
        poll_interval=int(os.getenv("POLL_INTERVAL", "30")),
        confirmations=int(os.getenv("CONFIRMATIONS", "5")),
        max_block_range=int(os.getenv("MAX_BLOCK_RANGE", "500")),
        min_amount=Decimal(os.getenv("MIN_AMOUNT", "0.01")),
    )
