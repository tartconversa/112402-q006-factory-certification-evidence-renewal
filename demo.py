"""无需外部服务的端到端演示：采集、核定、签发、变更影响和复审草案。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from green_review import GreenReviewApp
from green_review.utils import _json_default


def build_demo() -> GreenReviewApp:
    app = GreenReviewApp.in_memory()
    app._clock = lambda: datetime(2026, 9, 15, tzinfo=timezone.utc)  # noqa: SLF001

    app.receive_attachment_metadata(
        {
            "attachment_id": "BMAP-R1",
            "site_id": "S1",
            "kind": "boundary_map",
            "revision": 1,
            "site_revision": 1,
            "valid_from": "2026-01-01",
            "area_m2": 18000,
            "sha256": "boundary-map-v1",
        }
    )
    app.receive_portal_form(
        {
            "event_id": "form-h2-1",
            "submission_id": "FORM-S1-2026H2",
            "site_id": "S1",
            "company": "合成制造有限公司",
            "products": ["P1", "P2"],
            "period": "2026-H2",
            "metrics": [
                {"code": "electricity", "value": 290000, "unit": "kWh"},
                {"code": "water_consumption", "value": 8000, "unit": "m3"},
                {"code": "waste_to_landfill", "value": 60, "unit": "t"},
                {"code": "waste_recycled", "value": 180, "unit": "t"},
                {"code": "equipment_modification", "observation": "变频改造验收单 EQ-9"},
            ],
        }
    )
    app.receive_factor(
        {
            "factor_id": "grid_electricity",
            "version": 1,
            "value": "0.5703",
            "unit": "kgCO2e/kWh",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        }
    )
    app.receive_attachment_metadata(
        {
            "attachment_id": "CERT-1-DOC",
            "evidence_id": "CERT-1",
            "site_id": "S1",
            "kind": "certificate",
            "certificate_no": "CERT-CN-001",
            "issuer": "Example Accredited Body",
            "valid_from": "2025-01-01",
            "valid_to": "2026-12-31",
            "products": ["P1", "P2"],
            "sha256": "certificate-hash",
        }
    )
    app.receive_authorization(
        {"certificate_no": "CERT-CN-001", "status": "verified", "checked_at": "2026-07-02T00:00:00+00:00"}
    )

    scope = app.store.find_one("product_scopes", site_id="S1")
    app.confirm_boundary("S1", 1, "联络员 张敏")
    app.confirm_product_scope(scope["id"], "联络员 张敏")
    app.confirm_certificate_mapping("CERT-CERT-CN-001", "联络员 张敏")
    for metric in app.current_metrics("S1", "2026-H2"):
        app.accept_metric(metric["metric_id"], "评审 李工", caliber_note="口径含租赁车间，与 R1 边界一致")

    app.create_decision_draft("S1", "2026-H2", decision_id="D-H2", result="pass")
    app.issue_decision("D-H2", "评审 李工", effective_from="2026-07-01")

    # 重复提交不会产生新版本。
    app.receive_attachment_metadata(
        {
            "attachment_id": "CERT-1-DOC",
            "evidence_id": "CERT-1",
            "site_id": "S1",
            "kind": "certificate",
            "certificate_no": "CERT-CN-001",
            "valid_from": "2025-01-01",
            "valid_to": "2026-12-31",
            "products": ["P1", "P2"],
            "sha256": "certificate-hash",
        }
    )

    # 扩建边界图和因子更正到达后，自动生成复审草案。
    app.receive_attachment_metadata(
        {
            "attachment_id": "EXPANSION-MAP",
            "site_id": "S1",
            "kind": "boundary_map",
            "revision": 2,
            "site_revision": 2,
            "valid_from": "2026-08-01",
            "area_m2": 22000,
            "filename": "expansion-boundary.pdf",
            "sha256": "expansion-map-hash",
            "change_summary": "二号厂房扩建，统计边界增加 4000 m²",
        }
    )
    app.receive_factor(
        {
            "factor_id": "grid_electricity",
            "version": 2,
            "value": "0.5810",
            "unit": "kgCO2e/kWh",
            "correction_reason": "主管部门公开因子页面发布更正值",
            "corrects_version": 1,
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        }
    )
    return app


def main() -> int:
    app = build_demo()
    dashboard = app.dashboard(as_of="2026-09-15")
    summary = {
        "current_valid_certifications": [
            {"id": c["certificate_id"], "usable": c["usable_for_positive_decision"]}
            for c in dashboard["valid_certifications"]
        ],
        "pending_material_count": len(dashboard["pending_materials"]),
        "rereview_reasons": dashboard["rereview_reasons"],
        "effective_decisions": [d["id"] for d in dashboard["effective_decisions"]],
        "evidence_diffs": [
            {"decision": item["decision_id"], "diff": item["diff"]}
            for item in dashboard["evidence_diffs"]
            if item["diff"].get("added") or item["diff"].get("version_changed") or item["diff"].get("removed")
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
