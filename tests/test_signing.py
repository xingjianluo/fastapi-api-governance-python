"""来源签名与密钥轮换测试。"""


import pytest

from app.governance.errors import (
    SignatureCredentialsMissing,
    SignatureKeyRetired,
    SignatureKeyUnknown,
    SignatureMismatch,
    SignatureReplay,
    SignatureTimestampExpired,
)
from app.governance.signing import ACTIVE, GRACE, RETIRED, KeySpec, NonceStore, SignatureVerifier, sign_request

from tests.govlog import gov_log

NOW = 1_000_000.0
SKEW = 300


def build_verifier():
    keys = {
        "k-new": KeySpec(kid="k-new", secret="new-secret", state=ACTIVE),
        "k-old": KeySpec(
            kid="k-old", secret="old-secret", state=GRACE, grace_until=NOW + 120
        ),
        "k-dead": KeySpec(kid="k-dead", secret="dead-secret", state=RETIRED),
    }
    return SignatureVerifier(keys, skew_seconds=SKEW, nonce_store=NonceStore(ttl_seconds=600))


def headers(verifier, *, kid="k-new", secret="new-secret", ts=NOW, nonce="n1", body=b"{}", path="/v2/orders", sig=None, drop=None):
    sig = sig if sig is not None else sign_request(secret, "POST", path, str(int(ts)), nonce, body)
    h = {"kid": kid, "timestamp": str(int(ts)), "nonce": nonce, "signature": sig}
    for d in drop or []:
        h[d] = None
    return h


def verify(verifier, h, body=b"{}", path="/v2/orders", now=NOW):
    return verifier.verify("POST", path, body, h["timestamp"], h["nonce"], h["kid"], h["signature"], now=now)


def test_valid_signature_accepted():
    v = build_verifier()
    key = verify(v, headers(v))
    gov_log("valid-signature", {"kid": "k-new"}, {"state": key.state, "skew": SKEW}, "ACCEPTED")
    assert key.kid == "k-new"


def test_missing_header_distinct():
    v = build_verifier()
    h = headers(v, drop=["signature"])
    with pytest.raises(SignatureCredentialsMissing) as exc:
        verify(v, h)
    gov_log("missing-signature-header", h, {"missing": exc.value.details["missing"]}, f"REJECTED {exc.value.code}")
    assert exc.value.code == "signature_credentials_missing"


def test_unknown_kid_distinct():
    v = build_verifier()
    h = headers(v, kid="k-ghost", secret="new-secret")
    with pytest.raises(SignatureKeyUnknown) as exc:
        verify(v, h)
    gov_log("unknown-kid", {"kid": "k-ghost"}, {"known_kids": exc.value.details["known_kids"]}, f"REJECTED {exc.value.code}")
    assert exc.value.code == "signature_key_unknown"


def test_timestamp_outside_window_distinct():
    v = build_verifier()
    h = headers(v, ts=NOW - SKEW - 1)
    with pytest.raises(SignatureTimestampExpired) as exc:
        verify(v, h)
    gov_log(
        "timestamp-expired",
        {"timestamp": h["timestamp"], "server_time": NOW},
        {"drift_seconds": exc.value.details["drift_seconds"], "window_seconds": SKEW},
        f"REJECTED {exc.value.code}",
    )
    assert exc.value.code == "signature_timestamp_expired"


def test_signature_mismatch_distinct_from_expired():
    v = build_verifier()
    h = headers(v, sig="deadbeef")
    with pytest.raises(SignatureMismatch) as exc:
        verify(v, h)
    gov_log("signature-mismatch", {"kid": "k-new"}, {"reason": "hmac mismatch"}, f"REJECTED {exc.value.code}")
    assert exc.value.code == "signature_mismatch"


def test_body_tamper_detected():
    v = build_verifier()
    h = headers(v, body=b'{"qty":1}')
    with pytest.raises(SignatureMismatch):
        verify(v, h, body=b'{"qty":2}')
    gov_log("body-tamper", {"signed_body": b'{"qty":1}', "actual_body": b'{"qty":2}'}, {}, "REJECTED signature_mismatch")


def test_grace_key_accepted_inside_overlap_window():
    """轮换重叠期内旧密钥仍然生效。"""
    v = build_verifier()
    h = headers(v, kid="k-old", secret="old-secret")
    key = verify(v, h, now=NOW + 60)
    gov_log(
        "rotation-grace-valid",
        {"kid": "k-old", "now": NOW + 60, "grace_until": NOW + 120},
        {"state": GRACE},
        "ACCEPTED",
    )
    assert key.kid == "k-old"


def test_grace_key_boundary_then_retired():
    """超过宽限边界的同一秒起旧密钥被拒绝，原因可区分。"""
    v = build_verifier()
    h_ok = headers(v, kid="k-old", secret="old-secret", nonce="n-a")
    h_no = headers(v, kid="k-old", secret="old-secret", nonce="n-b")

    key = verify(v, h_ok, now=NOW + 120)  # 边界时刻：now == grace_until 仍允许
    assert key.kid == "k-old"
    with pytest.raises(SignatureKeyRetired) as exc:
        verify(v, h_no, now=NOW + 120.001)
    gov_log(
        "rotation-grace-expired",
        {"kid": "k-old", "grace_until": NOW + 120, "now": NOW + 120.001},
        {"boundary": "now > grace_until"},
        f"REJECTED {exc.value.code}",
    )
    assert exc.value.code == "signature_key_retired"


def test_retired_key_always_rejected():
    v = build_verifier()
    h = headers(v, kid="k-dead", secret="dead-secret")
    with pytest.raises(SignatureKeyRetired):
        verify(v, h)
    gov_log("retired-key", {"kid": "k-dead"}, {"state": RETIRED}, "REJECTED signature_key_retired")


def test_replay_rejected_distinct():
    v = build_verifier()
    h = headers(v, nonce="dup")
    verify(v, h)
    with pytest.raises(SignatureReplay) as exc:
        verify(v, h)
    gov_log("replay", {"nonce": "dup"}, {"seen": True, "ttl": 600}, f"REJECTED {exc.value.code}")
    assert exc.value.code == "signature_replay"
