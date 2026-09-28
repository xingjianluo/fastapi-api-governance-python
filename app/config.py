"""In-memory governance configuration.

Bundles the version registry, signing key ring, replay cache and rate-limit
rules behind one object so the FastAPI app and tests can build a fully
deterministic governance stack with a fake clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.clock import Clock, SystemClock
from app.idempotency import IdempotencyStore
from app.ratelimit import RateLimiter, RateLimitRule
from app.signature import KeyEntry, KeyRing, ReplayCache
from app.store import OrderStore
from app.versioning import VersionInfo, VersionLifecycle, VersionRegistry

# Default signing skew window: requests must be within +/- 5 minutes.
DEFAULT_SKEW_SECONDS = 300.0

# Rotation grace period: old and new keys overlap for 10 minutes.
ROTATION_GRACE_SECONDS = 600.0


@dataclass
class GovernanceConfig:
    clock: Clock
    version_registry: VersionRegistry
    key_ring: KeyRing
    replay_cache: ReplayCache
    rate_limiter: RateLimiter
    idempotency: IdempotencyStore
    orders: OrderStore
    skew_seconds: float = DEFAULT_SKEW_SECONDS
    secrets: dict[str, bytes] = field(default_factory=dict)
    # key id currently advertised to clients for signing
    active_key_ids: dict[str, str] = field(default_factory=dict)


def build_default_config(
    clock: Clock | None = None,
    *,
    deprecated_at_offset: float = -86_400.0,   # v1 deprecated 1 day ago
    sunset_in_seconds: float = 60 * 86_400.0,  # v1 retires in ~60 days
) -> GovernanceConfig:
    """Build the deterministic local demo configuration.

    Timeline anchored at the (possibly fake) clock's current time:
      * v1 is known and **deprecated**, with an explicit sunset timestamp;
      * v2 is the active default version;
      * caller ``acme`` is mid key-rotation: ``k-old`` still valid for
        ``ROTATION_GRACE_SECONDS`` and ``k-new`` already valid;
      * caller ``globex`` has a single active key.
    """
    clock = clock or SystemClock()
    now = clock.now()

    registry = VersionRegistry(
        versions={
            1: VersionInfo(
                version=1,
                lifecycle=VersionLifecycle.DEPRECATED,
                deprecated_at=now + deprecated_at_offset,
                sunset_at=now + sunset_in_seconds,
            ),
            2: VersionInfo(
                version=2,
                lifecycle=VersionLifecycle.ACTIVE,
            ),
        },
        clock=clock,
        default_version=2,
    )

    ring = KeyRing()
    ring.register_caller("acme", [
        KeyEntry(
            key_id="k-old",
            secret=b"acme-old-secret",
            not_valid_before=0.0,
            not_valid_after=now + ROTATION_GRACE_SECONDS,
        ),
        KeyEntry(
            key_id="k-new",
            secret=b"acme-new-secret",
            not_valid_before=now - ROTATION_GRACE_SECONDS,
            not_valid_after=float("inf"),
        ),
    ])
    ring.register_caller("globex", [
        KeyEntry(key_id="k-globex", secret=b"globex-secret",
                 not_valid_before=0.0, not_valid_after=float("inf")),
    ])

    rules = {
        "acme": RateLimitRule(rate_per_second=5.0, burst=10, daily_quota=10_000),
        "globex": RateLimitRule(rate_per_second=2.0, burst=4, daily_quota=1_000),
    }

    return GovernanceConfig(
        clock=clock,
        version_registry=registry,
        key_ring=ring,
        replay_cache=ReplayCache(clock),
        rate_limiter=RateLimiter(rules, clock=clock),
        idempotency=IdempotencyStore(),
        orders=OrderStore(),
        skew_seconds=DEFAULT_SKEW_SECONDS,
        secrets={"acme": b"acme-new-secret", "globex": b"globex-secret"},
        active_key_ids={"acme": "k-new", "globex": "k-globex"},
    )
