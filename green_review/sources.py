"""外部来源采集辅助：可注入 fetcher，也提供标准库 HTTP/HTML 解析。"""

from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .errors import SourceUnavailableError
from .utils import content_hash


def http_get_text(url: str, *, timeout: float = 5.0, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": "green-review-collector/1.0", "Accept": "application/json,text/csv,text/html;q=0.9,*/*;q=0.8"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 用户配置的采集地址
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceUnavailableError(f"来源暂时不可用: {url}: {exc}", source=url) from exc


def http_get_json(url: str, **kwargs) -> dict[str, Any]:
    text = http_get_text(url, **kwargs)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceUnavailableError(f"授权接口未返回合法 JSON: {url}", source=url) from exc
    if not isinstance(result, dict):
        raise SourceUnavailableError(f"授权接口返回不是 JSON 对象: {url}", source=url)
    return result


def http_post_json(url: str, payload: dict[str, Any], *, timeout: float = 5.0, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "User-Agent": "green-review-collector/1.0",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            text = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceUnavailableError(f"授权验证接口暂时不可用: {url}: {exc}", source=url) from exc
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceUnavailableError(f"授权验证接口未返回合法 JSON: {url}", source=url) from exc
    if not isinstance(result, dict):
        raise SourceUnavailableError(f"授权验证接口返回不是 JSON 对象: {url}", source=url)
    return result


class _FactorPageParser(HTMLParser):
    """识别内嵌 JSON、``data-factor-*`` 元素或简单两列表格。"""

    def __init__(self) -> None:
        super().__init__()
        self.json_blocks: list[str] = []
        self._in_script_json = False
        self._script_buffer: list[str] = []
        self.factor_attributes: dict[str, str] = {}
        self.table_rows: list[list[str]] = []
        self._in_td = False
        self._current_row: list[str] = []
        self._cell_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("type") == "application/json":
            self._in_script_json = True
            self._script_buffer = []
        if tag in {"div", "span", "data"} and attributes.get("data-factor-id"):
            self.factor_attributes = {
                "factor_id": attributes.get("data-factor-id") or "",
                "value": attributes.get("data-factor-value") or attributes.get("data-value") or "",
                "unit": attributes.get("data-factor-unit") or attributes.get("data-unit") or "",
                "version": attributes.get("data-factor-version") or attributes.get("data-version") or "",
                "effective_from": attributes.get("data-effective-from") or "",
                "effective_to": attributes.get("data-effective-to") or "",
            }
        if tag == "tr":
            self._current_row = []
        if tag in {"td", "th"}:
            self._in_td = True
            self._cell_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script_json:
            self.json_blocks.append("".join(self._script_buffer))
            self._in_script_json = False
        if tag in {"td", "th"} and self._in_td:
            self._current_row.append("".join(self._cell_buffer).strip())
            self._in_td = False
        if tag == "tr" and self._current_row:
            self.table_rows.append(self._current_row)

    def handle_data(self, data: str) -> None:
        if self._in_script_json:
            self._script_buffer.append(data)
        if self._in_td:
            self._cell_buffer.append(data)


def parse_factor_content(raw: Any, *, url: str | None = None, factor_id: str | None = None) -> dict[str, Any]:
    """把 dict/JSON/CSV/HTML 因子来源归一化为因子 payload。"""
    if isinstance(raw, dict):
        payload = dict(raw)
    elif isinstance(raw, (bytes, bytearray)):
        return parse_factor_content(raw.decode("utf-8", errors="replace"), url=url, factor_id=factor_id)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise SourceUnavailableError("因子页面内容为空", source=url)
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SourceUnavailableError("因子页面 JSON 无法解析", source=url) from exc
        elif text.startswith("["):
            try:
                rows = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SourceUnavailableError("因子页面 JSON 数组无法解析", source=url) from exc
            if not rows or not isinstance(rows[0], dict):
                raise SourceUnavailableError("因子 JSON 数组为空或结构不受支持", source=url)
            if factor_id:
                rows = [row for row in rows if str(row.get("factor_id") or row.get("code") or row.get("id")) == factor_id] or rows
            payload = dict(rows[0])
        elif "," in text.splitlines()[0]:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if not rows:
                raise SourceUnavailableError("因子 CSV 没有数据行", source=url)
            payload = dict(rows[0])
        else:
            parser = _FactorPageParser()
            parser.feed(text)
            payload = _factor_from_html(parser, factor_id)
    else:
        raise SourceUnavailableError(f"不支持的因子来源类型 {type(raw)!r}", source=url)

    if not isinstance(payload, dict):
        raise SourceUnavailableError("因子来源未形成对象", source=url)
    if factor_id:
        payload.setdefault("factor_id", factor_id)
    if url:
        payload.setdefault("source_url", url)
    payload.setdefault("retrieved_content_hash", content_hash(raw if isinstance(raw, str) else payload))
    return payload


def _factor_from_html(parser: _FactorPageParser, factor_id: str | None) -> dict[str, Any]:
    for block in parser.json_blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (data.get("value") is not None or data.get("factor_value") is not None):
            return data
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and (not factor_id or str(row.get("factor_id") or row.get("code")) == factor_id):
                    return row
    attrs = {key: value for key, value in parser.factor_attributes.items() if value}
    if attrs.get("value"):
        return attrs
    for row in parser.table_rows:
        joined = " | ".join(row)
        if factor_id and factor_id not in joined:
            continue
        # 常见两列：字段名/值；或一行：factor_id,value,unit,version。
        if len(row) >= 2 and re.search(r"\d", joined):
            if len(row) >= 3 and re.fullmatch(r"[A-Za-z0-9_.:-]+", row[0] or ""):
                return {"factor_id": row[0], "value": row[1], "unit": row[2] if len(row) > 2 else None}
            data = {}
            for index in range(0, len(row) - 1, 2):
                key = row[index].strip().lower().replace(" ", "_")
                data[key] = row[index + 1]
            if data.get("value"):
                return data
    raise SourceUnavailableError("无法从 HTML 因子页面识别因子数据", source=None)
