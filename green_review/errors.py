"""应用领域异常。"""

from __future__ import annotations


class GreenReviewError(Exception):
    """所有本应用异常的基类。"""


class ValidationError(GreenReviewError, ValueError):
    """输入资料不满足最低结构要求。"""


class NotFoundError(GreenReviewError, LookupError):
    """引用的站点、证据或决定不存在。"""


class ConflictError(GreenReviewError, RuntimeError):
    """请求与当前记录状态冲突。"""


class ReviewBlockedError(GreenReviewError, RuntimeError):
    """评审结论不满足签发条件。"""


class TimelineConflictError(GreenReviewError, RuntimeError):
    """证书/决定效期存在未解释的空档或重叠。

    ``conflicts`` 中保存结构化检查结果，便于调用方补充说明后重提。
    """

    def __init__(self, message: str, conflicts: list[dict] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


class SourceUnavailableError(GreenReviewError, RuntimeError):
    """公开因子页面或授权验证接口暂时不可用。"""

    def __init__(self, message: str, *, source: str | None = None, retry_after: str | None = None):
        super().__init__(message)
        self.source = source
        self.retry_after = retry_after
