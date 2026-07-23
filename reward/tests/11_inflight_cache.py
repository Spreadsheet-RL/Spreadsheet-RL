from __future__ import annotations

import threading
import time
from collections import OrderedDict
from copy import copy, deepcopy

from async_reward_api.inflight_cache import Inflight, InflightLruCache


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _test_owner_timeout_takeover() -> None:
    cache: InflightLruCache[str, str] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 0.05,
    )
    owner_started = threading.Event()
    owner_release = threading.Event()
    owner_result: list[str] = []

    def owner_compute() -> str:
        owner_started.set()
        owner_release.wait(timeout=2.0)
        return "owner"

    def run_owner() -> None:
        owner_result.append(cache.get_or_compute("k", owner_compute))

    owner_thread = threading.Thread(target=run_owner)
    owner_thread.start()
    _assert(owner_started.wait(timeout=1.0), "owner compute did not start")
    start = time.monotonic()
    takeover = cache.get_or_compute("k", lambda: "takeover")
    elapsed = time.monotonic() - start
    owner_release.set()
    owner_thread.join(timeout=1.0)
    _assert(not owner_thread.is_alive(), "owner thread did not finish")
    _assert(takeover == "takeover", f"waiter did not take over after timeout: {takeover!r}")
    _assert(elapsed < 0.5, f"waiter did not time out promptly: {elapsed:.3f}s")
    _assert(owner_result == ["owner"], f"owner result changed: {owner_result!r}")


def _test_error_result_not_cached() -> None:
    calls = {"count": 0}
    cache: InflightLruCache[str, tuple[bool, int]] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 1.0,
    )
    owner_started = threading.Event()
    owner_release = threading.Event()
    waiter_result: list[tuple[bool, int]] = []

    def compute_error() -> tuple[bool, int]:
        calls["count"] += 1
        owner_started.set()
        owner_release.wait(timeout=2.0)
        return False, calls["count"]

    def run_waiter() -> None:
        waiter_result.append(
            cache.get_or_compute(
                "k",
                lambda: (True, 999),
                should_cache=lambda entry: entry[0],
            )
        )

    owner_thread = threading.Thread(
        target=lambda: cache.get_or_compute(
            "k",
            compute_error,
            should_cache=lambda entry: entry[0],
        )
    )
    owner_thread.start()
    _assert(owner_started.wait(timeout=1.0), "error owner did not start")
    waiter_thread = threading.Thread(target=run_waiter)
    waiter_thread.start()
    owner_release.set()
    owner_thread.join(timeout=1.0)
    waiter_thread.join(timeout=1.0)
    _assert(not owner_thread.is_alive(), "error owner did not finish")
    _assert(not waiter_thread.is_alive(), "error waiter did not finish")
    _assert(waiter_result == [(False, 1)], f"waiter did not receive owner error result: {waiter_result!r}")

    success = cache.get_or_compute(
        "k",
        lambda: (True, 2),
        should_cache=lambda entry: entry[0],
    )
    _assert(success == (True, 2), f"error result was cached: {success!r}")


def _test_validate_hit_eviction() -> None:
    valid = {"value": True}
    calls = {"count": 0}
    cache: InflightLruCache[str, str] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 1.0,
    )

    def compute() -> str:
        calls["count"] += 1
        return f"value-{calls['count']}"

    first = cache.get_or_compute("k", compute, validate_hit=lambda _: valid["value"])
    second = cache.get_or_compute("k", compute, validate_hit=lambda _: valid["value"])
    valid["value"] = False
    third = cache.get_or_compute("k", compute, validate_hit=lambda _: valid["value"])
    _assert(first == "value-1", f"unexpected first value: {first!r}")
    _assert(second == "value-1", f"valid cache hit was not reused: {second!r}")
    _assert(third == "value-2", f"stale cache hit was not recomputed: {third!r}")


def _test_waiter_revalidates_inflight_result() -> None:
    calls = {"count": 0}
    cache: InflightLruCache[str, str] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 1.0,
    )
    owner_started = threading.Event()
    owner_release = threading.Event()
    owner_result: list[str] = []
    waiter_result: list[str] = []

    def owner_compute() -> str:
        owner_started.set()
        owner_release.wait(timeout=2.0)
        return "stale"

    def waiter_compute() -> str:
        calls["count"] += 1
        return "fresh"

    owner_thread = threading.Thread(
        target=lambda: owner_result.append(
            cache.get_or_compute("k", owner_compute, validate_hit=lambda value: value != "stale")
        )
    )
    owner_thread.start()
    _assert(owner_started.wait(timeout=1.0), "waiter validation owner did not start")

    waiter_thread = threading.Thread(
        target=lambda: waiter_result.append(
            cache.get_or_compute("k", waiter_compute, validate_hit=lambda value: value != "stale")
        )
    )
    waiter_thread.start()
    owner_release.set()
    owner_thread.join(timeout=1.0)
    waiter_thread.join(timeout=1.0)

    _assert(not owner_thread.is_alive(), "waiter validation owner did not finish")
    _assert(not waiter_thread.is_alive(), "waiter validation waiter did not finish")
    _assert(owner_result == ["stale"], f"owner result changed: {owner_result!r}")
    _assert(waiter_result == ["fresh"], f"waiter returned stale inflight result: {waiter_result!r}")
    _assert(calls["count"] == 1, f"waiter did not recompute exactly once: {calls['count']}")


def _test_dynamic_wait_getter() -> None:
    wait_s = {"value": 30.0}
    cache: InflightLruCache[str, str] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: wait_s["value"],
    )
    with cache.lock:
        cache.inflight["k"] = Inflight[str](event=threading.Event())

    wait_s["value"] = 0.05
    start = time.monotonic()
    takeover = cache.get_or_compute("k", lambda: "takeover")
    elapsed = time.monotonic() - start

    _assert(takeover == "takeover", f"waiter did not take over after dynamic timeout: {takeover!r}")
    _assert(elapsed < 1.0, f"dynamic timeout getter was ignored: {elapsed:.3f}s")


def _test_weighted_lru_eviction_and_direct_cache_operations() -> None:
    max_weight = {"value": 4}
    cache: InflightLruCache[str, tuple[str, int]] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 1.0,
        max_weight_getter=lambda: max_weight["value"],
        weight_getter=lambda value: value[1],
    )
    cache.get_or_compute("a", lambda: ("a", 2))
    cache.get_or_compute("b", lambda: ("b", 2))
    cache.get_or_compute("a", lambda: ("unexpected", 2))
    cache.get_or_compute("c", lambda: ("c", 2))
    with cache.lock:
        _assert(list(cache.cache) == ["a", "c"], f"weighted LRU order is wrong: {cache.cache!r}")

        cache.cache.clear()
        cache.cache["manual"] = ("manual", 4)
    cache.get_or_compute("fresh", lambda: ("fresh", 1))
    with cache.lock:
        _assert(
            list(cache.cache) == ["fresh"],
            f"direct cache mutation left stale weight accounting: {cache.cache!r}",
        )

        cache.cache["overweight"] = ("overweight", 5)
    max_weight["value"] = 3
    replacement = cache.get_or_compute("overweight", lambda: ("replacement", 1))
    _assert(
        replacement == ("replacement", 1),
        f"directly inserted overweight entry was returned: {replacement!r}",
    )
    with cache.lock:
        _assert(
            list(cache.cache.items()) == [("overweight", ("replacement", 1))],
            f"dynamic budget shrink left overweight entries: {cache.cache!r}",
        )

    max_weight["value"] = 0
    disabled_result = cache.get_or_compute("disabled", lambda: ("disabled", 1))
    _assert(disabled_result == ("disabled", 1), f"disabled cache changed result: {disabled_result!r}")
    with cache.lock:
        _assert(not cache.cache, f"zero weight budget left cached values: {cache.cache!r}")

    cache.clear()
    with cache.lock:
        _assert(not cache.cache, "weighted cache clear left cached values")
        _assert(not cache.inflight, "weighted cache clear left inflight values")


def _test_single_entry_over_weight_budget_is_not_cached() -> None:
    calls = {"count": 0}
    cache: InflightLruCache[str, tuple[str, int]] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 1.0,
        max_weight_getter=lambda: 3,
        weight_getter=lambda value: value[1],
    )

    def compute_heavy() -> tuple[str, int]:
        calls["count"] += 1
        return f"heavy-{calls['count']}", 4

    first = cache.get_or_compute("heavy", compute_heavy)
    second = cache.get_or_compute("heavy", compute_heavy)
    _assert(first == ("heavy-1", 4), f"unexpected first heavy result: {first!r}")
    _assert(second == ("heavy-2", 4), f"overweight result was cached: {second!r}")
    with cache.lock:
        _assert("heavy" not in cache.cache, "overweight entry remained cached")

    def compute_stale_heavy() -> tuple[str, int]:
        with cache.lock:
            cache.cache["heavy"] = ("newer", 1)
        return "stale-heavy", 4

    stale = cache.get_or_compute("heavy", compute_stale_heavy)
    _assert(stale == ("stale-heavy", 4), f"unexpected stale heavy result: {stale!r}")
    with cache.lock:
        _assert(
            cache.cache.get("heavy") == ("newer", 1),
            f"overweight owner removed a newer cache entry: {cache.cache!r}",
        )


def _test_weighted_cache_deduplicates_inflight_compute() -> None:
    cache: InflightLruCache[str, tuple[str, int]] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 1.0,
        max_weight_getter=lambda: 4,
        weight_getter=lambda value: value[1],
    )
    owner_started = threading.Event()
    owner_release = threading.Event()
    calls = {"count": 0}
    results: list[tuple[str, int]] = []

    def compute() -> tuple[str, int]:
        calls["count"] += 1
        owner_started.set()
        owner_release.wait(timeout=2.0)
        return "shared", 2

    owner = threading.Thread(target=lambda: results.append(cache.get_or_compute("k", compute)))
    waiter = threading.Thread(target=lambda: results.append(cache.get_or_compute("k", compute)))
    owner.start()
    _assert(owner_started.wait(timeout=1.0), "weighted cache owner did not start")
    waiter.start()
    owner_release.set()
    owner.join(timeout=1.0)
    waiter.join(timeout=1.0)
    _assert(not owner.is_alive() and not waiter.is_alive(), "weighted inflight threads did not finish")
    _assert(calls["count"] == 1, f"weighted inflight compute ran {calls['count']} times")
    _assert(results == [("shared", 2), ("shared", 2)], f"weighted inflight results differ: {results!r}")
    with cache.lock:
        _assert(list(cache.cache) == ["k"], f"weighted inflight result was not cached: {cache.cache!r}")
        _assert(not cache.inflight, f"weighted inflight marker leaked: {cache.inflight!r}")


def _test_weighted_owner_uses_current_budget_after_compute() -> None:
    max_weight = {"value": 10}
    cache: InflightLruCache[str, tuple[str, int]] = InflightLruCache(
        max_size_getter=lambda: 10,
        inflight_wait_s_getter=lambda: 1.0,
        max_weight_getter=lambda: max_weight["value"],
        weight_getter=lambda value: value[1],
    )
    owner_started = threading.Event()
    owner_release = threading.Event()
    results: list[tuple[str, int]] = []

    def compute() -> tuple[str, int]:
        owner_started.set()
        owner_release.wait(timeout=2.0)
        return "heavy", 8

    owner = threading.Thread(target=lambda: results.append(cache.get_or_compute("k", compute)))
    owner.start()
    _assert(owner_started.wait(timeout=1.0), "dynamic-budget owner did not start")
    max_weight["value"] = 1
    owner_release.set()
    owner.join(timeout=1.0)
    _assert(not owner.is_alive(), "dynamic-budget owner did not finish")
    _assert(results == [("heavy", 8)], f"dynamic-budget owner result changed: {results!r}")
    with cache.lock:
        _assert(not cache.cache, f"owner cached against stale weight budget: {cache.cache!r}")
        _assert(not cache.inflight, f"dynamic-budget inflight marker leaked: {cache.inflight!r}")


def _test_weighted_cache_does_not_reweigh_entries_on_hits_or_trim() -> None:
    weight_calls = {"count": 0}
    max_weight = {"value": 2_000}

    def weight_getter(value: tuple[str, int]) -> int:
        weight_calls["count"] += 1
        return value[1]

    cache: InflightLruCache[str, tuple[str, int]] = InflightLruCache(
        max_size_getter=lambda: 2_000,
        inflight_wait_s_getter=lambda: 1.0,
        max_weight_getter=lambda: max_weight["value"],
        weight_getter=weight_getter,
    )
    for index in range(1_000):
        key = str(index)
        cache.get_or_compute(key, lambda key=key: (key, 1))
    _assert(
        weight_calls["count"] == 1_000,
        f"weighted cache reweighed entries while filling: {weight_calls['count']}",
    )

    for index in range(1_000):
        key = str(index)
        cache.get_or_compute(key, lambda: ("unexpected", 1))
    _assert(
        weight_calls["count"] == 1_000,
        f"weighted cache reweighed entries on hits: {weight_calls['count']}",
    )

    max_weight["value"] = 500
    hit = cache.get_or_compute("999", lambda: ("unexpected", 1))
    _assert(hit == ("999", 1), f"dynamic weight trim evicted a recent entry: {hit!r}")
    _assert(
        weight_calls["count"] == 1_000,
        f"dynamic weight trim reweighed retained or evicted entries: {weight_calls['count']}",
    )
    with cache.lock:
        _assert(
            len(cache.cache) == 500,
            f"dynamic weight trim kept {len(cache.cache)} entries",
        )

    cache.get_or_compute("new", lambda: ("new", 1))
    _assert(
        weight_calls["count"] == 1_001,
        f"weighted insert did not weigh exactly one new value: {weight_calls['count']}",
    )
    with cache.lock:
        _assert(
            len(cache.cache) == 500,
            f"weighted insertion kept {len(cache.cache)} entries",
        )
        copied = cache.cache.copy()
        _assert(copied == cache.cache, f"weighted cache copy changed entries: {copied!r}")
        _assert(
            copied.total_weight == cache.cache.total_weight,
            f"weighted cache copy changed accounting: {copied.total_weight}",
        )
        shallow_copied = copy(cache.cache)
        _assert(
            shallow_copied == cache.cache
            and shallow_copied.total_weight == cache.cache.total_weight,
            f"weighted cache shallow copy changed state: {shallow_copied!r}",
        )
        deep_copied = deepcopy(cache.cache)
        _assert(
            deep_copied == cache.cache
            and deep_copied.total_weight == cache.cache.total_weight,
            f"weighted cache deep copy changed state: {deep_copied!r}",
        )
        base_copied = OrderedDict.copy(cache.cache)
        _assert(
            base_copied == cache.cache
            and base_copied.total_weight == cache.cache.total_weight,
            f"base OrderedDict copy changed state: {base_copied!r}",
        )
        base_copied["base-copy-new"] = ("base-copy-new", 2)
        _assert(
            base_copied.total_weight == cache.cache.total_weight + 2,
            f"base OrderedDict copy lost its weight getter: {base_copied.total_weight}",
        )


def _test_size_only_dynamic_budget_changes() -> None:
    max_size = {"value": 3}
    cache: InflightLruCache[str, str] = InflightLruCache(
        max_size_getter=lambda: max_size["value"],
        inflight_wait_s_getter=lambda: 1.0,
    )
    cache.get_or_compute("a", lambda: "stale-a")
    cache.get_or_compute("b", lambda: "stale-b")
    cache.get_or_compute("c", lambda: "stale-c")

    max_size["value"] = 1
    refreshed = cache.get_or_compute("a", lambda: "fresh-a")
    _assert(
        refreshed == "fresh-a", f"size shrink returned an evicted entry: {refreshed!r}"
    )
    with cache.lock:
        _assert(
            list(cache.cache.items()) == [("a", "fresh-a")],
            f"size shrink did not trim the size-only cache: {cache.cache!r}",
        )

    max_size["value"] = 0
    disabled = cache.get_or_compute("a", lambda: "disabled-a")
    _assert(
        disabled == "disabled-a",
        f"disabled size-only cache returned stale data: {disabled!r}",
    )
    with cache.lock:
        _assert(
            not cache.cache,
            f"disabled size-only cache retained entries: {cache.cache!r}",
        )

    max_size["value"] = 1
    reenabled = cache.get_or_compute("a", lambda: "reenabled-a")
    _assert(
        reenabled == "reenabled-a",
        f"size-only cache resurrected stale data: {reenabled!r}",
    )


def _test_size_only_owner_uses_current_budget_after_compute() -> None:
    max_size = {"value": 1}
    cache: InflightLruCache[str, str] = InflightLruCache(
        max_size_getter=lambda: max_size["value"],
        inflight_wait_s_getter=lambda: 1.0,
    )
    owner_started = threading.Event()
    owner_release = threading.Event()
    results: list[str] = []

    def compute() -> str:
        owner_started.set()
        owner_release.wait(timeout=2.0)
        return "owner"

    owner = threading.Thread(
        target=lambda: results.append(cache.get_or_compute("k", compute))
    )
    owner.start()
    _assert(
        owner_started.wait(timeout=1.0), "size-only dynamic-budget owner did not start"
    )
    max_size["value"] = 0
    owner_release.set()
    owner.join(timeout=1.0)
    _assert(not owner.is_alive(), "size-only dynamic-budget owner did not finish")
    _assert(
        results == ["owner"],
        f"size-only dynamic-budget owner result changed: {results!r}",
    )
    with cache.lock:
        _assert(
            not cache.cache, f"owner cached against stale size budget: {cache.cache!r}"
        )
        _assert(
            not cache.inflight,
            f"size-only dynamic-budget inflight marker leaked: {cache.inflight!r}",
        )


def main() -> int:
    _test_owner_timeout_takeover()
    _test_error_result_not_cached()
    _test_validate_hit_eviction()
    _test_waiter_revalidates_inflight_result()
    _test_dynamic_wait_getter()
    _test_weighted_lru_eviction_and_direct_cache_operations()
    _test_single_entry_over_weight_budget_is_not_cached()
    _test_weighted_cache_deduplicates_inflight_compute()
    _test_weighted_owner_uses_current_budget_after_compute()
    _test_weighted_cache_does_not_reweigh_entries_on_hits_or_trim()
    _test_size_only_dynamic_budget_changes()
    _test_size_only_owner_uses_current_budget_after_compute()
    print("OK: inflight cache behavior looks good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
