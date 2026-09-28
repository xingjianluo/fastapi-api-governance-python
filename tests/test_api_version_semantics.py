"""跨版本业务语义一致性：同一请求在相邻版本下不能静默丢字段/语义漂移。"""

import time

from fastapi.testclient import TestClient

from app.config import default_governance
from app.governance.signing import sign_request
from app.main import create_app
from app.schemas import CANONICAL_FIELDS, FIELD_PROVENANCE
from tests.govlog import gov_log


def client():
    return TestClient(create_app(default_governance()))


def post_order(c, *, version, idem, nonce, body=b'{"item_id":"item-a","qty":3}'):
    ts = str(int(time.time()))
    sig = sign_request("s3cr3t-current", "POST", "/api/orders", ts, nonce, body)
    headers = {
        "X-API-Version": version,
        "X-Caller-Id": "premium",
        "X-Key-Id": "k-2026-09",
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sig,
        "X-Idempotency-Key": idem,
        "Content-Type": "application/json",
    }
    return c.post("/api/orders", content=body, headers=headers)


def test_adjacent_versions_same_business_fields_and_values():
    c = client()
    body = b'{"item_id":"item-a","qty":3}'
    created = post_order(c, version="v2", idem="sem-1", nonce="sem-n1", body=body)
    assert created.status_code == 201
    order_id = created.json()["order_id"]

    # 同一订单在相邻版本下分别读取，业务字段必须逐字一致
    r1 = c.get(f"/api/orders/{order_id}", headers={"X-API-Version": "v1", "X-Caller-Id": "premium"})
    r2 = c.get(f"/api/orders/{order_id}", headers={"X-API-Version": "v2", "X-Caller-Id": "premium"})
    assert r1.status_code == 200 and r2.status_code == 200
    a, b = r1.json(), r2.json()

    # v1 的每个字段在 v2 中都必须存在且值相同（无静默丢失、无含义漂移）
    mismatches = {k: (a.get(k), b.get(k)) for k in a if a.get(k) != b.get(k)}
    missing = sorted(set(a) - set(b))
    gov_log(
        "adjacent-version-semantics",
        {"versions": ["v1", "v2"], "body": body.decode()},
        {"v1_fields": sorted(a), "missing_in_v2": missing, "value_mismatches": mismatches},
        "CONSISTENT" if not missing and not mismatches else "DRIFT",
    )
    assert not missing
    assert not mismatches
    # v2 只能做加法
    assert set(b) - set(a) == {"line"}
    assert b["line"]["amount_cents"] == a["amount_cents"]


def test_no_field_silently_dropped_against_canonical_model():
    """每个版本序列化器覆盖的透传字段必须是规范模型字段的子集，且非空。"""
    for version, fields in FIELD_PROVENANCE.items():
        unknown = fields - CANONICAL_FIELDS
        gov_log(
            "field-provenance",
            {"version": version},
            {"fields": sorted(fields), "unknown_fields": sorted(unknown)},
            "OK" if fields and not unknown else "BAD",
        )
        assert fields and not unknown


def test_deterministic_price_same_across_versions():
    c = client()
    body = b'{"item_id":"item-b","qty":2}'
    a = post_order(c, version="v1", idem="p1", nonce="pn1", body=body).json()
    b = post_order(c, version="v2", idem="p2", nonce="pn2", body=body).json()
    expected = 2500 * 2
    gov_log(
        "price-parity",
        {"item": "item-b", "qty": 2, "expected_cents": expected},
        {"v1_amount": a["amount_cents"], "v2_amount": b["amount_cents"]},
        "PARITY" if a["amount_cents"] == b["amount_cents"] == expected else "MISMATCH",
    )
    assert a["amount_cents"] == b["amount_cents"] == expected


def test_deprecated_v1_carries_sunset_info_v2_does_not():
    c = client()
    r1 = post_order(c, version="v1", idem="dep1", nonce="depn1")
    r2 = post_order(c, version="v2", idem="dep2", nonce="depn2")
    gov_log(
        "deprecation-signaling",
        {"v1_status": r1.headers.get("API-Version-Status"), "v2_status": r2.headers.get("API-Version-Status")},
        {"v1_sunset": r1.headers.get("Sunset"), "v1_link": r1.headers.get("Link")},
        "v1 deprecated+sunset info, v2 clean",
    )
    assert r1.headers["API-Version-Status"] == "deprecated"
    assert "Sunset" in r1.headers and "Deprecation" in r1.headers
    assert r2.headers["API-Version-Status"] == "supported"
    assert "Sunset" not in r2.headers
