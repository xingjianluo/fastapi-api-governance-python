"""Idempotency: concurrent collapse executes once; conflict never overwrites."""

import threading

from app.idempotency import IdempotencyStore, StoredResponse

from .conftest import log_decision


def test_concurrent_identical_requests_execute_once():
    store = IdempotencyStore()
    digest = IdempotencyStore.hash_payload(b'{"amount":100}')
    n = 16
    barrier = threading.Barrier(n)
    outcomes: dict[int, tuple] = {}
    executions = {"count": 0}
    lock = threading.Lock()

    def worker(i: int):
        barrier.wait()
        with store.reserve("KEY-1", digest, '{"amount":100}') as (kind, obj):
            if kind == "execute":
                with lock:
                    executions["count"] += 1
                threading.Event().wait(0.15)  # simulate slow write
                store.complete(obj, StoredResponse(
                    201, b'{"id":"ORD-X"}', {"h": "v"}, digest,
                    '{"amount":100}'))
                outcomes[i] = ("execute", b'{"id":"ORD-X"}')
            elif kind == "replay":
                outcomes[i] = ("replay", obj.body)
            else:
                outcomes[i] = ("conflict", None)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()

    executed = sum(1 for kind, _ in outcomes.values() if kind == "execute")
    replayed = sum(1 for kind, _ in outcomes.values() if kind == "replay")
    log_decision(
        "idempotency.concurrent_same_key",
        {"threads": n, "key": "KEY-1", "payload_hash": digest[:12]},
        "single in-flight slot; waiters block on threading.Event and replay",
        f"executed={executed}, replayed={replayed}, all_same_result="
        f"{all(body == b'{\"id\":\"ORD-X\"}' for _, body in outcomes.values())}",
    )
    assert executions["count"] == 1
    assert executed == 1 and replayed == n - 1
    assert all(body == b'{"id":"ORD-X"}' for _, body in outcomes.values())


def test_conflicting_payload_rejected_and_original_preserved():
    store = IdempotencyStore()
    h1 = IdempotencyStore.hash_payload(b'{"amount":100}')
    h2 = IdempotencyStore.hash_payload(b'{"amount":999}')

    with store.reserve("KEY-2", h1, '{"amount":100}') as (kind, slot):
        assert kind == "execute"
        store.complete(slot, StoredResponse(
            201, b'{"id":"ORD-1","amount":100}', {}, h1, '{"amount":100}'))

    with store.reserve("KEY-2", h2, '{"amount":999}') as (kind, stored):
        log_decision(
            "idempotency.payload_conflict",
            {"key": "KEY-2", "h1": h1[:12], "h2": h2[:12]},
            "request hash differs for same key -> conflict, no overwrite",
            f"kind={kind}, original={stored.body.decode()}",
        )
        assert kind == "conflict"
        assert stored.body == b'{"id":"ORD-1","amount":100}'

    # original result still intact and re-readable
    assert store.stored("KEY-2").body == b'{"id":"ORD-1","amount":100}'


def test_failed_first_attempt_allows_retry_then_replays():
    store = IdempotencyStore()
    h = IdempotencyStore.hash_payload(b"{}")
    # First attempt: acquired execution but the handler marks it failed
    # (mirrors the 422 path in app/main.py) without completing a response.
    with store.reserve("KEY-3", h, "{}") as (kind, slot):
        assert kind == "execute"
        store.fail(slot)

    # Retry takes over the failed slot and executes.
    with store.reserve("KEY-3", h, "{}") as (kind, slot):
        log_decision("idempotency.failure_retryable", {"key": "KEY-3"},
                     "first attempt failed before storing -> retry executes",
                     f"retry kind={kind}")
        assert kind == "execute"
        store.complete(slot, StoredResponse(200, b"ok", {}, h, "{}"))

    # Third call now replays the successful result.
    with store.reserve("KEY-3", h, "{}") as (kind, stored):
        assert kind == "replay" and stored.body == b"ok"


def test_exception_during_execution_releases_entire_slot():
    store = IdempotencyStore()
    h = IdempotencyStore.hash_payload(b"{}")
    try:
        with store.reserve("KEY-4", h, "{}") as (kind, slot):
            assert kind == "execute"
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Slot was deleted on exception -> a fresh execute is possible.
    with store.reserve("KEY-4", h, "{}") as (kind, slot):
        assert kind == "execute"
        store.complete(slot, StoredResponse(200, b"recovered", {}, h, "{}"))
    assert store.stored("KEY-4").body == b"recovered"


def test_distinct_keys_are_independent():
    store = IdempotencyStore()
    h = IdempotencyStore.hash_payload(b"{}")
    with store.reserve("A", h, "{}") as (k, s):
        assert k == "execute"
        store.complete(s, StoredResponse(201, b"A", {}, h, "{}"))
    with store.reserve("B", h, "{}") as (k, s):
        assert k == "execute"
        store.complete(s, StoredResponse(201, b"B", {}, h, "{}"))
    assert store.stored("A").body == b"A"
    assert store.stored("B").body == b"B"
