"""导入仓库自带的合成 CSV 样例。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .models import SOURCE_SAMPLE
from .utils import period_range


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, strict=True))


def import_samples(app, samples_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(samples_dir) if samples_dir else Path(__file__).resolve().parent.parent / "samples"
    event = app.ingest(
        SOURCE_SAMPLE,
        {"import": "sample_csv", "directory": str(root)},
        source_record_id="sample-csv-import",
    ).event
    event_id = event.get("id") if event else None

    sites = _read_csv(root / "sites.csv")
    evidence_rows = _read_csv(root / "evidence.csv")
    decision_rows = _read_csv(root / "decisions.csv")

    site_records = []
    for row in sites:
        record = app.upsert_site_boundary(
            {
                "site_id": row["site"],
                "company": row["company"],
                "area_m2": row["area_m2"],
                "valid_from": row["valid_from"],
                "revision": int(row["revision"]),
                "imported": True,
            },
            source_event_id=event_id,
        )
        site_records.append(record)

    evidence_records = []
    for row in evidence_rows:
        evidence = app._upsert_evidence_version(
            evidence_id=row["evidence"],
            kind=row["kind"],
            site_id=row["site"],
            value=row.get("value") or None,
            unit=row.get("unit") or None,
            valid_from=row.get("from") or None,
            valid_to=row.get("to") or None,
            source_event_id=event_id,
            payload={"sample_row": row},
            source_revision=int(row["revision"]) if row.get("revision") else None,
            title=f"样例证据 {row['evidence']} ({row['kind']})",
        )
        evidence_records.append(evidence)

        kind = row["kind"].lower()
        if kind in {"certificate", "audit", "third_party_certificate"}:
            app._upsert_certificate(
                {
                    "certificate_id": f"CERT-{row['evidence']}",
                    "certificate_no": row["evidence"],
                    "site_id": row["site"],
                    "issuer": "Sample third-party issuer",
                    "valid_from": row["from"],
                    "valid_to": row["to"],
                    "evidence_ref": f"{evidence['evidence_id']}@{evidence['version']}",
                    "verification_status": "not_verified",
                },
                {"id": event_id},
                evidence,
            )
        if row.get("value"):
            period_label = _period_from_dates(row.get("from"), row.get("to"))
            app._upsert_metric_version(
                {
                    "metric_id": f"M-SAMPLE-{row['evidence']}",
                    "site_id": row["site"],
                    "code": kind,
                    "value": row["value"],
                    "unit": row.get("unit"),
                    "period": period_label,
                    "period_start": row.get("from"),
                    "period_end": row.get("to"),
                    "evidence_ref": f"{evidence['evidence_id']}@{evidence['version']}",
                },
                {"id": event_id},
            )

    decision_records = []
    for row in decision_rows:
        if app.store.find_one("decisions", id=row["decision"]):
            continue
        refs = [ref for ref in (row.get("evidence_refs") or "").split("|") if ref]
        snapshot = app.snapshot_evidence_refs(refs)
        period = row["period"]
        start, end, label = period_range(period)
        boundary = app.store.find_one("sites", site_id=row["site"], revision=int(row["site_revision"]))
        decision = {
            "id": row["decision"],
            "_collection": "decisions",
            "type": "review_decision",
            "decision_id": row["decision"],
            "site_id": row["site"],
            "period": label,
            "period_start": start.isoformat() if start else None,
            "period_end": end.isoformat() if end else None,
            "state": row["state"],
            "result": row["result"],
            "conclusion": f"样例导入结论：{row['result']}",
            "scope": {
                "site_id": row["site"],
                "period": label,
                "period_start": start.isoformat() if start else None,
                "period_end": end.isoformat() if end else None,
                "site_revision": int(row["site_revision"]),
                "boundary_revisions": [
                    {
                        "boundary_id": boundary["id"] if boundary else None,
                        "revision": int(row["site_revision"]),
                        "valid_from": boundary.get("valid_from") if boundary else None,
                        "area_m2": boundary.get("area_m2") if boundary else None,
                        "confirmation_status": "imported",
                    }
                ],
                "product_codes": [],
                "boundary_statement": f"样例引用站点修订 R{row['site_revision']}",
            },
            "evidence": snapshot,
            "evidence_refs": refs,
            "metric_findings": [],
            "category_status": {},
            "unassessed": [],
            "completeness": {"imported": True},
            "conditions": [],
            "review_reasons": ["样例 CSV 导入，保留原 state/result"],
            "trigger_ids": [],
            "case_id": None,
            "parent_decision_id": None,
            "supersedes_id": None,
            "root_decision_id": row["decision"],
            "evidence_diff": None,
            "reviewer": "sample-import",
            "created_by": "sample-import",
            "created_at": "2026-01-15T00:00:00+00:00",
            "updated_at": "2026-01-15T00:00:00+00:00",
            "submitted_at": None,
            "issued_at": "2026-01-15T00:00:00+00:00" if row["state"] == "issued" else None,
            "effective_from": None,
            "effective_to": None,
            "timeline_results": [],
            "timeline_note": None,
            "imported_sample": True,
        }
        app.store.add("decisions", decision)
        decision_records.append(decision)

    # 打开样例期间的缺项视图。
    for site_id, period in [("S1", "2026-H1"), ("S1", "2026-H2"), ("S2", "2026-H1")]:
        try:
            app.assess(site_id, period, create_issues=True)
        except Exception:
            # 样例导入不应因评估增强字段而失败；原始 CSV 关联仍完整。
            continue
    app.save()
    return {
        "sites": len(site_records),
        "evidence": len(evidence_records),
        "decisions": len(decision_records),
    }


def _period_from_dates(start: str | None, end: str | None) -> str | None:
    if not start:
        return None
    year, month, _ = start.split("-")
    if month in {"01", "02", "03", "04", "05", "06"}:
        return f"{year}-H1"
    return f"{year}-H2"
