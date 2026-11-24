# 绿色制造申报证据采集与复审管理

一套仅依赖 Python 3.10+ 标准库的采集与材料管理应用。它接收企业门户表单、附件元数据、公开折算因子和授权验证结果的增量内容，保留重复提交、迟到修订、来源不可用、证据版本、联络员确认、评审判断、决定快照和复审差异。

## 快速运行

```bash
python3 -m unittest discover -s tests -v   # 运行测试
python3 demo.py                              # 跑完整内存工作流并输出 JSON 摘要

# 使用 JSON 文件仓储并导入仓库样例
python3 -m green_review --db /tmp/green-review.json init
python3 -m green_review --db /tmp/green-review.json import-samples
python3 -m green_review --db /tmp/green-review.json dashboard --as-of 2026-11-25 --text

# 启动标准库 HTTP 服务
python3 -m green_review --db /tmp/green-review.json serve --host 127.0.0.1 --port 8765
```

也可以直接运行根目录兼容入口：`python3 app.py dashboard --text`。

## 核心原则

1. **采集事件不可覆盖**：每次输入先写入 `events`，保存来源、来源记录号、接收时间、内容哈希和处理状态。
   - 相同来源记录 + 相同哈希：`duplicate`。
   - 相同来源记录 + 新内容/新版本：`revised`，证据生成新版本而不是覆盖旧版本。
   - 收到更旧的修订号：`out_of_order`，原文仍留存，但不会成为当前版本。
   - 结构或引用错误：`rejected`，错误和原始 payload 可追踪。
   - 外部页面/接口不可用：写入 `source_unavailable`，此前成功采集的数据保留。
2. **缺证据不等于零**：没有提交数值、数值为空或证据不存在时，指标和类别状态为 `unassessed`，并形成“未核定范围”。系统不会把缺失值改写成 `0`；企业明确提交的 `0` 会原样保留。
3. **联络员确认版本化对象**：厂区边界、产品范围、证明与产品/厂区的对应关系均记录确认人、时间和说明。新边界版本需要重新确认。
4. **已签发决定冻结证据**：决定中的 `evidence` 是包含 `evidence_id@version`、哈希、有效期和来源事件的快照。后续材料更正只会影响草案或生成复审草案，不会修改已签发决定引用的证据。
5. **复审确认后才生效**：变更影响分析可以自动列出未结事项、可能受影响的有效认证和证据差异，并创建草案；草案状态为 `draft/review`，只有评审人员调用签发并确认后才成为 `issued`。
6. **效期连续性可解释**：`timeline_report()` / `certificate_decision_continuity()` 检查空档和重叠。相邻日期不算空档；无法解释的空档/重叠抛出 `TimelineConflictError`，提供书面 `timeline_note` 后则作为“已解释异常”保留。
7. **秘书处视图完整**：`dashboard()` 提供当前有效证明、当日有效认证别名、待补材料、复审案件/原因、每份决定的证据差异和生效决定。

## Python API 工作流

```python
from green_review import GreenReviewApp

app = GreenReviewApp.open("/tmp/review.json")  # 或 GreenReviewApp.in_memory()

# 1. 接收门户表单；指标可引用表单自身证据或 attachments 中的证据
app.receive_portal_form({
    "submission_id": "FORM-001",
    "site_id": "S1",
    "products": ["P1"],
    "period": "2026-H1",
    "metrics": [
        {"code": "electricity", "value": 410000, "unit": "kWh"},
        {"code": "water_consumption", "value": 12000, "unit": "m3"},
        {"code": "waste_to_landfill", "value": 120, "unit": "t"},
        {"code": "equipment_modification", "observation": "变频改造验收单 EQ-1"},
    ],
})

# 2. 接收公开因子和第三方证明/授权验证
app.receive_factor({
    "factor_id": "grid_electricity", "version": 1,
    "value": "0.5703", "unit": "kgCO2e/kWh",
    "effective_from": "2026-01-01", "effective_to": "2026-12-31",
})
app.receive_attachment_metadata({
    "attachment_id": "CERT-DOC-1", "evidence_id": "CERT-1",
    "site_id": "S1", "kind": "certificate",
    "certificate_no": "CERT-001", "valid_from": "2025-01-01",
    "valid_to": "2026-12-31", "products": ["P1"], "sha256": "...",
})
app.receive_authorization({
    "certificate_no": "CERT-001", "status": "verified",
})

# 3. 联络员确认边界、产品范围和证明映射
app.confirm_boundary("S1", 1, "联络员甲")
scope = app.store.find_one("product_scopes", site_id="S1")
app.confirm_product_scope(scope["id"], "联络员甲")
app.confirm_certificate_mapping("CERT-CERT-001", "联络员甲")

# 4. 评审人员确认指标口径；缺项会出现在 assessment["unassessed"]
for metric in app.current_metrics("S1", "2026-H1"):
    app.accept_metric(metric["metric_id"], "评审乙", caliber_note="口径与 R1 边界一致")

assessment = app.assess("S1", "2026-H1")
print(assessment["unassessed"], assessment["completeness"])

# 5. 草案 -> 评审 -> 签发；pass 会强制检查未核定范围、指标确认和证明有效性
app.create_decision_draft("S1", "2026-H1", decision_id="D1", result="pass")
app.issue_decision("D1", "评审乙", effective_from="2026-07-01")

# 6. 扩建边界图或因子更正到达后，自动影响分析并生成/更新复审草案
result = app.receive_attachment_metadata({
    "attachment_id": "EXPANSION-MAP", "site_id": "S1",
    "kind": "boundary_map", "revision": 2, "site_revision": 2,
    "valid_from": "2026-08-01", "area_m2": 22000,
    "sha256": "new-map-hash", "change_summary": "二号厂房扩建",
})
impact = result.impacts[0]
print(impact["open_issue_ids"], impact["affected_valid_certificate_ids"], impact["draft_decision_ids"])

# 7. 秘书处视图和证据差异
dashboard = app.dashboard(as_of="2026-11-25")
print(dashboard["valid_certifications"])
print(dashboard["pending_materials"])
print(dashboard["rereview_reasons"])
print(app.decision_evidence_diff(impact["draft_decision_ids"][0]))
```

## 外部来源不可用与可注入采集器

```python
from green_review import SourceUnavailableError

def fetcher(url):
    raise SourceUnavailableError("HTTP 503", source=url)

result = app.collect_public_factor(factor_id="grid_electricity", fetcher=fetcher)
assert result.status == "source_unavailable"

app.verify_authorization("CERT-001", client=lambda url: (_ for _ in ()).throw(
    SourceUnavailableError("timeout", source=url)
))
```

内置 `green_review.sources.http_get_text/http_get_json` 使用 `urllib`，并支持 JSON、CSV、内嵌 JSON 或 `data-factor-*` HTML 因子页。生产环境可把 `fetcher/client` 替换为组织已有的 HTTP 客户端。

## HTTP 接口

服务启动后：

- `GET /health`
- `GET /api/dashboard?as_of=2026-11-25&site=S1`
- `GET /api/assess?site=S1&period=2026-H1`
- `GET /api/decisions/D1`
- `POST /api/events`，body 为 `{"source": "portal|attachment|factor|authorization", "payload": {...}}`
- `POST /api/portal`
- `POST /api/attachments`
- `POST /api/factors`
- `POST /api/authorizations`

示例：

```bash
curl -sS http://127.0.0.1:8765/api/dashboard | python3 -m json.tool
```

## 持久化结构

`GreenReviewApp.open("review.json")` 使用原子替换写入 JSON，内部集合包括：

- `events`：所有增量采集事件和失败/重复/拒绝记录。
- `evidence_versions`：不可覆盖的证据版本及哈希。
- `sites`：厂区边界版本。
- `product_scopes`：适用产品范围版本。
- `metric_versions`：指标读数、证据引用、口径确认状态和因子引用。
- `factors`：公开折算因子版本及更正关系。
- `certificates` / `verifications`：第三方证明、适用产品、效期和授权验证历史。
- `confirmations`：联络员确认记录。
- `issues`：待补材料和未结评审事项。
- `cases` / `impacts`：复审原因、影响范围和草案关联。
- `decisions`：草案、评审中、已签发和已替代决定，以及冻结证据快照。

## 仓库样例文件

合成绿色制造申报范围、能源证据及评审决定样本。金额为人民币元，生效日期为本地日期。所有示例标识符和数值均为合成数据。CSV 使用 UTF-8，首行为列名，空单元格表示来源未提供值。

- `samples/sites.csv`：企业提交的厂区边界和版本。列为 `site, company, area_m2, valid_from, revision`。
- `samples/evidence.csv`：能源、用水和第三方证明的有效期间及范围。列为 `evidence, site, kind, value, unit, from, to, revision`。
- `samples/decisions.csv`：已签发的评审结论及覆盖版本。列为 `decision, site, period, result, site_revision, evidence_refs, state`。

样例刻意包含：S1 在 2026-08-01 从边界 R1 扩建到 R2、H2 期间跨越边界版本、第三方证明 E3 在 2026-07-31 到期、D1 冻结引用 `E1@v1 + E3@v1`、D2/D3 为未完成草案。现有 `tests/test_sample_inputs.py` 继续验证 CSV 表头和引用完整性；新增工作流测试见 `tests/test_green_review_app.py`。

## 设计边界

本应用提供可运行的领域模型、JSON 仓储、CLI、HTTP 接收端和标准库采集适配器；没有内置真实企业门户鉴权或特定政府因子网站的页面结构。接入真实来源时，应在 `green_review/sources.py` 或外部适配器中完成页面解析，并继续通过 `GreenReviewApp.ingest()` 写入，以保留同一套幂等、版本和审计规则。
