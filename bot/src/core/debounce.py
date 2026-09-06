"""Per-key async timer-accumulator for the "batch of N" announcement.

A burst of forwards (album or loose) should yield one message, not one per
file. Each `add` (re)arms a timer; when it fires, the callback gets the whole
list for that key. asyncio-based, one in-flight timer per key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

Callback = Callable[[Hashable, list[Any]], Awaitable[None]]


class Accumulator:
    def __init__(self, callback: Callback, delay: float = 2.0):
        self._callback = callback
        self._delay = delay
        self._items: dict[Hashable, list[Any]] = {}
        self._timers: dict[Hashable, asyncio.Task] = {}

    def add(self, key: Hashable, item: Any) -> None:
        self._items.setdefault(key, []).append(item)
        timer = self._timers.get(key)
        if timer:
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._fire_later(key))

    async def _fire_later(self, key: Hashable) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        items = self._items.pop(key, [])
        self._timers.pop(key, None)
        await self._callback(key, items)
