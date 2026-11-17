"""补正推导：在原始采纳行之上叠加已通过校验的补正，重走网格/缺口/窗口。

关键约束：
- 原始批次的 samples / grid / windows / quarantine 一律不读不写，推导只在内存进行，
  因此预览永远不会污染正式结果，失败也不可能留下半成品。
- 补正行与原隔离行按 source_row 一一对应；补正行带 CORRECTED_ADOPT 标记，迟到不重判
  （补正是事后录入，不参与首次导入的水位推进）。
- 推导函数与首次导入共用 m4/m5 的同一份 align/analyse/repair，保证规则只有一个来源。
- 基础重复行（base duplicates）从 duplicates 表还原，保证行账里的「重复」额度不丢失。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app import store
from app.modules.m2_timebase import coerce, contract, offsets
from app.modules.m3_quality import rules
from app.modules.m3_quality.service import Judged, load_judged
from app.modules.m4_grid import service as grid_service
from app.modules.m5_gaps import service as gaps_service


def load_offset_entries(conn, run_id: str) -> list:
    rows = store.rows(
        conn,
        "SELECT sensor_id, start_utc, end_utc, seconds FROM offsets WHERE run_id = ?",
        (run_id,),
    )
    return [
        offsets.OffsetEntry(
            sensor_id=row["sensor_id"],
            start=coerce.to_utc(row["start_utc"]),
            end=coerce.to_utc(row["end_utc"]),
            seconds=row["seconds"],
        )
        for row in rows
    ]


def _correction_to_judged(item: dict) -> Judged:
    spec = contract.spec(item["sensor_id"])
    flags = ("CORRECTED_ADOPT",)
    return Judged(
        source_row=item["source_row"],
        sensor_id=item["sensor_id"],
        spec=spec,
        bucket=coerce.to_utc(item["bucket_utc"]),
        value=Decimal(item["value_text"]),
        unit=item["unit_text"] or spec.unit,
        quality=item["quality"] or "good",
        quality_rank=rules.quality_rank(item["quality"] or "good"),
        ingest_seq=item["ingest_seq"] if item["ingest_seq"] is not None else item["source_row"],
        offset=item["offset_seconds"] or 0,
        late=False,
        flags=flags,
    )


def _load_base_duplicate_rows(conn, run_id: str) -> list[Judged]:
    """从 duplicates 表还原首次导入被去重的原始行，作为历史重复保留。

    这些行已经计入行账的「重复」，补正推导时必须原样保留，不能因为重新 align 而消失。
    它们以最低优先级（ingest_seq=-1）重新进入对齐，且对应保留桶一定还在，必然仍判重复。
    """
    rows = store.rows(
        conn,
        "SELECT sensor_id, bucket_utc, dropped_row, dropped_value, dropped_quality"
        " FROM duplicates WHERE run_id = ? ORDER BY dropped_row",
        (run_id,),
    )
    restored: list[Judged] = []
    for row in rows:
        spec = contract.spec(row["sensor_id"])
        if spec is None:
            continue
        restored.append(
            Judged(
                source_row=row["dropped_row"],
                sensor_id=row["sensor_id"],
                spec=spec,
                bucket=coerce.to_utc(row["bucket_utc"]),
                value=Decimal(row["dropped_value"]),
                unit=spec.unit,
                quality=row["dropped_quality"],
                quality_rank=rules.quality_rank(row["dropped_quality"]),
                ingest_seq=-1,
                offset=0,
                late=False,
                flags=("BASE_DUPLICATE",),
            )
        )
    return restored


@dataclass
class Derived:
    judged: list
    aligned: object
    analysis: object
    repaired: object
    correction_rows: list
    correction_duplicates: list
    base_duplicates: list


def derive(conn, run_id: str, correction_items: list[dict]) -> Derived:
    """把给定补正（调用方保证已校验通过）叠加到原始采纳行上，完整重推。"""
    base = load_judged(conn, run_id)
    base_duplicates = _load_base_duplicate_rows(conn, run_id)

    extra: list[Judged] = []
    for item in correction_items:
        extra.append(_correction_to_judged(item))

    combined = list(base) + base_duplicates + extra
    aligned = grid_service.align_rows(combined)
    # 补正桶与既有采纳桶撞键而被去重的部分：这些行没有进采纳，仍占原始行额度
    correction_source_rows = {row.source_row for row in extra}
    correction_duplicates = [
        dup for dup in aligned.duplicates if dup.dropped_row in correction_source_rows
    ]
    analysis = grid_service.analyse(aligned.kept)
    repaired = gaps_service.repair_all(aligned.kept, analysis)
    return Derived(
        judged=combined,
        aligned=aligned,
        analysis=analysis,
        repaired=repaired,
        correction_rows=extra,
        correction_duplicates=correction_duplicates,
        base_duplicates=base_duplicates,
    )
