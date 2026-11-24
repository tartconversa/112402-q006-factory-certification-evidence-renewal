"""效期区间连续性检查。

闭区间日期按日计算。前后两段相邻（旧段 ``to`` 的次日等于新段 ``from``）
没有空档或重叠。空档/重叠只有在调用方提供书面说明时才允许，并会在报告中
保留 ``explained`` 标记。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .errors import TimelineConflictError
from .utils import parse_date


def check_interval_chain(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """检查同一条效期链上的记录。

    每个 item 至少包含 ``id``、``valid_from``、``valid_to``；可选
    ``explanation``/``timeline_note`` 用于解释异常。返回检查结果列表。
    """
    normalized: list[dict[str, Any]] = []
    for raw in items:
        start = parse_date(raw.get("valid_from") or raw.get("effective_from"), "valid_from")
        end = parse_date(raw.get("valid_to") or raw.get("effective_to"), "valid_to")
        if start is None:
            # 没有起点的记录无法参与严格连续性判断。
            continue
        normalized.append(
            {
                "id": raw.get("id") or raw.get("decision_id") or raw.get("certificate_id"),
                "kind": raw.get("kind", "record"),
                "valid_from": start,
                "valid_to": end,
                "explanation": raw.get("explanation") or raw.get("timeline_note"),
                "raw": raw,
            }
        )

    normalized.sort(key=lambda item: (item["valid_from"], item["valid_to"] or date.max, str(item["id"])))
    results: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for previous, current in zip(normalized, normalized[1:]):
        prev_end: date | None = previous["valid_to"]
        curr_start: date = current["valid_from"]
        problem: dict[str, Any] | None = None

        if prev_end is None:
            # 前段没有结束日，后段又开始：必然重叠，除非明确结束/说明。
            problem = {
                "type": "overlap",
                "between": [previous["id"], current["id"]],
                "message": f"{previous['id']} 未填写结束日，无法排除与 {current['id']} 重叠",
            }
        elif curr_start == prev_end + timedelta(days=1):
            results.append(
                {
                    "type": "contiguous",
                    "between": [previous["id"], current["id"]],
                    "gap_days": 0,
                }
            )
            continue
        elif curr_start > prev_end + timedelta(days=1):
            gap_days = (curr_start - prev_end).days - 1
            problem = {
                "type": "gap",
                "between": [previous["id"], current["id"]],
                "gap_days": gap_days,
                "message": f"{previous['id']} 与 {current['id']} 之间存在 {gap_days} 天空档",
            }
        else:
            overlap_days = (prev_end - curr_start).days + 1
            problem = {
                "type": "overlap",
                "between": [previous["id"], current["id"]],
                "overlap_days": overlap_days,
                "message": f"{previous['id']} 与 {current['id']} 重叠 {overlap_days} 天",
            }

        explanation = current.get("explanation") or previous.get("explanation")
        if explanation:
            problem["explained"] = True
            problem["explanation"] = explanation
            results.append(problem)
        else:
            problem["explained"] = False
            conflicts.append(problem)
            results.append(problem)

    if conflicts:
        raise TimelineConflictError(
            "存在未解释的效期空档或重叠",
            conflicts=conflicts,
        )
    return results
