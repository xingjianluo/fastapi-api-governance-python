"""测试公共配置：让单测日志打印输入、判定依据与最终状态。"""

import logging
import sys


def _configure_logging() -> None:
    root = logging.getLogger()
    if any(getattr(h, "_gov_configured", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handler._gov_configured = True  # type: ignore[attr-defined]
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


_configure_logging()
