"""绿色制造评审证据管理应用。

公开入口主要是 :class:`green_review.app.GreenReviewApp`。包内模块均只依赖
Python 标准库。
"""

from .app import GreenReviewApp, IngestResult
from .errors import (
    ConflictError,
    GreenReviewError,
    NotFoundError,
    ReviewBlockedError,
    SourceUnavailableError,
    TimelineConflictError,
    ValidationError,
)
from .models import (
    DecisionState,
    IssueStatus,
    ConfirmationStatus,
    IngestStatus,
)
from .sample import import_samples

__all__ = [
    "GreenReviewApp",
    "IngestResult",
    "import_samples",
    "GreenReviewError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "ReviewBlockedError",
    "SourceUnavailableError",
    "TimelineConflictError",
    "DecisionState",
    "IssueStatus",
    "ConfirmationStatus",
    "IngestStatus",
]

__version__ = "1.0.0"


def create_app(path: str | None = None, **kwargs):
    """创建应用；不传 path 时使用内存仓储。"""
    from .store import Store

    if path:
        return GreenReviewApp(Store(path), **kwargs)
    return GreenReviewApp(Store(), **kwargs)
