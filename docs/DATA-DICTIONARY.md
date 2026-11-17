# 定义契约 gridlab

## 采样行

| 列 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|
| `sensor_id` | 字符串 | — | 否 | 契约表内的传感器编号，区分大小写 |
| `sampled_at` | ISO8601 | — | 否 | 设备时钟记录的绝对时刻，可带 `Z` 或 `+08:00` |
| `offset_seconds` | 整数 | 秒 | 是 | 设备自报的时钟偏移，只作对照；真值以偏移表为准 |
| `value` | 十进制 | 见单位列 | 是 | 允许千分位与全角数字；空值哨兵见下 |
| `unit` | 字符串 | — | 否 | 写法归一后必须等于契约单位 |
| `quality` | 枚举 | — | 是 | `good` / `suspect` / `bad`，缺省按 `good` |
| `ingest_seq` | 整数 | — | 是 | 到达顺序，缺省用原始行号 |

## 数值

- 一切用量走 `Decimal`，落库以字符串保存，最小刻度 `0.001`；对外 JSON 也是字符串，禁止 float 往返。
- 补齐值按 `0.001` 四舍五入（`ROUND_HALF_UP`），锚点值原样保留。
- 覆盖度保留 `0.001`，页面按百分比显示一位小数。

## 时间

- 存储与计算一律 UTC，秒级，格式 `YYYY-MM-DDTHH:MM:SSZ`。
- 页面显示 `+08:00`，导出 CSV 同时给 UTC 与 `+08:00` 两列。
- 分钟桶：`[t, t+1min)`；跨日不特殊处理，`23:59:30` 落到当日 `23:59` 桶。
- 15 分钟窗口：`[w, w+15min)`，右端开；端点 `00:15:00` 属于 `[00:15, 00:30)`。

## 偏移与质量

- 偏移表条目为 `[effective_from, effective_to)`；同一传感器同一时刻命中两段即 `OFFSET_AMBIGUOUS`，该行隔离。
- 无条目时按行内 `offset_seconds`（缺省 0）校正，并标 `OFFSET_ASSUMED`。
- 表内偏移与行内偏移不一致时以表为准，标 `OFFSET_CONFLICT`，不隔离。

## 空值三态

| 写法 | 含义 | 处理 |
|---|---|---|
| 字段缺失 / 空串 | 未采集 | 隔离，原因码 `SENTINEL_VALUE` |
| `-` `--` `N/A` `null` `未知` `空` `无` | 哨兵 | 隔离，原因码 `SENTINEL_VALUE` |
| `0` | 真实零值 | 采纳，进合计 |

哨兵清单只在 `app/config.py` 定义一次。

## 去重键

- 去重键为 `(sensor_id, 分钟桶)`，不是全行哈希：同一分钟内的两次采样代表同一物理时刻，必须收敛为一条。
- 择一顺序：质量位 `good > suspect > bad` → `ingest_seq` 较大者 → 原始行号较大者；全序且与文件顺序无关。
- 未保留的一条进重复表，保留双方行号与数值，供人工复核。

## 空态

| 输入 | 结论码 | 含义 |
|---|---|---|
| 0 字节 | `EMPTY_FILE` | 文件是空的 |
| 只有空行 | `ALL_BLANK` | 没有任何表头与数据 |
| 仅表头 | `HEADER_ONLY` | 声明了列但没有数据行 |

三种空态都会生成批次，四账按 0 对 0 判平，结论码显示在总览页。

## 隔离补正（M8）

### corrections 表

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `correction_id` | 字符串 | 否 | 由 `run_id + source_row` 确定性派生，同一位补正 ID 恒定 |
| `run_id` / `source_row` | 字符串 / 整数 | 否 | 对应批次与原始隔离行号，二者唯一，保证补正与原行一一对应 |
| `status` | 枚举 | 否 | `DRAFT`（草稿，未替换正式结果）/ `PROMOTED`（已生效） |
| `sensor_id` `sampled_at` `offset_seconds` `value_text` `unit_text` `quality` `ingest_seq` | 同采样行 | 是 | 值班员录入的补正内容 |
| `validate_code` / `validate_detail` | 字符串 | 是 | 为空表示校验通过；非空为拦下的原因码与说明，此类草稿不能参与替换 |
| `bucket_utc` | ISO8601 | 是 | 按首采同一偏移规则推导出的分钟桶 |
| `content_hash` | 字符串 | 否 | 补正内容的规范化哈希，作为幂等键 |
| `attempts` | 整数 | 否 | 同一补正位的提交次数，重投只累加，不新增行/版本 |
| `created_at` / `updated_at` | ISO8601 | 否 | 首次登记与最近一次提交时间 |

### run_revisions 表

| 列 | 说明 |
|---|---|
| `revision_no` | 批次内自增版本号，从 1 开始 |
| `state` | `DRAFT`（事务内中间态，随回滚消失）/ `PROMOTED`（替换成功）/ `FAILED`（替换失败，旧版保留） |
| `base_run_id` | 被替换的基础批次，当前实现即本批次 `run_id`（原始批次不可变，结果表整批重写） |
| `correction_ids_json` | 本次纳入替换的补正 ID 列表 |
| `counts_json` / `totals_json` | 生效版本的计数与合计；FAILED 为空 |
| `error_code` / `error_detail` | 失败原因，成功为空 |

### 来源标记

- 采纳 CSV 新增 `origin` 列：`ORIGINAL` 为首次采纳行，`CORRECTED` 为补正生效行；补正行 `flags` 带 `CORRECTED_ADOPT`，基础重复行带 `BASE_DUPLICATE`。
- 单元格证据对补正点同时返回 `origin`、`correction`（补正记录）与 `quarantine`（原隔离证据），三者通过 `source_row` 串联。
- 原始 `quarantine` 表在替换前后逐行相等，补正台账由独立的 `/api/export/corrections.csv` 导出，不与隔离 CSV 混写。
