"""补正校验：用与采信完全相同的契约与规则判定一条补正能否被采纳。

校验不依赖任何已落库结果，只看补正内容与传感器契约，因此同一份补正无论重投多少次
得到同一结论。校验通过才允许进入「可替换」集合；未通过的补正保留草稿态并附原因码，
绝不会冒充正常行被采纳。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app import config
from app.modules.m2_timebase import coerce, contract, offsets
from app.modules.m3_quality import rules


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str | None
    detail: str
    sensor_id: str | None = None
    bucket_utc: str | None = None
    value: Decimal | None = None
    unit: str | None = None
    quality: str | None = None
    offset_seconds: int | None = None
    ingest_seq: int | None = None


class _Row:
    """适配 rules.judge 的最小行结构（与 m2 NormRow 同形）。"""

    def __init__(self, sensor_id, spec, bucket, value_text, unit_text, quality, ingest_seq, offset):
        self.sensor_id = sensor_id
        self.spec = spec
        self.bucket = bucket
        self.value_text = value_text
        self.unit_text = unit_text
        self.quality = quality
        self.ingest_seq = ingest_seq
        self.offset = offset
        self.flags = ()


def check(payload: dict, offset_entries: list) -> CheckResult:
    """payload 字段：sensor_id / sampled_at / offset_seconds / value / unit / quality / ingest_seq。"""
    sensor_id = (payload.get("sensor_id") or "").strip()
    spec = contract.spec(sensor_id)
    if spec is None:
        return CheckResult(False, "UNKNOWN_SENSOR", f"契约表无此传感器：{sensor_id or '空'}")

    raw_time = payload.get("sampled_at") or ""
    try:
        instant = coerce.to_utc(raw_time)
    except coerce.CoerceError:
        return CheckResult(False, "TIMESTAMP_INVALID", f"时间无法解析：{raw_time or '空'}")

    resolution = offsets.resolve(offset_entries, sensor_id, instant)
    if resolution.ambiguous:
        return CheckResult(
            False, "OFFSET_AMBIGUOUS", "偏移表区间重叠，无法确定设备偏移"
        )

    reported = coerce.parse_seconds(str(payload.get("offset_seconds") if payload.get("offset_seconds") is not None else ""))
    if resolution.code == "OFFSET_ASSUMED":
        offset = reported if reported is not None else 0
    else:
        offset = resolution.seconds

    utc = instant - timedelta(seconds=offset)
    bucket = coerce.floor_minute(utc)

    quality = (payload.get("quality") or "").strip().lower() or config.DEFAULT_QUALITY
    ingest = coerce.parse_seconds(str(payload.get("ingest_seq") if payload.get("ingest_seq") is not None else ""))
    ingest_seq = ingest if ingest is not None else 0

    row = _Row(
        sensor_id=sensor_id,
        spec=spec,
        bucket=bucket,
        value_text=payload.get("value") if payload.get("value") is not None else "",
        unit_text=payload.get("unit") or "",
        quality=quality,
        ingest_seq=ingest_seq,
        offset=offset,
    )
    verdict = rules.judge(row, None)
    bucket_text = bucket.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not verdict.ok:
        return CheckResult(False, verdict.code, verdict.detail, sensor_id, bucket_text)

    return CheckResult(
        ok=True,
        code=None,
        detail="校验通过",
        sensor_id=sensor_id,
        bucket_utc=bucket_text,
        value=verdict.value,
        unit=spec.unit,
        quality=quality,
        offset_seconds=offset,
        ingest_seq=ingest_seq,
    )
