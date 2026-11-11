"""Background loop: run poll_outbox() when the reMarkable cloud says something synced.

The cloud's notifications websocket wakes the loop: every sync (the tablet's, or a root swap by any
other client) sends one message. A slow timer (POLL_FALLBACK_S) covers lost messages. Without a
websocket (SSH transport, RM_NOTIFICATIONS_URL=off, or while reconnecting) it polls every
POLL_INTERVAL_S instead. Old files are pruned once a day.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .config import Settings, get_settings
from .core.pipeline import InkBridge, get_bridge

log = logging.getLogger("inkbridge.poller")

# One sync can send several messages, and our own move to Archive sends one too; wait for them to settle.
DEBOUNCE_S = 2.0
MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 300.0
# A connection that stayed up this long resets the reconnect backoff.
STABLE_S = 60.0


class Listener:
    """Keeps the notifications websocket open, setting `wake` on every message and on (re)connect."""

    def __init__(self, transport, wake: asyncio.Event):
        self.transport = transport
        self.wake = wake
        self.connected = False

    @staticmethod
    def supported(transport) -> bool:
        return hasattr(transport, "notifications") and getattr(transport, "notifications_enabled", True)

    async def run(self) -> None:
        backoff = MIN_BACKOFF_S
        while True:
            started = time.monotonic()
            try:
                async with self.transport.notifications() as events:
                    self.connected = True
                    log.info("listening for reMarkable cloud notifications")
                    self.wake.set()  # catch up on anything synced while disconnected
                    async for event in events:
                        log.info(
                            "notification %s from %s",
                            event.get("event") or "?",
                            event.get("sourceDeviceDesc") or event.get("sourceDeviceID") or "?",
                        )
                        self.wake.set()
                log.info("notifications websocket closed; reconnecting")
            except Exception as e:
                log.warning("notifications unavailable: %s", e)
            self.connected = False
            if time.monotonic() - started >= STABLE_S:
                backoff = MIN_BACKOFF_S
            log.info("reconnecting in %.0fs (polling meanwhile)", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


async def run(bridge: InkBridge, settings: Settings, debounce_s: float = DEBOUNCE_S) -> None:
    wake = asyncio.Event()
    wake.set()  # poll once on start
    transport = bridge.transport
    listener = Listener(transport, wake) if Listener.supported(transport) else None
    task = asyncio.create_task(listener.run()) if listener else None
    if listener:
        log.info("watching /%s via notifications, safety poll every %ss", settings.outbox_folder, settings.poll_fallback_s)
    else:
        log.info("polling /%s every %ss", settings.outbox_folder, settings.poll_interval_s)
    last_prune = 0.0
    try:
        while True:
            connected = listener is not None and listener.connected
            timeout = settings.poll_fallback_s if connected else settings.poll_interval_s
            try:
                await asyncio.wait_for(wake.wait(), timeout)
                await asyncio.sleep(debounce_s)
            except TimeoutError:
                pass
            wake.clear()
            try:
                docs = await bridge.poll_outbox()
                for d in docs:
                    log.info("pulled %s %r: %d marks", d.doc_id, d.title, d.marks)
                if time.time() - last_prune > 86400:
                    log.info("pruned %d old docs", bridge.prune())
                    last_prune = time.time()
            except Exception:
                log.exception("poll failed")
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def run_forever() -> None:
    await run(get_bridge(), get_settings())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
