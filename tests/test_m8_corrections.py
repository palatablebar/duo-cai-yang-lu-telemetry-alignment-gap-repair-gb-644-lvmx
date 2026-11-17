"""M8 隔离补正：草稿校验、一一对应、影响预览、整批替换、幂等与失败回滚。

覆盖需求的硬约束：
- 未补正/未通过校验的行不能冒充正常采纳；
- 补正与原隔离行按 source_row 一一对应，同一补正重跑不新增版本；
- 替换在单事务完成，失败保留旧版结果，不丢数据、不丢原始证据；
- 补正后窗口、缺口覆盖、导出来源一致，四账仍平衡。
"""

from __future__ import annotations

import csv
import io

import pytest

from app import store
from app.errors import InputError, NotFoundError, StateError
from app.modules.m3_quality import quarantine as quarantine_store
from app.modules.m6_batches import history, service as batch_service
from app.modules.m7_export import evidence, service as export_service
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
        "ingest_seq": None,
        "note": "",
    }
    payload.update(over)
    return payload


# ---------- 草稿与校验 ----------

def test_valid_correction_persists_as_draft_not_adopted(conn, run_main):
    """校验通过的补正只进草稿区，正式采纳结果在替换前不变。"""
    rid = run_main["run_id"]
    saved = correction_service.submit_correction(conn, rid, 72, _body(72))
    assert saved["status"] == correction_store.DRAFT
    assert saved["validation"]["ok"] is True
    assert saved["bucket_utc"] == "2026-03-01T00:22:00Z"

    # 正式结果仍是首版
    run = history.get(conn, rid)
    assert run["counts"]["accepted"] == MAIN["accepted"]
    assert store.value(conn, "SELECT COUNT(*) FROM samples WHERE run_id=?", (rid,), 0) == MAIN["accepted"]


def test_invalid_correction_kept_as_draft_with_reason(conn, run_main):
    """未通过校验的补正留痕但永远不能被采纳。"""
    rid = run_main["run_id"]
    saved = correction_service.submit_correction(
        conn, rid, 72, _body(72, value="999.0")  # 超出 TH-01 量程 [0,100]
    )
    assert saved["status"] == correction_store.DRAFT
    assert saved["validation"]["ok"] is False
    assert saved["validation"]["code"] == "OUT_OF_RANGE"

    # 没有任何校验通过的草稿时，promote 直接拒绝
    with pytest.raises(StateError) as exc:
        correction_service.promote(conn, rid)
    assert exc.value.code == "NO_VALID_CORRECTION"
    # 正式结果未被触碰
    assert history.get(conn, rid)["counts"]["accepted"] == MAIN["accepted"]


def test_correction_requires_quarantined_row(conn, run_main):
    """只能对隔离区里的行补正；正常采纳行拒绝补正。"""
    rid = run_main["run_id"]
    with pytest.raises(NotFoundError) as exc:
        correction_service.submit_correction(conn, rid, 1, _body(1))
    assert exc.value.code == "ROW_NOT_QUARANTINED"


def test_structural_damage_is_not_correctable(conn, run_main):
    """未知传感器等结构性原因不允许逐行补正，只能整批重传。"""
    rid = run_main["run_id"]
    # 行 92 是 UNKNOWN_SENSOR（XX-99）
    with pytest.raises(InputError) as exc:
        correction_service.submit_correction(
            conn, rid, 92, _body(92, sensor_id="XX-99", value="55.0")
        )
    assert exc.value.code == "NOT_CORRECTABLE"


def test_each_quarantine_row_has_at_most_one_correction(conn, run_main):
    """补正与原隔离行一一对应：同一 source_row 重复提交不新增行。"""
    rid = run_main["run_id"]
    first = correction_service.submit_correction(conn, rid, 72, _body(72))
    second = correction_service.submit_correction(conn, rid, 72, _body(72, value="46.7"))
    assert first["correction_id"] == second["correction_id"]
    rows = [c for c in correction_store.list_for_run(conn, rid) if c["source_row"] == 72]
    assert len(rows) == 1
    # 内容变化视为新一轮尝试，attempts 累加
    assert second["attempts"] == 2


def test_identical_resubmit_is_idempotent(conn, run_main):
    """完全相同的补正重投：同一行、同一版本，只累加尝试次数。"""
    rid = run_main["run_id"]
    payload = _body(72)
    correction_service.submit_correction(conn, rid, 72, payload)
    again = correction_service.submit_correction(conn, rid, 72, dict(payload))
    assert again["resubmitted"] is True
    assert again["attempts"] == 2
    assert store.value(conn, "SELECT COUNT(*) FROM corrections WHERE run_id=?", (rid,), 0) == 1
    assert store.value(conn, "SELECT COUNT(*) FROM run_revisions WHERE run_id=?", (rid,), 0) == 0


def test_promoted_row_cannot_be_resubmitted(conn, run_main):
    """已生效补正的隔离行不能再次提交，避免静默覆盖正式结果。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)
    with pytest.raises(StateError) as exc:
        correction_service.submit_correction(conn, rid, 72, _body(72, value="47.0"))
    assert exc.value.code == "CORRECTION_ALREADY_PROMOTED"


# ---------- 影响预览 ----------

def test_preview_lists_changed_windows_and_coverage(conn, run_main):
    """替换前能看到会改动哪些窗口，以及缺口覆盖如何迁移。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))  # TH-01 00:22
    correction_service.submit_correction(
        conn, rid, 81, _body(81, sampled_at="2026-03-01T00:26:20Z", value="46.0")
    )
    preview = correction_service.preview_impact(conn, rid)
    assert preview["has_preview"] is True
    # 两个补正桶都落在 00:15 窗口
    affected = {w.split("@")[1] for w in preview["affected_windows"]}
    assert "2026-03-01T00:15:00Z" in affected
    # 覆盖：实有 +2、补齐 -2（两个 FILLED 桶被实有值替代）
    assert preview["coverage"]["before"]["observed"] == MAIN["observed"]
    assert preview["coverage"]["after"]["observed"] == MAIN["observed"] + 2
    assert preview["coverage"]["after"]["filled"] == MAIN["filled"] - 2
    # 窗口实有桶数变化被列出
    changed = preview["window_changes"]
    assert any(w["after"]["window_start"] == "2026-03-01T00:15:00Z" for w in changed)
    # 网格点从 FILLED 变 OBSERVED
    grid_changes = preview["grid_changes"]
    states = {(g["after"]["bucket_utc"], g["before"]["state"], g["after"]["state"]) for g in grid_changes}
    assert ("2026-03-01T00:22:00Z", "FILLED", "OBSERVED") in states


def test_preview_is_empty_without_valid_drafts(conn, run_main):
    """没有有效草稿时不给误导性预览。"""
    preview = correction_service.preview_impact(conn, run_main["run_id"])
    assert preview["has_preview"] is False
    assert preview["window_changes"] == []


# ---------- 整批替换与四账 ----------

def test_promote_replaces_official_result_and_keeps_accounts_balanced(conn, run_main):
    """替换后行账/网格账/数值账/窗口账全部相等。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.submit_correction(
        conn, rid, 81, _body(81, sampled_at="2026-03-01T00:26:20Z", value="46.0")
    )
    result = correction_service.promote(conn, rid)
    counts = result["counts"]
    assert result["balanced"] is True
    # 两条隔离行被采纳：83 -> 85，隔离 7 -> 5
    assert counts["accepted"] == MAIN["accepted"] + 2
    assert counts["quarantined"] == MAIN["quarantined"] - 2
    assert counts["corrected"] == 2
    # 行账守恒
    assert counts["raw_records"] == counts["accepted"] + counts["duplicates"] + counts["quarantined"]
    # 网格账守恒
    assert counts["expected_buckets"] == counts["observed"] + counts["filled"] + counts["gaps"]
    # 落库 reconcile 也是平的
    view = batch_service.reconcile_view(conn, rid)
    assert view["balanced"] is True
    assert all(line["state"] == "OK" for line in view["lines"])


def test_promote_response_reports_actual_window_and_grid_changes(conn, run_main):
    """promote 回执给出本次替换真正改动的窗口与网格点，而不是替换后的空预览。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    result = correction_service.promote(conn, rid)
    impact = result["impact"]
    assert len(impact["window_changes"]) >= 1
    assert any(g["after"]["bucket_utc"] == "2026-03-01T00:22:00Z" for g in impact["grid_changes"])
    # 覆盖迁移：实有 +1、补齐 -1
    assert impact["coverage"]["after"]["observed"] == impact["coverage"]["before"]["observed"] + 1
    assert impact["coverage"]["after"]["filled"] == impact["coverage"]["before"]["filled"] - 1


def test_promote_marks_corrections_and_records_single_revision(conn, run_main):
    """替换后补正转 PROMOTED，并只登记一个修订版本。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    result = correction_service.promote(conn, rid)
    assert result["revision_no"] == 1
    item = correction_store.get_by_row(conn, rid, 72)
    assert item["status"] == correction_store.PROMOTED
    revisions = correction_store.list_revisions(conn, rid)
    assert len(revisions) == 1
    assert revisions[0]["state"] == correction_store.REV_PROMOTED
    assert revisions[0]["promoted_at"] is not None


def test_rerun_promote_does_not_create_new_revision(conn, run_main):
    """同一批补正重跑：无新草稿可替换时拒绝，且不新增版本。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)
    revisions_before = len(correction_store.list_revisions(conn, rid))
    with pytest.raises(StateError):
        correction_service.promote(conn, rid)
    assert len(correction_store.list_revisions(conn, rid)) == revisions_before


def test_promote_failure_rolls_back_and_preserves_old_version(conn, run_main, monkeypatch):
    """替换中途失败：整体回滚，正式结果保持旧版，并留 FAILED 修订。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    before = history.get(conn, rid)["counts"]

    def boom(*args, **kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr(correction_service, "_replace_result_tables", boom)
    with pytest.raises(StateError) as exc:
        correction_service.promote(conn, rid)
    assert exc.value.code == "PROMOTE_FAILED"

    # 旧版完整保留
    assert history.get(conn, rid)["counts"] == before
    assert store.value(conn, "SELECT COUNT(*) FROM samples WHERE run_id=?", (rid,), 0) == MAIN["accepted"]
    # 补正仍是草稿，可重试
    assert correction_store.get_by_row(conn, rid, 72)["status"] == correction_store.DRAFT
    # FAILED 修订留痕
    revisions = correction_store.list_revisions(conn, rid)
    assert revisions[0]["state"] == correction_store.REV_FAILED
    assert revisions[0]["error_code"] == "PROMOTE_FAILED"

    # 重试成功只产生一个 PROMOTED 版本（FAILED 之外）
    monkeypatch.undo()
    correction_service.promote(conn, rid)
    states = [r["state"] for r in correction_store.list_revisions(conn, rid)]
    assert states == [correction_store.REV_FAILED, correction_store.REV_PROMOTED]


def test_original_quarantine_evidence_is_never_deleted(conn, run_main):
    """原始证据保全：替换后隔离行、原值与原因码原样保留。"""
    rid = run_main["run_id"]
    original = quarantine_store.rows_of(conn, rid)
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)
    after = quarantine_store.rows_of(conn, rid)
    assert after == original
    assert len(after) == MAIN["quarantined"]


# ---------- 证据与导出来源一致 ----------

def test_corrected_cell_evidence_links_both_quarantine_and_correction(conn, run_main):
    """补正后的网格点能同时追回原隔离证据与补正记录。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)
    cell = evidence.cell(conn, rid, "TH-01", "2026-03-01T00:22:00Z")
    assert cell["origin"] == "CORRECTED"
    assert cell["state"] == "OBSERVED"
    assert cell["value"] == "46.6"
    assert cell["correction"] is not None
    assert cell["correction"]["status"] == "PROMOTED"
    # 原始隔离证据仍在
    assert len(cell["quarantine"]) == 1
    assert cell["quarantine"][0]["code"] == "OUT_OF_RANGE"


def test_accepted_csv_marks_origin(conn, run_main):
    """采纳 CSV 用 origin 列区分原始采纳与补正采纳，来源一致可追溯。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)
    text = export_service.accepted_csv(conn, rid)
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert "origin" in rows[0]
    origins = {row["origin"] for row in rows}
    assert origins == {"ORIGINAL", "CORRECTED"}
    corrected = [r for r in rows if r["origin"] == "CORRECTED"]
    assert len(corrected) == 1
    assert corrected[0]["source_row"] == "72"
    assert "CORRECTED_ADOPT" in corrected[0]["flags"]


def test_corrections_csv_export_matches_ledger(conn, run_main):
    """补正台账 CSV 与落库补正记录一致。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)
    text = export_service.corrections_csv(conn, rid)
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert len(rows) == 1
    assert rows[0]["source_row"] == "72"
    assert rows[0]["status"] == "PROMOTED"
    assert rows[0]["validate_code"] == ""
    assert rows[0]["attempts"] == "1"


def test_export_summary_includes_correction_file_only_after_promote(conn, run_main):
    """未替换时导出清单不含补正台账；替换后出现且行数正确。"""
    rid = run_main["run_id"]
    before = export_service.summary(conn, rid)
    assert all(f["name"] != "补正台账 CSV" for f in before["files"])
    correction_service.submit_correction(conn, rid, 72, _body(72))
    correction_service.promote(conn, rid)
    after = export_service.summary(conn, rid)
    ledger = [f for f in after["files"] if f["name"] == "补正台账 CSV"]
    assert len(ledger) == 1 and ledger[0]["rows"] == 1
    assert after["corrected"] == 1


# ---------- 撞键去重与多场景 ----------

def test_correction_colliding_existing_bucket_goes_to_duplicates(conn, run_main):
    """补正桶与既有采纳桶撞键时按统一去重规则处理，行账仍平衡。"""
    rid = run_main["run_id"]
    base_dups = run_main["counts"]["duplicates"]
    # 行 91（PM-02 OFF_RATE）补正到一个已存在的 PM-02 桶 00:06
    correction_service.submit_correction(
        conn,
        rid,
        91,
        _body(
            91,
            sensor_id="PM-02",
            sampled_at="2026-03-01T00:06:30Z",
            offset_seconds=90,
            value="40.0",
            unit="µg/m³",
        ),
    )
    result = correction_service.promote(conn, rid)
    counts = result["counts"]
    # 新增一条重复，采纳数不变（撞键的补正没进采纳）
    assert counts["duplicates"] == base_dups + 1
    assert counts["accepted"] == MAIN["accepted"]
    # 撞键补正从隔离搬到重复：隔离 -1、重复 +1，行账仍守恒
    assert counts["quarantined"] == MAIN["quarantined"] - 1
    assert counts["raw_records"] == counts["accepted"] + counts["duplicates"] + counts["quarantined"]


def test_correction_view_reports_per_row_status(conn, run_main):
    """质量页视图：每条隔离行都带补正状态与可补正标记。"""
    rid = run_main["run_id"]
    correction_service.submit_correction(conn, rid, 72, _body(72))
    view = correction_service.correction_view(conn, rid)
    assert len(view["rows"]) == MAIN["quarantined"]
    by_row = {row["source_row"]: row for row in view["rows"]}
    assert by_row[72]["correction"]["status"] == correction_store.DRAFT
    assert by_row[72]["correction"]["validation"]["ok"] is True
    # 其它隔离行（如行 52）尚无补正
    assert by_row[52]["correction"] is None
    # 未知传感器行标记为不可补正
    assert by_row[92]["correctable"] is False
    assert view["summary"]["valid_drafts"] == 1
    assert view["summary"]["not_correctable"] == 1


def test_correction_uses_same_contract_as_first_intake(conn, run_main):
    """补正校验与首采同规则：单位不符、坏时间、坏质量位都被拦下。"""
    rid = run_main["run_id"]
    cases = [
        (_body(62, sensor_id="WS-05", sampled_at="2026-03-01T00:18:12Z",
               offset_seconds=12, value="2.6", unit="km/h"), "UNIT_MISMATCH"),
        (_body(72, sampled_at="not-a-time"), "TIMESTAMP_INVALID"),
        (_body(72, quality="weird"), "QUALITY_UNKNOWN"),
        (_body(90, sensor_id="PM-02", sampled_at="2026-03-01T00:26:30Z",
               offset_seconds=90, value="err", unit="µg/m³"), "NOT_DECIMAL"),
    ]
    for body, expected_code in cases:
        saved = correction_service.submit_correction(conn, rid, body["source_row"], body)
        assert saved["validation"]["ok"] is False, (body, expected_code)
        assert saved["validation"]["code"] == expected_code
