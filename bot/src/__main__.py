import asyncio
import logging

from .bot import build_client
from .config import load_config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


async def _amain() -> None:
    cfg = load_config()
    client, state = build_client(cfg)
    await client.start(bot_token=cfg.bot_token)
    state.start_queue()
    await client.run_until_disconnected()


asyncio.run(_amain())
