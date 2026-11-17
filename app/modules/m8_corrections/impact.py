"""补正影响分析：正式结果与补正预览之间的窗口、缺口覆盖差异。

值班员在替换前必须看清「会改动哪些窗口」。这里只做只读比对：
- 窗口级：覆盖度或状态发生变化的 15 分钟窗口逐一列出（旧值 → 新值）；
- 缺口覆盖：实有 / 补齐 / 缺口三类桶数的迁移；
- 网格点：状态或值发生变化的桶（含补正行落点与受补齐策略牵动的相邻桶）。
"""

from __future__ import annotations

from app import store
from app.modules.m2_timebase import coerce, contract


def _stored_windows(conn, run_id: str) -> dict[tuple[str, str], dict]:
    rows = store.rows(
        conn,
        "SELECT sensor_id, window_start, observed, expected, coverage, state"
        " FROM windows WHERE run_id = ?",
        (run_id,),
    )
    return {
        (row["sensor_id"], row["window_start"]): {
            "sensor_id": row["sensor_id"],
            "window_start": row["window_start"],
            "observed": row["observed"],
            "expected": row["expected"],
            "coverage": row["coverage"],
            "state": row["state"],
        }
        for row in rows
    }


def _derived_windows(analysis) -> dict[tuple[str, str], dict]:
    out = {}
    if analysis is None:
        return out
    for item in analysis.windows:
        start = item.start.strftime("%Y-%m-%dT%H:%M:%SZ")
        out[(item.sensor_id, start)] = {
            "sensor_id": item.sensor_id,
            "window_start": start,
            "observed": item.observed,
            "expected": item.expected,
            "coverage": str(item.coverage),
            "state": item.state,
        }
    return out


def _stored_grid(conn, run_id: str) -> dict[tuple[str, str], dict]:
    rows = store.rows(
        conn,
        "SELECT sensor_id, bucket_utc, value, state, reason, source_row"
        " FROM grid WHERE run_id = ?",
        (run_id,),
    )
    return {(row["sensor_id"], row["bucket_utc"]): dict(row) for row in rows}


def _derived_grid(repaired) -> dict[tuple[str, str], dict]:
    out = {}
    for point in repaired.points:
        bucket = point.bucket.strftime("%Y-%m-%dT%H:%M:%SZ")
        out[(point.sensor_id, bucket)] = {
            "sensor_id": point.sensor_id,
            "bucket_utc": bucket,
            "value": None if point.value is None else str(point.value),
            "state": point.state,
            "reason": point.reason,
            "source_row": point.source_row,
        }
    return out


def window_impact(conn, run_id: str, derived) -> list[dict]:
    """返回发生变化的窗口：覆盖度、实有桶数或状态任一不同即列出。"""
    before = _stored_windows(conn, run_id)
    after = _derived_windows(derived.analysis)
    impact = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            impact.append({"change": "ADDED", "before": None, "after": new})
            continue
        if new is None:
            impact.append({"change": "REMOVED", "before": old, "after": None})
            continue
        if (
            old["observed"] != new["observed"]
            or old["coverage"] != new["coverage"]
            or old["state"] != new["state"]
            or old["expected"] != new["expected"]
        ):
            impact.append({"change": "CHANGED", "before": old, "after": new})
    return impact


def grid_impact(conn, run_id: str, derived) -> list[dict]:
    """返回状态或值发生变化的网格点，含补正落点与被牵动的补齐桶。"""
    before = _stored_grid(conn, run_id)
    after = _derived_grid(derived.repaired)
    impact = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            impact.append({"change": "ADDED", "before": None, "after": new})
            continue
        if new is None:
            impact.append({"change": "REMOVED", "before": old, "after": None})
            continue
        if old["state"] != new["state"] or old["value"] != new["value"] or old["reason"] != new["reason"]:
            impact.append({"change": "CHANGED", "before": old, "after": new})
    return impact


def coverage_summary(conn, run_id: str, derived) -> dict:
    """缺口覆盖迁移：正式结果与补正预览的实有/补齐/缺口桶数对照。"""
    stored = {
        row["state"]: row["n"]
        for row in store.rows(
            conn, "SELECT state, COUNT(*) AS n FROM grid WHERE run_id = ? GROUP BY state", (run_id,)
        )
    }
    derived_counts = derived.repaired.counts()
    old_windows = _stored_windows(conn, run_id)
    new_windows = _derived_windows(derived.analysis)
    return {
        "before": {
            "observed": stored.get("OBSERVED", 0),
            "filled": stored.get("FILLED", 0),
            "gaps": stored.get("GAP", 0),
            "partial_windows": sum(1 for item in old_windows.values() if item["state"] == "PARTIAL"),
            "windows": len(old_windows),
        },
        "after": {
            "observed": derived_counts["observed"],
            "filled": derived_counts["filled"],
            "gaps": derived_counts["gaps"],
            "partial_windows": sum(1 for item in new_windows.values() if item["state"] == "PARTIAL"),
            "windows": len(new_windows),
        },
    }


def affected_window_keys(correction_items: list[dict]) -> list[str]:
    """补正桶直接落入的 15 分钟窗口（按左闭右开对齐），用于页面快速定位。"""
    keys = set()
    for item in correction_items:
        if not item.get("bucket_utc"):
            continue
        start = contract.align_window(coerce.to_utc(item["bucket_utc"]), 15)
        keys.add(f"{item['sensor_id']}@{start.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return sorted(keys)
