"""Small cache with an item limit, safe across threads (the server handles requests in parallel)."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Hashable, TypeVar

T = TypeVar("T")


class LRUCache:
    """Keeps the `limit` most recently used items; the oldest goes first."""

    def __init__(self, limit: int):
        self._limit = limit
        self._items: OrderedDict[Hashable, object] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable, compute: Callable[[], T]) -> T:
        """Returns the stored value or computes it (outside the lock: two equal requests may compute together,
        but nobody else is blocked)."""
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        value = compute()
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return value

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
