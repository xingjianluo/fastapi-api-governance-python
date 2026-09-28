"""API version negotiation, deprecation and sunset policy.

Lifecycle: ``active`` -> ``deprecated`` (Deprecation + Sunset headers added)
-> ``retired`` (hard 410 rejection). Negotiation distinguishes:

* unknown version identifier      -> ``version.unknown``      (400)
* known but endpoint doesn't serve-> ``version.unsupported``  (400)
* past sunset date                -> ``version.retired``      (410)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.errors import (
    VERSION_RETIRED,
    VERSION_UNKNOWN,
    VERSION_UNSUPPORTED,
)
from app.clock import Clock, SystemClock

VERSION_HEADER = "X-Api-Version"
DEFAULT_VERSION_HEADER = "X-Api-Deprecated-Default"


class VersionLifecycle(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


@dataclass(frozen=True)
class VersionInfo:
    version: int
    lifecycle: VersionLifecycle
    deprecated_at: float | None = None   # unix ts when deprecation starts
    sunset_at: float | None = None       # unix ts when the version is retired


class VersionNegotiationError(Exception):
    def __init__(self, code, *, requested=None, supported=None, detail=None):
        super().__init__(code.message)
        self.error = code
        self.requested = requested
        self.supported = supported
        self.detail = detail


@dataclass
class VersionRegistry:
    versions: dict[int, VersionInfo]
    clock: Clock
    default_version: int

    def get(self, version: int) -> VersionInfo | None:
        return self.versions.get(version)

    def lifecycle_now(self, info: VersionInfo) -> VersionLifecycle:
        """Effective lifecycle at the current clock time."""
        now = self.clock.now()
        if info.sunset_at is not None and now >= info.sunset_at:
            return VersionLifecycle.RETIRED
        if (
            info.lifecycle == VersionLifecycle.DEPRECATED
            and info.deprecated_at is not None
            and now >= info.deprecated_at
        ):
            return VersionLifecycle.DEPRECATED
        if info.lifecycle == VersionLifecycle.RETIRED:
            return VersionLifecycle.RETIRED
        return VersionLifecycle.ACTIVE


def _parse_header(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip().lower().removeprefix("v")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise VersionNegotiationError(
            VERSION_UNKNOWN,
            requested=raw,
            detail="Version header must be an integer such as '1' or 'v2'.",
        )


def negotiate_version(
    headers,
    registry: VersionRegistry,
    *,
    endpoint_min_version: int = 1,
    endpoint_versions: tuple[int, ...] | None = None,
) -> tuple[VersionInfo, bool]:
    """Resolve a request to a concrete :class:`VersionInfo`.

    Returns ``(info, used_default)``. Raises :class:`VersionNegotiationError`
    with a distinguishable code on rejection.
    """
    raw = headers.get(VERSION_HEADER) if hasattr(headers, "get") else None
    requested = _parse_header(raw)
    used_default = requested is None
    version = registry.default_version if requested is None else requested

    info = registry.get(version)
    if info is None:
        raise VersionNegotiationError(
            VERSION_UNKNOWN,
            requested=version,
            supported=sorted(registry.versions),
            detail=f"Version {version!r} does not exist.",
        )

    effective = registry.lifecycle_now(info)
    if effective is VersionLifecycle.RETIRED:
        raise VersionNegotiationError(
            VERSION_RETIRED,
            requested=version,
            supported=[v for v, i in registry.versions.items()
                       if registry.lifecycle_now(i) is not VersionLifecycle.RETIRED],
            detail=(
                f"Version {version} retired at unix ts {int(info.sunset_at)}."
                if info.sunset_at else
                f"Version {version} is retired."
            ),
        )

    served = endpoint_versions
    if served is None:
        served = tuple(
            v for v in sorted(registry.versions) if v >= endpoint_min_version
        )
    if version not in served:
        raise VersionNegotiationError(
            VERSION_UNSUPPORTED,
            requested=version,
            supported=list(served),
            detail=(
                f"Endpoint does not serve version {version}; "
                f"served versions: {list(served)}."
            ),
        )

    return info, used_default


def deprecation_headers(info: VersionInfo, clock: Clock) -> dict[str, str]:
    """RFC 8594-ish deprecation/sunset headers for deprecated versions."""
    if info.deprecated_at is None or info.sunset_at is None:
        return {}
    now = clock.now()
    if now < info.deprecated_at:
        return {}
    seconds_left = max(0, int(info.sunset_at - now))
    sunset_http_date = _http_date(info.sunset_at)
    return {
        "Deprecation": f'@{int(info.deprecated_at)}',
        "Sunset": sunset_http_date,
        "X-Api-Deprecated": "true",
        "X-Api-Sunset-In-Seconds": str(seconds_left),
        "Link": f'<https://local.test/docs/versioning>; rel="deprecation"; type="text/html"',
    }


def _http_date(ts: float) -> str:
    from email.utils import formatdate

    return formatdate(ts, usegmt=True)
