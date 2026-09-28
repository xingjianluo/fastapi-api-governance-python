"""版本协商边界测试。"""

from datetime import date

import pytest

from app.governance.errors import (
    VersionMissing,
    VersionSunset,
    VersionUnknown,
    VersionUnsupportedCombo,
)
from app.governance.versioning import DEPRECATED, SUPPORTED, VersionRegistry, VersionSpec

from tests.govlog import gov_log


def build_registry() -> VersionRegistry:
    return VersionRegistry(
        {
            "v1": VersionSpec(
                version="v1",
                deprecated_at=date(2026, 6, 1),
                sunset_at=date(2027, 1, 1),
                successor="v2",
                docs_url="/docs/versions#v1-deprecation",
            ),
            "v2": VersionSpec(version="v2"),
            "v0": VersionSpec(
                version="v0",
                deprecated_at=date(2025, 1, 1),
                sunset_at=date(2025, 6, 1),
                successor="v1",
            ),
        }
    )


def test_missing_version_rejected():
    reg = build_registry()
    with pytest.raises(VersionMissing) as exc:
        reg.negotiate(None, today=date(2026, 9, 28))
    gov_log(
        "missing-version",
        {"header": None},
        {"known_versions": exc.value.details["known_versions"]},
        f"REJECTED {exc.value.code}",
    )
    assert exc.value.code == "version_missing"


def test_unknown_version_distinct_from_missing():
    reg = build_registry()
    with pytest.raises(VersionUnknown) as exc:
        reg.negotiate("v99", today=date(2026, 9, 28))
    gov_log(
        "unknown-version",
        {"header": "v99"},
        {"known_versions": exc.value.details["known_versions"]},
        f"REJECTED {exc.value.code}",
    )
    assert exc.value.code == "version_unknown"


def test_supported_version_ok():
    reg = build_registry()
    spec = reg.negotiate("v2", today=date(2026, 9, 28))
    gov_log("supported-version", {"header": "v2"}, {"status": spec.status}, "ACCEPTED")
    assert spec.status == SUPPORTED
    assert reg.deprecation_headers(spec) == {}


def test_unsupported_route_combo_distinct_reason():
    reg = build_registry()
    with pytest.raises(VersionUnsupportedCombo) as exc:
        reg.negotiate("v1", route_versions={"v2"}, today=date(2026, 9, 28))
    gov_log(
        "unsupported-combo",
        {"header": "v1", "route_versions": ["v2"]},
        {"supported_on_this_route": exc.value.details["supported_on_this_route"]},
        f"REJECTED {exc.value.code}",
    )
    assert exc.value.code == "version_unsupported_combo"


def test_deprecated_version_accepted_with_headers():
    reg = build_registry()
    spec = reg.negotiate("v1", today=date(2026, 9, 28))
    headers = reg.deprecation_headers(spec)
    gov_log(
        "deprecated-version",
        {"header": "v1", "today": "2026-09-28"},
        {"deprecated_at": "2026-06-01", "sunset_at": "2027-01-01"},
        "ACCEPTED with deprecation headers",
    )
    assert spec.status == DEPRECATED
    assert headers["Deprecation"] == "2026-06-01"
    assert "Sunset" in headers
    assert "successor-version" in headers["Link"]


def test_deprecation_boundary_day():
    """弃用生效日当天即进入弃用期；前一天仍是 supported。"""
    reg = build_registry()
    before = reg.negotiate("v1", today=date(2026, 5, 31))
    on_day = reg.negotiate("v1", today=date(2026, 6, 1))
    gov_log(
        "deprecation-boundary",
        {"days": ["2026-05-31", "2026-06-01"]},
        {"deprecated_at": "2026-06-01"},
        f"{before.status} -> {on_day.status}",
    )
    assert before.status == SUPPORTED
    assert on_day.status == DEPRECATED


def test_sunset_version_rejected_410():
    reg = build_registry()
    with pytest.raises(VersionSunset) as exc:
        reg.negotiate("v0", today=date(2025, 6, 1))
    gov_log(
        "sunset-version",
        {"header": "v0", "today": "2025-06-01"},
        {"sunset_at": exc.value.details["sunset_at"], "successor": exc.value.details["successor"]},
        f"REJECTED {exc.value.code}",
    )
    assert exc.value.code == "version_sunset"
    assert exc.value.status_code == 410
