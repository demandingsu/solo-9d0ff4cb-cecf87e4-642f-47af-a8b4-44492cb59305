"""园区公共用电逐小时净电量 API。

- POST /api/hierarchy            上传整版电表层级 CSV（上级表, 下级表），整版替换
- POST /api/readings             按批补传累计电量 CSV（表号, UTC整点, kWh读数）
- POST /api/meters/{id}/wrap-confirmations   登记回绕确认（UTC 小时 + 计数周期 M）
- GET  /api/wrap-confirmations   查看当前生效的回绕确认（可按表号过滤）
- GET  /api/meters/{id}/daily-net?date=YYYY-MM-DD   查询某 UTC 日逐小时净电量
- GET  /api/hierarchy            查看当前已生效层级（便于演示核对）
- GET  /api/batches              查看批次审计（含被拒批次及原因）
- GET  /api/health

一致性约定：每个请求一个事务；校验全部通过后才落库，任一步失败即回滚，
已提交的旧层级/旧读数/旧确认/旧查询结果不受影响。
"""
from __future__ import annotations

import math
import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from . import core, db
from .parsing import CsvError, parse_hierarchy_csv, parse_readings_csv, parse_utc_hour

DEFAULT_DB_PATH = os.environ.get(
    "ENERGY_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "energy.db")
)


# ---------- 响应模型（让 /docs 自描述） ----------

class BatchResult(BaseModel):
    batch_id: str
    status: str
    kind: str
    rows_applied: int
    message: str


class HourResult(BaseModel):
    hour_start: str
    status: str  # "ok" | "pending_review"
    net_kwh: Optional[float] = None
    master_delta_kwh: Optional[float] = None
    children_delta_kwh: Optional[float] = None
    review_reason: Optional[str] = None
    wrap_adjusted_meters: list[str] = []  # 本小时按回绕确认折算差值的表


class DailyNetResult(BaseModel):
    meter_id: str
    date: str
    child_meters: list[str]
    hours: list[HourResult]


class WrapConfirmationRequest(BaseModel):
    hour_start: str  # 该小时的起始 UTC 整点，如 2026-01-15T02:00:00Z
    modulus: float   # 计数周期 M（计数器量程，如 1000 表示 999 之后回到 0）


class WrapConfirmationResult(BaseModel):
    meter_id: str
    hour_start: str
    hour_end: str
    modulus: float
    start_kwh: float
    end_kwh: float
    adjusted_delta_kwh: float  # 确认后该小时差值 = M - 起值 + 终值
    status: str                # "registered" | "updated"
    message: str


class WrapConfirmationRecord(BaseModel):
    meter_id: str
    hour_start: str
    modulus: float
    start_kwh: float
    end_kwh: float
    created_at: str


class HierarchyEdge(BaseModel):
    parent_meter: str
    child_meter: str


class BatchRecord(BaseModel):
    batch_id: str
    kind: str
    status: str
    reason: Optional[str]
    row_count: int
    created_at: str


# ---------- 应用工厂 ----------

def create_app(db_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(
        title="园区公共用电逐小时净电量 API",
        description=(
            "上传整版电表层级与按批补传累计电量，查询指定总表某 UTC 日的逐小时净电量。"
            "每小时 = 总表相邻整点差值 - 直接下级表差值之和（孙表不再扣）。"
            "计数器回绕（如 M=1000 时 999→3）可按表号和 UTC 小时登记确认，"
            "确认后该小时差值按 M - 起值 + 终值 折算；"
            "读数被真正更正时依赖该点的确认自动失效，须按当前读数重新登记。"
        ),
        version="1.1.0",
    )
    app.state.db_path = os.path.abspath(db_path or DEFAULT_DB_PATH)
    db.init_db(app.state.db_path)

    def get_conn(request: Request):
        conn = db.connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _record_rejected(conn, kind: str, reason: str) -> None:
        """拒收批次留痕。独立短事务，不影响主数据。"""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, ?, 'rejected', ?, 0, ?)",
                (str(uuid.uuid4()), kind, reason, _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")

    # ---------- 上传：整版层级 ----------

    @app.post("/api/hierarchy", response_model=BatchResult, summary="上传整版电表层级 CSV（整版替换）")
    async def upload_hierarchy(file: UploadFile = File(...), conn=Depends(get_conn)):
        text = await _read_csv_text(file)
        try:
            edges = parse_hierarchy_csv(text)  # 纯函数校验：成环/多上级/自环在此拒收
        except CsvError as exc:
            _record_rejected(conn, "hierarchy", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM hierarchy")  # 整版替换
            conn.executemany(
                "INSERT INTO hierarchy (child_meter, parent_meter) VALUES (?, ?)",
                [(child, parent) for parent, child in edges],
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'hierarchy', 'applied', NULL, ?, ?)",
                (batch_id, len(edges), _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return BatchResult(
            batch_id=batch_id, status="applied", kind="hierarchy",
            rows_applied=len(edges),
            message=f"层级已整版替换，生效 {len(edges)} 条上下级关系",
        )

    # ---------- 上传：一批读数 ----------

    @app.post("/api/readings", response_model=BatchResult, summary="按批补传累计电量 CSV")
    async def upload_readings(file: UploadFile = File(...), conn=Depends(get_conn)):
        text = await _read_csv_text(file)
        try:
            rows = parse_readings_csv(text)  # 批内同键异值在此整批拒收
        except CsvError as exc:
            _record_rejected(conn, "readings", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        now = _utcnow()
        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 找出"真正更正"的整点：键已存在且新值不同（同值重传不算更正）
            changed_points: list[tuple[str, str]] = []
            for m, ts, kwh in rows:
                old = conn.execute(
                    "SELECT kwh FROM readings WHERE meter_id = ? AND ts_utc = ?",
                    (m, ts),
                ).fetchone()
                if old is not None and old["kwh"] != kwh:
                    changed_points.append((m, ts))

            # 同键异值 = 更正（覆盖）；同值重传 = 幂等（WHERE 条件使其不变）
            conn.executemany(
                """
                INSERT INTO readings (meter_id, ts_utc, kwh, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (meter_id, ts_utc) DO UPDATE SET
                    kwh = excluded.kwh, updated_at = excluded.updated_at
                WHERE readings.kwh <> excluded.kwh
                """,
                [(m, ts, kwh, now) for m, ts, kwh in rows],
            )

            # 真正更正任一整点 -> 依赖该点的回绕确认全部失效。
            # 确认 (meter, hour_start) 依赖 hour_start 与 hour_start+1h 两个整点，
            # 故被更正的 ts 既是某些确认的起点、也是另一些确认（hour_start = ts-1h）的终点。
            # 物理删除而非标记：之后即使读数改回原值，旧确认也不能复活，须重新登记。
            for m, ts in changed_points:
                conn.execute(
                    "DELETE FROM wrap_confirmations"
                    " WHERE meter_id = ? AND hour_start IN (?, ?)",
                    (m, ts, core.shift_hour_str(ts, -1)),
                )

            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'readings', 'applied', NULL, ?, ?)",
                (batch_id, len(rows), now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return BatchResult(
            batch_id=batch_id, status="applied", kind="readings",
            rows_applied=len(rows),
            message=f"读数批次已提交，{len(rows)} 个（表号, 整点）键已写入/更正",
        )

    # ---------- 查询：逐小时净电量 ----------

    @app.get(
        "/api/meters/{meter_id}/daily-net",
        response_model=DailyNetResult,
        summary="查询指定总表某 UTC 日的逐小时净电量",
    )
    def daily_net(
        meter_id: str,
        date_: str = Query(..., alias="date", description="UTC 日期，格式 YYYY-MM-DD"),
        conn=Depends(get_conn),
    ):
        try:
            day = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"日期格式应为 YYYY-MM-DD: {date_!r}")

        # 单事务快照：层级、读数与回绕确认取自同一已提交状态
        conn.execute("BEGIN")
        try:
            children = [
                r["child_meter"]
                for r in conn.execute(
                    "SELECT child_meter FROM hierarchy WHERE parent_meter = ?"
                    " ORDER BY child_meter",
                    (meter_id,),
                )
            ]
            participants = [meter_id, *children]
            placeholders = ",".join("?" for _ in participants)
            t_start, t_end = core.hour_str(day, 0), core.hour_str(day, 24)
            rows = conn.execute(
                f"SELECT meter_id, ts_utc, kwh FROM readings"
                f" WHERE meter_id IN ({placeholders}) AND ts_utc BETWEEN ? AND ?",
                (*participants, t_start, t_end),
            ).fetchall()
            readings = {(r["meter_id"], r["ts_utc"]): r["kwh"] for r in rows}
            conf_rows = conn.execute(
                f"SELECT meter_id, hour_start, modulus FROM wrap_confirmations"
                f" WHERE meter_id IN ({placeholders}) AND hour_start >= ? AND hour_start < ?",
                (*participants, t_start, t_end),
            ).fetchall()
            confirmations = {
                (r["meter_id"], r["hour_start"]): r["modulus"] for r in conf_rows
            }
            hours = core.compute_daily_net(meter_id, day, children, readings, confirmations)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return DailyNetResult(
            meter_id=meter_id, date=day.isoformat(), child_meters=children, hours=hours
        )

    # ---------- 回绕确认 ----------

    @app.post(
        "/api/meters/{meter_id}/wrap-confirmations",
        response_model=WrapConfirmationResult,
        summary="登记回绕确认（按表号和 UTC 小时，附计数周期 M）",
    )
    def register_wrap_confirmation(
        meter_id: str, req: WrapConfirmationRequest, conn=Depends(get_conn)
    ):
        try:
            hour_start = parse_utc_hour(req.hour_start, "hour_start")
        except CsvError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if not math.isfinite(req.modulus) or req.modulus <= 0:
            raise HTTPException(
                status_code=422, detail=f"计数周期 M 必须为正有限数: {req.modulus!r}"
            )
        hour_end = core.shift_hour_str(hour_start, 1)

        # 单事务：校验所依据的读数与写入的确认来自同一已提交状态；
        # 校验不过即回滚，不留下部分生效数据。
        conn.execute("BEGIN IMMEDIATE")
        try:
            have = {
                r["ts_utc"]: r["kwh"]
                for r in conn.execute(
                    "SELECT ts_utc, kwh FROM readings"
                    " WHERE meter_id = ? AND ts_utc IN (?, ?)",
                    (meter_id, hour_start, hour_end),
                )
            }
            missing = [t for t in (hour_start, hour_end) if t not in have]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"该小时起止读数不齐全: 表 {meter_id!r} 缺 {', '.join(missing)}",
                )
            start_kwh, end_kwh = have[hour_start], have[hour_end]
            if not (0.0 <= start_kwh < req.modulus and 0.0 <= end_kwh < req.modulus):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"起止读数须都在 [0, M) 内: 起值 {start_kwh}, 终值 {end_kwh},"
                        f" M {req.modulus}"
                    ),
                )
            if not end_kwh < start_kwh:
                raise HTTPException(
                    status_code=422,
                    detail=f"终值须小于起值才构成回绕: 起值 {start_kwh}, 终值 {end_kwh}",
                )

            replaced = (
                conn.execute(
                    "SELECT 1 FROM wrap_confirmations WHERE meter_id = ? AND hour_start = ?",
                    (meter_id, hour_start),
                ).fetchone()
                is not None
            )
            # 重复登记 = 按当前读数重新确认（新确认须依据当前读数）
            conn.execute(
                """
                INSERT INTO wrap_confirmations
                    (meter_id, hour_start, modulus, start_kwh, end_kwh, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (meter_id, hour_start) DO UPDATE SET
                    modulus = excluded.modulus, start_kwh = excluded.start_kwh,
                    end_kwh = excluded.end_kwh, created_at = excluded.created_at
                """,
                (meter_id, hour_start, req.modulus, start_kwh, end_kwh, _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        adjusted = req.modulus - start_kwh + end_kwh
        return WrapConfirmationResult(
            meter_id=meter_id,
            hour_start=hour_start,
            hour_end=hour_end,
            modulus=req.modulus,
            start_kwh=start_kwh,
            end_kwh=end_kwh,
            adjusted_delta_kwh=adjusted,
            status="updated" if replaced else "registered",
            message=(
                f"回绕确认已{'更新' if replaced else '登记'}: 表 {meter_id} "
                f"{hour_start}~{hour_end} 差值按 M - 起值 + 终值 = {adjusted} kWh 折算"
            ),
        )

    @app.get(
        "/api/wrap-confirmations",
        response_model=list[WrapConfirmationRecord],
        summary="查看当前生效的回绕确认（可按表号过滤）",
    )
    def list_wrap_confirmations(
        meter_id: Optional[str] = Query(None, description="按表号过滤"), conn=Depends(get_conn)
    ):
        conn.execute("BEGIN")
        try:
            if meter_id is None:
                rows = conn.execute(
                    "SELECT * FROM wrap_confirmations ORDER BY meter_id, hour_start"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wrap_confirmations WHERE meter_id = ?"
                    " ORDER BY meter_id, hour_start",
                    (meter_id,),
                ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [WrapConfirmationRecord(**dict(r)) for r in rows]

    # ---------- 辅助端点 ----------

    @app.get("/api/hierarchy", response_model=list[HierarchyEdge], summary="查看当前已生效层级")
    def get_hierarchy(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT parent_meter, child_meter FROM hierarchy"
                " ORDER BY parent_meter, child_meter"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [HierarchyEdge(**dict(r)) for r in rows]

    @app.get("/api/batches", response_model=list[BatchRecord], summary="批次审计（含被拒批次）")
    def list_batches(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [BatchRecord(**dict(r)) for r in rows]

    @app.get("/api/health", summary="健康检查")
    def health():
        return {"status": "ok"}

    return app


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _read_csv_text(file: UploadFile) -> str:
    raw = await file.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="CSV 必须是 UTF-8 编码")


app = create_app()
