"""回绕确认场景：登记与校验、差值折算、更正失效（改回不复活）、跨日小时。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DAY = "2026-01-15"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


# ---------- 辅助函数 ----------

def hierarchy_csv(edges: list[tuple[str, str]]) -> str:
    return "\n".join(["parent_meter,child_meter"] + [f"{p},{c}" for p, c in edges]) + "\n"


def readings_csv(rows: list[tuple[str, str, float]]) -> str:
    return "\n".join(["meter_id,ts_utc,kwh"] + [f"{m},{t},{v}" for m, t, v in rows]) + "\n"


def upload_hierarchy(client: TestClient, csv_text: str):
    return client.post("/api/hierarchy", files={"file": ("h.csv", csv_text, "text/csv")})


def upload_readings(client: TestClient, csv_text: str):
    return client.post("/api/readings", files={"file": ("r.csv", csv_text, "text/csv")})


def daily_net(client: TestClient, meter: str, day: str = DAY):
    resp = client.get(f"/api/meters/{meter}/daily-net", params={"date": day})
    assert resp.status_code == 200, resp.text
    return resp.json()


def confirm_wrap(client: TestClient, meter: str, hour_start: str, modulus: float):
    return client.post(
        f"/api/meters/{meter}/wrap-confirmations",
        json={"hour_start": hour_start, "modulus": modulus},
    )


def list_confirmations(client: TestClient, meter: str | None = None):
    params = {"meter_id": meter} if meter else {}
    resp = client.get("/api/wrap-confirmations", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def wrap_day_rows(meter: str) -> list[tuple[str, str, float]]:
    """某日 25 个整点：02:00=999 -> 03:00=3 回绕（M=1000 时合法差值为 4），其余小时 +7。"""
    rows = []
    for h in range(25):
        ts = f"2026-01-{15 + h // 24:02d}T{h % 24:02d}:00:00Z"
        if h == 0:
            v = 900.0
        elif h == 1:
            v = 950.0
        elif h == 2:
            v = 999.0
        else:
            v = 3.0 + 7.0 * (h - 3)
        rows.append((meter, ts, v))
    return rows


def flat_day_rows(meter: str, hourly_delta: float, base: float = 10000.0,
                  day: int = 15) -> list[tuple[str, str, float]]:
    return [
        (meter, f"2026-01-{day + h // 24:02d}T{h % 24:02d}:00:00Z", base + hourly_delta * h)
        for h in range(25)
    ]


# ---------- 1. 未确认的回绕仍是待核 ----------

def test_unconfirmed_wrap_is_pending_review(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    hours = daily_net(client, "W")["hours"]
    assert hours[2]["status"] == "pending_review"
    assert "读数倒退" in hours[2]["review_reason"]
    assert "999.0" in hours[2]["review_reason"] and "3.0" in hours[2]["review_reason"]
    assert hours[2]["net_kwh"] is None
    # 其余小时正常
    assert hours[1]["net_kwh"] == pytest.approx(49.0)
    assert hours[3]["net_kwh"] == pytest.approx(7.0)


# ---------- 2. 登记确认后按 M - 起值 + 终值 折算 ----------

def test_confirm_wraparound_adjusts_delta(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    resp = confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 1000.0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "registered"
    assert body["hour_end"] == "2026-01-15T03:00:00Z"
    assert body["adjusted_delta_kwh"] == pytest.approx(1000.0 - 999.0 + 3.0)

    hours = daily_net(client, "W")["hours"]
    assert hours[2]["status"] == "ok"
    assert hours[2]["master_delta_kwh"] == pytest.approx(4.0)
    assert hours[2]["net_kwh"] == pytest.approx(4.0)
    assert hours[2]["wrap_adjusted_meters"] == ["W"]
    # 相邻小时不受影响
    assert hours[1]["net_kwh"] == pytest.approx(49.0)
    assert hours[3]["net_kwh"] == pytest.approx(7.0)
    assert hours[3]["wrap_adjusted_meters"] == []

    # 确认列表可查
    confs = list_confirmations(client, "W")
    assert len(confs) == 1
    assert confs[0]["hour_start"] == "2026-01-15T02:00:00Z"
    assert confs[0]["modulus"] == 1000.0
    assert confs[0]["start_kwh"] == 999.0 and confs[0]["end_kwh"] == 3.0


def test_reregister_with_current_readings_updates(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    assert confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 1000.0).status_code == 200
    # 同一（表, 小时）重复登记 = 按当前读数更新确认
    resp = confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 2000.0)
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert resp.json()["adjusted_delta_kwh"] == pytest.approx(2000.0 - 999.0 + 3.0)
    hours = daily_net(client, "W")["hours"]
    assert hours[2]["master_delta_kwh"] == pytest.approx(1004.0)
    assert len(list_confirmations(client, "W")) == 1


# ---------- 3. 登记校验：不齐 / 越界 / 非回绕 / 非法 M / 非法整点 ----------

def test_registration_requires_both_endpoints(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    # 从未上传过读数的表
    resp = confirm_wrap(client, "GHOST", "2026-01-15T02:00:00Z", 1000.0)
    assert resp.status_code == 422 and "不齐全" in resp.json()["detail"]
    # 只有起点没有终点的小时（次日 00:00 之后没有点）
    resp = confirm_wrap(client, "W", "2026-01-16T00:00:00Z", 1000.0)
    assert resp.status_code == 422 and "不齐全" in resp.json()["detail"]


def test_registration_requires_readings_within_modulus(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    # 起值 999 不在 [0, 500) 内
    resp = confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 500.0)
    assert resp.status_code == 422 and "[0, M)" in resp.json()["detail"]


def test_registration_requires_end_less_than_start(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    # 01:00=950 -> 02:00=999 是正常增长，不是回绕
    resp = confirm_wrap(client, "W", "2026-01-15T01:00:00Z", 1000.0)
    assert resp.status_code == 422 and "回绕" in resp.json()["detail"]


def test_registration_rejects_bad_modulus_and_bad_hour(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    for bad_m in (0.0, -1000.0):
        resp = confirm_wrap(client, "W", "2026-01-15T02:00:00Z", bad_m)
        assert resp.status_code == 422 and "正有限数" in resp.json()["detail"]
    resp = confirm_wrap(client, "W", "2026-01-15T02:30:00Z", 1000.0)
    assert resp.status_code == 422 and "整点" in resp.json()["detail"]
    resp = confirm_wrap(client, "W", "2026-01-15 02:00:00", 1000.0)
    assert resp.status_code == 422 and "时区" in resp.json()["detail"]


def test_failed_registration_leaves_no_partial_state(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    assert confirm_wrap(client, "W", "2026-01-15T01:00:00Z", 1000.0).status_code == 422
    assert confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 500.0).status_code == 422
    assert list_confirmations(client) == []
    # 日查询结果与未登记时一致（失败无副作用）
    assert daily_net(client, "W")["hours"][2]["status"] == "pending_review"


# ---------- 4. 更正使确认失效；改回不复活；同值重传不影响 ----------

def test_correction_invalidates_and_change_back_does_not_revive(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    assert confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 1000.0).status_code == 200
    assert daily_net(client, "W")["hours"][2]["status"] == "ok"

    # 真正更正终点 03:00: 3 -> 5，确认立即失效
    assert upload_readings(
        client, readings_csv([("W", "2026-01-15T03:00:00Z", 5.0)])
    ).status_code == 200
    assert list_confirmations(client, "W") == []
    hours = daily_net(client, "W")["hours"]
    assert hours[2]["status"] == "pending_review"  # 999 -> 5 仍是未确认倒退
    assert "读数倒退" in hours[2]["review_reason"]

    # 读数改回原值 5 -> 3：旧确认不能复活
    assert upload_readings(
        client, readings_csv([("W", "2026-01-15T03:00:00Z", 3.0)])
    ).status_code == 200
    assert list_confirmations(client, "W") == []
    hours = daily_net(client, "W")["hours"]
    assert hours[2]["status"] == "pending_review"
    assert "读数倒退" in hours[2]["review_reason"]

    # 重新登记（依据当前读数）后再次生效
    resp = confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 1000.0)
    assert resp.status_code == 200 and resp.json()["status"] == "registered"
    assert daily_net(client, "W")["hours"][2]["net_kwh"] == pytest.approx(4.0)


def test_correction_of_start_point_invalidates(client):
    assert upload_readings(client, readings_csv(wrap_day_rows("W"))).status_code == 200
    assert confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 1000.0).status_code == 200
    # 更正起点 02:00: 999 -> 998
    assert upload_readings(
        client, readings_csv([("W", "2026-01-15T02:00:00Z", 998.0)])
    ).status_code == 200
    assert list_confirmations(client, "W") == []
    assert daily_net(client, "W")["hours"][2]["status"] == "pending_review"


def test_same_value_reupload_keeps_confirmation(client):
    rows = wrap_day_rows("W")
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    assert confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 1000.0).status_code == 200
    # 同值重传（含确认依赖的两个整点）不算更正，确认保留
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    assert len(list_confirmations(client, "W")) == 1
    assert daily_net(client, "W")["hours"][2]["net_kwh"] == pytest.approx(4.0)


def test_correction_of_unrelated_point_keeps_confirmation(client):
    rows = wrap_day_rows("W")
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    assert confirm_wrap(client, "W", "2026-01-15T02:00:00Z", 1000.0).status_code == 200
    # 更正与本确认无关的整点 10:00
    assert upload_readings(
        client, readings_csv([("W", "2026-01-15T10:00:00Z", 55.0)])
    ).status_code == 200
    assert len(list_confirmations(client, "W")) == 1
    assert daily_net(client, "W")["hours"][2]["net_kwh"] == pytest.approx(4.0)


# ---------- 5. 跨日小时：23:00 -> 次日 00:00 ----------

def test_cross_day_wrap_and_invalidation_across_midnight(client):
    # 2026-01-15: 00:00=100 起每小时 +10，23:00=999；次日 00:00=2（回绕，M=1000）
    rows = [("X", f"2026-01-15T{h:02d}:00:00Z", 100.0 + 10.0 * h) for h in range(23)]
    rows.append(("X", "2026-01-15T23:00:00Z", 999.0))
    # 2026-01-16 全天：00:00=2 起每小时 +5
    rows += [("X", f"2026-01-{16 + h // 24:02d}T{h % 24:02d}:00:00Z", 2.0 + 5.0 * h)
             for h in range(25)]
    assert upload_readings(client, readings_csv(rows)).status_code == 200

    # 跨日小时 23:00->次日00:00 未确认前是倒退待核
    assert daily_net(client, "X")["hours"][23]["status"] == "pending_review"

    # 登记跨日小时的回绕确认：差值 = 1000 - 999 + 2 = 3
    resp = confirm_wrap(client, "X", "2026-01-15T23:00:00Z", 1000.0)
    assert resp.status_code == 200
    assert resp.json()["hour_end"] == "2026-01-16T00:00:00Z"
    hours = daily_net(client, "X")["hours"]
    assert hours[23]["status"] == "ok"
    assert hours[23]["net_kwh"] == pytest.approx(3.0)
    assert hours[23]["wrap_adjusted_meters"] == ["X"]
    # 次日 hour 0（02:00 起 +5）不依赖该确认，正常
    day16 = daily_net(client, "X", "2026-01-16")["hours"]
    assert day16[0]["status"] == "ok"
    assert day16[0]["net_kwh"] == pytest.approx(5.0)

    # 更正次日 00:00（跨日小时的终点）-> 确认失效
    assert upload_readings(
        client, readings_csv([("X", "2026-01-16T00:00:00Z", 4.0)])
    ).status_code == 200
    assert list_confirmations(client, "X") == []
    assert daily_net(client, "X")["hours"][23]["status"] == "pending_review"
    # 次日 hour 0 用新值正常计算（4 -> 7）
    assert daily_net(client, "X", "2026-01-16")["hours"][0]["net_kwh"] == pytest.approx(3.0)


# ---------- 6. 与直属分表扣减规则组合 ----------

def test_wrap_on_child_meter_adjusts_children_delta(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C")])).status_code == 200
    rows = flat_day_rows("M", 100.0) + wrap_day_rows("C")
    assert upload_readings(client, readings_csv(rows)).status_code == 200

    # 未确认：小时 2 因下级倒退待核
    assert daily_net(client, "M")["hours"][2]["status"] == "pending_review"

    assert confirm_wrap(client, "C", "2026-01-15T02:00:00Z", 1000.0).status_code == 200
    hours = daily_net(client, "M")["hours"]
    # 下级差值折算为 4：净电量 = 100 - 4 = 96
    assert hours[2]["status"] == "ok"
    assert hours[2]["children_delta_kwh"] == pytest.approx(4.0)
    assert hours[2]["net_kwh"] == pytest.approx(96.0)
    assert hours[2]["wrap_adjusted_meters"] == ["C"]
    # 其它小时按原规则
    assert hours[0]["net_kwh"] == pytest.approx(100.0 - 50.0)
    assert hours[3]["net_kwh"] == pytest.approx(100.0 - 7.0)


def test_negative_net_after_adjustment_still_pending(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C")])).status_code == 200
    # 总表回绕折算后差值只有 4，下级正常 +30 -> 净值为负，仍待核
    rows = wrap_day_rows("M") + flat_day_rows("C", 30.0, base=5000.0)
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    assert confirm_wrap(client, "M", "2026-01-15T02:00:00Z", 1000.0).status_code == 200
    hours = daily_net(client, "M")["hours"]
    assert hours[2]["status"] == "pending_review"
    assert "净电量为负" in hours[2]["review_reason"]
    assert hours[2]["master_delta_kwh"] == pytest.approx(4.0)
    # 未回绕的小时正常
    assert hours[0]["net_kwh"] == pytest.approx(50.0 - 30.0)
