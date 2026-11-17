"""M8 隔离补正的 HTTP 端到端：接口状态码、质量页、幂等重投与 409 语义。"""

from __future__ import annotations


def _correction(**over):
    payload = {
        "source_row": 72,
        "sensor_id": "TH-01",
        "sampled_at": "2026-03-01T00:22:20Z",
        "offset_seconds": 20,
        "value": "46.6",
        "unit": "%RH",
        "quality": "good",
    }
    payload.update(over)
    return payload


def test_correction_api_full_flow(api):
    """草稿 → 预览 → 替换 → 修订，整条链路通过 HTTP 跑通。"""
    api.post("/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"})

    listing = api.get("/api/corrections")
    assert listing.status_code == 200
    assert listing.json()["summary"]["quarantined"] == 7

    created = api.post("/api/corrections", json=_correction())
    assert created.status_code == 200
    body = created.json()
    assert body["validation"]["ok"] is True
    correction_id = body["correction_id"]

    # 相同内容重投：同一 correction_id，不新增版本
    again = api.post("/api/corrections", json=_correction())
    assert again.json()["correction_id"] == correction_id
    assert api.get("/api/corrections/revisions").json()["revisions"] == []

    preview = api.get("/api/corrections/preview")
    assert preview.status_code == 200
    assert preview.json()["has_preview"] is True
    assert len(preview.json()["window_changes"]) >= 1

    promoted = api.post("/api/corrections/promote")
    assert promoted.status_code == 200
    payload = promoted.json()
    assert payload["revision_no"] == 1
    assert payload["balanced"] is True
    assert payload["counts"]["accepted"] == 84

    revisions = api.get("/api/corrections/revisions").json()["revisions"]
    assert len(revisions) == 1 and revisions[0]["state"] == "PROMOTED"

    # 再次替换：无有效草稿，409
    conflict = api.post("/api/corrections/promote")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "NO_VALID_CORRECTION"


def test_invalid_correction_rejected_from_promote(api):
    """未通过校验的草稿不能驱动替换，接口给出 409 而不是静默采纳。"""
    api.post("/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"})
    resp = api.post("/api/corrections", json=_correction(value="999.0"))
    assert resp.json()["validation"]["code"] == "OUT_OF_RANGE"
    conflict = api.post("/api/corrections/promote")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "NO_VALID_CORRECTION"


def test_correction_unknown_run_404(api):
    """针对不存在的批次操作给 404。"""
    resp = api.post("/api/corrections", json=_correction(run_id="GL-NOPE"))
    assert resp.status_code == 404
    assert resp.json()["error"] == "RUN_NOT_FOUND"


def test_quality_page_shows_correction_workflow(api):
    """质量页包含补正状态列、编辑器与替换区。"""
    api.post("/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"})
    page = api.get("/quality")
    assert page.status_code == 200
    text = page.text
    assert "未补正" in text
    assert "隔离行与补正状态" in text
    assert "替换影响预览" in text
    assert "/static/corrections.js" in text

    api.post("/api/corrections", json=_correction())
    api.post("/api/corrections/promote")
    after = api.get("/quality").text
    assert "已生效" in after
    assert "修订版本" in after


def test_corrections_csv_export_endpoint(api):
    """补正台账 CSV 端点在替换后可下载。"""
    api.post("/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"})
    api.post("/api/corrections", json=_correction())
    api.post("/api/corrections/promote")
    resp = api.get("/api/export/corrections.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.content.decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert len(lines) == 2  # 表头 + 一条补正
    assert "correction_id" in lines[0]


def test_evidence_endpoint_exposes_correction_provenance(api):
    """单元格证据接口返回来源标记与补正记录。"""
    api.post("/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"})
    api.post("/api/corrections", json=_correction())
    api.post("/api/corrections/promote")
    resp = api.get(
        "/api/export/evidence",
        params={"sensor_id": "TH-01", "bucket": "2026-03-01T00:22:00Z"},
    )
    payload = resp.json()
    assert payload["origin"] == "CORRECTED"
    assert payload["correction"]["status"] == "PROMOTED"
    assert len(payload["quarantine"]) == 1
