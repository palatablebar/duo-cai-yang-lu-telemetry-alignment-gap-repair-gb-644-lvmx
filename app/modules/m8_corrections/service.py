"""补正编排：草稿提交、影响预览、整批替换（promote）与修订版本登记。

工作流两阶段，正式结果只有值班员显式 promote 才会被替换：
1. submit：校验补正 → 存草稿（DRAFT）。未通过校验的补正也留痕，但永远停在草稿态，
   不能被采纳。同一内容重投只累加 attempts，不新增行、不新增版本。
2. promote：把全部「校验通过的草稿」在一个事务内推导并整批替换 samples/grid/windows/
   duplicates/reconcile，刷新 runs 计数，补正转 PROMOTED，登记修订版本。任何一步失败
   整体回滚，正式结果保持上一版；修订版本留 FAILED 记录但不产生新版本数据。
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_HALF_UP, Decimal

from app import config, errors, manifest, store
from app.modules.m3_quality import quarantine as quarantine_store
from app.modules.m3_quality import service as quality_service
from app.modules.m4_grid import service as grid_service
from app.modules.m5_gaps import service as gaps_service
from app.modules.m6_batches import compare, history

from . import derive as derive_mod
from . import impact as impact_mod
from . import store as correction_store
from . import validation

# 允许补正的隔离原因：行级可修正字段。结构性损坏（列数/截断/空文件）无法逐行补正。
CORRECTABLE_CODES = {
    "SENTINEL_VALUE",
    "NOT_DECIMAL",
    "OUT_OF_RANGE",
    "UNIT_MISMATCH",
    "QUALITY_UNKNOWN",
    "OFF_RATE",
    "TIMESTAMP_INVALID",
    "OFFSET_AMBIGUOUS",
}


def _now() -> str:
    return history.now_text()


def _content_hash(payload: dict) -> str:
    material = manifest.canonical_json(payload, )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _correction_id(run_id: str, source_row: int) -> str:
    digest = hashlib.sha256(f"{run_id}:{source_row}".encode("utf-8")).hexdigest()[:8].upper()
    return f"CR-{source_row:04d}-{digest}"


def _quarantine_row(conn, run_id: str, source_row: int) -> dict:
    rows = [
        dict(row)
        for row in store.rows(
            conn,
            "SELECT * FROM quarantine WHERE run_id = ? AND source_row = ? ORDER BY code",
            (run_id, source_row),
        )
    ]
    if not rows:
        raise errors.NotFoundError(
            "该原始行不在隔离区，无需补正",
            code="ROW_NOT_QUARANTINED",
            detail={"run_id": run_id, "source_row": source_row},
        )
    return rows[0]


def _payload_fields(body: dict) -> dict:
    return {
        "sensor_id": (body.get("sensor_id") or "").strip(),
        "sampled_at": (body.get("sampled_at") or "").strip(),
        "offset_seconds": body.get("offset_seconds"),
        "value": body.get("value"),
        "unit": (body.get("unit") or "").strip(),
        "quality": (body.get("quality") or "").strip().lower(),
        "ingest_seq": body.get("ingest_seq"),
    }


def submit_correction(conn, run_id: str, source_row: int, body: dict) -> dict:
    """登记一条补正草稿；同一内容重投幂等，不同内容覆盖草稿并累加尝试次数。"""
    run = history.get(conn, run_id)
    if run is None:
        raise errors.NotFoundError("批次不存在", code="RUN_NOT_FOUND", detail={"run_id": run_id})

    quarantined = _quarantine_row(conn, run_id, source_row)
    if quarantined["code"] not in CORRECTABLE_CODES:
        raise errors.InputError(
            "该隔离原因属于结构性损坏，不支持逐行补正",
            code="NOT_CORRECTABLE",
            detail={"source_row": source_row, "code": quarantined["code"]},
        )

    existing = correction_store.get_by_row(conn, run_id, source_row)
    if existing is not None and existing["status"] == correction_store.PROMOTED:
        raise errors.StateError(
            "该隔离行已有生效补正，不能重复提交",
            code="CORRECTION_ALREADY_PROMOTED",
            detail={"source_row": source_row, "correction_id": existing["correction_id"]},
        )

    fields = _payload_fields(body)
    note = (body.get("note") or "").strip()[:200]
    content_hash = _content_hash(fields)
    offset_entries = derive_mod.load_offset_entries(conn, run_id)
    checked = validation.check(fields, offset_entries)

    now = _now()
    if existing is None:
        correction_id = _correction_id(run_id, source_row)
        correction_store.insert(
            conn,
            {
                "correction_id": correction_id,
                "run_id": run_id,
                "source_row": source_row,
                "status": correction_store.DRAFT,
                "sensor_id": fields["sensor_id"] or None,
                "sampled_at": fields["sampled_at"] or None,
                "offset_seconds": checked.offset_seconds,
                "value_text": None if fields["value"] is None else str(fields["value"]),
                "unit_text": fields["unit"] or None,
                "quality": fields["quality"] or None,
                "ingest_seq": fields["ingest_seq"],
                "note": note,
                "validate_code": checked.code,
                "validate_detail": checked.detail,
                "bucket_utc": checked.bucket_utc,
                "content_hash": content_hash,
                "attempts": 1,
                "created_at": now,
                "updated_at": now,
            },
        )
        saved = correction_store.get(conn, run_id, correction_id)
        saved["resubmitted"] = False
    else:
        # 同一补正位再次提交：无论内容是否变化都不新增行/版本；内容变化视为新一轮尝试
        correction_store.bump_attempt(
            conn,
            run_id,
            existing["correction_id"],
            updated_at=now,
            validate_code=checked.code,
            validate_detail=checked.detail,
            bucket_utc=checked.bucket_utc,
        )
        conn.execute(
            "UPDATE corrections SET sensor_id = ?, sampled_at = ?, offset_seconds = ?, value_text = ?,"
            " unit_text = ?, quality = ?, ingest_seq = ?, note = ?, content_hash = ?"
            " WHERE run_id = ? AND correction_id = ?",
            (
                fields["sensor_id"] or None,
                fields["sampled_at"] or None,
                checked.offset_seconds,
                None if fields["value"] is None else str(fields["value"]),
                fields["unit"] or None,
                fields["quality"] or None,
                fields["ingest_seq"],
                note,
                content_hash,
                run_id,
                existing["correction_id"],
            ),
        )
        saved = correction_store.get(conn, run_id, existing["correction_id"])
        saved["resubmitted"] = True

    saved["validation"] = {
        "ok": checked.ok,
        "code": checked.code,
        "detail": checked.detail,
        "bucket_utc": checked.bucket_utc,
    }
    return saved


def _quantize(value: Decimal) -> Decimal:
    return Decimal(value).quantize(
        Decimal(1).scaleb(-config.VALUE_PLACES), rounding=ROUND_HALF_UP
    )


def _recompute_counts(conn, run_id: str, derived, raw_records: int, rejects: int) -> tuple[dict, dict]:
    accepted_sum = sum((row.value for row in derived.aligned.kept), Decimal("0"))
    observed_sum = sum(
        (point.value for point in derived.repaired.points if point.state == "OBSERVED"), Decimal("0")
    )
    filled_sum = sum(
        (point.value for point in derived.repaired.points if point.state == "FILLED"), Decimal("0")
    )
    corrected_count = len(derived.correction_rows)
    new_duplicates = len(derived.aligned.duplicates)
    # 补正桶撞到既有采纳桶而被去重的补正行：没有真正进入采纳，对应隔离行仍算隔离。
    collision_duplicates = len(derived.correction_duplicates)
    adopted_corrections = corrected_count - collision_duplicates
    # 行账守恒：原始行 = 采纳 + 重复 + 仍隔离。
    # 补正成功一条就把一条隔离行搬到采纳侧；撞键被去重的补正不消化隔离行。
    # 原始采纳内部的重复（base duplicates）始终保留在 duplicates，不参与补正消化。
    still_quarantined = rejects - adopted_corrections
    # 健全性：补正只能消化隔离行，不能让仍隔离变成负数或超过原隔离数
    if still_quarantined < 0 or still_quarantined > rejects:
        raise errors.StateError(
            "补正后隔离行数越界，已放弃替换并保留旧版结果",
            code="QUARANTINE_COUNT_INVALID",
            detail={"still_quarantined": still_quarantined, "original_quarantined": rejects},
        )
    counts = {
        "raw_records": raw_records,
        "accepted": len(derived.aligned.kept),
        "duplicates": new_duplicates,
        "quarantined": still_quarantined,
        "expected_buckets": sum(len(item) for item in derived.analysis.expected.values()),
        "observed": derived.repaired.observed,
        "filled": derived.repaired.filled,
        "gaps": derived.repaired.gaps,
        "grid_points": derived.repaired.total,
        "windows": len(derived.analysis.windows),
        "windows_stored": len(derived.analysis.windows),
        "blank_lines": int(store.value(
            conn, "SELECT blank_lines FROM intake WHERE run_id = ?", (run_id,), 0
        )),
        "segments": int(store.value(
            conn, "SELECT segments FROM intake WHERE run_id = ?", (run_id,), 0
        )),
        "truncated": int(store.value(
            conn, "SELECT truncated FROM intake WHERE run_id = ?", (run_id,), 0
        )),
        "late": sum(1 for row in derived.aligned.kept if row.late),
        "empty_code": history.get(conn, run_id)["counts"].get("empty_code"),
        "corrected": corrected_count,
        "balanced": False,
    }
    totals = {
        "accepted_sum": str(_quantize(accepted_sum)),
        "observed_sum": str(_quantize(observed_sum)),
        "filled_sum": str(_quantize(filled_sum)),
        "grid_sum": str(_quantize(observed_sum + filled_sum)),
    }
    return counts, totals


def _replace_result_tables(conn, run_id: str, derived, counts: dict, totals: dict) -> None:
    """在事务内清空并重写正式结果表；原始 intake/offsets/quarantine 不动。"""
    for table in ("samples", "duplicates", "grid", "windows", "reconcile"):
        conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    quality_service.persist_samples(conn, run_id, derived.aligned.kept)
    grid_service.persist_duplicates(conn, run_id, derived.aligned.duplicates)
    gaps_service.persist_grid(conn, run_id, derived.repaired.points)
    gaps_service.persist_windows(conn, run_id, derived.analysis.windows)

    stored_windows = int(
        store.value(conn, "SELECT COUNT(*) FROM windows WHERE run_id = ?", (run_id,), 0)
    )
    # 窗口账以实际落库数为准，防止内存推导与持久化之间出现静默漂移
    counts["windows_stored"] = stored_windows
    lines = compare.reconcile_lines(counts, totals)
    counts["balanced"] = compare.is_balanced(lines)
    conn.executemany(
        "INSERT INTO reconcile(run_id, line_no, name, expected, actual, delta, state, note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                run_id,
                line.line_no,
                line.name,
                str(line.expected),
                str(line.actual),
                str(line.delta),
                line.state,
                line.note,
            )
            for line in lines
        ],
    )


def promote(conn, run_id: str) -> dict:
    """把全部校验通过的草稿补正整批替换为正式结果；失败回滚并保留旧版。"""
    run = history.get(conn, run_id)
    if run is None:
        raise errors.NotFoundError("批次不存在", code="RUN_NOT_FOUND", detail={"run_id": run_id})

    drafts = correction_store.draft_rows(conn, run_id)
    valid_drafts = [item for item in drafts if item["validate_code"] is None]
    invalid_drafts = [item for item in drafts if item["validate_code"] is not None]
    if not valid_drafts:
        raise errors.StateError(
            "没有校验通过的补正草稿，无法替换正式结果",
            code="NO_VALID_CORRECTION",
            detail={"invalid_drafts": len(invalid_drafts)},
        )

    revision_no = correction_store.next_revision_no(conn, run_id)
    correction_ids = [item["correction_id"] for item in valid_drafts]

    raw_records = run["counts"]["raw_records"]
    total_quarantined = quarantine_store.count(conn, run_id)

    # 在任何写操作之前先算影响快照，作为本次替换实际改动的窗口/网格点回执
    pre_derived = derive_mod.derive(conn, run_id, valid_drafts)
    impact_snapshot = {
        "window_changes": impact_mod.window_impact(conn, run_id, pre_derived),
        "grid_changes": impact_mod.grid_impact(conn, run_id, pre_derived),
        "coverage": impact_mod.coverage_summary(conn, run_id, pre_derived),
        "affected_windows": impact_mod.affected_window_keys(valid_drafts),
    }

    domain_failure: errors.GridlabError | None = None
    try:
        with store.transaction(conn):
            correction_store.insert_revision(
                conn,
                {
                    "run_id": run_id,
                    "revision_no": revision_no,
                    "state": correction_store.REV_DRAFT,
                    "base_run_id": run_id,
                    "correction_ids": correction_ids,
                    "counts": None,
                    "totals": None,
                },
            )
            derived = derive_mod.derive(conn, run_id, valid_drafts)
            counts, totals = _recompute_counts(
                conn, run_id, derived, raw_records, total_quarantined
            )
            # 替换前先验证四账必然平衡，不平衡直接抛错回滚，旧版原样保留
            lines = compare.reconcile_lines(counts, totals)
            if not compare.is_balanced(lines):
                raise errors.StateError(
                    "补正后四账不平，已放弃替换并保留旧版结果",
                    code="RECONCILE_UNBALANCED",
                    detail={"lines": [compare.as_dict(line) for line in lines]},
                )
            _replace_result_tables(conn, run_id, derived, counts, totals)

            conn.execute(
                "UPDATE runs SET counts_json = ?, totals_json = ? WHERE run_id = ?",
                (
                    manifest.canonical_json(counts),
                    manifest.canonical_json(totals),
                    run_id,
                ),
            )
            now = _now()
            correction_store.mark_promoted(conn, run_id, correction_ids, now)
            correction_store.mark_revision_promoted(
                conn, run_id, revision_no, now, counts, totals
            )
    except errors.GridlabError as exc:
        domain_failure = exc
        failure_code, failure_message = exc.code, exc.message
    except Exception as exc:  # noqa: BLE001 - 任何失败都必须留下 FAILED 痕迹且不破坏旧版
        failure_code = "PROMOTE_FAILED"
        failure_message = (str(exc)[:200] or exc.__class__.__name__)
    else:
        failure_code, failure_message = None, None

    if failure_code is not None:
        # 事务已整体回滚：正式结果、补正草稿均保持替换前状态。
        # 在新事务里只登记一条 FAILED 修订（同号已随回滚释放），不产生任何新版本数据。
        with store.transaction(conn):
            correction_store.insert_revision(
                conn,
                {
                    "run_id": run_id,
                    "revision_no": revision_no,
                    "state": correction_store.REV_FAILED,
                    "base_run_id": run_id,
                    "correction_ids": correction_ids,
                    "counts": None,
                    "totals": None,
                    "error_code": failure_code,
                    "error_detail": failure_message,
                },
            )
        if domain_failure is not None:
            raise domain_failure
        raise errors.StateError(
            "替换过程失败，已回滚并保留旧版结果",
            code="PROMOTE_FAILED",
            detail={"reason": failure_message},
        )

    refreshed = history.get(conn, run_id)
    return {
        "run_id": run_id,
        "revision_no": revision_no,
        "promoted": correction_ids,
        "skipped_invalid": [
            {"correction_id": item["correction_id"], "code": item["validate_code"]}
            for item in invalid_drafts
        ],
        "counts": refreshed["counts"],
        "totals": refreshed["totals"],
        "balanced": refreshed["counts"].get("balanced", False),
        "impact": impact_snapshot,
    }


def preview_impact(conn, run_id: str) -> dict:
    """只读预览：当前校验通过的草稿若替换，会改动哪些窗口与网格点。"""
    run = history.get(conn, run_id)
    if run is None:
        raise errors.NotFoundError("批次不存在", code="RUN_NOT_FOUND", detail={"run_id": run_id})
    valid_drafts = [
        item for item in correction_store.draft_rows(conn, run_id) if item["validate_code"] is None
    ]
    if not valid_drafts:
        return {
            "run_id": run_id,
            "has_preview": False,
            "corrections": [],
            "window_changes": [],
            "grid_changes": [],
            "coverage": None,
            "affected_windows": [],
        }
    derived = derive_mod.derive(conn, run_id, valid_drafts)
    return {
        "run_id": run_id,
        "has_preview": True,
        "corrections": [
            {
                "correction_id": item["correction_id"],
                "source_row": item["source_row"],
                "sensor_id": item["sensor_id"],
                "bucket_utc": item["bucket_utc"],
                "value_text": item["value_text"],
            }
            for item in valid_drafts
        ],
        "window_changes": impact_mod.window_impact(conn, run_id, derived),
        "grid_changes": impact_mod.grid_impact(conn, run_id, derived),
        "coverage": impact_mod.coverage_summary(conn, run_id, derived),
        "affected_windows": impact_mod.affected_window_keys(valid_drafts),
    }


def correction_view(conn, run_id: str) -> dict:
    """质量页用：每条隔离行的补正状态 + 草稿影响 + 修订历史。"""
    all_items = correction_store.list_for_run(conn, run_id)
    by_row = {item["source_row"]: item for item in all_items}
    quarantined = quarantine_store.rows_of(conn, run_id)
    rows = []
    for entry in quarantined:
        item = by_row.get(entry["source_row"])
        rows.append(
            {
                "source_row": entry["source_row"],
                "quarantine_code": entry["code"],
                "quarantine_label": entry["label"],
                "sensor_id": entry["sensor_id"],
                "column_name": entry["column_name"],
                "raw_value": entry["raw_value"],
                "detail": entry["detail"],
                "correctable": entry["code"] in CORRECTABLE_CODES,
                "correction": _public_correction(item),
            }
        )
    drafts = correction_store.draft_rows(conn, run_id)
    return {
        "run_id": run_id,
        "rows": rows,
        "summary": {
            "quarantined": len(quarantined),
            "drafts": len(drafts),
            "valid_drafts": sum(1 for item in drafts if item["validate_code"] is None),
            "invalid_drafts": sum(1 for item in drafts if item["validate_code"] is not None),
            "promoted": sum(1 for item in all_items if item["status"] == correction_store.PROMOTED),
            "not_correctable": sum(1 for entry in quarantined if entry["code"] not in CORRECTABLE_CODES),
        },
        "revisions": correction_store.list_revisions(conn, run_id),
    }


def _public_correction(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {
        "correction_id": item["correction_id"],
        "status": item["status"],
        "source_row": item["source_row"],
        "sensor_id": item["sensor_id"],
        "sampled_at": item["sampled_at"],
        "bucket_utc": item["bucket_utc"],
        "value_text": item["value_text"],
        "unit_text": item["unit_text"],
        "quality": item["quality"],
        "offset_seconds": item["offset_seconds"],
        "ingest_seq": item["ingest_seq"],
        "note": item["note"],
        "attempts": item["attempts"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "validation": {
            "ok": item["validate_code"] is None,
            "code": item["validate_code"],
            "detail": item["validate_detail"],
        },
    }
