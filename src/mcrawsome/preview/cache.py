"""A byte-bounded LRU for reduced linear frames and finished preview pairs."""

from collections import OrderedDict
from collections.abc import Hashable
from threading import Lock


class FrameCache[T]:
    def __init__(self, limit: int) -> None:
        self._lock = Lock()
        self.limit = limit
        self.bytes = 0
        self._items: OrderedDict[Hashable, tuple[T, int]] = OrderedDict()

    def get(self, key: Hashable) -> T | None:
        with self._lock:
            return self._get(key)

    def usage(self) -> tuple[int, int, int]:
        """Retained payload bytes, configured limit and entry count in one locked snapshot."""
        with self._lock:
            return self.bytes, self.limit, len(self._items)

    def _get(self, key: Hashable) -> T | None:
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item[0]

    def put(self, key: Hashable, value: T, size: int) -> None:
        with self._lock:
            self._put(key, value, size)

    def _put(self, key: Hashable, value: T, size: int) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
        if size > self.limit:
            return
        while self._items and self.bytes + size > self.limit:
            _, (_, removed) = self._items.popitem(last=False)
            self.bytes -= removed
        self._items[key] = (value, size)
        self.bytes += size

    def retire(self) -> None:
        """Release a superseded generation and reject writes from cancelled lanes."""
        with self._lock:
            self.limit = 0
            self._items.clear()
            self.bytes = 0

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.bytes = 0
