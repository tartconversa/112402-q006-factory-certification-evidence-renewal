"""日期、哈希、编号和 JSON 持久化辅助函数。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .errors import ValidationError

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_date(value: Any, field: str = "date") -> date | None:
    """把 ISO 日期、``date`` 或空值转换为 ``date``。"""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    match = DATE_RE.match(text)
    if not match:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期，收到 {value!r}")
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法日期: {value!r}") from exc


def parse_datetime(value: Any, field: str = "timestamp") -> datetime:
    if value is None or value == "":
        return utc_now()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法 ISO 时间: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def period_range(period: str | dict | None) -> tuple[date | None, date | None, str]:
    """返回期间起止日期和规范标签。

    支持 ``2026``、``2026-H1/H2``、``2026-Q1..Q4``、``YYYY-MM``，以及包含
    ``start``/``end``/``label`` 的字典。无法识别时返回 ``(None, None, str)``。
    """
    if period is None:
        return None, None, ""
    if isinstance(period, dict):
        start = parse_date(period.get("start") or period.get("from"), "period.start")
        end = parse_date(period.get("end") or period.get("to"), "period.end")
        label = str(period.get("label") or period.get("name") or "")
        if not label and start and end:
            label = f"{start.isoformat()}..{end.isoformat()}"
        return start, end, label

    label = str(period).strip()
    if re.fullmatch(r"\d{4}", label):
        year = int(label)
        return date(year, 1, 1), date(year, 12, 31), label
    match = re.fullmatch(r"(\d{4})-H([12])", label, re.IGNORECASE)
    if match:
        year, half = int(match.group(1)), int(match.group(2))
        if half == 1:
            return date(year, 1, 1), date(year, 6, 30), label.upper()
        return date(year, 7, 1), date(year, 12, 31), label.upper()
    match = re.fullmatch(r"(\d{4})-Q([1-4])", label, re.IGNORECASE)
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        start_month = (quarter - 1) * 3 + 1
        start = date(year, start_month, 1)
        end_month = start_month + 2
        if end_month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, end_month + 1, 1) - timedelta(days=1)
        return start, end, label.upper()
    match = re.fullmatch(r"(\d{4})-(\d{2})", label)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        start = date(year, month, 1)
        if month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
        return start, end, label
    return None, None, label


def intervals_overlap(
    start_a: date | None,
    end_a: date | None,
    start_b: date | None,
    end_b: date | None,
) -> bool:
    """两个闭区间是否重叠；缺省端点视为开放。"""
    if start_a and end_b and start_a > end_b:
        return False
    if start_b and end_a and start_b > end_a:
        return False
    return True


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def content_hash(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return {"__decimal__": str(obj)}
    if isinstance(obj, (date, datetime)):
        return {"__iso__": obj.isoformat()}
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")


def json_object_hook(obj: dict[str, Any]) -> Any:
    if set(obj) == {"__decimal__"}:
        return Decimal(obj["__decimal__"])
    if set(obj) == {"__iso__"}:
        text = obj["__iso__"]
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            try:
                return date.fromisoformat(text)
            except ValueError:
                return text
    return obj


def atomic_write_json(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{__import__('os').getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, default=_json_default)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle, object_hook=json_object_hook)


def to_decimal(value: Any, field: str = "value") -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except Exception as exc:  # decimal.InvalidOperation etc.
        raise ValidationError(f"{field} 不是合法数值: {value!r}") from exc


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    return [value]


def compact_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None or value == "":
            continue
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def days_between(start: date, end: date) -> int:
    return (end - start).days
