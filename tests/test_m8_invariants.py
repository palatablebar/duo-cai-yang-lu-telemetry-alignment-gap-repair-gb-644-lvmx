"""M8 不变量：原始证据不丢、原始批次不可变、补正不污染其它批次、四账始终守恒。"""

from __future__ import annotations

from decimal import Decimal

from app import store
from app.modules.m3_quality import quarantine as quarantine_store
from app.modules.m6_batches import service as batch_service
from app.modules.m8_corrections import service as correction_service
from app.modules.m8_corrections import store as correction_store

from expected import MAIN


def _body(source_row: int, **over) -> dict:
    payload = {
        "source_row": source_row,
        "sensor_id": "TH-01",
        "sampled_at": "2026-03-01T00:22:20Z",
        "offset_seconds": 20,
        "value": "46.6",
        "unit": "%RH",
        "quality": "good",
    }
    payload.update(over)
    return payload


def test_original_tables_remain_readonly_after_promote(conn, run_main):
    """替换只重写 samples/grid/windows/duplicates/reconcile；intake/offsets/quarantine 原样。"""
    rid = run_main["run_id"]
    intake_before = dict(store.one(conn, "SELECT * FROM intake WHERE run_id=?", (rid,)))
    offsets_before = [dict(r) for r in store.rows(conn, "SELECT * FROM offsets WHERE run_id=?", (rid,))]
    quarantine_before = quarantine_store.rows_of(conn, rid)

    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)

    intake_after = dict(store.one(conn, "SELECT * FROM intake WHERE run_id=?", (rid,)))
    offsets_after = [dict(r) for r in store.rows(conn, "SELECT * FROM offsets WHERE run_id=?", (rid,))]
    quarantine_after = quarantine_store.rows_of(conn, rid)
    assert intake_after == intake_before
    assert offsets_after == offsets_before
    assert quarantine_after == quarantine_before


def test_corrections_are_scoped_per_run(conn, run_main, run_gap):
    """一个批次的补正绝不影响另一个批次。"""
    main_id = run_main["run_id"]
    gap_id = run_gap["run_id"]
    correction_service.submit_correction(
        conn, main_id, 72, _body(72)
    )
    correction_service.promote(conn, main_id)

    # gap 批次没有任何补正或修订
    assert correction_store.list_for_run(conn, gap_id) == []
    assert correction_store.list_revisions(conn, gap_id) == []
    gap_run = batch_service.history.get(conn, gap_id)
    assert gap_run["counts"]["accepted"] == 5  # GAP 样例首版采纳数不变


def test_reimport_remains_idempotent_with_corrections(conn, run_main):
    """补正与替换后，同输入重新导入仍命中同一批次键，不产生重复批次。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)

    runs_before = store.value(conn, "SELECT COUNT(*) FROM runs", (), 0)
    again = batch_service.execute_run(conn, sample="telemetry_raw", offsets="device_offsets")
    assert again["reused"] is True
    assert again["run_id"] == rid
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == runs_before


def test_grid_and_row_accounts_balanced_after_multiple_corrections(conn, run_main):
    """多条补正一次性替换后，行账与网格账同时守恒，合计 Decimal 精确。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.submit_correction(
        conn, rid, 81, _body(81, sampled_at="2026-03-01T00:26:20Z", value="46.0")
    )
    result = correction_service.promote(conn, rid)
    c, t = result["counts"], result["totals"]

    assert c["raw_records"] == c["accepted"] + c["duplicates"] + c["quarantined"]
    assert c["expected_buckets"] == c["observed"] + c["filled"] + c["gaps"]
    # 数值账：采纳合计 = 实有合计；补齐不回流
    assert Decimal(t["accepted_sum"]) == Decimal(t["observed_sum"])
    assert Decimal(t["grid_sum"]) == Decimal(t["observed_sum"]) + Decimal(t["filled_sum"])


def test_no_data_loss_grid_point_count_stable(conn, run_main):
    """替换前后网格点总数（应有桶）不变，只是状态在 OBSERVED/FILLED/GAP 间迁移。"""
    rid = run_main["run_id"]
    before = store.value(conn, "SELECT COUNT(*) FROM grid WHERE run_id=?", (rid,), 0)
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.submit_correction(
        conn, rid, 81, _body(81, sampled_at="2026-03-01T00:26:20Z", value="46.0")
    )
    correction_service.promote(conn, rid)
    after = store.value(conn, "SELECT COUNT(*) FROM grid WHERE run_id=?", (rid,), 0)
    assert before == after == MAIN["grid_points"]


def test_every_correction_traces_to_exactly_one_source_row(conn, run_main):
    """补正与隔离行一一对应：每条补正的 source_row 都能在隔离区找到且唯一。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.submit_correction(
        conn, rid, 81, _body(81, sampled_at="2026-03-01T00:26:20Z", value="46.0")
    )
    for item in correction_store.list_for_run(conn, rid):
        matches = store.rows(
            conn,
            "SELECT COUNT(*) AS n FROM quarantine WHERE run_id=? AND source_row=?",
            (rid, item["source_row"]),
        )
        assert matches[0]["n"] >= 1
    # 同一 source_row 在 corrections 里唯一
    rows = correction_store.list_for_run(conn, rid)
    source_rows = [r["source_row"] for r in rows]
    assert len(source_rows) == len(set(source_rows))


def test_failed_promote_leaves_no_partial_revision_data(conn, run_main, monkeypatch):
    """失败回滚后，结果表里没有任何补正痕迹（CORRECTED_ADOPT 不出现）。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(correction_service, "_replace_result_tables", boom)
    try:
        correction_service.promote(conn, rid)
    except Exception:
        pass

    leaked = store.value(
        conn,
        "SELECT COUNT(*) FROM samples WHERE run_id=? AND flags LIKE '%CORRECTED_ADOPT%'",
        (rid,),
        0,
    )
    assert leaked == 0
    # 正式网格仍是首版
    assert store.value(conn, "SELECT COUNT(*) FROM grid WHERE run_id=?", (rid,), 0) == MAIN["grid_points"]
