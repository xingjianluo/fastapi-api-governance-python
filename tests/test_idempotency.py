"""幂等处理测试，含并发重复提交。"""

import threading
import time

import pytest

from app.governance.errors import IdempotencyConflict, IdempotencyInProgress
from app.governance.idempotency import (
    ACQUIRED,
    IN_PROGRESS,
    REPLAY,
    IdempotencyStore,
    IdempotentResult,
)

from tests.govlog import gov_log


def test_acquire_replay_consistent_result():
    store = IdempotencyStore()
    assert store.begin("k1", "fp-a") == ACQUIRED
    assert store.begin("k1", "fp-a") == IN_PROGRESS
    store.finish("k1", IdempotentResult(201, b'{"id":"order-1"}'))
    assert store.begin("k1", "fp-a") == REPLAY
    got = store.get("k1")
    gov_log(
        "sequential-replay",
        {"key": "k1", "fingerprint": "fp-a"},
        {"stored_body": got.body.decode()},
        "first ACQUIRED then REPLAY same body",
    )
    assert got.status_code == 201
    assert got.body == b'{"id":"order-1"}'


def test_conflict_does_not_overwrite():
    store = IdempotencyStore()
    store.begin("k2", "fp-a")
    store.finish("k2", IdempotentResult(201, b'{"id":"original"}'))
    with pytest.raises(IdempotencyConflict) as exc:
        store.claim_or_raise("k2", "fp-different")
    got = store.get("k2")
    gov_log(
        "fingerprint-conflict",
        {"key": "k2", "original_fp": "fp-a", "new_fp": "fp-different"},
        {"kept_body": got.body.decode()},
        f"REJECTED {exc.value.code}; original kept",
    )
    assert exc.value.code == "idempotency_conflict"
    assert got.body == b'{"id":"original"}'  # 没有被覆盖


def test_concurrent_duplicate_submission_single_execution():
    """N 个并发相同请求，只能有 1 个 acquired，最终都拿到同一份结果。"""
    store = IdempotencyStore()
    outcomes: list[str] = []
    execution_count = {"n": 0}
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait()
        decision = store.begin("hot-key", "fp-x")
        with lock:
            outcomes.append(decision)
        if decision == ACQUIRED:
            execution_count["n"] += 1
            time.sleep(0.05)  # 模拟业务处理
            store.finish("hot-key", IdempotentResult(201, b'{"id":"only-one"}'))
        elif decision == IN_PROGRESS:
            result = store.wait_for_result("hot-key", timeout=2.0)
            assert result is not None
            with lock:
                outcomes.append(REPLAY)
            assert result.body == b'{"id":"only-one"}'

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    acquired = outcomes.count(ACQUIRED)
    replays = outcomes.count(REPLAY)
    gov_log(
        "concurrent-duplicate",
        {"threads": 8, "key": "hot-key", "fingerprint": "fp-x"},
        {"acquired": acquired, "in_progress_seen": outcomes.count(IN_PROGRESS), "replays": replays},
        f"executions={execution_count['n']}",
    )
    assert execution_count["n"] == 1
    assert acquired == 1
    assert store.get("hot-key").body == b'{"id":"only-one"}'


def test_in_progress_raises_distinct_error():
    store = IdempotencyStore()
    store.begin("k3", "fp-a")
    with pytest.raises(IdempotencyInProgress) as exc:
        store.claim_or_raise("k3", "fp-a")
    gov_log(
        "in-progress",
        {"key": "k3", "fingerprint": "fp-a"},
        {"retry_after_seconds": 1},
        f"REJECTED {exc.value.code}",
    )
    assert exc.value.code == "idempotency_in_progress"
