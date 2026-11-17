"""M8 隔离补正的入口：补正草稿、影响预览、整批替换、修订历史。

所有写操作都针对单个批次（默认最近批次），原始批次数据不可变，正式结果只在
POST /promote 成功后才替换；未通过校验的补正只能停在草稿态。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import errors, store
from app.modules.m6_batches import history

from . import service

router = APIRouter(tags=["m8 隔离补正"])


def _resolve_run(conn, run_id: str | None) -> str:
    if run_id:
        if history.get(conn, run_id) is None:
            raise errors.NotFoundError("批次不存在", code="RUN_NOT_FOUND", detail={"run_id": run_id})
        return run_id
    latest = history.latest(conn)
    if latest is None:
        raise errors.NotFoundError("还没有批次", code="NO_RUN")
    return latest["run_id"]


class CorrectionBody(BaseModel):
    source_row: int = Field(ge=1, le=10_000_000)
    sensor_id: str = Field(default="", max_length=32)
    sampled_at: str = Field(default="", max_length=40)
    offset_seconds: int | None = Field(default=None, ge=-100_000, le=100_000)
    value: str | None = Field(default=None, max_length=64)
    unit: str = Field(default="", max_length=16)
    quality: str = Field(default="", max_length=16)
    ingest_seq: int | None = Field(default=None, ge=0, le=10_000_000)
    note: str = Field(default="", max_length=200)
    run_id: str | None = Field(default=None, max_length=64)


@router.get("/api/corrections")
def corrections_api(run_id: str | None = Query(default=None, max_length=64)):
    conn = store.session()
    try:
        target = _resolve_run(conn, run_id)
        return service.correction_view(conn, target)
    finally:
        conn.close()


@router.post("/api/corrections")
def submit_correction(body: CorrectionBody):
    conn = store.session()
    try:
        target = _resolve_run(conn, body.run_id)
        saved = service.submit_correction(conn, target, body.source_row, body.model_dump())
        return JSONResponse(saved)
    finally:
        conn.close()


@router.get("/api/corrections/preview")
def preview_api(run_id: str | None = Query(default=None, max_length=64)):
    conn = store.session()
    try:
        target = _resolve_run(conn, run_id)
        return service.preview_impact(conn, target)
    finally:
        conn.close()


@router.post("/api/corrections/promote")
def promote_api(run_id: str | None = Query(default=None, max_length=64)):
    conn = store.session()
    try:
        target = _resolve_run(conn, run_id)
        return service.promote(conn, target)
    finally:
        conn.close()


@router.get("/api/corrections/revisions")
def revisions_api(run_id: str | None = Query(default=None, max_length=64)):
    from . import store as correction_store

    conn = store.session()
    try:
        target = _resolve_run(conn, run_id)
        return {
            "run_id": target,
            "revisions": correction_store.list_revisions(conn, target),
        }
    finally:
        conn.close()
