"""绿色制造证据采集、版本、复审和秘书处视图的端到端测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from green_review import (
    DecisionState,
    GreenReviewApp,
    IngestStatus,
    SourceUnavailableError,
    TimelineConflictError,
)
from green_review.sample import import_samples


class FixedClockMixin:
    def setUp(self):
        super().setUp()
        self.app = GreenReviewApp.in_memory()
        self.app._clock = lambda: datetime(2026, 9, 15, tzinfo=timezone.utc)

    def complete_site(
        self,
        site_id="S1",
        period="2026-H1",
        *,
        with_certificate=True,
        confirm=True,
        accept=True,
    ):
        self.app.receive_attachment_metadata(
            {
                "attachment_id": "BMAP1",
                "site_id": site_id,
                "kind": "boundary_map",
                "revision": 1,
                "site_revision": 1,
                "valid_from": "2026-01-01",
                "area_m2": 18000,
                "filename": "boundary.pdf",
                "sha256": "map-hash-1",
            }
        )
        self.app.receive_portal_form(
            {
                "submission_id": f"FORM-{site_id}-{period}",
                "site_id": site_id,
                "company": "C-1",
                "products": ["P1", "P2"],
                "period": period,
                "metrics": [
                    {"code": "electricity", "value": 410000, "unit": "kWh"},
                    {"code": "water_consumption", "value": 12000, "unit": "m3"},
                    {"code": "waste_to_landfill", "value": 120, "unit": "t"},
                    {"code": "waste_recycled", "value": 300, "unit": "t"},
                    {"code": "equipment_modification", "observation": "电机与照明改造验收单"},
                ],
            }
        )
        self.app.receive_factor(
            {
                "factor_id": "grid_electricity",
                "version": 1,
                "value": "0.5703",
                "unit": "kgCO2e/kWh",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "source_url": "https://factors.example/grid",
            }
        )
        scope = self.app.store.find_one("product_scopes", site_id=site_id)
        if confirm:
            self.app.confirm_boundary(site_id, 1, "联络员甲")
            self.app.confirm_product_scope(scope["id"], "联络员甲")
        if with_certificate:
            self.app.receive_attachment_metadata(
                {
                    "attachment_id": "CERT-DOC-1",
                    "evidence_id": "CERT-E1",
                    "site_id": site_id,
                    "kind": "certificate",
                    "certificate_no": "CERT-001",
                    "issuer": "Example Accredited Body",
                    "valid_from": "2025-01-01",
                    "valid_to": "2026-06-30",
                    "products": ["P1", "P2"],
                    "sha256": "cert-hash-1",
                }
            )
            self.app.receive_authorization(
                {
                    "certificate_id": "CERT-CERT-001",
                    "certificate_no": "CERT-001",
                    "status": "verified",
                    "checked_at": "2026-06-01T00:00:00+00:00",
                }
            )
            cert = self.app.store.find_one("certificates", certificate_no="CERT-001", is_current=True)
            if confirm:
                self.app.confirm_certificate_mapping(cert["certificate_id"], "联络员甲")
        if accept:
            for metric in self.app.current_metrics(site_id, period):
                self.app.accept_metric(metric["metric_id"], "评审乙", caliber_note="口径与边界一致")
        return scope


class IngestionTests(FixedClockMixin, unittest.TestCase):
    def test_duplicate_submission_is_recorded_without_new_version(self):
        payload = {"attachment_id": "A1", "site_id": "S1", "kind": "electricity", "sha256": "same", "revision": 1}
        first = self.app.receive_attachment_metadata(payload)
        second = self.app.receive_attachment_metadata(dict(payload))
        self.assertEqual(first.status, IngestStatus.PROCESSED)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.status, IngestStatus.DUPLICATE)
        self.assertEqual(len(self.app.evidence_versions("A1")), 1)
        duplicate_events = [e for e in self.app.store.all("events") if e.get("status") == "duplicate"]
        self.assertTrue(duplicate_events)

    def test_correction_creates_version_and_keeps_original_snapshot(self):
        self.app.receive_attachment_metadata(
            {"attachment_id": "A1", "kind": "electricity", "sha256": "h1", "revision": 1, "value": 100}
        )
        self.app.receive_attachment_metadata(
            {"attachment_id": "A1", "kind": "electricity", "sha256": "h2", "revision": 2, "value": 120}
        )
        versions = self.app.evidence_versions("A1")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["status"], "superseded")
        self.assertEqual(versions[1]["status"], "active")
        snapshot = self.app.snapshot_evidence_refs(["A1@1"])[0]
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["sha256"], "h1")

    def test_out_of_order_old_revision_is_retained_but_not_active(self):
        self.app.receive_attachment_metadata({"attachment_id": "A1", "sha256": "h2", "revision": 2})
        late_old = self.app.receive_attachment_metadata({"attachment_id": "A1", "sha256": "h1", "revision": 1})
        self.assertEqual(late_old.status, IngestStatus.OUT_OF_ORDER)
        self.assertEqual(len(self.app.evidence_versions("A1")), 1)
        self.assertEqual(self.app.get_evidence("A1")["sha256"], "h2")

    def test_source_unavailable_keeps_trace_and_previous_data(self):
        self.app.receive_factor(
            {"factor_id": "F1", "version": 1, "value": "2.0", "effective_from": "2026-01-01"}
        )
        result = self.app.record_source_unavailable(
            "public_factor", source_record_id="F1", url="https://factor.example/F1", error="HTTP 503"
        )
        self.assertEqual(result.status, IngestStatus.SOURCE_UNAVAILABLE)
        failures = [e for e in self.app.store.all("events") if e["status"] == "source_unavailable"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.app.get_factor("F1")["value"], 2)

        def unavailable_fetcher(_target):
            raise SourceUnavailableError("timeout", source="https://factor.example/F2")

        collected = self.app.collect_public_factor(factor_id="F2", fetcher=unavailable_fetcher)
        self.assertFalse(collected.ok)
        self.assertEqual(collected.status, IngestStatus.SOURCE_UNAVAILABLE)

    def test_rejected_payload_is_traceable(self):
        result = self.app.ingest("portal", {"submission_id": "BAD", "boundary": {"revision": 1}})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, IngestStatus.REJECTED)
        rejected = [e for e in self.app.store.all("events") if e["status"] == "rejected"]
        self.assertTrue(rejected[0]["errors"])


class AssessmentTests(FixedClockMixin, unittest.TestCase):
    def test_missing_metric_is_unassessed_not_zero(self):
        self.app.receive_portal_form(
            {"submission_id": "F-EMPTY", "site_id": "S9", "period": "2026-H1", "metrics": []}
        )
        assessment = self.app.assess("S9", "2026-H1")
        self.assertTrue(assessment["unassessed"])
        scopes = " ".join(item["scope"] + item["statement"] for item in assessment["unassessed"])
        self.assertIn("energy", scopes)
        self.assertIn("未核定", scopes)
        for metric in assessment["metrics"]:
            if metric["status"] == "unassessed":
                self.assertIsNone(metric["value"])
        pending = self.app.pending_materials("S9")
        self.assertTrue(any("未核定" in issue["title"] or "缺少" in issue["title"] for issue in pending))

    def test_explicit_zero_is_preserved_but_missing_is_not_zero(self):
        self.app.receive_portal_form(
            {
                "submission_id": "F-ZERO",
                "site_id": "SZ",
                "period": "2026-H1",
                "metrics": [{"code": "electricity", "value": 0, "unit": "kWh"}],
            }
        )
        metric = self.app.current_metrics("SZ", "2026-H1")[0]
        self.assertEqual(metric["value"], 0)
        self.assertTrue(metric["value_present"])

    def test_complete_scope_supports_pass_draft(self):
        self.complete_site()
        assessment = self.app.assess("S1", "2026-H1")
        self.assertEqual(assessment["unassessed"], [])
        self.assertTrue(assessment["completeness"]["ready_for_pass"])
        self.assertTrue(any(m.get("converted_impact") is not None for m in assessment["metrics"]))


class DecisionAndRereviewTests(FixedClockMixin, unittest.TestCase):
    def test_issued_decision_freezes_evidence_and_review_creates_new_decision(self):
        self.complete_site()
        draft = self.app.create_decision_draft("S1", "2026-H1", decision_id="D1", result="pass")
        decision = self.app.issue_decision(
            "D1", "评审乙", effective_from="2026-07-01", effective_to="2026-12-31"
        )
        self.assertEqual(decision["state"], DecisionState.ISSUED)
        old_electricity_ref = next(ref for ref in decision["evidence"] if ref["evidence_id"].startswith("FORM-"))

        # 企业补交更正后的表单和因子。
        self.app.receive_portal_form(
            {
                "event_id": "evt-correction",
                "submission_id": "FORM-S1-2026-H1",
                "site_id": "S1",
                "products": ["P1", "P2"],
                "period": "2026-H1",
                "revision": 2,
                "metrics": [
                    {"code": "electricity", "value": 415000, "unit": "kWh"},
                    {"code": "water_consumption", "value": 11800, "unit": "m3"},
                    {"code": "waste_to_landfill", "value": 110, "unit": "t"},
                    {"code": "waste_recycled", "value": 310, "unit": "t"},
                    {"code": "equipment_modification", "observation": "补充验收附件"},
                ],
            }
        )
        self.app.receive_factor(
            {
                "factor_id": "grid_electricity",
                "version": 2,
                "value": "0.5810",
                "unit": "kgCO2e/kWh",
                "correction_reason": "公开因子页面更正网格因子",
                "corrects_version": 1,
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
            }
        )
        impact = self.app.analyze_impact(
            "factor_corrected", factor_id="grid_electricity", summary="网格因子更正"
        )
        self.assertIn("D1", impact["affected_decision_ids"])
        new_draft_id = impact["draft_decision_ids"][0]
        new_draft = self.app.get_decision(new_draft_id)
        self.assertNotEqual(new_draft_id, "D1")
        self.assertEqual(new_draft["supersedes_id"], "D1")
        self.assertTrue(new_draft["review_reasons"])
        self.assertTrue(new_draft["evidence_diff"]["version_changed"] or new_draft["evidence_diff"]["added"])

        # 原决定和证据快照不变。
        old = self.app.get_decision("D1")
        self.assertEqual(old["state"], DecisionState.ISSUED)
        frozen_ref = next(ref for ref in old["evidence"] if ref["evidence_id"] == old_electricity_ref["evidence_id"])
        self.assertEqual(frozen_ref["version"], old_electricity_ref["version"])
        self.assertEqual(frozen_ref["sha256"], old_electricity_ref["sha256"])

        # 草案未经复审确认不改变当前生效决定。
        dashboard = self.app.dashboard(as_of="2026-09-15")
        self.assertIn("D1", [d["id"] for d in dashboard["effective_decisions"]])
        self.assertNotIn(new_draft_id, [d["id"] for d in dashboard["effective_decisions"]])

    def test_boundary_expansion_identifies_open_items_and_certificates(self):
        # H2 决定先签发，期间跨越 8 月 1 日扩建边界。
        self.complete_site(period="2026-H2")
        # 证明改为覆盖 H2。
        cert = self.app.store.find_one("certificates", certificate_no="CERT-001")
        cert["valid_from"] = "2025-01-01"
        cert["valid_to"] = "2026-12-31"
        self.app.store.update("certificates", cert)
        draft = self.app.create_decision_draft("S1", "2026-H2", decision_id="D-H2", result="pass")
        self.app.issue_decision("D-H2", "评审乙", effective_from="2026-07-01")

        impact = self.app.receive_attachment_metadata(
            {
                "attachment_id": "EXPANSION-MAP",
                "site_id": "S1",
                "kind": "boundary_map",
                "revision": 2,
                "site_revision": 2,
                "valid_from": "2026-08-01",
                "area_m2": 22000,
                "filename": "expansion.pdf",
                "sha256": "expansion-hash",
                "change_summary": "二号厂房扩建",
            }
        ).impacts[0]
        self.assertEqual(impact["change_type"], "boundary_changed")
        self.assertIn("D-H2", impact["affected_decision_ids"])
        self.assertTrue(impact["affected_certificates"])
        self.assertTrue(impact["open_issue_ids"] or self.app.pending_materials("S1"))
        draft_ids = impact["draft_decision_ids"]
        self.assertTrue(draft_ids)
        rereview = self.app.get_decision(draft_ids[0])
        self.assertEqual(rereview["supersedes_id"], "D-H2")
        self.assertTrue(any("边界" in reason or "boundary" in reason for reason in rereview["review_reasons"]))

    def test_pass_issue_confirmation_accepts_pending_metric_but_rejection_blocks(self):
        self.complete_site(accept=False)
        self.app.create_decision_draft("S1", "2026-H1", decision_id="D-BLOCK", result="pass")
        # 签发人确认时，待确认指标可由签发动作承接；显式驳回则必须先处理。
        decision = self.app.issue_decision("D-BLOCK", "评审乙", effective_from="2026-07-01")
        self.assertEqual(decision["state"], DecisionState.ISSUED)

        self.complete_site(site_id="S2", period="2026-H2", accept=False)
        metric = self.app.current_metrics("S2", "2026-H2")[0]
        self.app.accept_metric(metric["metric_id"], "评审乙", accepted=False, caliber_note="口径冲突")
        self.app.create_decision_draft("S2", "2026-H2", decision_id="D-BLOCK-2", result="pass")
        with self.assertRaises(Exception):
            self.app.issue_decision("D-BLOCK-2", "评审乙", effective_from="2026-07-01")

    def test_timeline_gap_and_overlap_require_explanation(self):
        self.complete_site()
        self.app.create_decision_draft("S1", "2026-H1", decision_id="D-T1", result="conditional", conditions=["补材料"])
        self.app.issue_decision("D-T1", "评审乙", effective_from="2026-07-01", effective_to="2026-07-31")
        self.app.create_decision_draft(
            "S1",
            "2026-H2",
            decision_id="D-T2",
            parent_decision_id="D-T1",
            result="conditional",
            conditions=["H2 用电补充第三方复核"],
        )
        with self.assertRaises(TimelineConflictError) as ctx:
            self.app.issue_decision("D-T2", "评审乙", effective_from="2026-09-01")
        self.assertTrue(ctx.exception.conflicts)
        # 补充书面说明后允许留下可解释空档。
        issued = self.app.issue_decision(
            "D-T2",
            "评审乙",
            result="conditional",
            conditions=["H2 用电补充第三方复核"],
            effective_from="2026-09-01",
            timeline_note="8 月空档用于企业补充边界图，秘书处已登记暂停计算",
            allow_stale_snapshot=True,
        )
        self.assertTrue(any(item.get("explained") for item in issued["timeline_results"]))


class PersistenceAndDashboardTests(unittest.TestCase):
    def test_json_persistence_retains_versions_and_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "review.json"
            app = GreenReviewApp.open(str(path))
            app.receive_attachment_metadata({"attachment_id": "A1", "site_id": "S1", "sha256": "h1"})
            app.save()
            reopened = GreenReviewApp.open(str(path))
            self.assertEqual(reopened.get_evidence("A1")["sha256"], "h1")
            dashboard = reopened.dashboard(as_of="2026-11-25")
            for key in ["current_certifications", "pending_materials", "rereview_reasons", "evidence_diffs"]:
                self.assertIn(key, dashboard)

    def test_sample_import_and_decision_snapshot(self):
        app = GreenReviewApp.in_memory()
        result = import_samples(app, Path(__file__).resolve().parent.parent / "samples")
        self.assertEqual(result["decisions"], 3)
        d1 = app.get_decision("D1")
        self.assertEqual([ref["evidence_id"] for ref in d1["evidence"]], ["E1", "E3"])
        self.assertEqual(all(ref["version"] == 1 for ref in d1["evidence"]), True)
        dashboard = app.dashboard(as_of="2026-11-25")
        self.assertTrue(dashboard["pending_materials"])
        self.assertTrue(any(c["certificate_id"] == "CERT-E3" for c in dashboard["current_certifications"]))
        e3 = next(c for c in dashboard["current_certifications"] if c["certificate_id"] == "CERT-E3")
        self.assertFalse(e3["date_valid"])


if __name__ == "__main__":
    unittest.main()
