"""关键场景测试：嵌套扣减、更正、拒收、一致性。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DAY = "2026-01-15"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


# ---------- 构造 CSV 的辅助函数 ----------

def hierarchy_csv(edges: list[tuple[str, str]]) -> str:
    lines = ["parent_meter,child_meter"] + [f"{p},{c}" for p, c in edges]
    return "\n".join(lines) + "\n"


def readings_csv(rows: list[tuple[str, str, float]]) -> str:
    lines = ["meter_id,ts_utc,kwh"] + [f"{m},{t},{v}" for m, t, v in rows]
    return "\n".join(lines) + "\n"


def full_day_rows(meter: str, hourly_delta: float, base: float = 1000.0,
                  skip_hours: set[int] | None = None) -> list[tuple[str, str, float]]:
    """生成某日 00:00 到次日 00:00 共 25 个整点读数，每小时增量固定。"""
    rows = []
    for h in range(25):
        if skip_hours and h in skip_hours:
            continue
        ts = f"2026-01-{15 + h // 24:02d}T{h % 24:02d}:00:00Z"
        rows.append((meter, ts, base + hourly_delta * h))
    return rows


def upload_hierarchy(client: TestClient, csv_text: str):
    return client.post("/api/hierarchy", files={"file": ("h.csv", csv_text, "text/csv")})


def upload_readings(client: TestClient, csv_text: str):
    return client.post("/api/readings", files={"file": ("r.csv", csv_text, "text/csv")})


def daily_net(client: TestClient, meter: str, day: str = DAY):
    resp = client.get(f"/api/meters/{meter}/daily-net", params={"date": day})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 1. 嵌套扣减：孙表不能再扣 ----------

def test_nested_deduction_grandchild_not_subtracted(client):
    # M -> C1 -> G1：M 的直接下级只有 C1，G1 是孙表
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1"), ("C1", "G1")])).status_code == 200
    rows = (
        full_day_rows("M", 100.0)   # 总表每小时 100
        + full_day_rows("C1", 30.0)  # 直接下级每小时 30（其中已含 G1 的 10）
        + full_day_rows("G1", 10.0)  # 孙表每小时 10，不能再扣
    )
    assert upload_readings(client, readings_csv(rows)).status_code == 200

    result = daily_net(client, "M")
    assert result["child_meters"] == ["C1"]
    for hour in result["hours"]:
        assert hour["status"] == "ok", hour
        # 100 - 30 = 70；若错误地把孙表也扣掉会得到 60
        assert hour["net_kwh"] == pytest.approx(70.0)
        assert hour["master_delta_kwh"] == pytest.approx(100.0)
        assert hour["children_delta_kwh"] == pytest.approx(30.0)


def test_multiple_direct_children_all_deducted(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1"), ("M", "C2"), ("C1", "G1")])).status_code == 200
    rows = (
        full_day_rows("M", 100.0) + full_day_rows("C1", 30.0)
        + full_day_rows("C2", 20.0) + full_day_rows("G1", 10.0)
    )
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    result = daily_net(client, "M")
    assert result["child_meters"] == ["C1", "C2"]
    for hour in result["hours"]:
        assert hour["net_kwh"] == pytest.approx(50.0)  # 100 - 30 - 20


# ---------- 2. 缺整点 / 倒退 / 负净值 -> 待核原因 ----------

def test_missing_hour_point_marks_two_adjacent_hours(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    # C1 缺 03:00 这个整点 -> 影响 02-03 与 03-04 两个小时
    rows = full_day_rows("M", 100.0) + full_day_rows("C1", 30.0, skip_hours={3})
    assert upload_readings(client, readings_csv(rows)).status_code == 200

    result = daily_net(client, "M")
    hours = result["hours"]
    for h in (0, 1, 4, 23):
        assert hours[h]["status"] == "ok", hours[h]
    for h in (2, 3):
        assert hours[h]["status"] == "pending_review"
        assert hours[h]["net_kwh"] is None
        assert "缺整点读数" in hours[h]["review_reason"]
        assert "C1" in hours[h]["review_reason"]


def test_missing_next_day_boundary_marks_last_hour(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    # 缺次日 00:00（第 24 个点）-> 只有 23:00-24:00 这个小时待核
    rows = full_day_rows("M", 100.0, skip_hours={24}) + full_day_rows("C1", 30.0)
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    hours = daily_net(client, "M")["hours"]
    assert all(hours[h]["status"] == "ok" for h in range(23))
    assert hours[23]["status"] == "pending_review"
    assert "2026-01-16T00:00:00Z" in hours[23]["review_reason"]


def test_backwards_reading(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    rows = full_day_rows("M", 100.0) + full_day_rows("C1", 30.0)
    # 让总表在 05:00 读数倒退
    rows = [(m, t, (v - 500.0 if (m == "M" and t == "2026-01-15T05:00:00Z") else v))
            for m, t, v in rows]
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    hours = daily_net(client, "M")["hours"]
    assert hours[4]["status"] == "pending_review"
    assert "读数倒退" in hours[4]["review_reason"]
    assert "M" in hours[4]["review_reason"]
    # 05:00 读数偏低也让 05-06 差值虚高为正，只有 04-05 报倒退
    assert hours[5]["status"] == "ok"


def test_negative_net(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1"), ("M", "C2")])).status_code == 200
    # 下级之和 80 > 总表 50 -> 净值为负
    rows = full_day_rows("M", 50.0) + full_day_rows("C1", 30.0) + full_day_rows("C2", 50.0)
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    hours = daily_net(client, "M")["hours"]
    assert all(h["status"] == "pending_review" for h in hours)
    assert "净电量为负" in hours[0]["review_reason"]


def test_meter_without_children(client):
    # 总表没有下级：净电量就是自身差值
    assert upload_hierarchy(client, hierarchy_csv([("OTHER", "X")])).status_code == 200
    assert upload_readings(client, readings_csv(full_day_rows("M", 42.0))).status_code == 200
    result = daily_net(client, "M")
    assert result["child_meters"] == []
    assert all(h["net_kwh"] == pytest.approx(42.0) for h in result["hours"])


# ---------- 3. 批次拒收 ----------

def test_same_key_different_value_in_one_batch_rejected_atomically(client):
    csv_text = readings_csv([
        ("M", "2026-01-15T00:00:00Z", 100.0),
        ("M", "2026-01-15T01:00:00Z", 200.0),
        ("M", "2026-01-15T00:00:00Z", 101.0),  # 同键异值
    ])
    resp = upload_readings(client, csv_text)
    assert resp.status_code == 422
    assert "同键异值" in resp.json()["detail"]
    # 整批拒收：合法行也不能入库
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    hours = daily_net(client, "M")["hours"]
    assert all(h["status"] == "pending_review" for h in hours)
    assert "缺整点读数" in hours[0]["review_reason"]
    # 拒收留痕
    batches = client.get("/api/batches").json()
    assert any(b["status"] == "rejected" and "同键异值" in b["reason"] for b in batches)


def test_same_key_same_value_in_one_batch_deduped(client):
    csv_text = readings_csv([
        ("M", "2026-01-15T00:00:00Z", 100.0),
        ("M", "2026-01-15T00:00:00Z", 100.0),  # 同键同值 -> 去重
        ("M", "2026-01-15T01:00:00Z", 110.0),
    ])
    resp = upload_readings(client, csv_text)
    assert resp.status_code == 200
    assert resp.json()["rows_applied"] == 2


def test_cycle_hierarchy_rejected_and_old_hierarchy_kept(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    resp = upload_hierarchy(client, hierarchy_csv([("A", "B"), ("B", "C"), ("C", "A")]))
    assert resp.status_code == 422
    assert "成环" in resp.json()["detail"]
    # 旧层级仍在
    assert client.get("/api/hierarchy").json() == [
        {"parent_meter": "M", "child_meter": "C1"}
    ]


def test_self_loop_and_multiple_parents_rejected(client):
    resp = upload_hierarchy(client, hierarchy_csv([("A", "A")]))
    assert resp.status_code == 422 and "自环" in resp.json()["detail"]
    resp = upload_hierarchy(client, hierarchy_csv([("P1", "C"), ("P2", "C")]))
    assert resp.status_code == 422 and "多个上级" in resp.json()["detail"]


def test_non_hour_and_non_utc_rejected(client):
    resp = upload_readings(client, readings_csv([("M", "2026-01-15T00:30:00Z", 1.0)]))
    assert resp.status_code == 422 and "整点" in resp.json()["detail"]
    resp = upload_readings(client, readings_csv([("M", "2026-01-15T08:00:00+08:00", 1.0)]))
    assert resp.status_code == 422 and "UTC" in resp.json()["detail"]
    resp = upload_readings(client, readings_csv([("M", "2026-01-15 00:00:00", 1.0)]))
    assert resp.status_code == 422 and "时区" in resp.json()["detail"]


# ---------- 4. 更正与幂等 ----------

def test_correction_updates_old_results(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    assert upload_readings(
        client, readings_csv(full_day_rows("M", 100.0) + full_day_rows("C1", 30.0))
    ).status_code == 200
    before = daily_net(client, "M")
    assert before["hours"][3]["net_kwh"] == pytest.approx(70.0)

    # 更正：C1 在 04:00 的读数从历史值改为更高值 -> 03-04 小时净电量变小
    corrected = [("C1", "2026-01-15T04:00:00Z", 1000.0 + 30.0 * 4 + 12.0)]
    assert upload_readings(client, readings_csv(corrected)).status_code == 200
    after = daily_net(client, "M")
    assert after["hours"][3]["net_kwh"] == pytest.approx(70.0 - 12.0)
    # 相邻的 04-05 小时也随之更新
    assert after["hours"][4]["net_kwh"] == pytest.approx(70.0 + 12.0)
    # 未受影响的小时不变
    assert after["hours"][0]["net_kwh"] == pytest.approx(70.0)


def test_same_value_reupload_is_idempotent(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    rows = full_day_rows("M", 100.0) + full_day_rows("C1", 30.0)
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    first = daily_net(client, "M")

    # 原样重传同值
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    second = daily_net(client, "M")
    assert first == second

    # 同值重传连 updated_at 都不应改变（数据库层面幂等）
    import sqlite3
    conn = sqlite3.connect(client.app.state.db_path)
    stamps1 = conn.execute("SELECT meter_id, ts_utc, updated_at FROM readings").fetchall()
    upload_readings(client, readings_csv(rows))
    stamps2 = conn.execute("SELECT meter_id, ts_utc, updated_at FROM readings").fetchall()
    conn.close()
    assert stamps1 == stamps2


def test_hierarchy_replacement_updates_old_dates(client):
    # 层级 v1：M -> C1
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    rows = (
        full_day_rows("M", 100.0) + full_day_rows("C1", 30.0) + full_day_rows("C2", 20.0)
    )
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    v1 = daily_net(client, "M")
    assert v1["child_meters"] == ["C1"]
    assert v1["hours"][0]["net_kwh"] == pytest.approx(70.0)

    # 整版替换为 M -> C2：同一历史日期的结果随之更新
    assert upload_hierarchy(client, hierarchy_csv([("M", "C2")])).status_code == 200
    v2 = daily_net(client, "M")
    assert v2["child_meters"] == ["C2"]
    assert v2["hours"][0]["net_kwh"] == pytest.approx(80.0)


def test_failed_upload_keeps_old_results(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    assert upload_readings(
        client, readings_csv(full_day_rows("M", 100.0) + full_day_rows("C1", 30.0))
    ).status_code == 200
    before = daily_net(client, "M")

    # 一次失败的层级上传（成环）和一次失败的读数上传（同键异值）
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1"), ("C1", "M")])).status_code == 422
    bad = readings_csv([("M", "2026-01-15T00:00:00Z", 1.0), ("M", "2026-01-15T00:00:00Z", 2.0)])
    assert upload_readings(client, bad).status_code == 422

    after = daily_net(client, "M")
    assert before == after  # 旧结果仍可用且不变


# ---------- 5. 上传与查询交错的一致性 ----------

def test_interleaved_upload_and_query_see_consistent_snapshot(client):
    """查询期间发生更正上传：每次查询必须看到全旧或全新的读数，不得混杂。"""
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    assert upload_readings(
        client, readings_csv(full_day_rows("M", 100.0, base=1000.0)
                             + full_day_rows("C1", 30.0, base=500.0))
    ).status_code == 200

    errors: list[str] = []
    stop = threading.Event()

    def writer():
        # 反复在 v1/v2 两套完整读数间更正（每套内部 25 个点一致）
        base = 2000.0
        while not stop.is_set():
            rows = full_day_rows("M", 100.0, base=base) + full_day_rows("C1", 30.0, base=base / 2)
            try:
                resp = upload_readings(client, readings_csv(rows))
                if resp.status_code != 200:
                    errors.append(f"upload failed: {resp.status_code} {resp.text}")
                    return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"upload exception: {exc}")
                return
            base = 3000.0 if base == 2000.0 else 2000.0

    def reader():
        for _ in range(200):
            try:
                result = daily_net(client, "M")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"query exception: {exc}")
                return
            deltas = {h["master_delta_kwh"] for h in result["hours"] if h["status"] == "ok"}
            # 快照一致：一次查询里所有小时的总表差值必须来自同一套读数（全 100）
            if deltas != {100.0}:
                errors.append(f"混杂快照: {deltas}")
                return
            # 净电量每小时必须一致（100-30=70）
            nets = {h["net_kwh"] for h in result["hours"] if h["status"] == "ok"}
            if nets != {70.0}:
                errors.append(f"净值不一致: {nets}")
                return

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    w.start(); r.start()
    r.join(); stop.set(); w.join(timeout=30)
    assert not errors, errors[:3]
