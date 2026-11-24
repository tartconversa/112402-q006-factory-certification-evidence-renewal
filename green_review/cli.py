"""命令行入口：采集增量 JSON、导入样例、查询秘书处仪表盘。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .app import GreenReviewApp
from .errors import GreenReviewError
from .sample import import_samples
from .utils import _json_default


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)


def load_json_argument(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="green-review", description="绿色制造申报证据采集与复审管理")
    parser.add_argument("--db", default=None, help="JSON 仓储路径；也可用 GREEN_REVIEW_DB")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化空仓储")
    sample = sub.add_parser("import-samples", help="导入 samples/ 中的 CSV")
    sample.add_argument("--samples", default=None)

    ingest = sub.add_parser("ingest", help="采集一条 JSON 增量内容")
    ingest.add_argument("source", choices=["portal", "attachment", "factor", "authorization"])
    ingest.add_argument("file", help="JSON 文件，- 表示标准输入")
    ingest.add_argument("--event-id")
    ingest.add_argument("--source-record-id")

    assess = sub.add_parser("assess", help="评估站点/期间的缺项")
    assess.add_argument("--site", required=True)
    assess.add_argument("--period")
    assess.add_argument("--as-of")

    dash = sub.add_parser("dashboard", help="秘书处当前视图")
    dash.add_argument("--site")
    dash.add_argument("--as-of")
    dash.add_argument("--text", action="store_true", help="输出中文文本摘要而非 JSON")

    decision = sub.add_parser("decision", help="查看决定和证据差异")
    decision.add_argument("decision_id")
    decision.add_argument("--diff", action="store_true")

    confirm = sub.add_parser("confirm", help="联络员确认边界、产品范围或证明映射")
    confirm.add_argument("subject", choices=["boundary", "scope", "certificate"])
    confirm.add_argument("subject_id")
    confirm.add_argument("--revision", type=int)
    confirm.add_argument("--by", required=True)
    confirm.add_argument("--note")

    draft = sub.add_parser("draft", help="创建评审/复审草案")
    draft.add_argument("--site", required=True)
    draft.add_argument("--period")
    draft.add_argument("--id", dest="decision_id")
    draft.add_argument("--parent")
    draft.add_argument("--result")
    draft.add_argument("--evidence", nargs="*")
    draft.add_argument("--reason", action="append")

    issue = sub.add_parser("issue", help="复审人员确认并签发草案")
    issue.add_argument("decision_id")
    issue.add_argument("--reviewer", required=True)
    issue.add_argument("--result")
    issue.add_argument("--conclusion")
    issue.add_argument("--effective-from")
    issue.add_argument("--effective-to")
    issue.add_argument("--timeline-note")
    issue.add_argument("--allow-stale-snapshot", action="store_true")

    serve = sub.add_parser("serve", help="启动标准库 HTTP 接收服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def make_app(args: argparse.Namespace) -> GreenReviewApp:
    import os

    db_path = args.db or os.environ.get("GREEN_REVIEW_DB") or "green_review.json"
    return GreenReviewApp.open(db_path)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        app = make_app(args)
        if args.command == "init":
            app.save()
            print(dumps({"ok": True, "db": str(app.store.path)}))
        elif args.command == "import-samples":
            result = import_samples(app, args.samples)
            print(dumps({"ok": True, **result}))
        elif args.command == "ingest":
            payload = load_json_argument(args.file)
            result = app.ingest(
                args.source,
                payload,
                event_id=args.event_id,
                source_record_id=args.source_record_id,
            )
            print(dumps(result.to_dict()))
            return 0 if result.ok else 2
        elif args.command == "assess":
            result = app.assess(args.site, args.period, as_of=args.as_of)
            # 输出中包含完整 issue 对象；JSON 可序列化。
            print(dumps(result))
        elif args.command == "dashboard":
            result = app.dashboard(as_of=args.as_of, site_id=args.site)
            if args.text:
                print(format_dashboard_text(result))
            else:
                print(dumps(result))
        elif args.command == "decision":
            result = app.get_decision(args.decision_id)
            if args.diff:
                result = {"decision": result, "evidence_diff": app.decision_evidence_diff(args.decision_id)}
            print(dumps(result))
        elif args.command == "confirm":
            if args.subject == "boundary":
                if args.revision is None:
                    raise GreenReviewError("确认 boundary 必须提供 --revision")
                result = app.confirm_boundary(args.subject_id, args.revision, args.by, note=args.note)
            elif args.subject == "scope":
                result = app.confirm_product_scope(args.subject_id, args.by, note=args.note)
            else:
                result = app.confirm_certificate_mapping(args.subject_id, args.by, note=args.note)
            print(dumps({"ok": True, "confirmation_id": result["id"]}))
        elif args.command == "draft":
            result = app.create_decision_draft(
                args.site,
                args.period,
                decision_id=args.decision_id,
                result=args.result,
                evidence_refs=args.evidence if args.evidence is not None else None,
                parent_decision_id=args.parent,
                review_reasons=args.reason,
            )
            print(dumps({"ok": True, "decision_id": result["id"], "state": result["state"]}))
        elif args.command == "issue":
            result = app.issue_decision(
                args.decision_id,
                args.reviewer,
                result=args.result,
                conclusion=args.conclusion,
                effective_from=args.effective_from,
                effective_to=args.effective_to,
                timeline_note=args.timeline_note,
                allow_stale_snapshot=args.allow_stale_snapshot,
            )
            print(dumps({"ok": True, "decision_id": result["id"], "state": result["state"], "effective_from": result["effective_from"]}))
        elif args.command == "serve":
            from .server import serve as run_server

            run_server(app, host=args.host, port=args.port)
        return 0
    except GreenReviewError as exc:
        print(dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1


def format_dashboard_text(dashboard: dict[str, Any]) -> str:
    lines = [f"秘书处视图（截至 {dashboard['as_of']}）", ""]
    lines.append(f"当前日期有效证明：{len(dashboard['current_certifications'])} 项")
    for cert in dashboard["current_certifications"]:
        flags = []
        flags.append("日期有效" if cert.get("date_valid") else "不在效期")
        flags.append("已验证" if cert.get("verification_ok") else f"验证状态={cert.get('verification_status')}")
        flags.append("映射已确认" if cert.get("mapping_confirmed") else "映射待确认")
        lines.append(f"- {cert.get('certificate_id')} ({cert.get('site_id')}, {cert.get('valid_from')}~{cert.get('valid_to')})：{'，'.join(flags)}")
    lines.append("")
    lines.append(f"待补/未结材料：{len(dashboard['pending_materials'])} 项")
    for issue in dashboard["pending_materials"]:
        lines.append(f"- [{issue.get('severity')}] {issue.get('site_id') or '全局'} {issue.get('title')}")
    lines.append("")
    lines.append(f"复审原因：{len(dashboard['rereview_reasons'])} 项")
    for case in dashboard["rereview_reasons"]:
        lines.append(f"- {case.get('case_id')} {case.get('site_id') or '全局'}：{'；'.join(case.get('reasons') or [])}")
    lines.append("")
    lines.append(f"证据差异记录：{len(dashboard['evidence_diffs'])} 份")
    for item in dashboard["evidence_diffs"]:
        diff = item["diff"]
        lines.append(
            f"- 决定 {item['decision_id']}（{item['state']}）新增 {diff.get('added')}、移除 {diff.get('removed')}、版本变化 {[x['evidence_id'] for x in diff.get('version_changed', [])]}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
