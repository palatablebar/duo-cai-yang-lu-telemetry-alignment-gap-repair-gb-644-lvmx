# 运行手册 gridlab

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18203/api/health
open http://127.0.0.1:18203/
```

首页点「导入样例」即可跑通主流程；批次与四账在 `/batches`。

## 重置批次库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

命名卷 `gridlab-data` 承载 `/app/data/gridlab.db`，删除卷即清空全部批次。

## 容器内测试

```bash
docker compose exec -T app python -m pytest -q
```

## 自带样例

| key | 文件 | 用途 |
|---|---|---|
| `telemetry_raw` | `sample_data/telemetry_raw.csv` | 主脏样例：编码 BOM、换行混用、重复键冲突、越界、哨兵、单位不符、迟到、偏离采样率、契约外传感器 |
| `gap_series` | `sample_data/gap_series.csv` | 2 桶线性补齐与 2 桶边缘缺锚的精确算术 |
| `midnight_edge` | `sample_data/midnight_edge.csv` | 跨日桶与 15 分钟窗口右端开 |
| `offset_overlap` | `sample_data/offset_overlap.csv` | 偏移区间重叠导致整行隔离 |
| `column_count` | `sample_data/column_count.csv` | 少列与多列 |
| `truncated_tail` | `sample_data/truncated_tail.csv` | 尾行截断 |
| `header_only` | `sample_data/header_only.csv` | 仅表头 |
| `blank_rows` | `sample_data/blank_rows.csv` | 全空行 |
| `crlf_quoted` | `sample_data/crlf_quoted.csv` | CRLF、引号内换行、重复表头分段 |
| `gb18030` | `sample_data/telemetry_gb18030.csv` | GB18030 与中文表头 |
| `empty` | `sample_data/empty_sample.csv` | 0 字节 |

偏移表固定用 `device_offsets.csv`；其中 `AM-06` 故意留两段重叠区间。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/samples/import` | 导入样例，body `{"sample": "...", "offsets": "device_offsets"}` |
| GET | `/api/samples` | 最近批次的采信元信息 |
| GET | `/api/timebase` | 传感器契约与偏移表 |
| GET | `/api/quarantine` | 隔离行与原因分布（含每条隔离行的补正状态） |
| GET | `/api/quarantine.csv` | 隔离 CSV（原始证据，永不改写） |
| GET | `/api/corrections` | 补正视图：每条隔离行是否已有补正、校验结论与修订历史 |
| POST | `/api/corrections` | 登记一条补正草稿，body 含 `source_row` 与补正字段，按首采同规则校验 |
| GET | `/api/corrections/preview` | 替换前只读预览：会改动的窗口、网格点与缺口覆盖迁移 |
| POST | `/api/corrections/promote` | 用全部校验通过的草稿单事务替换正式结果，失败回滚保留旧版 |
| GET | `/api/corrections/revisions` | 修订版本清单（PROMOTED / FAILED） |
| GET | `/api/grid` | 网格点摘要 |
| POST | `/api/grid` | 从采纳行重新推导网格并逐点比对 |
| GET | `/api/gaps` | 缺口段与窗口覆盖 |
| POST | `/api/gaps/repair` | 从采纳行重新推导缺口并与落库比对 |
| GET | `/api/batches` | 批次清单 |
| GET | `/api/reconcile` | 四账 |
| GET | `/api/export/grid.csv` | 网格 CSV |
| GET | `/api/export/accepted.csv` | 采纳 CSV（含 `origin` 列区分 ORIGINAL / CORRECTED） |
| GET | `/api/export/quarantine.csv` | 隔离 CSV |
| GET | `/api/export/corrections.csv` | 补正台账 CSV（替换后才出现在导出清单） |
| GET | `/api/export/manifest.json` | 运行清单 |
| GET | `/api/export/evidence` | 单元格证据，参数 `sensor_id` 与 `bucket`；补正点同时返回原隔离证据与补正记录 |

## 隔离坏行补正流程（M8）

1. 在 `/quality` 的「隔离行与补正状态」找到可补正的行（结构性损坏如列数不符、未知传感器不可逐行补正，需整批重传）。
2. 点「补正」录入传感器、采样时刻、偏移秒、值、单位、质量位，保存为草稿。系统用与首次采信完全相同的契约校验：通过才可参与替换，未通过则停在草稿态并显示原因码。
3. 在「替换影响预览」核对会改动哪些 15 分钟窗口、网格点状态迁移（如 FILLED→OBSERVED）与缺口覆盖变化；此时正式结果不变。
4. 确认后点「替换正式结果」。后端在单事务内重算 samples/grid/windows/duplicates/reconcile 并刷新批次计数，四账不平或任何异常都会整体回滚、保留旧版，并在修订版本里留一条 FAILED。
5. 替换后隔离 CSV、原始行号与原值原样保留；采纳 CSV 的补正行标 `origin=CORRECTED` 与 `CORRECTED_ADOPT`，单元格证据可同时追回原隔离行与补正记录。

约束：同一原始行至多一条补正（按 `source_row` 一一对应）；同一补正重复提交只累加尝试次数，不新增行或版本；已生效的补正不能再次提交。

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 409 `STATE_CONFLICT` | 偏移表被改动后与既有批次冲突 | 改回偏移表，或删除命名卷按新表重跑 |
| 400 `HEADER_MISSING` | 输入列名不在别名表内 | 在 `app/modules/m1_intake/sniff.py` 的别名表补一条 |
| 404 `RUN_NOT_FOUND` | 指定了不存在的批次号 | 省略 `run_id` 参数即取最近批次 |
| 400 `NOT_CORRECTABLE` | 隔离原因为结构性损坏，不支持逐行补正 | 修复源文件后整批重传 |
| 404 `ROW_NOT_QUARANTINED` | 对不在隔离区的正常行发起补正 | 仅能对隔离行补正 |
| 409 `NO_VALID_CORRECTION` | 没有校验通过的草稿就触发替换 | 先修正草稿直到校验通过 |
| 409 `CORRECTION_ALREADY_PROMOTED` | 该行补正已生效 | 已生效补正不可覆盖，需要时走新批次 |
| 409 `PROMOTE_FAILED` / `RECONCILE_UNBALANCED` | 替换中途失败或四账不平 | 已自动回滚保留旧版，修订版本可见 FAILED 原因，修复草稿后重试 |
| 四账「不平」 | 有人改了落库行 | 用 `POST /api/grid` 与 `POST /api/gaps/repair` 重新推导比对，差额在 `/batches` 可见 |
