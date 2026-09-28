"""API 版本协商与弃用/下线策略。

版本生命周期::

    introduced -> supported -> deprecated (弃用宽限期) -> sunset (下线)

规则：
- 客户端必须显式选择版本（``X-API-Version``），缺省拒绝，避免服务端
  隐式升级带来的语义漂移。
- 注册表中不存在的版本号 -> VersionUnknown(404)。
- 版本存在，但某 (接口, 版本) 组合声明不支持 -> VersionUnsupportedCombo。
- 过了 sunset 日期 -> VersionSunset(410)，并附带下线说明。
- 处于弃用期 -> 请求仍被接受，但响应带 Deprecation / Sunset / Link 头。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from email.utils import formatdate

from .errors import (
    VersionMissing,
    VersionSunset,
    VersionUnknown,
    VersionUnsupportedCombo,
)

SUPPORTED = "supported"
DEPRECATED = "deprecated"
SUNSET = "sunset"


@dataclass(frozen=True)
class VersionSpec:
    """单个 API 版本的静态声明与某一时刻的生效状态。"""

    version: str
    deprecated_at: date | None = None
    sunset_at: date | None = None
    successor: str | None = None
    docs_url: str | None = None
    status: str = SUPPORTED

    @property
    def is_deprecated(self) -> bool:
        return self.status == DEPRECATED

    @property
    def is_sunset(self) -> bool:
        return self.status == SUNSET


def _effective_status(spec: VersionSpec, today: date) -> str:
    if spec.sunset_at is not None and today >= spec.sunset_at:
        return SUNSET
    if spec.deprecated_at is not None and today >= spec.deprecated_at:
        return DEPRECATED
    return SUPPORTED


class VersionRegistry:
    """版本注册表：负责版本存在性、生命周期和接口组合的判定。"""

    def __init__(self, versions: dict[str, VersionSpec]) -> None:
        self._versions = dict(versions)

    @property
    def known_versions(self) -> list[str]:
        return sorted(self._versions)

    def with_status(self, version: str, today: date | None = None) -> VersionSpec:
        today = today or date.today()
        spec = self._versions[version]
        if spec.status == _effective_status(spec, today):
            return spec
        return VersionSpec(
            version=spec.version,
            deprecated_at=spec.deprecated_at,
            sunset_at=spec.sunset_at,
            successor=spec.successor,
            docs_url=spec.docs_url,
            status=_effective_status(spec, today),
        )

    def negotiate(
        self,
        requested: str | None,
        *,
        route_versions: set[str] | None = None,
        today: date | None = None,
    ) -> VersionSpec:
        """判定请求版本是否可用于某接口。

        Args:
            requested: 客户端通过 X-API-Version 提交的版本标识。
            route_versions: 该接口声明支持的版本集合；None 表示注册表内
                所有未下线版本均可用。
            today: 注入的当前日期，便于测试边界。
        """
        today = today or date.today()

        if requested is None or requested.strip() == "":
            raise VersionMissing(
                "缺少 API 版本，请通过 X-API-Version 显式指定目标版本",
                details={"known_versions": self.known_versions},
            )

        if requested not in self._versions:
            raise VersionUnknown(
                f"未知的 API 版本: {requested!r}",
                details={"requested": requested, "known_versions": self.known_versions},
            )

        spec = self.with_status(requested, today)

        if spec.is_sunset:
            raise VersionSunset(
                f"版本 {requested} 已于 {spec.sunset_at} 下线，请升级到 "
                f"{spec.successor or '最新版本'}",
                details={
                    "requested": requested,
                    "sunset_at": spec.sunset_at.isoformat(),
                    "successor": spec.successor,
                },
            )

        if route_versions is not None and requested not in route_versions:
            raise VersionUnsupportedCombo(
                f"接口在版本 {requested} 下不可用",
                details={
                    "requested": requested,
                    "supported_on_this_route": sorted(route_versions),
                },
            )

        return spec

    def deprecation_headers(self, spec: VersionSpec) -> dict[str, str]:
        """为处于弃用期的版本生成可解释的响应头。"""
        if not spec.is_deprecated:
            return {}
        headers = {
            "Deprecation": spec.deprecated_at.isoformat(),
            "API-Version": spec.version,
            "API-Version-Status": DEPRECATED,
        }
        if spec.sunset_at is not None:
            sunset_dt = datetime(
                spec.sunset_at.year, spec.sunset_at.month, spec.sunset_at.day
            )
            headers["Sunset"] = formatdate(sunset_dt.timestamp(), usegmt=True)
        links = []
        if spec.docs_url:
            links.append(f'<{spec.docs_url}>; rel="deprecation"')
        if spec.successor:
            links.append(f'</docs/versions#{spec.successor}>; rel="successor-version"')
        if links:
            headers["Link"] = ", ".join(links)
        return headers
