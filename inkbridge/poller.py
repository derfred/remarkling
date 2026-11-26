"""Background loop: run poll() when the reMarkable cloud says something synced.

The cloud's notifications websocket wakes the loop: every sync (the tablet's, or a root swap by any
other client) sends one message. A slow timer (POLL_FALLBACK_S) covers lost messages. Without a
websocket (RM_NOTIFICATIONS_URL=off, or while reconnecting) it polls every
POLL_INTERVAL_S instead. Old files are pruned once a day.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import logs
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

    async def run(self) -> None:
        backoff = MIN_BACKOFF_S
        while True:
            started = time.monotonic()
            try:
                async with self.transport.notifications() as events:
                    self.connected = True
                    log.info("notifications connected")
                    self.wake.set()  # catch up on anything synced while disconnected
                    async for event in events:
                        log.info("notification received", extra={
                            "event": event.get("event"),
                            "source_device": event.get("sourceDeviceDesc"),
                            "source_device_id": event.get("sourceDeviceID"),
                        })
                        self.wake.set()
                log.info("notifications closed")
            except Exception as e:
                log.warning("notifications unavailable", extra={"error": str(e), "error_type": type(e).__name__})
            self.connected = False
            if time.monotonic() - started >= STABLE_S:
                backoff = MIN_BACKOFF_S
            log.info("notifications reconnecting; polling meanwhile", extra={"backoff_s": backoff})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


async def run(bridge: InkBridge, settings: Settings, debounce_s: float = DEBOUNCE_S) -> None:
    wake = asyncio.Event()
    wake.set()  # poll once on start
    transport = bridge.transport
    listener = Listener(transport, wake) if transport.notifications_enabled else None
    task = asyncio.create_task(listener.run()) if listener else None
    if listener:
        log.info("poller started", extra={"mode": "notifications", "outbox": settings.outbox_folder,
                                          "poll_every_s": settings.poll_fallback_s})
    else:
        log.info("poller started", extra={"mode": "polling", "outbox": settings.outbox_folder,
                                          "poll_every_s": settings.poll_interval_s})
    last_prune = 0.0
    try:
        while True:
            connected = listener is not None and listener.connected
            timeout = settings.poll_fallback_s if connected else settings.poll_interval_s
            try:
                await asyncio.wait_for(wake.wait(), timeout)
                await asyncio.sleep(debounce_s)
                trigger = "wake"
            except TimeoutError:
                trigger = "timer"
            wake.clear()
            try:
                started = time.monotonic()
                fetches = getattr(transport, "blob_fetches", 0)
                result = await bridge.poll()
                log.info("poll finished", extra={
                    "trigger": trigger,
                    "pulled": len(result.pulled),
                    "new_ink": len(result.changed),
                    "failed": len(result.failed),
                    "blob_fetches": getattr(transport, "blob_fetches", 0) - fetches,
                    "duration_s": round(time.monotonic() - started, 3),
                })
                for d in result.pulled:
                    log.info("doc pulled", extra={"doc_id": d.doc_id, "title": d.title, "interpretation": d.interpretation})
                for c in result.changed:
                    log.info("new ink", extra={"doc_id": c.doc_id, "title": c.title, "location": c.location,
                                               "changed_pages": c.changed_pages})
                for f in result.failed:
                    log.warning("doc failed", extra={"doc_id": f.doc_id, "title": f.title, "stage": f.stage,
                                                  "attempts": f.attempts, "error": f.error})
                if time.time() - last_prune > 86400:
                    log.info("old docs pruned", extra={"count": bridge.prune()})
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
    logs.setup()
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
