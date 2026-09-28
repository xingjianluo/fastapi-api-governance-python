"""写入请求的 HMAC 来源签名、时间窗校验、密钥轮换与重放检测。

签名材料（canonical string，行用 ``\\n`` 分隔）::

    <METHOD>
    <PATH>
    <X-Timestamp>
    <X-Nonce>
    <sha256(body) 的十六进制摘要>

客户端用 base string 做 HMAC-SHA256，把十六进制摘要放入 ``X-Signature``。

密钥轮换：
    active  当前签名密钥，正常收发。
    grace   上一代密钥，在 ``grace_until`` 之前仍可验签（轮换重叠期），
            超过宽限边界即等效 retired，返回 signature_key_retired。
    retired 已彻底停用，一律拒绝。

拒绝原因彼此可区分：
    缺头 -> credentials_missing；kid 未知 -> key_unknown；
    超宽限 -> key_retired；时间窗失效 -> timestamp_expired；
    nonce 重用 -> replay；摘要不符 -> mismatch。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass

from .errors import (
    SignatureCredentialsMissing,
    SignatureKeyRetired,
    SignatureKeyUnknown,
    SignatureMismatch,
    SignatureReplay,
    SignatureTimestampExpired,
)

ACTIVE = "active"
GRACE = "grace"
RETIRED = "retired"


@dataclass(frozen=True)
class KeySpec:
    kid: str
    secret: str
    state: str = ACTIVE
    grace_until: float | None = None  # 仅 grace 状态使用：宽限截止的绝对时间戳

    def effective_state(self, now: float) -> str:
        if self.state == GRACE and self.grace_until is not None and now > self.grace_until:
            return RETIRED
        return self.state


class NonceStore:
    """线程安全的 nonce 去重器，条目带过期时间，防止时间窗内重放。"""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str, now: float) -> bool:
        """原子操作：未见过则登记并返回 True；已存在且未过期返回 False。"""
        with self._lock:
            self._prune(now)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now + self._ttl
            return True

    def _prune(self, now: float) -> None:
        expired = [n for n, exp in self._seen.items() if exp <= now]
        for n in expired:
            self._seen.pop(n, None)


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def build_signing_base(
    method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> bytes:
    return "\n".join(
        [method.upper(), path, str(timestamp), nonce, body_digest(body)]
    ).encode("utf-8")


def compute_signature(secret: str, signing_base: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), signing_base, hashlib.sha256).hexdigest()


def sign_request(
    secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> str:
    """供客户端/测试使用：生成合法签名。"""
    return compute_signature(secret, build_signing_base(method, path, timestamp, nonce, body))


class SignatureVerifier:
    def __init__(
        self,
        keys: dict[str, KeySpec],
        skew_seconds: int = 300,
        nonce_store: NonceStore | None = None,
    ) -> None:
        self._keys = dict(keys)
        self._skew = skew_seconds
        self._nonces = nonce_store or NonceStore(ttl_seconds=skew_seconds * 2)

    def verify(
        self,
        method: str,
        path: str,
        body: bytes,
        timestamp: str | None,
        nonce: str | None,
        kid: str | None,
        signature: str | None,
        now: float | None = None,
    ) -> KeySpec:
        now = time.time() if now is None else now

        # 1) 凭据完整性
        if not kid or not timestamp or not nonce or not signature:
            raise SignatureCredentialsMissing(
                "缺少签名所需的请求头",
                details={
                    "missing": [
                        name
                        for name, value in (
                            ("X-Key-Id", kid),
                            ("X-Timestamp", timestamp),
                            ("X-Nonce", nonce),
                            ("X-Signature", signature),
                        )
                        if not value
                    ]
                },
            )

        # 2) 密钥存在性
        key = self._keys.get(kid)
        if key is None:
            raise SignatureKeyUnknown(
                f"未知的 key id: {kid!r}",
                details={"kid": kid, "known_kids": sorted(self._keys)},
            )

        # 3) 密钥轮换宽限边界
        state = key.effective_state(now)
        if state == RETIRED:
            raise SignatureKeyRetired(
                f"密钥 {kid} 已超过轮换宽限期并停用",
                details={"kid": kid, "grace_until": key.grace_until, "now": now},
            )

        # 4) 时间窗
        try:
            ts = float(timestamp)
        except (TypeError, ValueError):
            raise SignatureTimestampExpired(
                "X-Timestamp 不是合法的 Unix 时间戳",
                details={"timestamp": timestamp},
            )
        drift = abs(now - ts)
        if drift > self._skew:
            raise SignatureTimestampExpired(
                "请求时间戳超出允许的时间窗",
                details={
                    "timestamp": ts,
                    "server_time": now,
                    "drift_seconds": drift,
                    "window_seconds": self._skew,
                },
            )

        # 5) 签名摘要比对（先比对再登记 nonce，避免坏请求污染去重器）。
        # 基串中的时间戳沿用客户端发送的原始字符串，保证两端逐字节一致。
        expected = compute_signature(
            key.secret, build_signing_base(method, path, timestamp, nonce, body)
        )
        if not hmac.compare_digest(expected, signature):
            raise SignatureMismatch(
                "签名与请求内容不匹配",
                details={
                    "kid": kid,
                    "key_state": state,
                    "drift_seconds": drift,
                },
            )

        # 6) 重放：同一 nonce 在 TTL 内只能出现一次（原子判重）
        if not self._nonces.check_and_add(nonce, now):
            raise SignatureReplay(
                f"nonce {nonce!r} 已在时间窗内使用过，判定为重放请求",
                details={"nonce": nonce, "kid": kid},
            )

        return key
