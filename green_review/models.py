"""枚举和指标目录。

记录主体在当前版本中以可 JSON 持久化的字典保存；枚举集中约束状态字符串，
同时兼容样例 CSV 中已有的 ``draft/review/issued`` 等值。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - 简单格式
        return self.value


class IngestStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    REVISED = "revised"
    OUT_OF_ORDER = "out_of_order"
    REJECTED = "rejected"
    SOURCE_UNAVAILABLE = "source_unavailable"


class EvidenceStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    IMPORTED = "imported"


class IssueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    WAIVED = "waived"


class IssueSeverity(StrEnum):
    INFO = "info"
    CONDITION = "condition"
    BLOCKER = "blocker"


class DecisionState(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    ISSUED = "issued"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class Conclusion(StrEnum):
    PASS = "pass"
    CONDITIONAL = "conditional"
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    PENDING = "pending"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"
    INVALID = "invalid"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


class ReviewCaseStatus(StrEnum):
    OPEN = "open"
    DRAFT_CREATED = "draft_created"
    CONFIRMED = "confirmed"
    CLOSED = "closed"


SOURCE_PORTAL = "portal_form"
SOURCE_ATTACHMENT = "attachment_metadata"
SOURCE_FACTOR = "public_factor"
SOURCE_AUTH = "authorization_verification"
SOURCE_SAMPLE = "sample_csv"

SOURCE_ALIASES = {
    "portal": SOURCE_PORTAL,
    "portal_form": SOURCE_PORTAL,
    "form": SOURCE_PORTAL,
    "enterprise_portal": SOURCE_PORTAL,
    "attachment": SOURCE_ATTACHMENT,
    "attachments": SOURCE_ATTACHMENT,
    "attachment_metadata": SOURCE_ATTACHMENT,
    "factor": SOURCE_FACTOR,
    "factors": SOURCE_FACTOR,
    "public_factor": SOURCE_FACTOR,
    "public_factors": SOURCE_FACTOR,
    "public_factor_page": SOURCE_FACTOR,
    "authorization": SOURCE_AUTH,
    "auth": SOURCE_AUTH,
    "auth_verification": SOURCE_AUTH,
    "authorization_verification": SOURCE_AUTH,
    "sample_csv": SOURCE_SAMPLE,
    "sample": SOURCE_SAMPLE,
}

# 指标类别和常见别名。别名同时接受中文键，方便门户表单直接映射。
METRIC_ALIASES = {
    "energy": "energy_consumption",
    "energy_consumption": "energy_consumption",
    "energy use": "energy_consumption",
    "能耗": "energy_consumption",
    "能源消耗": "energy_consumption",
    "electricity": "electricity",
    "electricity_consumption": "electricity",
    "power": "electricity",
    "用电": "electricity",
    "用电量": "electricity",
    "water": "water_consumption",
    "water_consumption": "water_consumption",
    "water_use": "water_consumption",
    "用水": "water_consumption",
    "用水量": "water_consumption",
    "water_intake": "water_consumption",
    "waste": "waste_destination",
    "waste_destination": "waste_destination",
    "waste_disposal": "waste_destination",
    "废料去向": "waste_destination",
    "废弃物去向": "waste_destination",
    "waste_to_landfill": "waste_to_landfill",
    "landfill": "waste_to_landfill",
    "填埋": "waste_to_landfill",
    "waste_recycled": "waste_recycled",
    "recycling": "waste_recycled",
    "回收": "waste_recycled",
    "equipment": "equipment_modification",
    "equipment_modification": "equipment_modification",
    "equipment_upgrade": "equipment_modification",
    "equipment_retrofit": "equipment_modification",
    "设备改造": "equipment_modification",
    "改造记录": "equipment_modification",
}

# 每个评审期间至少要能显示一个已核定指标/记录的类别。
REQUIRED_CATEGORIES = ("energy", "water", "waste", "equipment")

METRIC_SPECS: dict[str, dict] = {
    "energy_consumption": {
        "category": "energy",
        "label": "综合能耗",
        "unit": None,
        "factor_id": None,
        "numeric": True,
    },
    "electricity": {
        "category": "energy",
        "label": "电力消耗",
        "unit": "kWh",
        "factor_id": "grid_electricity",
        "numeric": True,
    },
    "water_consumption": {
        "category": "water",
        "label": "用水量",
        "unit": "m3",
        "factor_id": "water_supply",
        "numeric": True,
    },
    "waste_destination": {
        "category": "waste",
        "label": "废料去向记录",
        "unit": None,
        "factor_id": None,
        "numeric": False,
    },
    "waste_to_landfill": {
        "category": "waste",
        "label": "填埋废料",
        "unit": "t",
        "factor_id": "landfill_waste",
        "numeric": True,
    },
    "waste_recycled": {
        "category": "waste",
        "label": "回收/利用废料",
        "unit": "t",
        "factor_id": None,
        "numeric": True,
    },
    "equipment_modification": {
        "category": "equipment",
        "label": "设备改造记录",
        "unit": None,
        "factor_id": None,
        "numeric": False,
    },
}

CATEGORY_LABELS = {
    "energy": "能耗",
    "water": "用水",
    "waste": "废料去向",
    "equipment": "设备改造",
}


def normalize_metric_code(code: str | None) -> str:
    if code is None:
        raise ValueError("metric code is required")
    key = str(code).strip().lower().replace("-", "_").replace(" ", "_")
    if key in METRIC_ALIASES:
        return METRIC_ALIASES[key]
    # 中文别名和保留原键
    raw = str(code).strip()
    if raw in METRIC_ALIASES:
        return METRIC_ALIASES[raw]
    return key
