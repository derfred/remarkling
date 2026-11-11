import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

import pytest

from inkbridge import poller


class NotifyingTransport:
    """Each connection replays messages put on `queue` until a None closes it; `fail` refuses one."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.connects = 0
        self.fail = 0

    @asynccontextmanager
    async def notifications(self):
        self.connects += 1
        if self.fail:
            self.fail -= 1
            raise RuntimeError("refused")

        async def events():
            while (event := await self.queue.get()) is not None:
                yield event

        yield events()


class CountingBridge:
    def __init__(self, transport):
        self.transport = transport
        self.polls = 0

    async def poll_outbox(self):
        self.polls += 1
        return []

    def prune(self):
        return 0


def settings(interval=60.0, fallback=60.0):
    return SimpleNamespace(outbox_folder="Outbox", poll_interval_s=interval, poll_fallback_s=fallback)


async def wait_until(cond, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.005)


async def stop(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(poller, "MIN_BACKOFF_S", 0.01)


async def test_notifications_trigger_polls_and_bursts_are_debounced():
    t = NotifyingTransport()
    bridge = CountingBridge(t)
    task = asyncio.create_task(poller.run(bridge, settings(), debounce_s=0.05))
    try:
        await wait_until(lambda: bridge.polls == 1)  # start-up / on-connect catch-up
        await asyncio.sleep(0.1)
        assert bridge.polls == 1

        for _ in range(3):
            t.queue.put_nowait({"event": "SyncComplete"})
        await wait_until(lambda: bridge.polls == 2)
        await asyncio.sleep(0.15)
        assert bridge.polls == 2  # three messages inside the debounce window, one poll
    finally:
        await stop(task)


async def test_reconnects_and_catches_up_after_drop():
    t = NotifyingTransport()
    t.fail = 1
    bridge = CountingBridge(t)
    task = asyncio.create_task(poller.run(bridge, settings(), debounce_s=0.01))
    try:
        await wait_until(lambda: t.connects == 2)  # first attempt refused, retried
        await wait_until(lambda: bridge.polls >= 1)
        polls = bridge.polls
        t.queue.put_nowait(None)  # server closes the connection
        await wait_until(lambda: t.connects == 3)
        await wait_until(lambda: bridge.polls > polls)  # reconnect triggers a catch-up poll
    finally:
        await stop(task)


async def test_falls_back_to_timer_without_notifications():
    bridge = CountingBridge(transport=object())  # e.g. SSH: no notifications()
    task = asyncio.create_task(poller.run(bridge, settings(interval=0.05), debounce_s=0))
    try:
        await wait_until(lambda: bridge.polls >= 3)
    finally:
        await stop(task)


async def test_safety_poll_while_connected():
    t = NotifyingTransport()
    bridge = CountingBridge(t)
    task = asyncio.create_task(poller.run(bridge, settings(interval=60, fallback=0.05), debounce_s=0))
    try:
        await wait_until(lambda: bridge.polls >= 4)
    finally:
        await stop(task)


def test_disabled_notifications_are_not_supported():
    assert not poller.Listener.supported(SimpleNamespace(notifications=None, notifications_enabled=False))
    assert poller.Listener.supported(NotifyingTransport())
