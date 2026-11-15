"""Fan-out for live dashboard updates (server-sent events).

The collector posts several samples per cycle and failed-login bursts arrive in
batches, so events are throttled per kind: clients get one wake-up per second at
most and then refetch the dashboard themselves.
"""

import asyncio
import time
from typing import Any

THROTTLE_SECONDS = 1.0


class DashboardHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_publish: dict[str, float] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, payload: dict[str, Any], throttle_seconds: float = THROTTLE_SECONDS) -> bool:
        """Queues an event for every connected client; returns False when throttled."""
        if not self._subscribers or self._loop is None or self._loop.is_closed():
            return False
        now = time.monotonic()
        last = self._last_publish.get(event)
        if last is not None and now - last < throttle_seconds:
            return False
        self._last_publish[event] = now
        try:
            self._loop.call_soon_threadsafe(self._fanout, (event, payload))
        except RuntimeError:  # the loop stopped between the check and the call
            return False
        return True

    def _fanout(self, message: tuple[str, dict[str, Any]]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)


hub = DashboardHub()
