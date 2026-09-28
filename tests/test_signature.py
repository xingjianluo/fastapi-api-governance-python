"""Signature: expired vs invalid vs replay; rotation overlap boundary."""

import pytest

from app.config import DEFAULT_SKEW_SECONDS, ROTATION_GRACE_SECONDS
from app.errors import (
    SIG_EXPIRED,
    SIG_INVALID,
    SIG_MISSING_CREDENTIALS,
    SIG_REPLAY,
    SIG_UNKNOWN_CALLER,
)
from app.signature import ReplayCache, verify_signature

from .conftest import T0, log_decision

BODY = b'{"product":"widget","quantity":1,"amount":100}'
PATH = "/orders"


def _verify(config, clock, headers, body=BODY):
    return verify_signature(
        headers=headers, raw_body=body, method="POST", path=PATH,
        key_ring=config.key_ring, clock=clock,
        replay_cache=config.replay_cache, skew_seconds=config.skew_seconds,
    )


def test_both_keys_valid_during_rotation_overlap(config, clock, sign_headers):
    for secret, key_id in [(b"acme-old-secret", "k-old"),
                           (b"acme-new-secret", "k-new")]:
        h = sign_headers("POST", PATH, secret=secret, body=BODY)
        d = _verify(config, clock, h)
        log_decision("sig.rotation.overlap",
                     {"secret": key_id, "timestamp": clock.now()},
                     f"key valid in [{T0}, {T0 + ROTATION_GRACE_SECONDS})",
                     f"accepted matched={d.matched_key_id}")
        assert d.accepted and d.matched_key_id == key_id


def test_old_key_rejected_at_grace_boundary(config, clock, sign_headers):
    # Within the skew window but near the old key's validity end:
    # timestamp exactly at old key's not_valid_after -> inclusive-valid
    boundary = T0 + 300.0  # inside +/- 300s skew; old key valid until T0+600
    h = sign_headers("POST", PATH, secret=b"acme-old-secret", body=BODY,
                     timestamp=boundary)
    d = _verify(config, clock, h)
    log_decision("sig.rotation.old_still_in_grace",
                 {"timestamp": boundary, "old_key_until": T0 + ROTATION_GRACE_SECONDS},
                 "ts <= old.not_valid_after and within skew -> old key accepted",
                 f"matched={d.matched_key_id}")
    assert d.accepted and d.matched_key_id == "k-old"

    # Move the *wall clock* to the grace boundary: a fresh request signed
    # with the old key at boundary+1 has no candidate key -> invalid.
    clock.advance(ROTATION_GRACE_SECONDS + 1)
    h = sign_headers("POST", PATH, secret=b"acme-old-secret", body=BODY)
    d = _verify(config, clock, h)
    log_decision("sig.rotation.old_expired",
                 {"now": clock.now(),
                  "old_key_until": T0 + ROTATION_GRACE_SECONDS},
                 "old key outside interval -> no candidate -> signature.invalid",
                 d.error.code)
    assert not d.accepted and d.error is SIG_INVALID

    # new key still valid at/after boundary
    h = sign_headers("POST", PATH, secret=b"acme-new-secret", body=BODY)
    assert _verify(config, clock, h).accepted


def test_expired_timestamp_distinguishable(config, clock, sign_headers):
    h = sign_headers("POST", PATH, secret=b"acme-new-secret", body=BODY,
                     timestamp=T0 - DEFAULT_SKEW_SECONDS - 1)
    d = _verify(config, clock, h)
    log_decision("sig.timestamp_expired",
                 {"drift_seconds": DEFAULT_SKEW_SECONDS + 1,
                  "skew": DEFAULT_SKEW_SECONDS},
                 "abs(now - ts) > skew window", d.error.code)
    assert not d.accepted and d.error is SIG_EXPIRED


def test_bad_signature_distinguishable(config, clock, sign_headers):
    h = sign_headers("POST", PATH, secret=b"acme-new-secret", body=BODY,
                     signature="0" * 64)
    d = _verify(config, clock, h)
    log_decision("sig.invalid", {"signature": "000...0"},
                 "hmac compare_digest fails for all candidate keys",
                 d.error.code)
    assert not d.accepted and d.error is SIG_INVALID


def test_tampered_body_fails(config, clock, sign_headers):
    h = sign_headers("POST", PATH, secret=b"acme-new-secret", body=BODY)
    d = _verify(config, clock, h, body=b'{"product":"EVIL"}')
    assert not d.accepted and d.error is SIG_INVALID


def test_replay_distinguishable_from_bad_sig(config, clock, sign_headers):
    h = sign_headers("POST", PATH, secret=b"acme-new-secret", body=BODY,
                     nonce="replay-nonce")
    first = _verify(config, clock, h)
    second = _verify(config, clock, h)  # identical everything
    log_decision("sig.replay", {"nonce": "replay-nonce"},
                 "ReplayCache already holds (acme, nonce)",
                 f"first={first.accepted}, second={second.error.code}")
    assert first.accepted
    assert not second.accepted and second.error is SIG_REPLAY


def test_same_nonce_different_caller_is_not_replay(config, clock, sign_headers):
    h1 = sign_headers("POST", PATH, caller="acme",
                      secret=b"acme-new-secret", body=BODY, nonce="shared-n")
    h2 = sign_headers("POST", PATH, caller="globex",
                      secret=b"globex-secret", body=BODY, nonce="shared-n")
    assert _verify(config, clock, h1).accepted
    assert _verify(config, clock, h2).accepted


def test_unknown_caller_and_missing_headers(config, clock, sign_headers):
    h = sign_headers("POST", PATH, caller="ghost", secret=b"x", body=BODY)
    d = _verify(config, clock, h)
    assert d.error is SIG_UNKNOWN_CALLER
    d = verify_signature(
        headers={}, raw_body=BODY, method="POST", path=PATH,
        key_ring=config.key_ring, clock=clock,
        replay_cache=config.replay_cache, skew_seconds=config.skew_seconds,
    )
    assert d.error is SIG_MISSING_CREDENTIALS


def test_replay_cache_entries_expire_after_skew_window(config, clock, sign_headers):
    cache = ReplayCache(clock)
    from app.signature import verify_signature as vs
    h = sign_headers("POST", PATH, secret=b"acme-new-secret", body=BODY,
                     nonce="ttl-nonce")
    assert vs(headers=h, raw_body=BODY, method="POST", path=PATH,
              key_ring=config.key_ring, clock=clock, replay_cache=cache,
              skew_seconds=config.skew_seconds).accepted
    clock.advance(DEFAULT_SKEW_SECONDS + 1)
    # Re-sign at the ORIGINAL timestamp: nonce has been pruned (no replay),
    # but drift is now beyond the skew window -> EXPIRED, not replay.
    h = sign_headers("POST", PATH, secret=b"acme-new-secret", body=BODY,
                     nonce="ttl-nonce", timestamp=T0)
    d = vs(headers=h, raw_body=BODY, method="POST", path=PATH,
           key_ring=config.key_ring, clock=clock, replay_cache=cache,
           skew_seconds=config.skew_seconds)
    assert d.error is SIG_EXPIRED
    assert len(cache) == 0
