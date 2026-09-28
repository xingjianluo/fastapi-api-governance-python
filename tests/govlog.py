"""测试用治理判定日志助手：打印输入、判定依据与最终状态。"""

import logging


def gov_log(case: str, inputs: dict, basis: dict, status: str) -> None:
    logging.getLogger("governance.test").info(
        "case=%s | input=%s | basis=%s | status=%s", case, inputs, basis, status
    )
