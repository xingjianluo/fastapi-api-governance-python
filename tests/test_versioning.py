"""Version negotiation: unknown / unsupported / retired / deprecated."""

import pytest

from app.clock import FakeClock
from app.errors import VERSION_RETIRED, VERSION_UNKNOWN, VERSION_UNSUPPORTED
from app.versioning import (
    VersionInfo,
    VersionLifecycle,
    VersionNegotiationError,
    VersionRegistry,
    deprecation_headers,
    negotiate_version,
)

from .conftest import T0, log_decision


def _registry(clock):
    return VersionRegistry(
        versions={
            1: VersionInfo(1, VersionLifecycle.DEPRECATED,
                           deprecated_at=T0 - 86400, sunset_at=T0 + 86400),
            2: VersionInfo(2, VersionLifecycle.ACTIVE),
        },
        clock=clock,
        default_version=2,
    )


def test_default_version_is_latest_active(clock):
    reg = _registry(clock)
    info, used_default = negotiate_version({}, reg)
    log_decision("version.default", {"header": None},
                 "no X-Api-Version -> registry.default_version",
                 f"v{info.version}, used_default={used_default}")
    assert info.version == 2 and used_default is True


def test_explicit_deprecated_version_accepted_with_headers(clock):
    reg = _registry(clock)
    info, _ = negotiate_version({"X-Api-Version": "v1"}, reg)
    headers = deprecation_headers(info, clock)
    log_decision(
        "version.deprecated", {"X-Api-Version": "v1"},
        "deprecated_at <= now < sunset_at -> accepted + Deprecation/Sunset",
        f"v{info.version} headers={sorted(headers)}",
    )
    assert info.version == 1
    assert headers["X-Api-Deprecated"] == "true"
    assert headers["Sunset"] and headers["Deprecation"].startswith("@")
    assert int(headers["X-Api-Sunset-In-Seconds"]) == 86400


def test_unknown_version_is_distinguishable(clock):
    reg = _registry(clock)
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate_version({"X-Api-Version": "9"}, reg)
    log_decision("version.unknown", {"X-Api-Version": "9"},
                 "no VersionInfo registered -> version.unknown",
                 exc.value.error.code)
    assert exc.value.error is VERSION_UNKNOWN
    assert exc.value.supported == [1, 2]


def test_malformed_version_header_is_unknown(clock):
    reg = _registry(clock)
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate_version({"X-Api-Version": "banana"}, reg)
    assert exc.value.error is VERSION_UNKNOWN


def test_unsupported_version_combo_is_distinguishable(clock):
    reg = _registry(clock)
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate_version(
            {"X-Api-Version": "1"}, reg, endpoint_versions=(2,)
        )
    log_decision("version.unsupported", {"X-Api-Version": "1",
                                         "endpoint_versions": [2]},
                 "version exists but endpoint serves (2,) only",
                 exc.value.error.code)
    assert exc.value.error is VERSION_UNSUPPORTED


def test_retired_after_sunset_boundary(clock):
    reg = _registry(clock)
    # exactly at the boundary inclusive -> retired
    clock2 = FakeClock(T0 + 86400)
    reg2 = VersionRegistry(reg.versions, clock2, 2)
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate_version({"X-Api-Version": "1"}, reg2)
    log_decision("version.retired", {"now": clock2.now(),
                                     "sunset_at": T0 + 86400},
                 "now >= sunset_at -> hard 410 version.retired",
                 exc.value.error.code)
    assert exc.value.error is VERSION_RETIRED


def test_one_second_before_sunset_still_deprecated_not_retired(clock):
    reg = VersionRegistry(
        _registry(FakeClock(T0 + 86400 - 1)).versions,
        FakeClock(T0 + 86400 - 1), 2,
    )
    info, _ = negotiate_version({"X-Api-Version": "1"}, reg)
    assert info.version == 1
    assert reg.lifecycle_now(info) is VersionLifecycle.DEPRECATED
