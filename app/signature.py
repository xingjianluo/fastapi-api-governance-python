"""HMAC request signing with timestamp window, key rotation and replay cache.

Canonical string (newline separated, exact bytes)::

    POST
    /orders
    <unix timestamp>
    <nonce>
    <sha256-hex of raw request body>

Key rotation uses explicit, inclusive validity intervals. During the overlap
period both keys verify successfully; the boundary is unambiguous:

* ``not_valid_before <= timestamp <= not_valid_after`` -> candidate key
* only after the old key's ``not_valid_after`` is it rejected (SIG_INVALID)
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from dataclasses import dataclass, field

from app.clock import Clock
from app.errors import (
    SIG_EXPIRED,
    SIG_INVALID,
    SIG_MISSING_CREDENTIALS,
    SIG_REPLAY,
    SIG_UNKNOWN_CALLER,
)

SKEW_SECONDS_DEFAULT = 300.0


@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    not_valid_before: float = 0.0
    not_valid_after: float = float("inf")

    def valid_at(self, ts: float) -> bool:
        return self.not_valid_before <= ts <= self.not_valid_after


class KeyRing:
    """Per-caller key set supporting overlapping rotation windows."""

    def __init__(self) -> None:
        self._callers: dict[str, list[KeyEntry]] = {}
        self._lock = threading.RLock()

    def register_caller(self, caller_id: str, keys: list[KeyEntry]) -> None:
        with self._lock:
            self._callers[caller_id] = list(keys)

    def caller_keys(self, caller_id: str) -> list[KeyEntry] | None:
        with self._lock:
            keys = self._callers.get(caller_id)
            return list(keys) if keys is not None else None

    def rotate(self, caller_id: str, new_key: KeyEntry) -> None:
        """Add a newly valid key.

        The overlap/grace boundary is expressed declaratively through each
        key's ``not_valid_before``/``not_valid_after`` interval, so callers
        register the old key with its retirement time and the new key with
        its activation time; this method simply publishes the new entry.
        """
        with self._lock:
            self._callers.setdefault(caller_id, []).append(new_key)


@dataclass
class SignatureDecision:
    accepted: bool
    error: object = None
    detail: str | None = None
    matched_key_id: str | None = None
    context: dict = field(default_factory=dict)


class ReplayCache:
    """Remembers used (caller, nonce) pairs until after the skew window."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def check_and_record(self, caller: str, nonce: str, ttl: float) -> bool:
        """Return True if fresh (and record it), False if a replay."""
        with self._lock:
            self._prune()
            key = (caller, nonce)
            if key in self._seen:
                return False
            self._seen[key] = self._clock.now() + ttl
            return True

    def _prune(self) -> None:
        now = self._clock.now()
        expired = [k for k, expiry in self._seen.items() if expiry <= now]
        for k in expired:
            del self._seen[k]

    def __len__(self) -> int:
        with self._lock:
            self._prune()
            return len(self._seen)


def build_canonical_string(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return "\n".join([method.upper(), path, timestamp, nonce, body_hash]).encode()


def sign(
    secret: bytes,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    mac = hmac.new(
        secret,
        build_canonical_string(method, path, timestamp, nonce, body),
        hashlib.sha256,
    )
    return mac.hexdigest()


def verify_signature(
    *,
    headers,
    raw_body: bytes,
    method: str,
    path: str,
    key_ring: KeyRing,
    clock: Clock,
    replay_cache: ReplayCache,
    skew_seconds: float = SKEW_SECONDS_DEFAULT,
) -> SignatureDecision:
    """Validate credentials, timestamp window, HMAC and replay status.

    Checks are ordered so rejections are distinguishable:
    missing headers -> unknown caller -> expired timestamp -> bad signature
    -> replay.
    """
    caller = _header(headers, "x-caller-id")
    timestamp_raw = _header(headers, "x-timestamp")
    nonce = _header(headers, "x-nonce")
    signature = _header(headers, "x-signature")

    if not all([caller, timestamp_raw, nonce, signature]):
        return SignatureDecision(
            False, SIG_MISSING_CREDENTIALS,
            "All of X-Caller-Id, X-Timestamp, X-Nonce, X-Signature are required.",
        )

    try:
        timestamp = float(timestamp_raw)
    except (TypeError, ValueError):
        return SignatureDecision(
            False, SIG_EXPIRED,
            f"X-Timestamp must be a unix timestamp in seconds, got {timestamp_raw!r}.",
        )

    keys = key_ring.caller_keys(caller)
    if keys is None:
        return SignatureDecision(
            False, SIG_UNKNOWN_CALLER, context={"caller_id": caller},
        )

    now = clock.now()
    drift = abs(now - timestamp)
    if drift > skew_seconds:
        return SignatureDecision(
            False, SIG_EXPIRED,
            f"Timestamp drift {drift:.0f}s exceeds skew window {skew_seconds:.0f}s.",
            context={"caller_id": caller, "drift_seconds": drift,
                     "skew_seconds": skew_seconds},
        )

    candidate_keys = [k for k in keys if k.valid_at(timestamp)]
    canonical = build_canonical_string(method, path, timestamp_raw, nonce, raw_body)
    matched = None
    for key in candidate_keys:
        expected = hmac.new(key.secret, canonical, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            matched = key
            break
    if matched is None:
        reason = "no key valid at that timestamp" if not candidate_keys else "mac mismatch"
        return SignatureDecision(
            False, SIG_INVALID,
            f"Signature verification failed ({reason}).",
            context={"caller_id": caller, "candidate_key_ids":
                     [k.key_id for k in candidate_keys]},
        )

    if not replay_cache.check_and_record(caller, nonce, skew_seconds):
        return SignatureDecision(
            False, SIG_REPLAY,
            f"Nonce {nonce!r} was already used by caller {caller!r}.",
            context={"caller_id": caller, "nonce": nonce},
        )

    return SignatureDecision(True, matched_key_id=matched.key_id,
                             context={"caller_id": caller, "nonce": nonce})


def _header(headers, name: str) -> str | None:
    """Case-insensitive header lookup (plain dict or Starlette Headers)."""
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None and not _is_case_insensitive(headers):
            wanted = name.lower()
            for key, val in headers.items():
                if key.lower() == wanted:
                    value = val
                    break
    else:
        value = None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_case_insensitive(headers) -> bool:
    # Starlette's Headers normalises keys; a plain dict does not.
    return headers.__class__.__name__ == "Headers"
