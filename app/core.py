"""逐小时净电量计算。

规则（与需求逐条对应）：
- 每小时净电量 = 总表相邻整点差值 - 直接下级表差值之和；孙表及更深不再扣；
- 参与表（总表 + 全部直接下级）缺任一整点 -> 该小时只给待核原因；
- 任一参与表读数倒退（差值为负）-> 该小时只给待核原因；
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


def compute_daily_net(
    meter_id: str,
    day: date,
    children: list[str],
    readings: dict[tuple[str, str], float],
) -> list[dict]:
    """计算 24 个小时的净电量。

    readings: {(meter_id, ts_utc): kwh}，由调用方在同一事务快照内取出。
    """
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

        # 2) 读数倒退
        deltas = {m: readings[(m, t1)] - readings[(m, t0)] for m in participants}
        backwards = [
            f"表 {m} 读数倒退: {t0}={readings[(m, t0)]} -> {t1}={readings[(m, t1)]}"
            for m in participants
            if deltas[m] < -EPS
        ]
        if backwards:
            entry["status"] = "pending_review"
            entry["review_reason"] = "; ".join(backwards)
            hours.append(entry)
            continue

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
