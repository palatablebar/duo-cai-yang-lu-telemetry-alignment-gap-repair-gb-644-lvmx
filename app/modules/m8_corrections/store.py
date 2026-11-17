"""补正落盘：补正草稿、补正尝试与批次修订版本。

补正记录与原始隔离行通过 (run_id, source_row) 一一对应；同一补正内容重投只累加
attempts、刷新 updated_at，不新增行（content_hash 是幂等键）。原始批次表永不改写，
修订版本单独登记在 run_revisions，正式结果只有在 promote 成功后才整批替换。
"""

from __future__ import annotations

import json

from app import store

DRAFT = "DRAFT"
PROMOTED = "PROMOTED"
CORRECTION_STATES = (DRAFT, PROMOTED)

# 修订版本状态机：DRAFT（仅草稿，未替换）→ PROMOTED（已替换正式结果）/ FAILED（保留旧版）
REV_DRAFT = "DRAFT"
REV_PROMOTED = "PROMOTED"
REV_FAILED = "FAILED"


def correction_exists(conn, run_id: str, source_row: int) -> bool:
    return store.one(
        conn,
        "SELECT 1 FROM corrections WHERE run_id = ? AND source_row = ?",
        (run_id, source_row),
    ) is not None


def get(conn, run_id: str, correction_id: str) -> dict | None:
    row = store.one(
        conn,
        "SELECT * FROM corrections WHERE run_id = ? AND correction_id = ?",
        (run_id, correction_id),
    )
    return _to_dict(row)


def get_by_row(conn, run_id: str, source_row: int) -> dict | None:
    row = store.one(
        conn,
        "SELECT * FROM corrections WHERE run_id = ? AND source_row = ?",
        (run_id, source_row),
    )
    return _to_dict(row)


def list_for_run(conn, run_id: str) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM corrections WHERE run_id = ? ORDER BY source_row, correction_id",
        (run_id,),
    )
    return [_to_dict(row) for row in rows]


def draft_rows(conn, run_id: str) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM corrections WHERE run_id = ? AND status = ? ORDER BY source_row",
        (run_id, DRAFT),
    )
    return [_to_dict(row) for row in rows]


def promoted_ids(conn, run_id: str) -> list[str]:
    rows = store.rows(
        conn,
        "SELECT correction_id FROM corrections WHERE run_id = ? AND status = ? ORDER BY source_row",
        (run_id, PROMOTED),
    )
    return [row["correction_id"] for row in rows]


def insert(conn, item: dict) -> None:
    conn.execute(
        "INSERT INTO corrections(correction_id, run_id, source_row, status, sensor_id, sampled_at,"
        " offset_seconds, value_text, unit_text, quality, ingest_seq, note, validate_code,"
        " validate_detail, bucket_utc, content_hash, attempts, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item["correction_id"],
            item["run_id"],
            item["source_row"],
            item["status"],
            item.get("sensor_id"),
            item.get("sampled_at"),
            item.get("offset_seconds"),
            item.get("value_text"),
            item.get("unit_text"),
            item.get("quality"),
            item.get("ingest_seq"),
            item.get("note", ""),
            item.get("validate_code"),
            item.get("validate_detail"),
            item.get("bucket_utc"),
            item["content_hash"],
            item["attempts"],
            item["created_at"],
            item["updated_at"],
        ),
    )


def bump_attempt(conn, run_id: str, correction_id: str, *, updated_at: str,
                 validate_code: str | None, validate_detail: str | None,
                 bucket_utc: str | None) -> None:
    """同一补正重投：尝试次数 +1，覆盖校验结论，不新增版本、不新增行。"""
    conn.execute(
        "UPDATE corrections SET attempts = attempts + 1, updated_at = ?, validate_code = ?,"
        " validate_detail = ?, bucket_utc = ? WHERE run_id = ? AND correction_id = ?",
        (updated_at, validate_code, validate_detail, bucket_utc, run_id, correction_id),
    )


def mark_promoted(conn, run_id: str, correction_ids: list[str], updated_at: str) -> None:
    if not correction_ids:
        return
    conn.executemany(
        "UPDATE corrections SET status = ?, updated_at = ? WHERE run_id = ? AND correction_id = ?",
        [(PROMOTED, updated_at, run_id, cid) for cid in correction_ids],
    )


def revision_latest(conn, run_id: str) -> dict | None:
    row = store.one(
        conn,
        "SELECT * FROM run_revisions WHERE run_id = ? ORDER BY revision_no DESC LIMIT 1",
        (run_id,),
    )
    return _revision_to_dict(row)


def revision_by_no(conn, run_id: str, revision_no: int) -> dict | None:
    row = store.one(
        conn,
        "SELECT * FROM run_revisions WHERE run_id = ? AND revision_no = ?",
        (run_id, revision_no),
    )
    return _revision_to_dict(row)


def list_revisions(conn, run_id: str) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM run_revisions WHERE run_id = ? ORDER BY revision_no",
        (run_id,),
    )
    return [_revision_to_dict(row) for row in rows]


def next_revision_no(conn, run_id: str) -> int:
    current = store.value(
        conn, "SELECT COALESCE(MAX(revision_no), 0) FROM run_revisions WHERE run_id = ?", (run_id,), 0
    )
    return int(current) + 1


def insert_revision(conn, item: dict) -> None:
    conn.execute(
        "INSERT INTO run_revisions(run_id, revision_no, state, base_run_id, promoted_at,"
        " correction_ids_json, counts_json, totals_json, error_code, error_detail)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            item["run_id"],
            item["revision_no"],
            item["state"],
            item["base_run_id"],
            item.get("promoted_at"),
            json.dumps(item.get("correction_ids", []), ensure_ascii=False),
            json.dumps(item["counts"], ensure_ascii=False) if item.get("counts") is not None else None,
            json.dumps(item["totals"], ensure_ascii=False) if item.get("totals") is not None else None,
            item.get("error_code"),
            item.get("error_detail"),
        ),
    )


def mark_revision_promoted(conn, run_id: str, revision_no: int, promoted_at: str,
                           counts: dict, totals: dict) -> None:
    conn.execute(
        "UPDATE run_revisions SET state = ?, promoted_at = ?, counts_json = ?, totals_json = ?,"
        " error_code = NULL, error_detail = NULL WHERE run_id = ? AND revision_no = ?",
        (
            REV_PROMOTED,
            promoted_at,
            json.dumps(counts, ensure_ascii=False),
            json.dumps(totals, ensure_ascii=False),
            run_id,
            revision_no,
        ),
    )


def mark_revision_failed(conn, run_id: str, revision_no: int, code: str, detail: str) -> None:
    conn.execute(
        "UPDATE run_revisions SET state = ?, error_code = ?, error_detail = ?"
        " WHERE run_id = ? AND revision_no = ?",
        (REV_FAILED, code, detail[:300], run_id, revision_no),
    )


def _to_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "correction_id": row["correction_id"],
        "run_id": row["run_id"],
        "source_row": row["source_row"],
        "status": row["status"],
        "sensor_id": row["sensor_id"],
        "sampled_at": row["sampled_at"],
        "offset_seconds": row["offset_seconds"],
        "value_text": row["value_text"],
        "unit_text": row["unit_text"],
        "quality": row["quality"],
        "ingest_seq": row["ingest_seq"],
        "note": row["note"],
        "validate_code": row["validate_code"],
        "validate_detail": row["validate_detail"],
        "bucket_utc": row["bucket_utc"],
        "content_hash": row["content_hash"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _revision_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "revision_no": row["revision_no"],
        "state": row["state"],
        "base_run_id": row["base_run_id"],
        "promoted_at": row["promoted_at"],
        "correction_ids": json.loads(row["correction_ids_json"] or "[]"),
        "counts": json.loads(row["counts_json"]) if row["counts_json"] else None,
        "totals": json.loads(row["totals_json"]) if row["totals_json"] else None,
        "error_code": row["error_code"],
        "error_detail": row["error_detail"],
    }
