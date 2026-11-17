"""CSV 解析与纯函数校验。

所有解析/校验都不触碰数据库：失败时直接抛 CsvError，调用方据此整次拒收，
已提交的层级与读数保持原样（"失败后旧结果仍可用"）。
"""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone


class CsvError(ValueError):
    """CSV 内容不合法，整次拒收。"""


# ---- 表头别名（数据员可能用英文或中文表头） ----
_PARENT_HEADERS = {"parent_meter", "parent", "上级表", "上级表号"}
_CHILD_HEADERS = {"child_meter", "child", "下级表", "下级表号"}
_METER_HEADERS = {"meter_id", "meter", "表号"}
_TS_HEADERS = {"ts_utc", "timestamp", "utc整点", "时间", "时间戳"}
_KWH_HEADERS = {"kwh", "kwh读数", "读数", "电量读数"}

TS_FORMAT = "%Y-%m-%dT%H:00:00Z"


def _rows_from_text(text: str) -> list[list[str]]:
    # utf-8-sig 去掉 Excel 导出的 BOM
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    return [[(cell or "").strip() for cell in row] for row in reader]


def _header_index(header: list[str], aliases: set[str], kind: str) -> int:
    for i, name in enumerate(header):
        if name.lower() in aliases:
            return i
    raise CsvError(
        f"{kind} CSV 缺少表头列 {sorted(aliases)}，实际表头: {header}"
    )


def parse_utc_hour(raw: str, lineno: int) -> str:
    """把输入时间戳规范化为 'YYYY-MM-DDTHH:00:00Z'。

    只接受显式 UTC（Z 或 +00:00）且分/秒必须为 0，否则整批拒收，
    避免时区歧义。
    """
    s = raw.strip()
    if not s:
        raise CsvError(f"第 {lineno} 行: 时间为空")
    normalized = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise CsvError(f"第 {lineno} 行: 时间格式无法解析: {raw!r}")
    if dt.tzinfo is None:
        raise CsvError(f"第 {lineno} 行: 时间必须带 UTC 时区标记（Z 或 +00:00）: {raw!r}")
    if dt.utcoffset() != timezone.utc.utcoffset(None):
        raise CsvError(f"第 {lineno} 行: 只接受 UTC 时间，收到: {raw!r}")
    if dt.minute or dt.second or dt.microsecond:
        raise CsvError(f"第 {lineno} 行: 必须是 UTC 整点（分秒为 0）: {raw!r}")
    return dt.strftime(TS_FORMAT)


def parse_hierarchy_csv(text: str) -> list[tuple[str, str]]:
    """解析整版层级 CSV，返回去重后的 (parent, child) 边列表。

    拒收条件：缺表头、空值、自环、一表多上级、成环。
    """
    rows = _rows_from_text(text)
    if not rows:
        raise CsvError("CSV 为空")
    header = rows[0]
    pi = _header_index(header, _PARENT_HEADERS, "层级")
    ci = _header_index(header, _CHILD_HEADERS, "层级")

    child_to_parent: dict[str, str] = {}
    for lineno, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue  # 跳过空行
        if len(row) <= max(pi, ci):
            raise CsvError(f"第 {lineno} 行: 列数不足")
        parent, child = row[pi], row[ci]
        if not parent or not child:
            raise CsvError(f"第 {lineno} 行: 上级表/下级表不能为空")
        if parent == child:
            raise CsvError(f"第 {lineno} 行: 表 {child!r} 的上级不能是自己（自环）")
        existing = child_to_parent.get(child)
        if existing is not None and existing != parent:
            raise CsvError(
                f"一表有多个上级: 表 {child!r} 同时挂在 {existing!r}（已有）"
                f"和 {parent!r}（第 {lineno} 行）下"
            )
        child_to_parent[child] = parent

    if not child_to_parent:
        raise CsvError("CSV 没有任何层级数据行")

    _detect_cycle(child_to_parent)
    return sorted((parent, child) for child, parent in child_to_parent.items())


def _detect_cycle(child_to_parent: dict[str, str]) -> None:
    """沿 child -> parent 链检测环。每只表至多一个上级，故链式走访即可。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    for start in child_to_parent:
        if color.get(start, WHITE) != WHITE:
            continue
        node, path = start, []
        while node in child_to_parent and color.get(node, WHITE) == WHITE:
            color[node] = GRAY
            path.append(node)
            node = child_to_parent[node]
        if node in child_to_parent and color.get(node) == GRAY:
            cycle = path[path.index(node):] + [node]
            raise CsvError("层级成环: " + " -> ".join(cycle))
        for n in path:
            color[n] = BLACK


def parse_readings_csv(text: str) -> list[tuple[str, str, float]]:
    """解析一批读数 CSV，返回去重后的 (meter_id, ts_utc, kwh)。

    批内同键同值去重；同键异值整批拒收。
    """
    rows = _rows_from_text(text)
    if not rows:
        raise CsvError("CSV 为空")
    header = rows[0]
    mi = _header_index(header, _METER_HEADERS, "读数")
    ti = _header_index(header, _TS_HEADERS, "读数")
    ki = _header_index(header, _KWH_HEADERS, "读数")

    seen: dict[tuple[str, str], tuple[float, int]] = {}
    out: list[tuple[str, str, float]] = []
    for lineno, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        if len(row) <= max(mi, ti, ki):
            raise CsvError(f"第 {lineno} 行: 列数不足")
        meter = row[mi]
        if not meter:
            raise CsvError(f"第 {lineno} 行: 表号为空")
        ts = parse_utc_hour(row[ti], lineno)
        raw_kwh = row[ki]
        try:
            kwh = float(raw_kwh)
        except ValueError:
            raise CsvError(f"第 {lineno} 行: kWh 读数不是数值: {raw_kwh!r}")
        if not math.isfinite(kwh):
            raise CsvError(f"第 {lineno} 行: kWh 读数非法（NaN/Inf）: {raw_kwh!r}")

        key = (meter, ts)
        if key in seen:
            prev_kwh, prev_lineno = seen[key]
            if prev_kwh != kwh:
                raise CsvError(
                    f"一批内同键异值: 表 {meter!r} 在 {ts} 第 {prev_lineno} 行为 "
                    f"{prev_kwh}，第 {lineno} 行为 {kwh}，整批拒收"
                )
            continue  # 同键同值：幂等去重
        seen[key] = (kwh, lineno)
        out.append((meter, ts, kwh))

    if not out:
        raise CsvError("CSV 没有任何读数数据行")
    return out
