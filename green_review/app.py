"""应用门面：采集、证据版本、评审、复审和秘书处视图。

设计要点：

* 所有输入先形成不可覆盖的采集事件；重复内容只记录 duplicate，不变更领域数据。
* 稳定业务 ID 的材料更正会产生新 ``version``，旧版本和哈希继续保留。
* 缺少数值或证据时得到 ``unassessed``，绝不会被改写成 ``0``。
* 已签发决定保存证据快照；新草案/新决定不回写旧决定的证据清单。
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from .errors import (
    ConflictError,
    NotFoundError,
    ReviewBlockedError,
    SourceUnavailableError,
    TimelineConflictError,
    ValidationError,
)
from .models import (
    CATEGORY_LABELS,
    METRIC_SPECS,
    REQUIRED_CATEGORIES,
    SOURCE_ALIASES,
    SOURCE_ATTACHMENT,
    SOURCE_AUTH,
    SOURCE_FACTOR,
    SOURCE_PORTAL,
    ConfirmationStatus,
    DecisionState,
    IngestStatus,
    IssueSeverity,
    IssueStatus,
    ReviewCaseStatus,
    VerificationStatus,
    normalize_metric_code,
)
from .store import Store
from .timeline import check_interval_chain
from .sources import parse_factor_content
from .utils import (
    as_list,
    compact_dict,
    content_hash,
    file_sha256,
    intervals_overlap,
    iso_date,
    parse_date,
    parse_datetime,
    period_range,
    to_decimal,
    unique_strings,
    utc_now,
    utc_now_iso,
)

UNSET = object()


def transactional(method: Callable) -> Callable:
    """让一个高层用例在同一把可重入仓储锁内完成。"""

    def wrapper(self: "GreenReviewApp", *args: Any, **kwargs: Any) -> Any:
        with self.store.lock:
            return method(self, *args, **kwargs)

    wrapper.__name__ = getattr(method, "__name__", "wrapper")
    wrapper.__doc__ = method.__doc__
    return wrapper


class IngestResult:
    """一次增量采集的结果。"""

    def __init__(
        self,
        *,
        status: str,
        event: dict[str, Any] | None = None,
        records: list[dict[str, Any]] | None = None,
        impacts: list[dict[str, Any]] | None = None,
        duplicate: bool = False,
        errors: list[str] | None = None,
        app: "GreenReviewApp | None" = None,
    ):
        self.status = str(status)
        self.event = event
        self.records = records or []
        self.impacts = impacts or []
        self.duplicate = duplicate
        self.errors = errors or []
        self.ok = self.status not in {
            str(IngestStatus.REJECTED),
            str(IngestStatus.SOURCE_UNAVAILABLE),
        }
        self._app = app

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "duplicate": self.duplicate,
            "errors": self.errors,
            "event_id": self.event.get("id") if self.event else None,
            "records": [
                compact_dict(
                    {
                        "collection": record.get("_collection"),
                        "id": record.get("id"),
                        "type": record.get("type"),
                        "evidence_id": record.get("evidence_id"),
                        "metric_id": record.get("metric_id"),
                        "version": record.get("version"),
                        "status": record.get("status"),
                    }
                )
                for record in self.records
            ],
            "impact_ids": [item.get("id") for item in self.impacts],
        }


class GreenReviewApp:
    """绿色制造证据管理应用的高层 API。"""

    def __init__(
        self,
        store: Store | str | "PathLike" | None = None,
        path: str | "PathLike" | None = None,
        clock: Callable[[], Any] | None = None,
        **kwargs: Any,
    ):
        # 兼容 GreenReviewApp("/path/to/db.json") 与 GreenReviewApp(db_path=...)。
        if isinstance(store, (str, bytes)) or hasattr(store, "__fspath__"):
            path = store
            store = None
        path = path or kwargs.get("db_path") or kwargs.get("database_path") or kwargs.get("store_path") or kwargs.get("file_path")
        storage = kwargs.get("storage") or kwargs.get("repository")
        if isinstance(storage, Store):
            store = storage
        elif isinstance(storage, (str, bytes)) or hasattr(storage, "__fspath__"):
            path = storage
        self.store = store if isinstance(store, Store) else Store(path)
        self._clock = clock or utc_now

    # ------------------------------------------------------------------
    # 构造和基础辅助
    # ------------------------------------------------------------------
    @classmethod
    def open(cls, path: str | "PathLike") -> "GreenReviewApp":
        return cls(path=path)

    @classmethod
    def load(cls, path: str | "PathLike") -> "GreenReviewApp":
        return cls(path=path)

    @classmethod
    def from_path(cls, path: str | "PathLike") -> "GreenReviewApp":
        return cls(path=path)

    @classmethod
    def create(cls, path: str | "PathLike" | None = None) -> "GreenReviewApp":
        app = cls(path=path)
        app.save()
        return app

    @classmethod
    def in_memory(cls) -> "GreenReviewApp":
        return cls(Store())

    def now(self):
        value = self._clock()
        if isinstance(value, date) and not hasattr(value, "hour"):
            from datetime import datetime, timezone

            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return value

    def today(self) -> date:
        return self.now().date()

    def save(self) -> None:
        self.store.save()

    def _new_id(self, prefix: str, key: str | None = None) -> str:
        return self.store.next_id(prefix, key or prefix)

    @staticmethod
    def _normalize_source(source: str) -> str:
        key = str(source).strip().lower().replace("-", "_")
        if key not in SOURCE_ALIASES:
            raise ValidationError(f"未知采集来源 {source!r}")
        return SOURCE_ALIASES[key]

    def _event_by_client_id(self, event_id: str) -> dict[str, Any] | None:
        return self.store.find_one("events", id=event_id)

    # ------------------------------------------------------------------
    # 采集事件
    # ------------------------------------------------------------------
    def ingest_many(self, source: str, payloads: list[dict[str, Any]], **kwargs) -> list[IngestResult]:
        return [self.ingest(source, payload, **kwargs) for payload in payloads]

    @transactional
    def ingest(
        self,
        source: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        source_record_id: str | None = None,
        received_at: Any = None,
        occurred_at: Any = None,
        actor: str | None = None,
    ) -> IngestResult:
        """接收一条增量内容。重复、旧顺序内容和校验失败均留下事件记录。"""
        source_name = self._normalize_source(source)
        if not isinstance(payload, dict):
            result = self._record_rejected(
                source_name,
                payload if isinstance(payload, dict) else {"raw": payload},
                ["payload 必须是 JSON 对象"],
                event_id=event_id,
                source_record_id=source_record_id,
                received_at=received_at,
            )
            return result

        client_event_id = event_id or payload.get("event_id")
        if client_event_id and self._event_by_client_id(str(client_event_id)):
            old = self._event_by_client_id(str(client_event_id))
            assert old is not None
            return IngestResult(
                status=old.get("status", IngestStatus.DUPLICATE),
                event=old,
                duplicate=True,
                app=self,
            )

        record_id = self._source_record_id(source_name, payload, source_record_id)
        payload_hash = str(payload.get("content_hash") or content_hash(payload))
        prior_same_hash = self.store.find_one(
            "events",
            source=source_name,
            source_record_id=record_id,
            content_hash=payload_hash,
        )
        if prior_same_hash is not None and prior_same_hash.get("status") != str(IngestStatus.REJECTED):
            event = self._base_event(
                source_name,
                payload,
                event_id=client_event_id,
                source_record_id=record_id,
                payload_hash=payload_hash,
                received_at=received_at,
                occurred_at=occurred_at,
                actor=actor,
            )
            event["status"] = str(IngestStatus.DUPLICATE)
            event["duplicate_of"] = prior_same_hash.get("id")
            self.store.add("events", event)
            self.save()
            return IngestResult(status=IngestStatus.DUPLICATE, event=event, duplicate=True, app=self)

        snapshot = copy.deepcopy(self.store.data)
        try:
            source_revision = self._payload_revision(payload)
            out_of_order = self._is_out_of_order(source_name, record_id, source_revision)
            event = self._base_event(
                source_name,
                payload,
                event_id=client_event_id,
                source_record_id=record_id,
                payload_hash=payload_hash,
                received_at=received_at,
                occurred_at=occurred_at,
                actor=actor,
            )
            event["source_revision"] = source_revision
            if out_of_order:
                event["status"] = str(IngestStatus.OUT_OF_ORDER)
                event["warning"] = "收到比当前版本更旧的修订；内容已留存，但不会成为有效版本"
                self.store.add("events", event)
                self.save()
                return IngestResult(status=IngestStatus.OUT_OF_ORDER, event=event, app=self)

            prior_versions = [
                item
                for item in self.store.find("events", source=source_name, source_record_id=record_id)
                if item.get("status") in {str(IngestStatus.PROCESSED), str(IngestStatus.REVISED)}
            ]
            event["status"] = str(IngestStatus.REVISED if prior_versions else IngestStatus.PROCESSED)
            self.store.add("events", event)

            records: list[dict[str, Any]] = []
            impacts: list[dict[str, Any]] = []
            if source_name == SOURCE_PORTAL:
                records = self._process_portal_form(payload, event)
                site_id = payload.get("site_id") or payload.get("site", {}).get("site_id") if isinstance(payload.get("site"), dict) else payload.get("site_id")
                period = payload.get("period") or payload.get("reporting_period")
                if site_id:
                    assessment = self.assess(str(site_id), period, create_issues=True)
                    event["assessment_summary"] = assessment.get("completeness")
                boundary_data = payload.get("boundary") if isinstance(payload.get("boundary"), dict) else {}
                boundary_revision = boundary_data.get("revision", payload.get("site_revision", payload.get("revision")))
                portal_boundary_changed = payload.get("change_type") == "boundary_expansion" or (
                    isinstance(boundary_revision, int) and boundary_revision > 1
                )
                if portal_boundary_changed and site_id:
                    impacts.append(
                        self.analyze_impact(
                            change_type="boundary_changed",
                            site_id=str(site_id),
                            trigger_event_id=event["id"],
                            summary=payload.get("change_summary") or "门户表单提交扩建或更正后的厂区边界",
                        )
                    )
                for attachment in as_list(payload.get("attachments") or payload.get("files")):
                    if not isinstance(attachment, dict):
                        continue
                    kind = str(attachment.get("kind", "")).lower()
                    revision = attachment.get("revision") or attachment.get("site_revision")
                    if kind in {"boundary_map", "site_boundary_map", "expansion_map"} and (
                        payload.get("change_type") == "boundary_expansion" or not isinstance(revision, int) or revision > 1
                    ):
                        impacts.append(
                            self.analyze_impact(
                                change_type="boundary_changed",
                                site_id=str(attachment.get("site_id") or site_id),
                                trigger_event_id=event["id"],
                                summary=attachment.get("change_summary") or "门户表单补交厂区边界图",
                            )
                        )
            elif source_name == SOURCE_ATTACHMENT:
                record = self._process_attachment(payload, event)
                records = [record] if isinstance(record, dict) else record
                if self._boundary_impact_needed(payload, record):
                    impact = self.analyze_impact(
                        change_type="boundary_changed",
                        site_id=record.get("site_id") or payload.get("site_id"),
                        trigger_event_id=event["id"],
                        summary=payload.get("change_summary") or "收到或更正厂区边界图",
                    )
                    impacts.append(impact)
            elif source_name == SOURCE_FACTOR:
                record = self._process_factor(payload, event)
                records = [record]
                factor_is_new_version = isinstance(record.get("version"), int) and int(record["version"]) > 1
                if record.get("correction_of") or payload.get("correction_reason") or payload.get("corrects_version") or factor_is_new_version:
                    impact = self.analyze_impact(
                        change_type="factor_corrected",
                        factor_id=record.get("factor_id"),
                        trigger_event_id=event["id"],
                        summary=payload.get("correction_reason") or "公开折算因子发生更正",
                    )
                    impacts.append(impact)
            elif source_name == SOURCE_AUTH:
                records = self._process_verification(payload, event)

            event["result_records"] = [
                compact_dict(
                    {
                        "collection": record.get("_collection"),
                        "id": record.get("id"),
                        "evidence_id": record.get("evidence_id"),
                        "version": record.get("version"),
                    }
                )
                for record in records
                if isinstance(record, dict)
            ]
            event["impact_ids"] = [item.get("id") for item in impacts]
            self.save()
            return IngestResult(
                status=event["status"],
                event=event,
                records=records,
                impacts=impacts,
                app=self,
            )
        except (ValidationError, NotFoundError, ConflictError) as exc:
            # 回滚领域变更，但保留一条 rejected 采集事件。
            self.store.data = snapshot
            error_event = self._record_rejected(
                source_name,
                payload,
                [str(exc)],
                event_id=client_event_id,
                source_record_id=record_id,
                received_at=received_at,
                payload_hash=payload_hash,
                save=False,
            )
            self.save()
            return IngestResult(
                status=IngestStatus.REJECTED,
                event=error_event,
                errors=[str(exc)],
                app=self,
            )
        except Exception as exc:
            self.store.data = snapshot
            error_event = self._record_rejected(
                source_name,
                payload,
                [f"internal_error: {type(exc).__name__}: {exc}"],
                event_id=client_event_id,
                source_record_id=record_id,
                received_at=received_at,
                payload_hash=payload_hash,
                save=False,
            )
            self.save()
            raise

    def _source_record_id(self, source: str, payload: dict[str, Any], explicit: str | None) -> str:
        if explicit:
            return str(explicit)
        keys_by_source = {
            SOURCE_PORTAL: ["submission_id", "form_id", "portal_submission_id", "id"],
            SOURCE_ATTACHMENT: ["attachment_id", "evidence_id", "file_id", "id"],
            SOURCE_FACTOR: ["factor_id", "code", "id"],
            SOURCE_AUTH: ["verification_id", "authorization_id", "certificate_no", "certificate_id", "id"],
        }
        for key in keys_by_source.get(source, ["source_record_id", "id"]):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return content_hash(payload)[:24]

    @staticmethod
    def _payload_revision(payload: dict[str, Any]) -> int | str | None:
        value = payload.get("revision", payload.get("version"))
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)

    def _is_out_of_order(self, source: str, record_id: str, revision: int | str | None) -> bool:
        if revision is None or not isinstance(revision, int):
            return False
        events = self.store.find("events", source=source, source_record_id=record_id)
        max_revision = max(
            (int(event["source_revision"]) for event in events if isinstance(event.get("source_revision"), int)),
            default=None,
        )
        return max_revision is not None and revision < max_revision

    def _base_event(
        self,
        source: str,
        payload: dict[str, Any],
        *,
        event_id: str | None,
        source_record_id: str,
        payload_hash: str,
        received_at: Any,
        occurred_at: Any,
        actor: str | None,
    ) -> dict[str, Any]:
        received = parse_datetime(received_at, "received_at") if received_at else self.now()
        occurred = parse_datetime(occurred_at, "occurred_at") if occurred_at else received
        return {
            "id": str(event_id) if event_id else self._new_id("EVT", "event"),
            "type": "ingestion_event",
            "source": source,
            "source_record_id": source_record_id,
            "status": IngestStatus.RECEIVED.value,
            "actor": actor,
            "received_at": received.isoformat(),
            "occurred_at": occurred.isoformat(),
            "content_hash": payload_hash,
            "payload": copy.deepcopy(payload),
            "created_at": utc_now_iso(),
        }

    def _record_rejected(
        self,
        source: str,
        payload: dict[str, Any],
        errors: list[str],
        *,
        event_id: str | None = None,
        source_record_id: str | None = None,
        received_at: Any = None,
        payload_hash: str | None = None,
        save: bool = True,
    ) -> dict[str, Any]:
        record_id = source_record_id or self._source_record_id(source, payload if isinstance(payload, dict) else {}, None)
        event = self._base_event(
            source,
            payload if isinstance(payload, dict) else {"raw": payload},
            event_id=event_id,
            source_record_id=record_id,
            payload_hash=payload_hash or content_hash(payload),
            received_at=received_at,
            occurred_at=None,
            actor=None,
        )
        event["status"] = str(IngestStatus.REJECTED)
        event["errors"] = errors
        self.store.add("events", event)
        if save:
            self.save()
        return event

    def record_source_unavailable(
        self,
        source: str,
        *,
        source_record_id: str | None = None,
        url: str | None = None,
        error: Exception | str | None = None,
        retry_after: str | None = None,
        received_at: Any = None,
    ) -> IngestResult:
        """记录一次外部来源不可用；不覆盖此前成功采集的内容。"""
        source_name = self._normalize_source(source)
        message = str(error) if error else "source unavailable"
        payload = {
            "url": url,
            "error": message,
            "retry_after": retry_after,
            "unavailable": True,
        }
        event = self._base_event(
            source_name,
            payload,
            event_id=None,
            source_record_id=source_record_id or url or self._new_id("SRCFAIL", "source_failure"),
            payload_hash=content_hash(payload),
            received_at=received_at,
            occurred_at=None,
            actor="source-collector",
        )
        event["status"] = str(IngestStatus.SOURCE_UNAVAILABLE)
        event["error"] = message
        event["retry_after"] = retry_after
        self.store.add("events", event)
        self.save()
        return IngestResult(status=IngestStatus.SOURCE_UNAVAILABLE, event=event, errors=[message], app=self)

    # ------------------------------------------------------------------
    # 外部来源采集适配器
    # ------------------------------------------------------------------
    def collect_public_factor(
        self,
        *,
        factor_id: str | None = None,
        url: str | None = None,
        fetcher: Callable[[str], Any] | None = None,
        source_record_id: str | None = None,
    ) -> IngestResult:
        """通过可注入的 ``fetcher`` 采集因子页面。

        ``fetcher`` 接收 URL 或 factor_id，返回 dict/list/文本。测试可注入
        本地函数；生产可使用 :func:`green_review.sources.http_get_text`。
        """
        target = url or factor_id
        if not target:
            raise ValidationError("factor_id 或 url 至少提供一个")
        try:
            if fetcher is None:
                from .sources import http_get_text

                text = http_get_text(str(target))
                payload = parse_factor_content(text, url=str(target), factor_id=factor_id)
            else:
                raw = fetcher(str(target))
                payload = parse_factor_content(raw, url=str(target), factor_id=factor_id)
            if factor_id and isinstance(payload, dict):
                payload.setdefault("factor_id", factor_id)
            if url and isinstance(payload, dict):
                payload.setdefault("source_url", url)
            return self.ingest(SOURCE_FACTOR, payload, source_record_id=source_record_id or factor_id or url)
        except SourceUnavailableError as exc:
            result = self.record_source_unavailable(
                SOURCE_FACTOR,
                source_record_id=source_record_id or factor_id,
                url=url or str(target),
                error=exc,
                retry_after=exc.retry_after,
            )
            return result

    def verify_authorization(
        self,
        certificate_no: str,
        *,
        endpoint: str | None = None,
        client: Callable[[str], Any] | None = None,
        payload: dict[str, Any] | None = None,
        request_payload: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> IngestResult:
        """调用或接收授权验证结果；接口不可用时记录 unavailable。"""
        if payload is not None:
            body = dict(payload)
            body.setdefault("certificate_no", certificate_no)
            return self.ingest(SOURCE_AUTH, body)
        target = endpoint or certificate_no
        try:
            if client is None:
                if str(method).upper() == "POST":
                    from .sources import http_post_json

                    response = http_post_json(
                        str(target),
                        request_payload or {"certificate_no": certificate_no},
                    )
                else:
                    from .sources import http_get_json

                    response = http_get_json(str(target))
            else:
                response = client(str(target))
            if not isinstance(response, dict):
                raise ValidationError("授权验证接口返回必须是 JSON 对象")
            response.setdefault("certificate_no", certificate_no)
            return self.ingest(SOURCE_AUTH, response)
        except SourceUnavailableError as exc:
            return self.record_source_unavailable(
                SOURCE_AUTH,
                source_record_id=certificate_no,
                url=endpoint,
                error=exc,
                retry_after=exc.retry_after,
            )

    # ------------------------------------------------------------------
    # 站点边界和产品范围
    # ------------------------------------------------------------------
    def _process_portal_form(self, payload: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        submission_id = str(payload.get("submission_id") or payload.get("form_id") or event["source_record_id"])
        form_evidence_id = payload.get("form_evidence_id") or f"FORM-{submission_id}"

        site_payload = payload.get("site") if isinstance(payload.get("site"), dict) else {}
        if isinstance(payload.get("site"), str):
            site_id = payload.get("site_id") or payload.get("site")
        elif isinstance(payload.get("site"), dict):
            site_id = payload.get("site_id") or site_payload.get("site_id") or site_payload.get("site")
        else:
            site_id = payload.get("site_id")
        boundary_payload = payload.get("boundary") if isinstance(payload.get("boundary"), dict) else {}
        if boundary_payload and not site_id:
            raise ValidationError("boundary 资料缺少 site_id")
        if site_id and (boundary_payload or payload.get("valid_from") or payload.get("area_m2") or payload.get("revision") or payload.get("site_revision")):
            boundary_data = {
                **site_payload,
                **boundary_payload,
                "site_id": site_id,
                "company": payload.get("company") or payload.get("company_id") or site_payload.get("company"),
                "valid_from": boundary_payload.get("valid_from", payload.get("valid_from")),
                "area_m2": boundary_payload.get("area_m2", payload.get("area_m2")),
                "revision": boundary_payload.get("revision", payload.get("site_revision", payload.get("revision"))),
                "boundary_map_evidence_id": boundary_payload.get("boundary_map_evidence_id", payload.get("boundary_map")),
            }
            records.append(self.upsert_site_boundary(boundary_data, source_event_id=event["id"]))

        products = payload.get("products") or payload.get("product_codes") or payload.get("product_scope")
        if site_id and products:
            scope = self.upsert_product_scope(
                {
                    "scope_id": payload.get("scope_id"),
                    "site_id": site_id,
                    "products": products,
                    "revision": payload.get("scope_revision") or payload.get("product_revision"),
                    "valid_from": payload.get("product_valid_from") or payload.get("valid_from"),
                    "valid_to": payload.get("product_valid_to") or payload.get("valid_to"),
                },
                source_event_id=event["id"],
            )
            records.append(scope)

        form_evidence = self._upsert_evidence_version(
            evidence_id=str(form_evidence_id),
            kind="portal_form",
            site_id=str(site_id) if site_id else None,
            title=f"门户表单 {submission_id}",
            source_event_id=event["id"],
            payload=payload,
            received_at=event["received_at"],
            source_revision=self._payload_revision(payload),
        )
        records.append(form_evidence)

        attachments = payload.get("attachments") or payload.get("files") or []
        for attachment in as_list(attachments):
            if not isinstance(attachment, dict):
                continue
            item = dict(attachment)
            item.setdefault("site_id", site_id)
            processed = self._process_attachment(item, event)
            if isinstance(processed, list):
                records.extend(processed)
            else:
                records.append(processed)

        period = payload.get("period") or payload.get("reporting_period")
        metrics = payload.get("metrics") or payload.get("readings") or payload.get("data") or []
        for metric_payload in as_list(metrics):
            if not isinstance(metric_payload, dict):
                continue
            metric_data = dict(metric_payload)
            metric_data.setdefault("site_id", site_id)
            metric_data.setdefault("period", period)
            metric_data.setdefault("form_evidence_id", form_evidence["evidence_id"])
            metric_data.setdefault("form_evidence_version", form_evidence["version"])
            records.append(self._upsert_metric_version(metric_data, event))

        # 有些表单把单个指标字段平铺在顶层。
        for code in ("electricity", "energy_consumption", "water_consumption"):
            if code in payload and not metrics:
                metric_data = {
                    "site_id": site_id,
                    "period": period,
                    "code": code,
                    "value": payload.get(code),
                    "unit": payload.get(f"{code}_unit"),
                    "evidence_ref": f"{form_evidence['evidence_id']}@{form_evidence['version']}",
                }
                records.append(self._upsert_metric_version(metric_data, event))
        return records

    def receive_portal_form(self, payload: dict[str, Any], **kwargs) -> IngestResult:
        return self.ingest(SOURCE_PORTAL, payload, **kwargs)

    def receive_attachment_metadata(self, payload: dict[str, Any], **kwargs) -> IngestResult:
        return self.ingest(SOURCE_ATTACHMENT, payload, **kwargs)

    def receive_factor(self, payload: dict[str, Any], **kwargs) -> IngestResult:
        return self.ingest(SOURCE_FACTOR, payload, **kwargs)

    def receive_authorization(self, payload: dict[str, Any], **kwargs) -> IngestResult:
        return self.ingest(SOURCE_AUTH, payload, **kwargs)

    # ------------------------------------------------------------------
    # 证据版本
    # ------------------------------------------------------------------
    def _process_attachment(self, payload: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        evidence_id = payload.get("evidence_id") or payload.get("attachment_id") or payload.get("file_id")
        kind = str(payload.get("kind") or payload.get("evidence_kind") or "attachment")
        sha256 = payload.get("sha256") or payload.get("file_sha256")
        file_path = payload.get("file_path")
        if not sha256 and file_path:
            sha256 = file_sha256(file_path)
        evidence = self._upsert_evidence_version(
            evidence_id=str(evidence_id) if evidence_id else None,
            kind=kind,
            site_id=self._optional_str(payload.get("site_id") or payload.get("site")),
            title=payload.get("title") or payload.get("filename") or payload.get("name"),
            filename=payload.get("filename") or payload.get("file_name"),
            uri=payload.get("uri") or payload.get("url") or payload.get("path"),
            media_type=payload.get("media_type") or payload.get("content_type"),
            sha256=sha256,
            value=payload.get("value"),
            unit=payload.get("unit"),
            period_start=payload.get("period_start") or payload.get("period_from"),
            period_end=payload.get("period_end") or payload.get("period_to"),
            valid_from=payload.get("valid_from", payload.get("from")),
            valid_to=payload.get("valid_to", payload.get("to")),
            product_codes=payload.get("product_codes") or payload.get("products"),
            issuer=payload.get("issuer"),
            certificate_no=payload.get("certificate_no") or payload.get("cert_no"),
            source_event_id=event["id"],
            payload=payload,
            received_at=event["received_at"],
            source_revision=self._payload_revision(payload),
            metadata=payload.get("metadata"),
        )

        cert_like = kind.lower() in {"certificate", "cert", "third_party_certificate", "audit", "proof"} or payload.get("certificate_no") or payload.get("certificate")
        if cert_like:
            cert_payload = payload.get("certificate") if isinstance(payload.get("certificate"), dict) else {}
            merged = {**payload, **cert_payload}
            merged.setdefault("evidence_ref", f"{evidence['evidence_id']}@{evidence['version']}")
            self._upsert_certificate(merged, event, evidence)

        boundary_like = kind.lower() in {"boundary_map", "site_boundary_map", "expansion_map", "boundary"} or (
            payload.get("site_id") and (payload.get("area_m2") is not None or payload.get("valid_from") and payload.get("revision") is not None)
        )
        if boundary_like and payload.get("site_id"):
            boundary_data = {
                "site_id": payload["site_id"],
                "company": payload.get("company"),
                "area_m2": payload.get("area_m2"),
                "valid_from": payload.get("boundary_valid_from") or payload.get("valid_from") or payload.get("from"),
                "revision": payload.get("site_revision") or payload.get("boundary_revision") or payload.get("revision"),
                "boundary_map_evidence_id": evidence["evidence_id"],
                "boundary_map_evidence_version": evidence["version"],
                "change_summary": payload.get("change_summary"),
            }
            if boundary_data["valid_from"]:
                self.upsert_site_boundary(boundary_data, source_event_id=event["id"])
        return evidence

    def _upsert_evidence_version(
        self,
        *,
        evidence_id: str | None,
        kind: str,
        source_event_id: str,
        payload: dict[str, Any],
        site_id: str | None = None,
        title: str | None = None,
        filename: str | None = None,
        uri: str | None = None,
        media_type: str | None = None,
        sha256: str | None = None,
        value: Any = UNSET,
        unit: str | None = None,
        period_start: Any = None,
        period_end: Any = None,
        valid_from: Any = None,
        valid_to: Any = None,
        product_codes: Any = None,
        issuer: str | None = None,
        certificate_no: str | None = None,
        received_at: Any = None,
        source_revision: int | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not evidence_id:
            evidence_id = self._new_id("E", "evidence")
        existing_versions = self.store.find("evidence_versions", evidence_id=evidence_id)
        hash_value = str(sha256) if sha256 else content_hash(payload)
        for existing in existing_versions:
            if existing.get("sha256") == hash_value or existing.get("content_hash") == content_hash(payload):
                return existing

        if isinstance(source_revision, int):
            max_source_revision = max(
                (int(item["source_revision"]) for item in existing_versions if isinstance(item.get("source_revision"), int)),
                default=None,
            )
            if max_source_revision is not None and source_revision < max_source_revision:
                raise ConflictError(f"证据 {evidence_id} 收到旧修订 {source_revision}，当前为 {max_source_revision}")

        version = max((int(item.get("version", 0)) for item in existing_versions), default=0) + 1
        for old in existing_versions:
            if old.get("status") == "active":
                old["status"] = "superseded"
                old["superseded_by_version"] = version
                self.store.update("evidence_versions", old)

        declared_value = None if value is UNSET else to_decimal(value, "evidence.value")
        record = {
            "id": f"{evidence_id}@v{version}",
            "_collection": "evidence_versions",
            "type": "evidence_version",
            "evidence_id": evidence_id,
            "version": version,
            "source_revision": source_revision,
            "status": "active",
            "kind": str(kind or "attachment"),
            "site_id": site_id,
            "title": title,
            "filename": filename,
            "uri": uri,
            "media_type": media_type,
            "sha256": hash_value,
            "content_hash": content_hash(payload),
            "declared_value": declared_value,
            "unit": unit,
            "period_start": iso_date(parse_date(period_start, "period_start")),
            "period_end": iso_date(parse_date(period_end, "period_end")),
            "valid_from": iso_date(parse_date(valid_from, "valid_from")),
            "valid_to": iso_date(parse_date(valid_to, "valid_to")),
            "product_codes": unique_strings(as_list(product_codes)),
            "issuer": issuer,
            "certificate_no": certificate_no,
            "metadata": metadata or {},
            "source_event_id": source_event_id,
            "received_at": parse_datetime(received_at).isoformat() if received_at else utc_now_iso(),
            "created_at": utc_now_iso(),
            "supersedes_version": version - 1 if version > 1 else None,
        }
        self.store.add("evidence_versions", record)
        return record

    @staticmethod
    def parse_evidence_ref(ref: Any, default_version: int | None = None) -> tuple[str, int | None]:
        if isinstance(ref, dict):
            evidence_id = ref.get("evidence_id") or ref.get("id") or ref.get("ref")
            version = ref.get("version") or default_version
            if not evidence_id:
                raise ValidationError(f"无效证据引用: {ref!r}")
            return str(evidence_id), int(version) if version not in (None, "") else None
        text = str(ref).strip()
        match_version = default_version
        if "@" in text:
            text, suffix = text.rsplit("@", 1)
            if suffix.startswith("v"):
                suffix = suffix[1:]
            match_version = int(suffix)
        elif ":v" in text:
            text, suffix = text.rsplit(":v", 1)
            match_version = int(suffix)
        return text, match_version

    def get_evidence(self, ref: Any, version: int | None = None) -> dict[str, Any]:
        evidence_id, parsed_version = self.parse_evidence_ref(ref, version)
        target_version = parsed_version
        rows = self.store.find("evidence_versions", evidence_id=evidence_id)
        if not rows:
            raise NotFoundError(f"证据 {evidence_id} 不存在")
        if target_version is not None:
            for row in rows:
                if int(row.get("version", 0)) == int(target_version):
                    return row
            raise NotFoundError(f"证据 {evidence_id} 不存在版本 v{target_version}")
        active = [row for row in rows if row.get("status") == "active"]
        if active:
            return max(active, key=lambda row: int(row["version"]))
        return max(rows, key=lambda row: int(row["version"]))

    def evidence_versions(self, evidence_id: str) -> list[dict[str, Any]]:
        return sorted(
            self.store.find("evidence_versions", evidence_id=str(evidence_id)),
            key=lambda row: int(row.get("version", 0)),
        )

    def snapshot_evidence_refs(self, refs: list[Any] | None) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ref in refs or []:
            evidence = self.get_evidence(ref)
            if evidence["evidence_id"] in seen:
                continue
            seen.add(evidence["evidence_id"])
            snapshot.append(
                {
                    "ref": f"{evidence['evidence_id']}@v{evidence['version']}",
                    "evidence_id": evidence["evidence_id"],
                    "version": int(evidence["version"]),
                    "sha256": evidence.get("sha256"),
                    "content_hash": evidence.get("content_hash"),
                    "kind": evidence.get("kind"),
                    "title": evidence.get("title"),
                    "filename": evidence.get("filename"),
                    "uri": evidence.get("uri"),
                    "site_id": evidence.get("site_id"),
                    "valid_from": evidence.get("valid_from"),
                    "valid_to": evidence.get("valid_to"),
                    "product_codes": list(evidence.get("product_codes") or []),
                    "source_event_id": evidence.get("source_event_id"),
                }
            )
        return snapshot

    @staticmethod
    def diff_evidence_snapshots(
        old: list[dict[str, Any]],
        new: list[dict[str, Any]],
    ) -> dict[str, Any]:
        old_by_id = {item["evidence_id"]: item for item in old or []}
        new_by_id = {item["evidence_id"]: item for item in new or []}
        added = [new_by_id[key] for key in new_by_id.keys() - old_by_id.keys()]
        removed = [old_by_id[key] for key in old_by_id.keys() - new_by_id.keys()]
        version_changed = []
        unchanged = []
        for key in old_by_id.keys() & new_by_id.keys():
            old_item, new_item = old_by_id[key], new_by_id[key]
            if int(old_item.get("version", 0)) != int(new_item.get("version", 0)) or old_item.get("sha256") != new_item.get("sha256"):
                version_changed.append(
                    {
                        "evidence_id": key,
                        "from_version": old_item.get("version"),
                        "to_version": new_item.get("version"),
                        "from_sha256": old_item.get("sha256"),
                        "to_sha256": new_item.get("sha256"),
                        "old": old_item,
                        "new": new_item,
                    }
                )
            else:
                unchanged.append(key)
        return {
            "added": sorted([item["evidence_id"] for item in added]),
            "removed": sorted([item["evidence_id"] for item in removed]),
            "version_changed": sorted(version_changed, key=lambda item: item["evidence_id"]),
            "unchanged": sorted(unchanged),
            "old_count": len(old or []),
            "new_count": len(new or []),
        }

    # ------------------------------------------------------------------
    # 边界和产品范围写入
    # ------------------------------------------------------------------
    def upsert_site_boundary(self, data: dict[str, Any], *, source_event_id: str | None = None) -> dict[str, Any]:
        site_id = self._optional_str(data.get("site_id") or data.get("site"))
        if not site_id:
            raise ValidationError("site boundary 缺少 site_id")
        valid_from = parse_date(data.get("valid_from"), "site.valid_from")
        if valid_from is None:
            raise ValidationError(f"站点 {site_id} 边界版本缺少 valid_from")
        supplied_revision = data.get("revision", data.get("site_revision"))
        existing_rows = self.store.find("sites", site_id=site_id)
        if supplied_revision in (None, ""):
            revision = max((int(row.get("revision", 0)) for row in existing_rows), default=0) + 1
        else:
            revision = int(supplied_revision)
        existing = self.store.find_one("sites", site_id=site_id, revision=revision)
        core = {
            "site_id": site_id,
            "company": data.get("company") or data.get("company_id"),
            "area_m2": str(to_decimal(data.get("area_m2"), "area_m2")) if data.get("area_m2") not in (None, "") else None,
            "valid_from": valid_from.isoformat(),
            "boundary_map_evidence_id": data.get("boundary_map_evidence_id"),
            "boundary_map_evidence_version": data.get("boundary_map_evidence_version"),
            "description": data.get("description"),
            "boundary": data.get("boundary"),
        }
        core_hash = content_hash(core)
        if existing:
            if existing.get("content_hash") == core_hash:
                return existing
            raise ConflictError(f"站点 {site_id} 边界修订 R{revision} 已存在且内容不同；更正必须使用新 revision")

        record = {
            "id": data.get("boundary_id") or f"BND-{site_id}-R{revision}",
            "_collection": "sites",
            "type": "site_boundary",
            **core,
            "revision": revision,
            "content_hash": core_hash,
            "confirmation_status": str(ConfirmationStatus.PENDING),
            "confirmed_by": None,
            "confirmed_at": None,
            "source_event_id": source_event_id,
            "change_summary": data.get("change_summary"),
            "created_at": utc_now_iso(),
        }
        if data.get("imported"):
            record["confirmation_status"] = str(ConfirmationStatus.IMPORTED)
        if data.get("confirmed_by"):
            record["confirmation_status"] = str(ConfirmationStatus.CONFIRMED)
            record["confirmed_by"] = data["confirmed_by"]
            record["confirmed_at"] = parse_datetime(data.get("confirmed_at")).isoformat() if data.get("confirmed_at") else utc_now_iso()
        self.store.add("sites", record)
        return record

    def upsert_product_scope(self, data: dict[str, Any], *, source_event_id: str | None = None) -> dict[str, Any]:
        site_id = self._optional_str(data.get("site_id") or data.get("site"))
        if not site_id:
            raise ValidationError("product scope 缺少 site_id")
        codes = unique_strings(as_list(data.get("products") or data.get("product_codes")))
        supplied_revision = data.get("revision", data.get("scope_revision"))
        rows = self.store.find("product_scopes", site_id=site_id)
        revision = int(supplied_revision) if supplied_revision not in (None, "") else max((int(r.get("revision", 0)) for r in rows), default=0) + 1
        scope_id = data.get("scope_id") or f"PS-{site_id}-R{revision}"
        existing = self.store.find_one("product_scopes", id=scope_id) or self.store.find_one("product_scopes", site_id=site_id, revision=revision)
        record = {
            "id": scope_id if not existing else existing["id"],
            "_collection": "product_scopes",
            "type": "product_scope",
            "scope_id": scope_id if not existing else existing["scope_id"],
            "site_id": site_id,
            "revision": revision,
            "product_codes": codes,
            "products": data.get("products") if isinstance(data.get("products"), list) else codes,
            "valid_from": iso_date(parse_date(data.get("valid_from"), "scope.valid_from")),
            "valid_to": iso_date(parse_date(data.get("valid_to"), "scope.valid_to")),
            "confirmation_status": str(ConfirmationStatus.PENDING),
            "confirmed_by": None,
            "confirmed_at": None,
            "source_event_id": source_event_id,
            "created_at": utc_now_iso(),
        }
        if existing:
            if existing.get("product_codes") == codes and existing.get("valid_from") == record["valid_from"] and existing.get("valid_to") == record["valid_to"]:
                return existing
            raise ConflictError(f"产品范围 {scope_id} 已存在且内容不同；请使用新 revision")
        self.store.add("product_scopes", record)
        return record

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value is None or value == "":
            return None
        return str(value)

    def site_revisions(self, site_id: str) -> list[dict[str, Any]]:
        return sorted(self.store.find("sites", site_id=str(site_id)), key=lambda r: int(r.get("revision", 0)))

    def current_site_revision(self, site_id: str, as_of: Any = None) -> dict[str, Any] | None:
        day = parse_date(as_of, "as_of") if as_of else self.today()
        rows = [row for row in self.site_revisions(site_id) if parse_date(row.get("valid_from")) and parse_date(row["valid_from"]) <= day]
        return rows[-1] if rows else (self.site_revisions(site_id)[-1] if self.site_revisions(site_id) else None)

    # ------------------------------------------------------------------
    # 指标版本
    # ------------------------------------------------------------------
    def _upsert_metric_version(self, data: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        site_id = self._optional_str(data.get("site_id") or data.get("site"))
        code = normalize_metric_code(data.get("code") or data.get("metric") or data.get("metric_code"))
        start, end, period_label = period_range(data.get("period") or data.get("period_label"))
        start = parse_date(data.get("period_start") or data.get("from"), "period_start") or start
        end = parse_date(data.get("period_end") or data.get("to"), "period_end") or end
        if not period_label:
            period_label = data.get("period_label") or data.get("reporting_period") or (
                f"{start.isoformat()}..{end.isoformat()}" if start and end else "unspecified-period"
            )
        metric_id = data.get("metric_id") or data.get("id") or self._metric_natural_id(site_id, code, start, end, period_label)
        evidence_ref = data.get("evidence_ref") or data.get("evidence") or data.get("evidence_id")
        if not evidence_ref and data.get("form_evidence_id"):
            evidence_ref = f"{data['form_evidence_id']}@{data.get('form_evidence_version')}"

        evidence = None
        evidence_error = None
        if evidence_ref:
            try:
                evidence = self.get_evidence(evidence_ref)
            except NotFoundError as exc:
                evidence_error = str(exc)

        raw_value_present = "value" in data and data.get("value") not in (None, "")
        raw_value = data.get("value")
        spec = METRIC_SPECS.get(code, {})
        numeric_spec = bool(spec.get("numeric", True))
        numeric_value = to_decimal(raw_value, f"metric {code}.value") if raw_value_present and numeric_spec else None
        if evidence is None or (numeric_spec and not raw_value_present):
            status = "unassessed"
        else:
            status = "supported"

        existing = self.store.find("metric_versions", metric_id=metric_id)
        payload_hash = content_hash(
            {
                "code": code,
                "value": str(numeric_value) if numeric_value is not None else None,
                "unit": data.get("unit"),
                "evidence_ref": str(evidence_ref),
                "period": period_label,
                "start": iso_date(start),
                "end": iso_date(end),
            }
        )
        for old in existing:
            if old.get("content_hash") == payload_hash:
                return old
        version = max((int(row.get("version", 0)) for row in existing), default=0) + 1
        for old in existing:
            old["is_current"] = False
            self.store.update("metric_versions", old)

        unit = data.get("unit") or spec.get("unit")
        record = {
            "id": f"{metric_id}@v{version}",
            "_collection": "metric_versions",
            "type": "metric_version",
            "metric_id": metric_id,
            "version": version,
            "is_current": True,
            "site_id": site_id,
            "code": code,
            "category": spec.get("category", data.get("category") or "other"),
            "label": spec.get("label", code),
            "period_label": period_label,
            "period_start": iso_date(start),
            "period_end": iso_date(end),
            "value": numeric_value,
            "raw_value": raw_value if not numeric_spec else None,
            "observation": data.get("observation") or data.get("destination") or data.get("destinations") or data.get("records"),
            "value_present": raw_value_present,
            "unit": unit,
            "status": status,
            "reason": evidence_error or ("value_missing" if status == "unassessed" and evidence is not None else None),
            "evidence_ref": f"{evidence['evidence_id']}@{evidence['version']}" if evidence else str(evidence_ref) if evidence_ref else None,
            "evidence_id": evidence["evidence_id"] if evidence else None,
            "evidence_version": int(evidence["version"]) if evidence else None,
            "boundary_revision": data.get("boundary_revision") or data.get("site_revision"),
            "product_codes": unique_strings(as_list(data.get("product_codes") or data.get("products"))),
            "caliber": data.get("caliber") or data.get("caliber_note"),
            "review_status": "pending",
            "accepted": False,
            "reviewer": None,
            "reviewed_at": None,
            "caliber_note": data.get("caliber_note"),
            "factor_id": data.get("factor_id") or spec.get("factor_id"),
            "source_event_id": event.get("id"),
            "content_hash": payload_hash,
            "created_at": utc_now_iso(),
        }
        self.store.add("metric_versions", record)
        if status == "unassessed":
            self.open_issue(
                issue_key=f"missing_metric:{metric_id}:v{version}",
                code="missing_metric_evidence",
                site_id=site_id,
                period=period_label,
                metric_id=metric_id,
                metric_code=code,
                category=record["category"],
                severity=IssueSeverity.BLOCKER if record["category"] in REQUIRED_CATEGORIES else IssueSeverity.CONDITION,
                title=f"指标 {record['label']} 缺少可核定证据或数值",
                detail=evidence_error or "未提供数值；该指标保持未核定，不能按 0 填补",
            )
        return record

    @staticmethod
    def _metric_natural_id(site_id: str | None, code: str, start: date | None, end: date | None, label: str) -> str:
        return "M:{}:{}:{}:{}".format(
            site_id or "unknown",
            code,
            iso_date(start) or "na",
            iso_date(end) or label.replace(" ", "_"),
        )

    def current_metrics(self, site_id: str | None = None, period: str | dict | None = None) -> list[dict[str, Any]]:
        rows = [row for row in self.store.all("metric_versions") if row.get("is_current")]
        if site_id is not None:
            rows = [row for row in rows if row.get("site_id") == site_id]
        start, end, _ = period_range(period)
        if start or end:
            rows = [
                row
                for row in rows
                if intervals_overlap(
                    parse_date(row.get("period_start")),
                    parse_date(row.get("period_end")),
                    start,
                    end,
                )
            ]
        return rows

    def accept_metric(
        self,
        metric_id: str,
        reviewer: str,
        *,
        accepted: bool = True,
        caliber_note: str | None = None,
        as_of: Any = None,
    ) -> dict[str, Any]:
        rows = self.store.find("metric_versions", metric_id=metric_id, is_current=True)
        if not rows:
            # 允许直接传记录 id。
            row = self.store.find_one("metric_versions", id=metric_id)
            if row is None:
                raise NotFoundError(f"指标 {metric_id} 不存在")
        else:
            row = max(rows, key=lambda r: int(r.get("version", 0)))
        row["accepted"] = bool(accepted)
        row["review_status"] = "accepted" if accepted else "rejected"
        row["reviewer"] = reviewer
        row["reviewed_at"] = (parse_datetime(as_of).isoformat() if as_of else utc_now_iso())
        if caliber_note is not None:
            row["caliber_note"] = caliber_note
            row["caliber"] = caliber_note
        self.store.update("metric_versions", row)
        self.save()
        return row

    # ------------------------------------------------------------------
    # 折算因子
    # ------------------------------------------------------------------
    def _process_factor(self, payload: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        factor_id = str(payload.get("factor_id") or payload.get("code") or payload.get("id") or event["source_record_id"])
        value = to_decimal(payload.get("value") or payload.get("factor_value"), "factor.value")
        if value is None:
            raise ValidationError(f"因子 {factor_id} 缺少 value")
        rows = self.store.find("factors", factor_id=factor_id)
        version = payload.get("version")
        if version in (None, ""):
            version = max((int(r.get("version", 0)) for r in rows if str(r.get("version")).isdigit()), default=0) + 1
        else:
            try:
                version = int(version)
            except (TypeError, ValueError):
                version = str(version)
        core_hash = content_hash(
            {
                "factor_id": factor_id,
                "version": version,
                "value": str(value),
                "unit": payload.get("unit"),
                "effective_from": payload.get("effective_from") or payload.get("from"),
                "effective_to": payload.get("effective_to") or payload.get("to"),
                "applies_to": payload.get("applies_to") or payload.get("metric_codes"),
            }
        )
        existing = self.store.find_one("factors", factor_id=factor_id, version=version)
        if existing:
            if existing.get("content_hash") == core_hash:
                return existing
            raise ConflictError(f"因子 {factor_id} 版本 {version} 已存在且内容不同；更正必须使用新版本")
        record = {
            "id": f"FACTOR-{factor_id}-v{version}",
            "_collection": "factors",
            "type": "conversion_factor",
            "factor_id": factor_id,
            "version": version,
            "value": value,
            "unit": payload.get("unit"),
            "effective_from": iso_date(parse_date(payload.get("effective_from") or payload.get("from"), "factor.from")),
            "effective_to": iso_date(parse_date(payload.get("effective_to") or payload.get("to"), "factor.to")),
            "source_url": payload.get("source_url") or payload.get("url"),
            "source_event_id": event["id"],
            "content_hash": core_hash,
            "correction_of": payload.get("corrects_version") or payload.get("correction_of"),
            "correction_reason": payload.get("correction_reason"),
            "applies_to": unique_strings(as_list(payload.get("applies_to") or payload.get("metric_codes"))),
            "retrieved_at": event.get("received_at"),
            "created_at": utc_now_iso(),
            "is_current": True,
        }
        for old in rows:
            # 仅当新版本是数字递增时把旧数字版本标记非当前；字符串日期版本也保留。
            if isinstance(version, int) and isinstance(old.get("version"), int) and old["version"] < version:
                old["is_current"] = False
                self.store.update("factors", old)
        self.store.add("factors", record)

        # 同时保留一份证据视图，便于决定统一引用。
        self._upsert_evidence_version(
            evidence_id=payload.get("evidence_id") or f"FACTOR-{factor_id}",
            kind="public_factor",
            title=payload.get("title") or f"公开折算因子 {factor_id} v{version}",
            uri=record["source_url"],
            value=value,
            unit=payload.get("unit"),
            valid_from=record["effective_from"],
            valid_to=record["effective_to"],
            source_event_id=event["id"],
            payload=payload,
            received_at=event["received_at"],
        )
        return record

    def get_factor(self, factor_id: str, *, on_date: Any = None, version: Any = None) -> dict[str, Any] | None:
        rows = self.store.find("factors", factor_id=factor_id)
        if version not in (None, ""):
            return self.store.find_one("factors", factor_id=factor_id, version=int(version) if str(version).isdigit() else version)
        if on_date is not None:
            day = parse_date(on_date, "on_date")
            effective = [
                row
                for row in rows
                if (not row.get("effective_from") or parse_date(row["effective_from"]) <= day)
                and (not row.get("effective_to") or parse_date(row["effective_to"]) >= day)
            ]
            if effective:
                return max(effective, key=lambda r: (r.get("effective_from") or "", r.get("version") if isinstance(r.get("version"), int) else 0))
        active = [row for row in rows if row.get("is_current")]
        if active:
            return max(active, key=lambda r: r.get("version") if isinstance(r.get("version"), int) else 0)
        return max(rows, key=lambda r: r.get("version") if isinstance(r.get("version"), int) else 0, default=None)

    # ------------------------------------------------------------------
    # 第三方证明和授权验证
    # ------------------------------------------------------------------
    def _upsert_certificate(
        self,
        payload: dict[str, Any],
        event: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        certificate_no = payload.get("certificate_no") or payload.get("cert_no") or payload.get("number")
        certificate_id = str(payload.get("certificate_id") or (f"CERT-{certificate_no}" if certificate_no else self._new_id("CERT", "certificate")))
        current_by_number = self.store.find_one("certificates", certificate_no=str(certificate_no), is_current=True) if certificate_no else None
        if current_by_number and not payload.get("certificate_id"):
            certificate_id = current_by_number["certificate_id"]
        rows = self.store.find("certificates", certificate_id=certificate_id)
        current_row = max(rows, key=lambda row: int(row.get("version", 0)), default=None)
        version = max((int(row.get("version", 0)) for row in rows), default=0) + 1
        valid_from = payload.get("valid_from", payload.get("from"))
        valid_to = payload.get("valid_to", payload.get("to"))
        product_codes = unique_strings(as_list(payload.get("product_codes") or payload.get("products") or payload.get("applicable_products")))
        if current_row:
            certificate_no = certificate_no or current_row.get("certificate_no")
            valid_from = valid_from or current_row.get("valid_from")
            valid_to = valid_to or current_row.get("valid_to")
            product_codes = product_codes or list(current_row.get("product_codes") or [])
        core = {
            "certificate_no": str(certificate_no) if certificate_no else None,
            "issuer": payload.get("issuer") or payload.get("issuing_body") or (current_row or {}).get("issuer"),
            "site_id": self._optional_str(payload.get("site_id") or payload.get("site")) or (current_row or {}).get("site_id"),
            "valid_from": iso_date(parse_date(valid_from, "cert.from")),
            "valid_to": iso_date(parse_date(valid_to, "cert.to")),
            "product_codes": product_codes,
            "standard": payload.get("standard") or payload.get("scheme") or (current_row or {}).get("standard"),
        }
        core_hash = content_hash(core)
        for row in rows:
            if row.get("content_hash") == core_hash:
                return row
        for row in rows:
            row["is_current"] = False
            self.store.update("certificates", row)

        verification_status = payload.get("verification_status") or payload.get("status")
        if isinstance(payload.get("authorized"), bool):
            verification_status = VerificationStatus.VERIFIED if payload["authorized"] else VerificationStatus.INVALID
        record = {
            "id": f"{certificate_id}@v{version}",
            "_collection": "certificates",
            "type": "certificate",
            "certificate_id": certificate_id,
            "version": version,
            "is_current": True,
            **core,
            "content_hash": core_hash,
            "evidence_ref": payload.get("evidence_ref") or (f"{evidence['evidence_id']}@{evidence['version']}" if evidence else None),
            "evidence_id": evidence["evidence_id"] if evidence else payload.get("evidence_id"),
            "evidence_version": int(evidence["version"]) if evidence else payload.get("evidence_version"),
            "verification_status": str(verification_status or VerificationStatus.NOT_VERIFIED),
            "latest_check_status": str(verification_status or VerificationStatus.NOT_VERIFIED),
            "last_checked_at": event.get("received_at") if event else utc_now_iso(),
            "mapping_status": str(ConfirmationStatus.PENDING),
            "mapped_by": None,
            "mapped_at": None,
            "replaces_certificate_id": payload.get("replaces_certificate_id") or payload.get("renews"),
            "timeline_note": payload.get("timeline_note"),
            "source_event_id": event.get("id") if event else payload.get("source_event_id"),
            "created_at": utc_now_iso(),
        }
        self.store.add("certificates", record)
        return record

    def _process_verification(self, payload: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
        status = payload.get("status")
        if status in (None, "") and "authorized" in payload:
            status = VerificationStatus.VERIFIED if payload.get("authorized") else VerificationStatus.INVALID
        status = str(status or VerificationStatus.NOT_VERIFIED).lower()
        cert_no = payload.get("certificate_no") or payload.get("cert_no")
        cert_id = payload.get("certificate_id") or (f"CERT-{cert_no}" if cert_no else None)
        checked_at = payload.get("checked_at") or event.get("received_at")
        verification = {
            "id": payload.get("verification_id") or self._new_id("VER", "verification"),
            "_collection": "verifications",
            "type": "authorization_verification",
            "certificate_id": cert_id,
            "certificate_no": cert_no,
            "authorization_id": payload.get("authorization_id"),
            "status": status,
            "authorized": payload.get("authorized"),
            "checked_at": parse_datetime(checked_at).isoformat(),
            "response_reference": payload.get("response_reference") or payload.get("reference"),
            "response_hash": payload.get("response_hash") or content_hash(payload),
            "source_event_id": event["id"],
            "created_at": utc_now_iso(),
        }
        self.store.add("verifications", verification)

        cert = None
        if cert_id:
            cert = self.store.find_one("certificates", certificate_id=cert_id, is_current=True)
            if cert is None:
                cert = self.store.find_one("certificates", certificate_id=cert_id)
        if cert is None and (cert_no or payload.get("issuer") or payload.get("valid_from")):
            cert_payload = dict(payload)
            if cert_id:
                cert_payload["certificate_id"] = cert_id
            cert = self._upsert_certificate(cert_payload, event)
        if cert is not None:
            definitive_failure = status in {str(VerificationStatus.INVALID), "revoked", "suspended"}
            if status == str(VerificationStatus.UNAVAILABLE):
                cert["latest_check_status"] = str(VerificationStatus.UNAVAILABLE)
                # 保留上一次确定性验证状态，不因暂时网络故障改成无效。
            else:
                cert["verification_status"] = status
                cert["latest_check_status"] = status
            cert["last_checked_at"] = verification["checked_at"]
            cert.setdefault("verification_ids", []).append(verification["id"])
            if definitive_failure:
                self.open_issue(
                    issue_key=f"certificate_verification_failed:{cert['certificate_id']}:v{cert['version']}",
                    code="certificate_verification_failed",
                    site_id=cert.get("site_id"),
                    certificate_id=cert["certificate_id"],
                    severity=IssueSeverity.BLOCKER,
                    title=f"证明 {cert.get('certificate_no') or cert['certificate_id']} 授权验证未通过",
                    detail=payload.get("reason") or f"验证状态 {status}",
                )
        return [verification] + ([cert] if cert else [])

    def current_certificates(self, as_of: Any = None, site_id: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        site_id = site_id or kwargs.get("site")
        day = parse_date(as_of, "as_of") if as_of else self.today()
        result = []
        for cert in self.store.all("certificates"):
            if not cert.get("is_current", True):
                continue
            if site_id and cert.get("site_id") != site_id:
                continue
            start = parse_date(cert.get("valid_from"))
            end = parse_date(cert.get("valid_to"))
            date_valid = (start is None or start <= day) and (end is None or end >= day)
            verification_ok = cert.get("verification_status") == str(VerificationStatus.VERIFIED)
            mapping_ok = cert.get("mapping_status") == str(ConfirmationStatus.CONFIRMED)
            item = copy.deepcopy(cert)
            item["date_valid"] = date_valid
            item["verification_ok"] = verification_ok
            item["mapping_confirmed"] = mapping_ok
            item["usable_for_positive_decision"] = date_valid and verification_ok and mapping_ok
            if cert.get("latest_check_status") == str(VerificationStatus.UNAVAILABLE):
                item["source_warning"] = "授权验证接口最近一次不可用；显示的是上次确定性状态"
            result.append(item)
        return sorted(result, key=lambda c: (c.get("site_id") or "", c.get("certificate_id") or ""))

    # ------------------------------------------------------------------
    # 联络员确认
    # ------------------------------------------------------------------
    def confirm_boundary(
        self,
        site_id: str,
        revision: int,
        confirmed_by: str,
        *,
        confirmed: bool = True,
        note: str | None = None,
        as_of: Any = None,
    ) -> dict[str, Any]:
        row = self.store.find_one("sites", site_id=str(site_id), revision=int(revision))
        if row is None:
            raise NotFoundError(f"站点 {site_id} R{revision} 不存在")
        return self._confirm_subject(
            "boundary",
            row["id"],
            confirmed_by,
            confirmed=confirmed,
            note=note,
            as_of=as_of,
            target=row,
            issue_key=f"boundary_unconfirmed:{site_id}:R{revision}",
        )

    def confirm_product_scope(self, scope_id: str, confirmed_by: str, **kwargs) -> dict[str, Any]:
        row = self.store.find_one("product_scopes", id=str(scope_id)) or self.store.find_one("product_scopes", scope_id=str(scope_id))
        if row is None:
            raise NotFoundError(f"产品范围 {scope_id} 不存在")
        return self._confirm_subject(
            "product_scope",
            row["id"],
            confirmed_by,
            target=row,
            issue_key=f"product_scope_unconfirmed:{row['site_id']}:{row['id']}",
            **kwargs,
        )

    def confirm_certificate_mapping(
        self,
        certificate_id: str,
        confirmed_by: str,
        *,
        product_codes: list[str] | None = None,
        confirmed: bool = True,
        note: str | None = None,
        as_of: Any = None,
    ) -> dict[str, Any]:
        cert = self.store.find_one("certificates", certificate_id=str(certificate_id), is_current=True)
        if cert is None:
            cert = self.store.find_one("certificates", certificate_id=str(certificate_id))
        if cert is None:
            raise NotFoundError(f"证明 {certificate_id} 不存在")
        if product_codes is not None:
            cert["product_codes"] = unique_strings(product_codes)
        result = self._confirm_subject(
            "certificate_mapping",
            cert["id"],
            confirmed_by,
            confirmed=confirmed,
            note=note,
            as_of=as_of,
            target=cert,
            issue_key=f"certificate_mapping_unconfirmed:{cert['certificate_id']}:v{cert['version']}",
        )
        cert["mapping_status"] = str(ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.REJECTED)
        cert["mapped_by"] = confirmed_by
        cert["mapped_at"] = result["confirmed_at"]
        self.store.update("certificates", cert)
        return cert

    def _confirm_subject(
        self,
        subject_type: str,
        subject_id: str,
        confirmed_by: str,
        *,
        confirmed: bool = True,
        note: str | None = None,
        as_of: Any = None,
        target: dict[str, Any] | None = None,
        issue_key: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": self._new_id("CNF", "confirmation"),
            "_collection": "confirmations",
            "type": "liaison_confirmation",
            "subject_type": subject_type,
            "subject_id": subject_id,
            "confirmed": bool(confirmed),
            "status": str(ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.REJECTED),
            "confirmed_by": confirmed_by,
            "confirmed_at": parse_datetime(as_of).isoformat() if as_of else utc_now_iso(),
            "note": note,
        }
        self.store.add("confirmations", record)
        if target is not None:
            target["confirmation_status"] = record["status"]
            target["confirmed_by"] = confirmed_by
            target["confirmed_at"] = record["confirmed_at"]
            self.store.update(target.get("_collection", "sites" if subject_type == "boundary" else "product_scopes"), target)
        if confirmed and issue_key:
            for issue in self.store.find("issues", issue_key=issue_key, status=str(IssueStatus.OPEN)):
                issue["status"] = str(IssueStatus.RESOLVED)
                issue["resolved_at"] = record["confirmed_at"]
                issue["resolution_note"] = note or f"{confirmed_by} 已确认"
                self.store.update("issues", issue)
        self.save()
        return record

    # ------------------------------------------------------------------
    # 未结事项
    # ------------------------------------------------------------------
    def open_issue(self, *, issue_key: str, code: str, title: str, severity: str = IssueSeverity.CONDITION, **fields) -> dict[str, Any]:
        existing_open = self.store.find_one("issues", issue_key=issue_key, status=str(IssueStatus.OPEN))
        if existing_open:
            return existing_open
        latest = None
        rows = self.store.find("issues", issue_key=issue_key)
        if rows:
            latest = max(rows, key=lambda row: int(row.get("occurrence", 1)))
        issue = {
            "id": self._new_id("ISS", "issue"),
            "_collection": "issues",
            "type": "review_issue",
            "issue_key": issue_key,
            "occurrence": int(latest.get("occurrence", 0)) + 1 if latest else 1,
            "code": code,
            "title": title,
            "severity": str(severity),
            "status": str(IssueStatus.OPEN),
            "site_id": fields.get("site_id"),
            "period": fields.get("period"),
            "metric_id": fields.get("metric_id"),
            "metric_code": fields.get("metric_code"),
            "category": fields.get("category"),
            "evidence_id": fields.get("evidence_id"),
            "certificate_id": fields.get("certificate_id"),
            "factor_id": fields.get("factor_id"),
            "case_id": fields.get("case_id"),
            "detail": fields.get("detail"),
            "auto_generated": fields.get("auto_generated", True),
            "opened_at": utc_now_iso(),
            "resolved_at": None,
            "resolution_note": None,
            "history": [],
        }
        self.store.add("issues", issue)
        return issue

    def resolve_issue(self, issue_id: str, reviewer: str, note: str | None = None) -> dict[str, Any]:
        issue = self.store.find_one("issues", id=issue_id) or self.store.find_one("issues", issue_key=issue_id)
        if issue is None:
            raise NotFoundError(f"未结事项 {issue_id} 不存在")
        issue["status"] = str(IssueStatus.RESOLVED)
        issue["resolved_at"] = utc_now_iso()
        issue["resolved_by"] = reviewer
        issue["resolution_note"] = note
        self.store.update("issues", issue)
        self.save()
        return issue

    def pending_materials(self, site_id: str | None = None, as_of: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        # 兼容 pending_materials("2026-11-25") 这种把日期作为第一参数的调用。
        if site_id is not None and as_of is None:
            try:
                parse_date(site_id, "as_of")
                as_of = site_id
                site_id = kwargs.get("site_id")
            except ValidationError:
                pass
        if kwargs.get("site_id") and site_id is None:
            site_id = kwargs["site_id"]
        issues = self.store.find("issues", status=str(IssueStatus.OPEN))
        if site_id:
            issues = [issue for issue in issues if issue.get("site_id") in {None, site_id} or issue.get("site_id") == site_id]
        # 全局因子问题不应因 site 过滤消失？上面保留 site_id None。
        return sorted(issues, key=lambda i: (str(i.get("site_id") or ""), str(i.get("severity")), i.get("id") or ""))

    # ------------------------------------------------------------------
    # 评审完整性评估
    # ------------------------------------------------------------------
    def assess(
        self,
        site_id: str,
        period: str | dict | None = None,
        as_of: Any = None,
        create_issues: bool = True,
        require_certificate: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        as_of = as_of if as_of is not None else kwargs.get("on_date") or kwargs.get("date")
        site_id = str(site_id)
        day = parse_date(as_of, "as_of") if as_of else self.today()
        period_start, period_end, period_label = period_range(period)
        if isinstance(period, dict):
            period_label = period_label or period.get("label") or ""
        active_issue_keys: set[str] = set()

        def raise_issue(key: str, code: str, title: str, severity: str, **fields) -> None:
            active_issue_keys.add(key)
            if create_issues:
                self.open_issue(
                    issue_key=key,
                    code=code,
                    title=title,
                    severity=severity,
                    site_id=site_id,
                    period=period_label,
                    **fields,
                )

        # 边界版本及其生效区间。
        revisions = self.site_revisions(site_id)
        boundary_segments: list[dict[str, Any]] = []
        for index, revision in enumerate(revisions):
            start = parse_date(revision.get("valid_from"))
            next_start = parse_date(revisions[index + 1].get("valid_from")) if index + 1 < len(revisions) else None
            end = next_start - timedelta(days=1) if next_start else None
            revision["_effective_end"] = iso_date(end)
            if intervals_overlap(start, end, period_start, period_end):
                boundary_segments.append(revision)

        if not revisions:
            raise_issue(
                f"boundary_missing:{site_id}",
                "boundary_missing",
                "缺少厂区边界和统计口径",
                IssueSeverity.BLOCKER,
            )
        elif period_start or period_end:
            if not boundary_segments:
                raise_issue(
                    f"boundary_outside_period:{site_id}:{period_label}",
                    "boundary_outside_period",
                    "评审期间没有对应的厂区边界版本",
                    IssueSeverity.BLOCKER,
                )
            elif len(boundary_segments) > 1:
                raise_issue(
                    f"boundary_split:{site_id}:{period_label}",
                    "boundary_split_period",
                    "评审期间跨越多个厂区边界版本，必须分段或明确口径",
                    IssueSeverity.BLOCKER,
                    detail="涉及版本 " + ",".join(str(r.get("revision")) for r in boundary_segments),
                )

        for revision in boundary_segments or revisions[-1:]:
            rev_no = int(revision["revision"])
            if revision.get("confirmation_status") != str(ConfirmationStatus.CONFIRMED):
                # imported 是历史迁移状态，仍要求联络员在新流程中确认。
                raise_issue(
                    f"boundary_unconfirmed:{site_id}:R{rev_no}",
                    "boundary_unconfirmed",
                    f"厂区边界 R{rev_no} 尚未由联络员确认",
                    IssueSeverity.BLOCKER,
                )
            if not revision.get("boundary_map_evidence_id"):
                severity = IssueSeverity.BLOCKER if rev_no > 1 else IssueSeverity.CONDITION
                raise_issue(
                    f"boundary_map_missing:{site_id}:R{rev_no}",
                    "boundary_map_missing",
                    f"厂区边界 R{rev_no} 缺少边界图或附件元数据",
                    severity,
                )

        # 产品范围。
        scopes = self.store.find("product_scopes", site_id=site_id)
        overlapping_scopes = [
            scope
            for scope in scopes
            if intervals_overlap(
                parse_date(scope.get("valid_from")),
                parse_date(scope.get("valid_to")),
                period_start,
                period_end,
            )
        ] if (period_start or period_end) else scopes
        if not overlapping_scopes:
            raise_issue(
                f"product_scope_missing:{site_id}:{period_label or 'current'}",
                "product_scope_missing",
                "缺少产品范围或适用产品清单",
                IssueSeverity.BLOCKER,
            )
        for scope in overlapping_scopes:
            if scope.get("confirmation_status") != str(ConfirmationStatus.CONFIRMED):
                raise_issue(
                    f"product_scope_unconfirmed:{site_id}:{scope['id']}",
                    "product_scope_unconfirmed",
                    f"产品范围 {scope.get('scope_id') or scope['id']} 尚未确认",
                    IssueSeverity.BLOCKER,
                )

        # 指标和折算因子。
        metrics = self.current_metrics(site_id, period)
        metric_findings: list[dict[str, Any]] = []
        categories: dict[str, dict[str, Any]] = {
            category: {
                "category": category,
                "label": CATEGORY_LABELS[category],
                "status": "unassessed",
                "metrics": [],
                "reasons": [],
            }
            for category in REQUIRED_CATEGORIES
        }
        for metric in metrics:
            finding = copy.deepcopy(metric)
            factor = None
            factor_id = metric.get("factor_id")
            if factor_id:
                factor_date = period_end or day
                factor = self.get_factor(factor_id, on_date=factor_date) or self.get_factor(factor_id)
            if metric.get("value") is not None and factor is not None and factor.get("value") is not None:
                impact = to_decimal(metric["value"]) * factor["value"]
                finding["converted_impact"] = impact
                finding["impact_unit"] = factor.get("unit")
                finding["factor_ref"] = f"{factor['factor_id']}@v{factor['version']}"
                finding["impact_status"] = "supported"
            elif factor_id and metric.get("value") is not None:
                finding["converted_impact"] = None
                finding["impact_status"] = "unassessed_factor"
                finding["reason"] = finding.get("reason") or "missing_factor"
                raise_issue(
                    f"factor_missing:{factor_id}:{site_id}:{period_label}",
                    "factor_missing",
                    f"指标 {metric.get('label')} 引用的折算因子 {factor_id} 缺失",
                    IssueSeverity.CONDITION,
                    factor_id=factor_id,
                )
            else:
                finding["converted_impact"] = None
                finding["impact_status"] = "not_calculated"
            metric_findings.append(finding)
            category = metric.get("category") or "other"
            categories.setdefault(
                category,
                {"category": category, "label": category, "status": "unassessed", "metrics": [], "reasons": []},
            )
            categories[category]["metrics"].append(finding)
            if metric.get("status") == "unassessed":
                categories[category]["reasons"].append(metric.get("reason") or "metric_unassessed")
                raise_issue(
                    f"metric_unassessed:{metric['metric_id']}:v{metric['version']}",
                    "metric_unassessed",
                    f"指标 {metric.get('label')} 未核定",
                    IssueSeverity.BLOCKER,
                    metric_id=metric.get("metric_id"),
                    metric_code=metric.get("code"),
                    category=category,
                    detail=metric.get("reason"),
                )

        unassessed: list[dict[str, Any]] = []
        for category in REQUIRED_CATEGORIES:
            bucket = categories[category]
            supported = [m for m in bucket["metrics"] if m.get("status") == "supported"]
            if not bucket["metrics"]:
                bucket["status"] = "unassessed"
                bucket["reasons"].append("no_submission")
                unassessed.append(
                    {
                        "scope": f"{site_id}/{period_label or 'unspecified'}/{category}",
                        "category": category,
                        "label": CATEGORY_LABELS[category],
                        "status": "unassessed",
                        "reason": "no_submission",
                        "statement": f"{CATEGORY_LABELS[category]}范围未核定；缺少提交或证据，未按零消耗/零排放填补",
                    }
                )
                raise_issue(
                    f"category_missing:{site_id}:{period_label or 'all'}:{category}",
                    "category_missing",
                    f"缺少{CATEGORY_LABELS[category]}证据，相关范围未核定",
                    IssueSeverity.BLOCKER,
                    category=category,
                )
            elif not supported:
                bucket["status"] = "unassessed"
                unassessed.append(
                    {
                        "scope": f"{site_id}/{period_label or 'unspecified'}/{category}",
                        "category": category,
                        "label": CATEGORY_LABELS[category],
                        "status": "unassessed",
                        "reason": "evidence_or_value_missing",
                        "statement": f"{CATEGORY_LABELS[category]}指标缺少有效证据或数值，范围未核定，不能按零填补",
                    }
                )
            else:
                all_accepted = all(m.get("accepted") for m in supported)
                bucket["status"] = "review_accepted" if all_accepted else "supported_pending_review"

        # 证明有效性。
        certificates = []
        cert_rows = [c for c in self.store.all("certificates") if c.get("site_id") == site_id and c.get("is_current", True)]
        for cert in cert_rows:
            start = parse_date(cert.get("valid_from"))
            end = parse_date(cert.get("valid_to"))
            if not intervals_overlap(start, end, period_start, period_end):
                continue
            item = copy.deepcopy(cert)
            item["covers_full_period"] = (
                period_start is None or period_end is None or (start is not None and start <= period_start and (end is None or end >= period_end))
            )
            certificates.append(item)
            if end and period_end and end < period_end:
                raise_issue(
                    f"certificate_expired_during_period:{cert['certificate_id']}:{period_label}",
                    "certificate_expired",
                    f"证明 {cert.get('certificate_no') or cert['certificate_id']} 在评审期间内到期",
                    IssueSeverity.BLOCKER,
                    certificate_id=cert["certificate_id"],
                    detail=f"有效期至 {cert.get('valid_to')}，评审期间至 {period_end.isoformat()}",
                )
            if cert.get("verification_status") != str(VerificationStatus.VERIFIED):
                raise_issue(
                    f"certificate_not_verified:{cert['certificate_id']}:v{cert['version']}",
                    "certificate_not_verified",
                    "证明尚未通过授权验证",
                    IssueSeverity.BLOCKER if require_certificate else IssueSeverity.CONDITION,
                    certificate_id=cert["certificate_id"],
                )
            if cert.get("mapping_status") != str(ConfirmationStatus.CONFIRMED):
                raise_issue(
                    f"certificate_mapping_unconfirmed:{cert['certificate_id']}:v{cert['version']}",
                    "certificate_mapping_unconfirmed",
                    "证明对应的产品/范围尚未由联络员确认",
                    IssueSeverity.BLOCKER if require_certificate else IssueSeverity.CONDITION,
                    certificate_id=cert["certificate_id"],
                )

        if require_certificate and not certificates:
            raise_issue(
                f"certificate_missing:{site_id}:{period_label or 'current'}",
                "certificate_missing",
                "缺少支持结论的第三方证明",
                IssueSeverity.BLOCKER,
            )

        site_open_issues = self.store.find("issues", site_id=site_id, status=str(IssueStatus.OPEN))

        def issue_in_period(issue: dict[str, Any]) -> bool:
            issue_period = issue.get("period")
            return not period_label or issue_period in {None, "", period_label}

        blockers = [i for i in site_open_issues if i.get("severity") == str(IssueSeverity.BLOCKER) and issue_in_period(i)]
        conditions = [i for i in site_open_issues if i.get("severity") == str(IssueSeverity.CONDITION) and issue_in_period(i)]
        # 全局因子事项也纳入。
        global_factor_issues = [i for i in self.store.find("issues", status=str(IssueStatus.OPEN)) if i.get("site_id") is None and i.get("factor_id")]
        conditions.extend(global_factor_issues)

        ready_for_pass = not unassessed and not blockers
        if ready_for_pass:
            ready_for_pass = all(
                metric.get("accepted")
                for metric in metric_findings
                if metric.get("status") == "supported"
            )
        result = {
            "site_id": site_id,
            "period": period_label,
            "period_start": iso_date(period_start),
            "period_end": iso_date(period_end),
            "as_of": iso_date(day),
            "boundary_revisions": boundary_segments,
            "product_scopes": overlapping_scopes,
            "metrics": metric_findings,
            "categories": categories,
            "unassessed": unassessed,
            "certificates": certificates,
            "blockers": blockers,
            "conditions": conditions,
            "active_issue_keys": sorted(active_issue_keys),
            "completeness": {
                "all_required_categories_present": not any(c["status"] == "unassessed" for c in categories.values()),
                "unassessed_count": len(unassessed),
                "blocker_count": len(blockers),
                "condition_count": len(conditions),
                "ready_for_pass": ready_for_pass,
                "statement": "缺失范围保持未核定，未以零值替代" if unassessed else "四类必备材料均有可追踪记录",
            },
        }
        if create_issues:
            self._reconcile_assessment_issues(site_id, period_label, active_issue_keys)
            self.save()
        return result

    def _reconcile_assessment_issues(self, site_id: str, period_label: str, active_keys: set[str]) -> None:
        prefixes = (
            "category_missing:",
            "metric_unassessed:",
            "missing_metric:",
            "boundary_",
            "product_scope_",
            "certificate_",
            "factor_missing:",
        )
        for issue in self.store.find("issues", site_id=site_id, status=str(IssueStatus.OPEN)):
            key = issue.get("issue_key", "")
            if not key.startswith(prefixes):
                continue
            if period_label and issue.get("period") not in {None, period_label} and not key.startswith(("boundary_unconfirmed", "boundary_map_missing")):
                continue
            if key not in active_keys and issue.get("auto_generated", True):
                issue["status"] = str(IssueStatus.RESOLVED)
                issue["resolved_at"] = utc_now_iso()
                issue["resolution_note"] = "重新评估时该缺项已满足或被新版本替代"
                self.store.update("issues", issue)

    # ------------------------------------------------------------------
    # 评审决定和草案
    # ------------------------------------------------------------------
    def suggest_evidence_refs(self, site_id: str, period: str | dict | None = None, assessment: dict[str, Any] | None = None) -> list[str]:
        assessment = assessment or self.assess(site_id, period, create_issues=False)
        refs: list[str] = []
        for metric in assessment.get("metrics", []):
            if metric.get("evidence_ref"):
                refs.append(metric["evidence_ref"])
            if metric.get("factor_ref"):
                factor_id, version = self.parse_evidence_ref(metric["factor_ref"])
                factor_evidence_id = f"FACTOR-{factor_id}"
                factor_evidence = self.store.find_one("evidence_versions", evidence_id=factor_evidence_id, version=version)
                if factor_evidence:
                    refs.append(f"{factor_evidence_id}@v{version}")
        for cert in assessment.get("certificates", []):
            if cert.get("evidence_ref"):
                refs.append(cert["evidence_ref"])
        for boundary in assessment.get("boundary_revisions", []):
            if boundary.get("boundary_map_evidence_id"):
                version = boundary.get("boundary_map_evidence_version")
                refs.append(
                    f"{boundary['boundary_map_evidence_id']}" + (f"@v{version}" if version else "")
                )
        return unique_strings(refs)

    @staticmethod
    def _normalize_conditions(conditions: list[Any] | None) -> list[dict[str, Any]]:
        result = []
        for index, condition in enumerate(conditions or [], 1):
            if isinstance(condition, str):
                result.append({"id": f"C{index}", "text": condition, "status": "open"})
            elif isinstance(condition, dict):
                item = copy.deepcopy(condition)
                item.setdefault("id", f"C{index}")
                item.setdefault("status", "open")
                if not item.get("text") and item.get("description"):
                    item["text"] = item["description"]
                result.append(item)
        return result

    @transactional
    def create_decision_draft(
        self,
        site_id: str,
        period: str | dict | None = None,
        result: str | None = None,
        *,
        decision_id: str | None = None,
        conclusion: str | None = None,
        evidence_refs: list[Any] | None = None,
        product_codes: list[str] | None = None,
        conditions: list[Any] | None = None,
        reviewer: str | None = None,
        created_by: str | None = None,
        parent_decision_id: str | None = None,
        supersedes_id: str | None = None,
        review_reasons: list[str] | None = None,
        trigger_ids: list[str] | None = None,
        case_id: str | None = None,
        auto_evidence: bool = True,
        require_certificate: bool = False,
        save: bool = True,
    ) -> dict[str, Any]:
        site_id = str(site_id)
        parent = None
        parent_id = parent_decision_id or supersedes_id
        if parent_id:
            parent = self.get_decision(parent_id)
            period = period or parent.get("period")
            site_id = parent.get("site_id", site_id)

        assessment = self.assess(site_id, period, create_issues=True, require_certificate=require_certificate)
        if evidence_refs is None and auto_evidence:
            evidence_refs = self.suggest_evidence_refs(site_id, period, assessment)
        evidence_snapshot = self.snapshot_evidence_refs(evidence_refs or [])

        if product_codes is None and parent:
            product_codes = (parent.get("scope") or {}).get("product_codes")
        if product_codes is None:
            product_codes = sorted({code for scope in assessment["product_scopes"] for code in scope.get("product_codes", [])})

        boundary_scope = [
            {
                "boundary_id": row.get("id"),
                "revision": row.get("revision"),
                "valid_from": row.get("valid_from"),
                "effective_end": row.get("_effective_end"),
                "area_m2": row.get("area_m2"),
                "confirmation_status": row.get("confirmation_status"),
            }
            for row in assessment["boundary_revisions"]
        ]
        diff = None
        if parent:
            diff = self.diff_evidence_snapshots(parent.get("evidence", []), evidence_snapshot)

        decision_id = decision_id or self._new_id("D", "decision")
        if self.store.find_one("decisions", id=decision_id):
            raise ConflictError(f"决定 {decision_id} 已存在")
        period_start, period_end, period_label = period_range(period)
        decision = {
            "id": decision_id,
            "_collection": "decisions",
            "type": "review_decision",
            "decision_id": decision_id,
            "site_id": site_id,
            "period": period_label or (parent.get("period") if parent else None),
            "period_start": iso_date(period_start) or (parent or {}).get("period_start"),
            "period_end": iso_date(period_end) or (parent or {}).get("period_end"),
            "state": str(DecisionState.DRAFT),
            "result": result or (parent.get("result") if parent else None) or "pending",
            "requested_result": result,
            "conclusion": conclusion,
            "scope": {
                "site_id": site_id,
                "period": period_label,
                "period_start": iso_date(period_start),
                "period_end": iso_date(period_end),
                "boundary_revisions": boundary_scope,
                "product_codes": unique_strings(product_codes or []),
                "boundary_statement": self._boundary_statement(boundary_scope),
            },
            "evidence": evidence_snapshot,
            "evidence_refs": [item["evidence_id"] for item in evidence_snapshot],
            "metric_findings": copy.deepcopy(assessment["metrics"]),
            "category_status": copy.deepcopy(assessment["categories"]),
            "unassessed": copy.deepcopy(assessment["unassessed"]),
            "completeness": copy.deepcopy(assessment["completeness"]),
            "conditions": self._normalize_conditions(conditions),
            "review_reasons": unique_strings(review_reasons or []),
            "trigger_ids": trigger_ids or [],
            "case_id": case_id,
            "parent_decision_id": parent["id"] if parent else None,
            "supersedes_id": (supersedes_id or parent["id"]) if parent or supersedes_id else None,
            "root_decision_id": (parent.get("root_decision_id") or parent["id"]) if parent else decision_id,
            "evidence_diff": diff,
            "reviewer": reviewer,
            "created_by": created_by or reviewer,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "submitted_at": None,
            "issued_at": None,
            "effective_from": None,
            "effective_to": None,
            "timeline_results": [],
            "timeline_note": None,
            "issue_history": [],
        }
        self.store.add("decisions", decision)
        if case_id:
            case = self.store.find_one("cases", id=case_id)
            if case:
                case["draft_decision_id"] = decision_id
                case["status"] = str(ReviewCaseStatus.DRAFT_CREATED)
                case.setdefault("decision_ids", []).append(decision_id)
                self.store.update("cases", case)
        if save:
            self.save()
        return decision

    @staticmethod
    def _boundary_statement(segments: list[dict[str, Any]]) -> str:
        if not segments:
            return "厂区边界未核定"
        if len(segments) == 1:
            seg = segments[0]
            return f"采用厂区边界 R{seg['revision']}（{seg.get('valid_from')} 起，面积 {seg.get('area_m2')} m²）"
        revisions = "、".join(f"R{seg['revision']}" for seg in segments)
        return f"评审期间跨越边界版本 {revisions}，结论按对应生效区间分段适用"

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        decision = self.store.find_one("decisions", id=str(decision_id)) or self.store.find_one("decisions", decision_id=str(decision_id))
        if decision is None:
            raise NotFoundError(f"决定 {decision_id} 不存在")
        return decision

    def create_review_draft(self, *args, **kwargs) -> dict[str, Any]:
        """``create_decision_draft`` 的语义别名。"""
        return self.create_decision_draft(*args, **kwargs)

    def create_rereview_draft(
        self,
        decision_id: str,
        reason: str | list[str] | None = None,
        *,
        trigger_ids: list[str] | None = None,
        case_id: str | None = None,
        decision_id_new: str | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        old = self.get_decision(decision_id)
        reasons = as_list(reason)
        existing = self.store.find_one("decisions", supersedes_id=old["id"], state=str(DecisionState.DRAFT))
        if existing is None:
            existing = self.store.find_one("decisions", parent_decision_id=old["id"], state=str(DecisionState.REVIEW))
        if existing is not None:
            # 新的更正到达已存在但尚未签发的复审草案时，把同名证据推进到最新版，
            # 并在 refresh_diff 中保留本次自动推进的差异。已签发决定不会进入这里。
            existing = self.refresh_draft(existing["id"], add_suggested=True)
            for item in reasons:
                if item and item not in existing.setdefault("review_reasons", []):
                    existing["review_reasons"].append(str(item))
            if trigger_ids:
                existing["trigger_ids"] = unique_strings((existing.get("trigger_ids") or []) + trigger_ids)
            if case_id and not existing.get("case_id"):
                existing["case_id"] = case_id
            existing["updated_at"] = utc_now_iso()
            self.store.update("decisions", existing)
            self.save()
            return existing
        logical_refs = [item["evidence_id"] for item in old.get("evidence", [])]
        return self.create_decision_draft(
            old["site_id"],
            old.get("period"),
            decision_id=decision_id_new,
            parent_decision_id=old["id"],
            supersedes_id=old["id"],
            evidence_refs=logical_refs if logical_refs else None,
            review_reasons=[str(r) for r in reasons if r],
            trigger_ids=trigger_ids,
            case_id=case_id,
            **kwargs,
        )

    def refresh_draft(self, decision_id: str, *, add_suggested: bool = True, reviewer: str | None = None) -> dict[str, Any]:
        decision = self.get_decision(decision_id)
        if decision.get("state") == str(DecisionState.ISSUED):
            raise ConflictError("已签发决定不可刷新；请创建复审草案")
        old_snapshot = copy.deepcopy(decision.get("evidence", []))
        logical_ids = [item["evidence_id"] for item in old_snapshot]
        refs = [f"{evidence_id}" for evidence_id in logical_ids]
        assessment = self.assess(decision["site_id"], decision.get("period"), create_issues=True)
        if add_suggested:
            refs.extend(self.suggest_evidence_refs(decision["site_id"], decision.get("period"), assessment))
        refs = unique_strings(refs)
        new_snapshot = self.snapshot_evidence_refs(refs)
        baseline = self.get_decision(decision["supersedes_id"]) if decision.get("supersedes_id") else None
        decision["evidence"] = new_snapshot
        decision["evidence_refs"] = [item["evidence_id"] for item in new_snapshot]
        decision["metric_findings"] = copy.deepcopy(assessment["metrics"])
        decision["category_status"] = copy.deepcopy(assessment["categories"])
        decision["unassessed"] = copy.deepcopy(assessment["unassessed"])
        decision["completeness"] = copy.deepcopy(assessment["completeness"])
        decision["evidence_diff"] = self.diff_evidence_snapshots((baseline or {}).get("evidence", []), new_snapshot) if baseline else self.diff_evidence_snapshots(old_snapshot, new_snapshot)
        decision["refresh_diff"] = self.diff_evidence_snapshots(old_snapshot, new_snapshot)
        decision["updated_at"] = utc_now_iso()
        if reviewer:
            decision["reviewer"] = reviewer
        self.store.update("decisions", decision)
        self.save()
        return decision

    def submit_for_review(self, decision_id: str, reviewer: str | None = None) -> dict[str, Any]:
        decision = self.get_decision(decision_id)
        if decision["state"] not in {str(DecisionState.DRAFT), str(DecisionState.REVIEW)}:
            raise ConflictError(f"决定处于 {decision['state']}，不能提交评审")
        decision["state"] = str(DecisionState.REVIEW)
        decision["submitted_at"] = utc_now_iso()
        if reviewer:
            decision["reviewer"] = reviewer
        decision["updated_at"] = utc_now_iso()
        self.store.update("decisions", decision)
        self.save()
        return decision

    # Alias matching common workflow wording.
    submit_decision_for_review = submit_for_review

    @transactional
    def issue_decision(
        self,
        decision_id: str,
        reviewer: str,
        *args: Any,
        confirmed: bool = True,
        result: str | None = None,
        conclusion: str | None = None,
        conditions: list[Any] | None = None,
        effective_from: Any = None,
        effective_to: Any = None,
        timeline_note: str | None = None,
        predecessor_certificate_id: str | None = None,
        allow_stale_snapshot: bool = False,
        as_of: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # 容忍 issue_decision(id, reviewer, "pass") 或 ..., True, "pass", "2026-01-01"
        # 这类未使用关键字的调用。
        for positional in args:
            if isinstance(positional, bool):
                confirmed = positional
            elif isinstance(positional, (list, tuple)):
                conditions = list(positional)
            elif isinstance(positional, str) and positional.lower() in {
                "pass",
                "conditional",
                "fail",
                "incomplete",
                "pending",
            }:
                result = positional
            elif isinstance(positional, str):
                try:
                    parse_date(positional)
                    if effective_from is None:
                        effective_from = positional
                    else:
                        effective_to = positional
                except ValidationError:
                    conclusion = positional
        effective_from = effective_from or kwargs.get("effective_date")
        timeline_note = timeline_note or kwargs.get("explanation")
        if not reviewer or not str(reviewer).strip():
            raise ValidationError("签发决定必须记录复审人员")
        if not confirmed:
            raise ReviewBlockedError("复审人员尚未确认，不能产生生效结论")
        decision = self.get_decision(decision_id)
        if decision["state"] == str(DecisionState.ISSUED):
            raise ConflictError(f"决定 {decision_id} 已签发；已签发决定不可修改")
        if decision["state"] == str(DecisionState.SUPERSEDED):
            raise ConflictError(f"决定 {decision_id} 已被替代，不能签发")

        if result:
            decision["result"] = str(result)
        if conclusion is not None:
            decision["conclusion"] = conclusion
        if conditions is not None:
            decision["conditions"] = self._normalize_conditions(conditions)
        if timeline_note is not None:
            decision["timeline_note"] = timeline_note

        result_name = str(decision.get("result") or "").lower()
        if result_name in {"", "pending", "draft", "review"}:
            raise ReviewBlockedError("签发前必须给出 pass/conditional/fail/incomplete 结论")

        stale = self._stale_snapshot_refs(decision)
        if stale and not allow_stale_snapshot:
            raise ReviewBlockedError(
                "草案引用的证据已有更新版本；请 refresh_draft 并复核，或显式 allow_stale_snapshot=True 确认保留原快照: "
                + ",".join(stale)
            )

        # 签发确认本身代表评审人员对未明确驳回的数据口径作出判断；
        # 已显式 rejected 的指标仍会在结果校验中阻断 pass。
        self._confirm_pending_metrics_for_issue(decision, reviewer)
        # 重新读取指标确认状态，但不改变决定选择的证据快照。
        self._apply_metric_review_status(decision)
        self._validate_result_requirements(decision, result_name)

        effective_start = parse_date(effective_from, "effective_from") or (parse_date(as_of) if as_of else self.today())
        effective_end = parse_date(effective_to, "effective_to")
        candidate_view = copy.deepcopy(decision)
        candidate_view["effective_from"] = effective_start.isoformat()
        candidate_view["effective_to"] = effective_end.isoformat() if effective_end else None
        candidate_view["timeline_note"] = timeline_note or decision.get("timeline_note")

        timeline = self.timeline_report(
            site_id=decision["site_id"],
            product_codes=(decision.get("scope") or {}).get("product_codes"),
            candidate=candidate_view,
            include_certificates=bool(predecessor_certificate_id or decision.get("predecessor_certificate_id")),
            certificate_ids=[predecessor_certificate_id or decision.get("predecessor_certificate_id")]
            if (predecessor_certificate_id or decision.get("predecessor_certificate_id"))
            else None,
            mixed_kinds=bool(predecessor_certificate_id or decision.get("predecessor_certificate_id")),
            raise_on_conflict=False,
        )
        unexplained = [item for item in timeline.get("conflicts", []) if not item.get("explained")]
        if unexplained and not timeline_note:
            raise TimelineConflictError("存在未解释的效期空档或重叠", conflicts=unexplained)

        issued_at = utc_now_iso()
        decision["effective_from"] = effective_start.isoformat()
        decision["effective_to"] = effective_end.isoformat() if effective_end else None
        decision["issued_at"] = issued_at
        decision["reviewer"] = reviewer
        decision["confirmed_by"] = reviewer
        decision["confirmed_at"] = issued_at
        decision["state"] = str(DecisionState.ISSUED)
        decision["timeline_results"] = timeline.get("results", [])
        if unexplained and timeline_note:
            for conflict in unexplained:
                conflict["explained"] = True
                conflict["explanation"] = timeline_note
            decision["timeline_note"] = timeline_note
            decision["timeline_results"].extend(unexplained)

        if decision.get("supersedes_id"):
            old = self.get_decision(decision["supersedes_id"])
            old["state"] = str(DecisionState.SUPERSEDED)
            old["superseded_by"] = decision["id"]
            old["superseded_at"] = decision["issued_at"]
            # 不修改 old["evidence"]，原证据版本和哈希完整保留。
            self.store.update("decisions", old)

        if decision.get("case_id"):
            case = self.store.find_one("cases", id=decision["case_id"])
            if case:
                case["status"] = str(ReviewCaseStatus.CONFIRMED)
                case["confirmed_decision_id"] = decision["id"]
                self.store.update("cases", case)

        decision["updated_at"] = utc_now_iso()
        self.store.update("decisions", decision)
        self.save()
        return decision

    def _stale_snapshot_refs(self, decision: dict[str, Any]) -> list[str]:
        stale = []
        for item in decision.get("evidence", []):
            rows = self.store.find("evidence_versions", evidence_id=item["evidence_id"])
            if not rows:
                continue
            latest = max(rows, key=lambda r: int(r.get("version", 0)))
            if int(latest["version"]) != int(item["version"]):
                stale.append(f"{item['evidence_id']}@v{item['version']}->v{latest['version']}")
        return stale

    def _confirm_pending_metrics_for_issue(self, decision: dict[str, Any], reviewer: str) -> None:
        timestamp = utc_now_iso()
        for finding in decision.get("metric_findings", []):
            if finding.get("status") != "supported":
                continue
            current = self.store.find_one("metric_versions", metric_id=finding.get("metric_id"), is_current=True)
            if current and current.get("review_status") != "rejected" and not current.get("accepted"):
                current["accepted"] = True
                current["review_status"] = "accepted"
                current["reviewer"] = reviewer
                current["reviewed_at"] = timestamp
                current["caliber_note"] = current.get("caliber_note") or "复审签发时由评审人员确认口径"
                self.store.update("metric_versions", current)

    def _apply_metric_review_status(self, decision: dict[str, Any]) -> None:
        for finding in decision.get("metric_findings", []):
            current = self.store.find_one("metric_versions", metric_id=finding.get("metric_id"), is_current=True)
            if current:
                finding["accepted"] = current.get("accepted")
                finding["review_status"] = current.get("review_status")
                finding["reviewer"] = current.get("reviewer")
                finding["caliber_note"] = current.get("caliber_note")

    def _validate_result_requirements(self, decision: dict[str, Any], result: str) -> None:
        scope = decision.get("scope") or {}
        if not decision.get("site_id"):
            raise ReviewBlockedError("结论缺少厂区适用范围")
        if result == "pass":
            if not scope.get("boundary_revisions"):
                raise ReviewBlockedError("pass 结论缺少已经识别的厂区边界/统计口径范围")
            fresh_assessment = self.assess(decision["site_id"], decision.get("period"), create_issues=False)
            if not fresh_assessment.get("completeness", {}).get("ready_for_pass"):
                blocker_titles = "；".join(issue.get("title", "") for issue in fresh_assessment.get("blockers", [])[:5])
                raise ReviewBlockedError(f"当前口径/证据仍不满足 pass 条件: {blocker_titles or '存在未核定范围或未确认项'}")
            if decision.get("unassessed"):
                labels = "、".join(item.get("label") or item.get("category") for item in decision["unassessed"])
                raise ReviewBlockedError(f"存在未核定范围，不能作出 pass 结论: {labels}")
            not_accepted = [
                m.get("metric_id")
                for m in decision.get("metric_findings", [])
                if m.get("status") == "supported" and not m.get("accepted")
            ]
            if not_accepted:
                raise ReviewBlockedError("以下指标尚未经评审人员确认口径: " + ",".join(filter(None, not_accepted)))
            # 决定引用的证明必须覆盖被评审期间；验证/映射必须完成。
            period_start = parse_date(decision.get("period_start"))
            period_end = parse_date(decision.get("period_end"))
            for evidence in decision.get("evidence", []):
                if evidence.get("kind") in {"certificate", "audit", "third_party_certificate"}:
                    cert = self.store.find_one("certificates", evidence_id=evidence["evidence_id"]) or self.store.find_one(
                        "certificates", evidence_ref=f"{evidence['evidence_id']}@v{evidence['version']}"
                    )
                    if cert:
                        cert_start = parse_date(cert.get("valid_from"))
                        cert_end = parse_date(cert.get("valid_to"))
                        if period_start and period_end:
                            covers_period = (cert_start is None or cert_start <= period_start) and (cert_end is None or cert_end >= period_end)
                        else:
                            review_day = self.today()
                            covers_period = (cert_start is None or cert_start <= review_day) and (cert_end is None or cert_end >= review_day)
                        if not covers_period:
                            raise ReviewBlockedError(f"引用证明 {cert.get('certificate_id')} 未覆盖评审期间")
                        if cert.get("verification_status") != "verified":
                            raise ReviewBlockedError(f"引用证明 {cert.get('certificate_id')} 尚未验证通过")
                        if cert.get("mapping_status") != "confirmed":
                            raise ReviewBlockedError(f"引用证明 {cert.get('certificate_id')} 尚未确认产品/厂区对应关系")
            for boundary in scope.get("boundary_revisions", []):
                current_boundary = self.store.find_one("sites", id=boundary.get("boundary_id")) if boundary.get("boundary_id") else None
                if current_boundary and current_boundary.get("confirmation_status") != "confirmed":
                    raise ReviewBlockedError(f"厂区边界 R{current_boundary.get('revision')} 尚未经联络员确认")
            if not decision.get("evidence"):
                raise ReviewBlockedError("pass 结论必须引用至少一项证据")
        if result == "conditional" and not decision.get("conditions"):
            raise ReviewBlockedError("conditional 结论必须列出整改条件")
        if result == "incomplete" and not decision.get("conclusion"):
            labels = "、".join(item.get("label") or item.get("category", "") for item in decision.get("unassessed", []))
            decision["conclusion"] = (
                "证据不足，结论为未完成/未核定；未核定范围未按零消耗或零排放处理。"
                + (f" 未核定指标类别：{labels}。" if labels else "")
                + " 请按整改条件补交材料后申请复审。"
            )
        if result == "fail" and not decision.get("conclusion"):
            raise ReviewBlockedError("fail 结论必须填写 conclusion 说明")

    # ------------------------------------------------------------------
    # 变更影响分析和复审草案
    # ------------------------------------------------------------------
    def _boundary_impact_needed(self, payload: dict[str, Any], record: dict[str, Any] | None = None) -> bool:
        kind = str((record or {}).get("kind") or payload.get("kind") or "").lower()
        is_map = kind in {"boundary_map", "site_boundary_map", "expansion_map", "boundary"}
        if not is_map and payload.get("change_type") != "boundary_expansion":
            return False
        if payload.get("change_type") == "boundary_expansion":
            return True
        revision = payload.get("site_revision") or payload.get("revision") or payload.get("boundary_revision")
        if isinstance(revision, int) and revision > 1:
            return True
        site_id = self._optional_str((record or {}).get("site_id") or payload.get("site_id"))
        if site_id and self.store.find("decisions", site_id=site_id):
            return any(
                d.get("state") in {str(DecisionState.ISSUED), str(DecisionState.DRAFT), str(DecisionState.REVIEW)}
                for d in self.store.find("decisions", site_id=site_id)
            )
        return False

    @transactional
    def analyze_impact(
        self,
        change_type: str,
        *,
        site_id: str | None = None,
        factor_id: str | None = None,
        trigger_event_id: str | None = None,
        summary: str | None = None,
        as_of: Any = None,
        create_drafts: bool = True,
    ) -> dict[str, Any]:
        day = parse_date(as_of, "as_of") if as_of else self.today()
        impact = {
            "id": self._new_id("IMP", "impact"),
            "_collection": "impacts",
            "type": "impact_analysis",
            "change_type": change_type,
            "site_id": site_id,
            "factor_id": factor_id,
            "summary": summary or change_type,
            "trigger_event_id": trigger_event_id,
            "as_of": day.isoformat(),
            "created_at": utc_now_iso(),
            "open_issue_ids": [],
            "affected_certificates": [],
            "affected_decision_ids": [],
            "draft_decision_ids": [],
            "case_ids": [],
        }

        affected_sites = {str(site_id)} if site_id else set()
        open_issues: list[dict[str, Any]] = []

        if change_type == "boundary_changed":
            if site_id:
                open_issues.extend(self.pending_materials(site_id))
                certs = self.current_certificates(day, site_id=site_id)
                for cert in certs:
                    impact["affected_certificates"].append(
                        {
                            "certificate_id": cert["certificate_id"],
                            "version": cert["version"],
                            "reason": "boundary_scope_changed：扩建改变统计边界，证明对应关系需重新确认",
                            "date_valid": cert.get("date_valid"),
                            "usable": cert.get("usable_for_positive_decision"),
                        }
                    )
                decisions = [
                    d
                    for d in self.store.all("decisions")
                    if d.get("site_id") == site_id and d.get("state") in {str(DecisionState.ISSUED), str(DecisionState.DRAFT), str(DecisionState.REVIEW)}
                ]
                boundary = self.current_site_revision(site_id, day)
                change_start = parse_date((boundary or {}).get("valid_from"))
                for decision in decisions:
                    start = parse_date(decision.get("period_start"))
                    end = parse_date(decision.get("period_end"))
                    if change_start and not intervals_overlap(start, end, change_start, None) and end and end < change_start:
                        continue
                    impact["affected_decision_ids"].append(decision["id"])
                self.open_issue(
                    issue_key=f"boundary_change_review:{site_id}:{trigger_event_id or impact['id']}",
                    code="boundary_change_needs_rereview",
                    site_id=site_id,
                    severity=IssueSeverity.CONDITION,
                    title="厂区边界发生扩建或更正，需要复审适用范围和证据",
                    detail=summary,
                    trigger_event_id=trigger_event_id,
                )
        elif change_type == "factor_corrected":
            factor = self.get_factor(factor_id) if factor_id else None
            applies_to = set((factor or {}).get("applies_to") or [])
            metric_rows = []
            for metric in self.store.find("metric_versions", is_current=True):
                spec_factor = METRIC_SPECS.get(metric.get("code"), {}).get("factor_id")
                if metric.get("factor_id") == factor_id or spec_factor == factor_id or metric.get("code") in applies_to:
                    if site_id and metric.get("site_id") != site_id:
                        continue
                    metric_rows.append(metric)
                    if metric.get("site_id"):
                        affected_sites.add(metric["site_id"])
            metric_ids = {m.get("metric_id") for m in metric_rows}
            for issue in self.store.find("issues", status=str(IssueStatus.OPEN)):
                if issue.get("factor_id") == factor_id or issue.get("metric_id") in metric_ids or (site_id and issue.get("site_id") == site_id):
                    open_issues.append(issue)
            self.open_issue(
                issue_key=f"factor_corrected:{factor_id}:{trigger_event_id or impact['id']}",
                code="factor_correction_needs_rereview",
                site_id=site_id,
                factor_id=factor_id,
                severity=IssueSeverity.CONDITION,
                title=f"折算因子 {factor_id} 已更正，引用它的指标和结论需要复审",
                detail=summary,
                trigger_event_id=trigger_event_id,
            )
            for decision in self.store.all("decisions"):
                if decision.get("state") not in {str(DecisionState.ISSUED), str(DecisionState.DRAFT), str(DecisionState.REVIEW)}:
                    continue
                if site_id and decision.get("site_id") != site_id:
                    continue
                uses_factor = any(str(f.get("factor_ref", "")).startswith(f"{factor_id}@") for f in decision.get("metric_findings", []))
                if uses_factor or decision.get("site_id") in affected_sites:
                    impact["affected_decision_ids"].append(decision["id"])
                    for evidence in decision.get("evidence", []):
                        if evidence.get("kind") in {"certificate", "audit", "third_party_certificate"}:
                            impact["affected_certificates"].append(
                                {
                                    "certificate_id": evidence.get("evidence_id"),
                                    "version": evidence.get("version"),
                                    "reason": "factor_correction_may_change_result：证明支持的结论需随因子更正复核",
                                }
                            )
            for affected_site in affected_sites:
                for cert in self.current_certificates(day, site_id=affected_site):
                    if not any(c.get("certificate_id") == cert["certificate_id"] for c in impact["affected_certificates"]):
                        impact["affected_certificates"].append(
                            {
                                "certificate_id": cert["certificate_id"],
                                "version": cert["version"],
                                "reason": "same_site_factor_impact_potential：同厂区有效认证可能受重算影响",
                                "date_valid": cert.get("date_valid"),
                                "usable": cert.get("usable_for_positive_decision"),
                            }
                        )

        impact["open_issue_ids"] = unique_strings([i.get("id") for i in open_issues if i.get("id")])
        impact["affected_decision_ids"] = unique_strings(impact["affected_decision_ids"])
        impact["affected_certificates"] = {item["certificate_id"]: item for item in impact["affected_certificates"]}.values()
        impact["affected_certificates"] = list(impact["affected_certificates"])
        impact["affected_valid_certificate_ids"] = [
            item.get("certificate_id")
            for item in impact["affected_certificates"]
            if item.get("date_valid", True)
        ]

        case = {
            "id": self._new_id("CASE", "case"),
            "_collection": "cases",
            "type": "review_case",
            "status": str(ReviewCaseStatus.OPEN),
            "change_type": change_type,
            "site_id": site_id,
            "factor_id": factor_id,
            "impact_id": impact["id"],
            "trigger_event_id": trigger_event_id,
            "reasons": [summary or change_type],
            "open_issue_ids": impact["open_issue_ids"],
            "affected_certificate_ids": [c.get("certificate_id") for c in impact["affected_certificates"]],
            "decision_ids": [],
            "draft_decision_ids": [],
            "created_at": utc_now_iso(),
        }
        self.store.add("cases", case)
        impact["case_ids"].append(case["id"])

        if create_drafts:
            reason = f"[{change_type}] {summary or '来源增量可能影响原结论'}"
            for decision_id in impact["affected_decision_ids"]:
                decision = self.get_decision(decision_id)
                case["decision_ids"].append(decision_id)
                if decision.get("state") == str(DecisionState.ISSUED):
                    draft = self.create_rereview_draft(
                        decision_id,
                        reason,
                        trigger_ids=[trigger_event_id] if trigger_event_id else [],
                        case_id=case["id"],
                        save=False,
                    )
                    impact["draft_decision_ids"].append(draft["id"])
                    case["draft_decision_ids"].append(draft["id"])
                elif decision.get("state") in {str(DecisionState.DRAFT), str(DecisionState.REVIEW)}:
                    if reason not in decision.setdefault("review_reasons", []):
                        decision["review_reasons"].append(reason)
                    if trigger_event_id:
                        decision["trigger_ids"] = unique_strings((decision.get("trigger_ids") or []) + [trigger_event_id])
                    decision["case_id"] = decision.get("case_id") or case["id"]
                    self.store.update("decisions", decision)
                    impact["draft_decision_ids"].append(decision["id"])
                    case["draft_decision_ids"].append(decision["id"])
            impact["draft_decision_ids"] = unique_strings(impact["draft_decision_ids"])
            case["decision_ids"] = unique_strings(case["decision_ids"])
            case["draft_decision_ids"] = unique_strings(case["draft_decision_ids"])
            if case["draft_decision_ids"]:
                case["status"] = str(ReviewCaseStatus.DRAFT_CREATED)

        self.store.add("impacts", impact)
        self.store.update("cases", case)
        self.save()
        return impact

    def review_reasons(self, *, open_only: bool = True) -> list[dict[str, Any]]:
        cases = self.store.all("cases")
        if open_only:
            cases = [case for case in cases if case.get("status") != str(ReviewCaseStatus.CLOSED)]
        return sorted(cases, key=lambda case: case.get("created_at", ""), reverse=True)

    # ------------------------------------------------------------------
    # 效期链
    # ------------------------------------------------------------------
    def timeline_report(
        self,
        *,
        site_id: str | None = None,
        product_code: str | None = None,
        product_codes: list[str] | None = None,
        candidate: dict[str, Any] | None = None,
        include_certificates: bool = True,
        certificate_ids: list[str] | None = None,
        mixed_kinds: bool = False,
        raise_on_conflict: bool = True,
    ) -> dict[str, Any]:
        codes = set(product_codes or ([product_code] if product_code else []))
        certificate_id_filter = {str(value) for value in certificate_ids or [] if value}
        items: list[dict[str, Any]] = []
        for cert in self.store.all("certificates"):
            if not include_certificates:
                continue
            if cert.get("is_current") is False:
                continue
            if certificate_id_filter and cert.get("certificate_id") not in certificate_id_filter:
                continue
            if site_id and cert.get("site_id") != site_id:
                continue
            cert_codes = set(cert.get("product_codes") or [])
            if codes and cert_codes and not (codes & cert_codes):
                continue
            items.append(
                {
                    "id": cert.get("certificate_id"),
                    "kind": "certificate",
                    "valid_from": cert.get("valid_from"),
                    "valid_to": cert.get("valid_to"),
                    "timeline_note": cert.get("timeline_note"),
                }
            )
        for decision in self.store.all("decisions"):
            if decision.get("state") not in {str(DecisionState.ISSUED), str(DecisionState.SUPERSEDED)}:
                continue
            if candidate and decision.get("id") == candidate.get("id"):
                continue
            if site_id and decision.get("site_id") != site_id:
                continue
            decision_codes = set((decision.get("scope") or {}).get("product_codes") or [])
            if codes and decision_codes and not (codes & decision_codes):
                continue
            # 连续性检查只使用明确的生效区间；样例/历史导入若没有 effective_from，
            # 不凭 issued_at 猜测效期起点。
            start = decision.get("effective_from")
            items.append(
                {
                    "id": decision.get("id"),
                    "kind": "decision",
                    "valid_from": start,
                    "valid_to": decision.get("effective_to"),
                    "timeline_note": decision.get("timeline_note"),
                }
            )
        if candidate:
            items.append(
                {
                    "id": candidate.get("id"),
                    "kind": "candidate_decision",
                    "valid_from": candidate.get("effective_from"),
                    "valid_to": candidate.get("effective_to"),
                    "timeline_note": candidate.get("timeline_note"),
                }
            )
        chains = [items] if mixed_kinds else [
            [item for item in items if item.get("kind") == "certificate"],
            [item for item in items if item.get("kind") != "certificate"],
        ]
        results: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        try:
            for chain in chains:
                if not chain:
                    continue
                try:
                    results.extend(check_interval_chain(chain))
                except TimelineConflictError as exc:
                    if raise_on_conflict:
                        raise
                    results.extend(exc.conflicts)
                    conflicts.extend(exc.conflicts)
            if not conflicts:
                conflicts = [item for item in results if item.get("type") != "contiguous" and not item.get("explained")]
            explained = [item for item in results if item.get("explained")]
            return {"items": items, "results": results, "conflicts": conflicts, "explained": explained, "valid": not conflicts}
        except TimelineConflictError:
            raise

    # ------------------------------------------------------------------
    # 秘书处仪表盘和证据差异
    # ------------------------------------------------------------------
    def decision_evidence_diff(self, decision_id: str, baseline_id: str | None = None) -> dict[str, Any]:
        decision = self.get_decision(decision_id)
        if baseline_id is None:
            baseline_id = decision.get("parent_decision_id") or decision.get("supersedes_id")
        if baseline_id:
            baseline = self.get_decision(baseline_id)
            diff = self.diff_evidence_snapshots(baseline.get("evidence", []), decision.get("evidence", []))
            diff["baseline_decision_id"] = baseline_id
            diff["target_decision_id"] = decision_id
            return diff
        empty_diff = self.diff_evidence_snapshots([], decision.get("evidence", []))
        empty_diff["baseline_decision_id"] = None
        empty_diff["target_decision_id"] = decision_id
        return empty_diff

    def dashboard(self, as_of: Any = None, site_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        # 兼容 dashboard("S1") 与 dashboard(as_of="2026-...", site_id="S1")。
        if as_of is not None and site_id is None:
            try:
                parse_date(as_of, "as_of")
            except ValidationError:
                site_id = str(as_of)
                as_of = kwargs.get("as_of") or kwargs.get("date")
        as_of = as_of or kwargs.get("date") or kwargs.get("on_date")
        site_id = site_id or kwargs.get("site")
        day = parse_date(as_of, "as_of") if as_of else self.today()
        certs = self.current_certificates(day, site_id=site_id)
        decisions = self.store.all("decisions")
        if site_id:
            decisions = [d for d in decisions if d.get("site_id") == site_id]
        effective_decisions = []
        for decision in decisions:
            if decision.get("state") != str(DecisionState.ISSUED):
                continue
            start = parse_date(decision.get("effective_from")) or (parse_date(decision.get("issued_at", "")[:10]) if decision.get("issued_at") else None)
            end = parse_date(decision.get("effective_to"))
            if start and start <= day and (end is None or end >= day):
                effective_decisions.append(decision)
        evidence_diffs = []
        for decision in sorted(decisions, key=lambda d: d.get("created_at", "")):
            baseline_id = decision.get("parent_decision_id") or decision.get("supersedes_id")
            if decision.get("evidence_diff"):
                diff = decision["evidence_diff"]
            else:
                diff = self.decision_evidence_diff(decision["id"], baseline_id)
            evidence_diffs.append(
                {
                    "decision_id": decision["id"],
                    "state": decision.get("state"),
                    "site_id": decision.get("site_id"),
                    "parent_decision_id": baseline_id,
                    "baseline_decision_id": baseline_id,
                    "diff": diff,
                }
            )
        valid_certs = [cert for cert in certs if cert.get("date_valid")]
        return {
            "as_of": day.isoformat(),
            "site_id": site_id,
            # current_certifications 保留全部当前版本证明及 date_valid 标志；
            # valid_certifications 只列当日仍在有效期内的证明。
            "current_certifications": certs,
            "valid_certifications": valid_certs,
            "current_valid_certifications": valid_certs,
            "active_certificates": valid_certs,
            "effective_decisions": effective_decisions,
            "pending_materials": self.pending_materials(site_id),
            "review_cases": self.review_reasons(open_only=True),
            "rereview_reasons": [
                {"case_id": case["id"], "site_id": case.get("site_id"), "reasons": case.get("reasons"), "draft_decision_ids": case.get("draft_decision_ids", [])}
                for case in self.review_reasons(open_only=True)
            ],
            "evidence_diffs": evidence_diffs,
            "decisions": [
                {
                    "id": d.get("id"),
                    "site_id": d.get("site_id"),
                    "period": d.get("period"),
                    "state": d.get("state"),
                    "result": d.get("result"),
                    "evidence_refs": d.get("evidence_refs"),
                    "unassessed_count": len(d.get("unassessed") or []),
                    "supersedes_id": d.get("supersedes_id"),
                }
                for d in decisions
            ],
        }

    # ------------------------------------------------------------------
    # 常见命名别名，方便不同调用方发现接口
    # ------------------------------------------------------------------
    def check_timeline(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.timeline_report(*args, **kwargs)

    def validate_timeline(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.timeline_report(*args, **kwargs)

    def certificate_decision_continuity(
        self,
        certificate_id: str,
        decision_id_or_record: Any,
        *,
        timeline_note: str | None = None,
        raise_on_conflict: bool = True,
    ) -> dict[str, Any]:
        """只检查一张旧证明与一个新决定之间的空档/重叠。"""
        cert = self.store.find_one("certificates", certificate_id=str(certificate_id))
        if cert is None:
            raise NotFoundError(f"证明 {certificate_id} 不存在")
        decision = self.get_decision(decision_id_or_record) if isinstance(decision_id_or_record, str) else decision_id_or_record
        items = [
            {
                "id": cert.get("certificate_id"),
                "kind": "certificate",
                "valid_from": cert.get("valid_from"),
                "valid_to": cert.get("valid_to"),
                "timeline_note": cert.get("timeline_note"),
            },
            {
                "id": decision.get("id"),
                "kind": "decision",
                "valid_from": decision.get("effective_from"),
                "valid_to": decision.get("effective_to"),
                "timeline_note": timeline_note or decision.get("timeline_note"),
            },
        ]
        try:
            results = check_interval_chain(items)
            return {"valid": True, "results": results, "conflicts": []}
        except TimelineConflictError as exc:
            if raise_on_conflict and not timeline_note:
                raise
            if timeline_note:
                for conflict in exc.conflicts:
                    conflict["explained"] = True
                    conflict["explanation"] = timeline_note
                return {"valid": True, "results": exc.conflicts, "conflicts": [], "explained": exc.conflicts}
            return {"valid": False, "results": exc.conflicts, "conflicts": exc.conflicts}

    def get_dashboard(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.dashboard(*args, **kwargs)

    def evidence_diff(self, decision_id: str, baseline_id: str | None = None) -> dict[str, Any]:
        return self.decision_evidence_diff(decision_id, baseline_id)

    def list_current_certifications(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.current_certificates(*args, **kwargs)

    get_current_certificates = current_certificates
    get_current_certifications = current_certificates

    def valid_certificates(self, as_of: Any = None, *, site_id: str | None = None) -> list[dict[str, Any]]:
        return [cert for cert in self.current_certificates(as_of, site_id=site_id) if cert.get("date_valid")]

    current_valid_certificates = valid_certificates

    def unassessed_scopes(self, site_id: str, period: str | dict | None = None) -> list[dict[str, Any]]:
        return self.assess(site_id, period, create_issues=False).get("unassessed", [])

    def attachments_by_received_date(self, site_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.all("evidence_versions")
        if site_id:
            rows = [row for row in rows if row.get("site_id") == site_id]
        return sorted(rows, key=lambda row: row.get("received_at") or row.get("created_at") or "")

    def review_metric(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.accept_metric(*args, **kwargs)

    def confirm_decision(self, decision_id: str, reviewer: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """复审人员确认并签发新生效结论的语义别名。"""
        kwargs.setdefault("confirmed", True)
        return self.issue_decision(decision_id, reviewer, *args, **kwargs)

    def list_pending_materials(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.pending_materials(*args, **kwargs)

    get_pending_materials = pending_materials
    list_open_issues = pending_materials

    def list_review_reasons(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.review_reasons(*args, **kwargs)

    get_review_reasons = review_reasons

    def receive_public_factor_page(self, payload: dict[str, Any], **kwargs) -> IngestResult:
        return self.receive_factor(payload, **kwargs)

    def receive_authorization_result(self, payload: dict[str, Any], **kwargs) -> IngestResult:
        return self.receive_authorization(payload, **kwargs)

    # 常见 CRUD/工作流命名别名。
    submit_form = receive_portal_form
    add_portal_form = receive_portal_form
    add_attachment = receive_attachment_metadata
    add_attachment_metadata = receive_attachment_metadata
    add_factor = receive_factor
    add_conversion_factor = receive_factor
    verify_authorization_result = receive_authorization
    confirm_boundary_revision = confirm_boundary
    confirm_site_boundary = confirm_boundary
    confirm_product = confirm_product_scope
    confirm_certificate = confirm_certificate_mapping
    create_draft = create_decision_draft
    create_decision = create_decision_draft
    approve_decision = confirm_decision
    issue = issue_decision
