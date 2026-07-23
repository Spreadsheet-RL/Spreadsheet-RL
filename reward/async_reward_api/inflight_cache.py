from __future__ import annotations

import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar, overload

K = TypeVar("K")
V = TypeVar("V")
T = TypeVar("T")

_MISSING = object()


class _TrackedOrderedDict(OrderedDict[K, V]):
    def __init__(self, weight_getter: Callable[[V], int] | None = None) -> None:
        super().__init__()
        self._weight_getter = weight_getter
        self._weights: dict[K, int] = {}
        self.total_weight = 0

    def _value_weight(self, value: V) -> int:
        if self._weight_getter is None:
            return 0
        return max(0, int(self._weight_getter(value)))

    def set_with_weight(self, key: K, value: V, weight: int) -> None:
        previous_weight = self._weights.get(key, 0)
        super().__setitem__(key, value)
        self._weights[key] = weight
        self.total_weight += weight - previous_weight

    def __setitem__(self, key: K, value: V) -> None:
        self.set_with_weight(key, value, self._value_weight(value))

    def __delitem__(self, key: K) -> None:
        super().__delitem__(key)
        self.total_weight -= self._weights.pop(key)

    @overload
    def pop(self, key: K) -> V: ...

    @overload
    def pop(self, key: K, default: V | T) -> V | T: ...

    def pop(self, key: K, default: object = _MISSING) -> V | object:
        try:
            value = super().__getitem__(key)
        except KeyError:
            if default is _MISSING:
                raise
            return default
        self.__delitem__(key)
        return value

    def popitem(self, last: bool = True) -> tuple[K, V]:
        if not self:
            raise KeyError("dictionary is empty")
        key = next(reversed(self)) if last else next(iter(self))
        return key, self.pop(key)

    def clear(self) -> None:
        super().clear()
        self._weights.clear()
        self.total_weight = 0

    def copy(self) -> _TrackedOrderedDict[K, V]:
        copied = type(self)()
        for key, value in self.items():
            copied.set_with_weight(key, value, self._weights[key])
        return copied

    def __copy__(self) -> _TrackedOrderedDict[K, V]:
        return self.copy()

    def __deepcopy__(self, memo: dict[int, object]) -> _TrackedOrderedDict[K, V]:
        copied = type(self)()
        memo[id(self)] = copied
        for key, value in self.items():
            copied.set_with_weight(
                deepcopy(key, memo),
                deepcopy(value, memo),
                self._weights[key],
            )
        return copied


def _new_tracked_ordered_dict(
    weight_getter: Callable[[V], int] | None,
) -> _TrackedOrderedDict[K, V]:
    class _ConfiguredTrackedOrderedDict(_TrackedOrderedDict[K, V]):
        def __init__(self) -> None:
            super().__init__(weight_getter)

    return _ConfiguredTrackedOrderedDict()


@dataclass
class Inflight(Generic[V]):
    event: threading.Event
    result: V | None = None
    exc: BaseException | None = None


class InflightLruCache(Generic[K, V]):
    def __init__(
        self,
        *,
        max_size_getter: Callable[[], int],
        inflight_wait_s_getter: Callable[[], float],
        max_weight_getter: Callable[[], int] | None = None,
        weight_getter: Callable[[V], int] | None = None,
    ) -> None:
        if (max_weight_getter is None) != (weight_getter is None):
            raise ValueError(
                "max_weight_getter and weight_getter must be configured together"
            )
        self._max_size_getter = max_size_getter
        self._inflight_wait_s_getter = inflight_wait_s_getter
        self._max_weight_getter = max_weight_getter
        self._weight_getter = weight_getter
        self.lock = threading.Lock()
        self.cache = _new_tracked_ordered_dict(weight_getter)
        self.inflight: dict[K, Inflight[V]] = {}

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()
            self.inflight.clear()

    def _value_weight(self, value: V) -> int:
        if self._weight_getter is None:
            return 0
        return max(0, int(self._weight_getter(value)))

    def _trim_cache(self, *, cache_size: int, cache_weight: int | None) -> None:
        if cache_size <= 0 or (cache_weight is not None and cache_weight <= 0):
            self.cache.clear()
            return

        while self.cache and (
            len(self.cache) > cache_size
            or (cache_weight is not None and self.cache.total_weight > cache_weight)
        ):
            self.cache.popitem(last=False)

    def _cache_result(
        self,
        key: K,
        value: V,
        *,
        cache_size: int,
        cache_weight: int | None,
    ) -> None:
        value_weight = self._value_weight(value)
        if cache_weight is not None and value_weight > cache_weight:
            return
        self.cache.set_with_weight(key, value, value_weight)
        self.cache.move_to_end(key)
        self._trim_cache(cache_size=cache_size, cache_weight=cache_weight)

    def get_or_compute(
        self,
        key: K,
        compute: Callable[[], V],
        *,
        validate_hit: Callable[[V], bool] | None = None,
        should_cache: Callable[[V], bool] | None = None,
    ) -> V:
        with self.lock:
            cache_size = self._max_size_getter()
            cache_weight = (
                self._max_weight_getter()
                if self._max_weight_getter is not None
                else None
            )
            self._trim_cache(cache_size=cache_size, cache_weight=cache_weight)
            cache_enabled = cache_size > 0 and (
                cache_weight is None or cache_weight > 0
            )
            if cache_enabled:
                hit = self.cache.get(key)
                if hit is not None:
                    if validate_hit is None or validate_hit(hit):
                        self.cache.move_to_end(key)
                        return hit
                    self.cache.pop(key, None)

                inflight = self.inflight.get(key)
                if inflight is None:
                    inflight = Inflight[V](event=threading.Event())
                    self.inflight[key] = inflight
                    is_owner = True
                else:
                    is_owner = False

        if not cache_enabled:
            return compute()

        if not is_owner:
            if not inflight.event.wait(timeout=float(self._inflight_wait_s_getter())):
                with self.lock:
                    if self.inflight.get(key) is inflight:
                        self.inflight.pop(key, None)
                return self._compute_and_maybe_cache(
                    key,
                    compute,
                    should_cache=should_cache,
                    inflight=None,
                )
            if inflight.exc is not None:
                raise inflight.exc
            if inflight.result is not None:
                if validate_hit is None or validate_hit(inflight.result):
                    return inflight.result
                with self.lock:
                    self.cache.pop(key, None)
            with self.lock:
                hit = self.cache.get(key)
                if hit is not None:
                    if validate_hit is None or validate_hit(hit):
                        self.cache.move_to_end(key)
                        return hit
                    self.cache.pop(key, None)
            return self._compute_and_maybe_cache(
                key,
                compute,
                should_cache=should_cache,
                inflight=None,
            )

        return self._compute_and_maybe_cache(
            key,
            compute,
            should_cache=should_cache,
            inflight=inflight,
        )

    def _compute_and_maybe_cache(
        self,
        key: K,
        compute: Callable[[], V],
        *,
        should_cache: Callable[[V], bool] | None,
        inflight: Inflight[V] | None,
    ) -> V:
        try:
            result = compute()
        except BaseException as exc:
            if inflight is not None:
                with self.lock:
                    inflight.exc = exc
                    if self.inflight.get(key) is inflight:
                        self.inflight.pop(key, None)
                    inflight.event.set()
            raise

        with self.lock:
            cache_size = self._max_size_getter()
            cache_weight = (
                self._max_weight_getter()
                if self._max_weight_getter is not None
                else None
            )
            self._trim_cache(cache_size=cache_size, cache_weight=cache_weight)
            cache_enabled = cache_size > 0 and (
                cache_weight is None or cache_weight > 0
            )
            if cache_enabled and (should_cache is None or should_cache(result)):
                self._cache_result(
                    key,
                    result,
                    cache_size=cache_size,
                    cache_weight=cache_weight,
                )
            if inflight is not None:
                inflight.result = result
                if self.inflight.get(key) is inflight:
                    self.inflight.pop(key, None)
                inflight.event.set()
        return result
