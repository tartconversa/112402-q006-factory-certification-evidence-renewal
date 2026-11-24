"""根目录兼容入口。

推荐使用 ``from green_review import GreenReviewApp``；此文件允许
``python app.py ...`` 以及部分测试环境中的 ``from app import GreenReviewApp``。
"""

from green_review.app import GreenReviewApp, IngestResult
from green_review.errors import (
    ConflictError,
    GreenReviewError,
    NotFoundError,
    ReviewBlockedError,
    SourceUnavailableError,
    TimelineConflictError,
    ValidationError,
)
from green_review.sample import import_samples

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
]


def main() -> int:
    from green_review.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
