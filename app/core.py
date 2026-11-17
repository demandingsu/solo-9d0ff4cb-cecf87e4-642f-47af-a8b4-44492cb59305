"""逐小时净电量计算。

规则（与需求逐条对应）：
- 每小时净电量 = 总表相邻整点差值 - 直接下级表差值之和；孙表及更深不再扣；
- 参与表（总表 + 全部直接下级）缺任一整点 -> 该小时只给待核原因；
- 任一参与表读数倒退（差值为负）-> 该小时只给待核原因；
  但若该（表, 小时）已登记回绕确认，则该表差值按 M - 起值 + 终值 折算，
  不再视为倒退（计数器回绕是合法现象）；
- 净值为负 -> 该小时只给待核原因。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .parsing import TS_FORMAT

EPS = 1e-9  # 浮点容差：|x| <= EPS 视为 0


def hour_str(day: date, hour: int) -> str:
    dt = datetime.combine(day, time(hour % 24), tzinfo=timezone.utc)
    if hour == 24:
        dt += timedelta(days=1)
    return dt.strftime(TS_FORMAT)


def shift_hour_str(ts: str, hours: int) -> str:
    """把规范化整点串平移若干小时（可跨日）。"""
    dt = datetime.strptime(ts, TS_FORMAT) + timedelta(hours=hours)
    return dt.strftime(TS_FORMAT)


def compute_daily_net(
    meter_id: str,
    day: date,
    children: list[str],
    readings: dict[tuple[str, str], float],
    confirmations: dict[tuple[str, str], float] | None = None,
) -> list[dict]:
    """计算 24 个小时的净电量。

    readings: {(meter_id, ts_utc): kwh}，由调用方在同一事务快照内取出。
    confirmations: {(meter_id, hour_start): 计数周期 M}，与 readings 取自同一快照；
    已确认的（表, 小时）差值按 M - 起值 + 终值 折算。
    """
    confirmations = confirmations or {}
    participants = [meter_id, *children]
    hours: list[dict] = []

    for h in range(24):
        t0, t1 = hour_str(day, h), hour_str(day, h + 1)
        entry = {
            "hour_start": t0,
            "status": "ok",
            "net_kwh": None,
            "master_delta_kwh": None,
            "children_delta_kwh": None,
            "review_reason": None,
            "wrap_adjusted_meters": [],
        }

        # 1) 参与表缺任一整点
        missing = [
            f"表 {m} 缺 {t}" for m in participants for t in (t0, t1) if (m, t) not in readings
        ]
        if missing:
            entry["status"] = "pending_review"
            entry["review_reason"] = "缺整点读数: " + "; ".join(missing)
            hours.append(entry)
            continue

        # 2) 读数倒退；已登记回绕确认的按 M - 起值 + 终值 折算
        deltas: dict[str, float] = {}
        adjusted: list[str] = []
        backwards: list[str] = []
        for m in participants:
            start, end = readings[(m, t0)], readings[(m, t1)]
            raw = end - start
            modulus = confirmations.get((m, t0))
            if raw < 0 and modulus is not None:
                deltas[m] = modulus - start + end
                adjusted.append(m)
                continue
            deltas[m] = raw
            if raw < -EPS:
                backwards.append(
                    f"表 {m} 读数倒退: {t0}={start} -> {t1}={end}"
                )
        if backwards:
            entry["status"] = "pending_review"
            entry["review_reason"] = "; ".join(backwards)
            hours.append(entry)
            continue

        entry["wrap_adjusted_meters"] = adjusted
        master_delta = deltas[meter_id]
        children_delta = sum(deltas[c] for c in children)
        net = master_delta - children_delta
        entry["master_delta_kwh"] = master_delta
        entry["children_delta_kwh"] = children_delta

        # 3) 净值为负
        if net < -EPS:
            entry["status"] = "pending_review"
            entry["review_reason"] = (
                f"净电量为负: 总表差值 {master_delta} - 直接下级差值和 "
                f"{children_delta} = {net}"
            )
        else:
            entry["net_kwh"] = 0.0 if abs(net) <= EPS else net
        hours.append(entry)

    return hours
