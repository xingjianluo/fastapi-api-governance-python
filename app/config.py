"""集中式治理配置：版本表、签名密钥（含轮换）、限流配额、存储。

全部为进程内组件，不依赖任何外部服务，方便本地验证。
"""

from __future__ import annotations

from datetime import date

from .governance.idempotency import IdempotencyStore
from .governance.ratelimit import RateLimiter, RateLimitRule
from .governance.signing import ACTIVE, GRACE, KeySpec, NonceStore, SignatureVerifier
from .governance.versioning import VersionRegistry, VersionSpec

# 签名允许的时间窗（秒）
SIGNATURE_SKEW_SECONDS = 300

# 密钥轮换：宽限期绝对截止时间在运行时按"当前时间 + 重叠秒数"生成，
# 保证演示/测试时始终能同时观察到新旧密钥。
ROTATION_GRACE_OVERLAP_SECONDS = 600


def build_version_registry(today: date | None = None) -> VersionRegistry:
    today = today or date.today()
    # 固定锚点：2026-09-28 时 v1 处于弃用期、v2 受支持、v0 已下线。
    versions = {
        "v0": VersionSpec(
            version="v0",
            deprecated_at=date(2025, 1, 1),
            sunset_at=date(2025, 6, 1),
            successor="v1",
            docs_url="/docs/versioning.md#v0",
        ),
        "v1": VersionSpec(
            version="v1",
            deprecated_at=date(2026, 6, 1),
            sunset_at=date(2027, 1, 1),
            successor="v2",
            docs_url="/docs/versioning.md#v1",
        ),
        "v2": VersionSpec(version="v2"),
    }
    return VersionRegistry(versions)


def build_signature_verifier(now: float | None = None) -> SignatureVerifier:
    import time

    now = time.time() if now is None else now
    keys = {
        # 新一代签名密钥
        "k-2026-09": KeySpec(kid="k-2026-09", secret="s3cr3t-current", state=ACTIVE),
        # 上一代：处于轮换重叠宽限期内，新旧同时生效
        "k-2025-12": KeySpec(
            kid="k-2025-12",
            secret="s3cr3t-previous",
            state=GRACE,
            grace_until=now + ROTATION_GRACE_OVERLAP_SECONDS,
        ),
        # 已退役的老密钥（宽限期已过）
        "k-2024-06": KeySpec(
            kid="k-2024-06",
            secret="s3cr3t-retired",
            state=GRACE,
            grace_until=1,
        ),
    }
    return SignatureVerifier(
        keys,
        skew_seconds=SIGNATURE_SKEW_SECONDS,
        nonce_store=NonceStore(ttl_seconds=SIGNATURE_SKEW_SECONDS * 2),
    )


def build_rate_limiter() -> RateLimiter:
    return RateLimiter(
        rules={
            "premium": RateLimitRule("premium", rps_limit=5, quota_limit=1000, quota_window_seconds=3600),
            "basic": RateLimitRule("basic", rps_limit=3, quota_limit=50, quota_window_seconds=3600),
        },
        default=RateLimitRule("anonymous", rps_limit=2, quota_limit=20, quota_window_seconds=3600),
    )


class Governance:
    """进程内共享的治理组件集合（单例风格）。"""

    def __init__(self) -> None:
        self.versions = build_version_registry()
        self.signatures = build_signature_verifier()
        self.idempotency = IdempotencyStore()
        self.rate_limiter = build_rate_limiter()


def default_governance() -> Governance:
    return Governance()
