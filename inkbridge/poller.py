"""Background loop: poll_outbox() every POLL_INTERVAL_S seconds; prune old files once a day."""

from __future__ import annotations

import asyncio
import logging
import time

from .config import get_settings
from .core.pipeline import get_bridge

log = logging.getLogger("inkbridge.poller")


async def run_forever() -> None:
    settings = get_settings()
    bridge = get_bridge()
    last_prune = 0.0
    log.info("polling /%s every %ss", settings.outbox_folder, settings.poll_interval_s)
    while True:
        try:
            docs = await bridge.poll_outbox()
            for d in docs:
                log.info("pulled %s %r: %d marks", d.doc_id, d.title, d.marks)
            if time.time() - last_prune > 86400:
                log.info("pruned %d old docs", bridge.prune())
                last_prune = time.time()
        except Exception:
            log.exception("poll failed")
        await asyncio.sleep(settings.poll_interval_s)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
