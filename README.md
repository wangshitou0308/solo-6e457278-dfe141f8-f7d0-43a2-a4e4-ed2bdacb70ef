# 换位法校验 API（Change Ringing Method Validator）

为英式鸣钟（change ringing）组织者提供的换位法（method）校验服务。
纯 Python 标准库实现（`http.server` + `sqlite3`），**无需联网、无第三方依赖**，
Python 3.8+ 即可运行。

## 功能

- **Place notation 解析**：支持 `x`/`X`/`-`（全换）、位置数字（`1`…`8`）、`.` 分隔、
  `,` 逗号对称展开（`a,b` → `a + reverse(a[:-1]) + b`，如 `x16x16x16,12` 即
  Plain Bob Minor 的 12 变）；支持 4–8 口钟与自定义起始排列。
- **严格校验**：先按钟数补全可推断的首尾 place（如 6 口钟 `3` → `36`、`2` → `12`），
  其余位置必须组成相邻交换对。非法字符、place 越界、同一 change 内 place 重复、
  无法配对——都会**定位到原记号**（`token` + `offset`）报错，绝不自动修正。
- **Rows 展开**：lead 长度、lead head（结束排列）、回到 rounds 的周期（leads/rows）、
  hunt bells（lead head 中位置未变的钟）。
- **Truth 检查**：仅在一个 extent（`stage!` 行）内按 row 唯一性检查；首个重复 row
  标出两处位置（index/lead/change），逐行条目以 `repeat` 标记；**发现重复后仍继续
  展开**，直到在 lead 边界回到起始排列（闭合）或达到上限，闭合周期与完整轨迹总会给出。
  `premature_rounds`（lead 中途回到 rounds，视为对第 0 行的重复）、
  `exceeded_limit`（超过上限未闭合）、`untrue`（有重复）分别报告。
- **组合（composition）**：分析时可用 `overrides` 在指定 lead 的某一变以 notation
  覆盖（如 bob/single），报告保留覆盖点前后轨迹（`before_row`/`after_row`）。
- **Spliced touch（多方法拼接）**：按区段编排 touch——每段指定 `method_id`、lead 数
  与段内 change 覆盖；各方法钟数必须一致，切换只发生在 lead 边界，下一段接着上一段
  末行展开（不重置为各方法的 start_row）。truth 对整段 touch 统一判定（重复 row、
  提前回到起始排列、结尾闭合、row 上限），逐行标明区段/方法版本/lead/change/覆盖来源；
  报告汇总各方法使用的 leads/rows、切换点前后 row、首次重复两处位置与未应用覆盖。
  方法不存在、钟数不一、覆盖越界或总行数超限时按区段报错且不落库。
- **持久化**：SQLite 保存方法版本（同名自动递增 version）、全部分析报告与 spliced touch。
- **比较与下载**：比较两版的周期与重复位置、比较两次 touch；报告均可下载为 JSON。

## 运行

```bash
python3 server.py --host 127.0.0.1 --port 8000 --db ringing.db
# 文档页: http://127.0.0.1:8000/
```

测试与演示：

```bash
python3 tests.py       # 44 个单元/接口测试
python3 examples.py    # 端到端演示（需先启动 server）
```

## Place notation 语法

| 记号 | 含义 |
|---|---|
| `x` / `X` / `-` | 全换（cross）：所有相邻位置交换，无 place |
| `1`…`8` | place：该位置的钟不动，如 `16`、`1256`；不得超出钟数 |
| `.` | 分隔 change（`x`/`-` 前后可省略）；空白忽略 |
| `,` | 对称展开：`a,b` → `a + reverse(a[:-1]) + b`（a 的最后一个 change 为 half-lead 支点，镜像不重复；b 为 lead end），至多一个逗号 |

解析规则：

1. 每个 change 先按钟数补全可推断的首尾 place：最低（最高）显式 place 之下（上）
   若有奇数个未占位置，则在第 1 位（末位）补一个隐含 place。
   例：6 口钟 `3`→`36`，`1`→`16`，`2`→`12`，`5`→`56`，`23`→`1236`。
2. 其余位置必须两两组成相邻交换对，否则报错（如 6 口钟 `13`：1 与 3 之间只剩
   一个位置，无法配对）。
3. 错误一律定位原记号，不自动修正：

```json
{"code": "notation_error", "error": "places cannot be paired as adjacent swaps",
 "token": "13", "offset": 2}
```

## API 一览

除 `GET /`（HTML 文档）外均接收/返回 JSON。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/parse` | 仅解析记号：`{"stage":6,"notation":"x.16,x.12"}` |
| POST | `/api/methods` | 创建方法版本：`{"name","stage","notation","start_row"?}` |
| GET | `/api/methods` | 列出全部方法版本 |
| GET | `/api/methods/{id}` | 方法详情 + 解析后的 changes |
| POST | `/api/analyses` | 创建分析：`{"method_id","overrides"?,"max_rows"?}` |
| GET | `/api/analyses` | 列出分析（`?method_id=` 过滤） |
| GET | `/api/analyses/{id}` | 完整报告（含逐行 rows） |
| GET | `/api/analyses/{id}/rows?from=0&to=60` | 逐行结果（`to` 为含端点的行号） |
| GET | `/api/analyses/{id}/download` | 下载报告 JSON（attachment） |
| GET/POST | `/api/compare` | 比较两版：`?a=1&b=2`（分析 id）或 `{"method_a":1,"method_b":2}`（自动建分析） |
| POST | `/api/touches` | 创建 spliced touch：`{"segments":[{"method_id":1,"leads":2,"overrides"?},...],"start_row"?,"max_rows"?}` |
| GET | `/api/touches` | 列出全部 touch（摘要） |
| GET | `/api/touches/{id}` | 完整 touch 报告（含逐行 rows） |
| GET | `/api/touches/{id}/rows?segment=2&from=0&to=60` | 逐行结果：`segment` 按区段过滤，`from`/`to` 按行号切片 |
| GET | `/api/touches/{id}/download` | 下载 touch 报告 JSON（attachment） |
| GET/POST | `/api/touches/compare` | 比较两次 touch：`?a=1&b=2`（touch id） |

### 创建方法版本

```bash
curl -s -X POST localhost:8000/api/methods -d '{
  "name": "Plain Bob Minor", "stage": 6,
  "notation": "x.16.x.16.x.16.x.16.x.16.x.12"}'
```

同名方法再次创建会自动递增 `version`；`start_row` 缺省为 rounds，
可传 `"214365"`、`"2,1,4,3,6,5"` 或 `[2,1,4,3,6,5]`。

### 创建分析（含组合覆盖）

```bash
curl -s -X POST localhost:8000/api/analyses -d '{
  "method_id": 1,
  "overrides": [{"lead": 1, "change": 12, "notation": "14"}]}'
```

`overrides` 把第 1 个 lead 的第 12 变（原为 `12`）替换为 `14`（一个 bob）。
覆盖记号必须恰好解析为一个 change；报告中保留覆盖点前后轨迹：

```json
{"lead": 1, "change": 12, "notation": "14", "replaces_token": "12",
 "applied": true, "row_index": 12, "before_row": "132546", "after_row": "123564"}
```

### 报告字段

```json
{
  "status": "ok",                  // ok | untrue | premature_rounds | exceeded_limit
  "closed": true,
  "lead_length": 12,
  "lead_head": "135264",
  "period_leads": 5,
  "period_rows": 60,
  "hunt_bells": [1],
  "working_bells": [2, 3, 4, 5, 6],
  "truth": {"true": true, "first_repeat": null},
  "premature_rounds": null,
  "problems": [],
  "rows": [{"index": 0, "lead": 0, "change": 0, "row": "123456",
            "token": null, "override": false, "repeat": false}, ...]
}
```

- `untrue`：`truth.first_repeat` 给出重复 row 及两处位置
  （`{"index","lead","change"}`）；展开不中断，闭合时 `period_leads`/`period_rows`
  照常给出。
- `premature_rounds`：在 lead 中途回到起始排列，给出位置；同时计入
  `truth.first_repeat`（第 0 行与该行），`truth.true` 为 `false`。
- `exceeded_limit`：达到 `max_rows`（默认一个 extent = `stage!`）仍未闭合，
  `problems` 含 `not_closed`；此前的重复仍会记录在 `truth.first_repeat`。

### 比较两版

```bash
curl -s 'localhost:8000/api/compare?a=1&b=2'
```

```json
{"a": {"analysis_id": 1, "method": "Plain Bob Minor", "version": 1, ...},
 "b": {"analysis_id": 2, "method": "Plain Bob Minor", "version": 2, ...},
 "comparison": {
   "period_rows_equal": false, "period_rows_delta": 24,
   "both_closed": true, "both_true": true,
   "first_repeat_same_position": null,
   "a": {"period_rows": 60, "hunt_bells": [1], ...},
   "b": {"period_rows": 36, "hunt_bells": [1, 2, 3], ...}}}
```

### Spliced touch（多方法拼接）

把多个方法版本按区段（segment）拼成一段 touch：

```bash
curl -s -X POST localhost:8000/api/touches -d '{
  "segments": [
    {"method_id": 1, "leads": 2},
    {"method_id": 2, "leads": 2,
     "overrides": [{"lead": 1, "change": 12, "notation": "14"}]},
    {"method_id": 1, "leads": 1}
  ]}'
```

语义：

- 每段指定 `method_id`、lead 数（`leads` ≥ 1）与段内 change 覆盖
  （`overrides` 的 `lead` 从 1 起按**段内**计，`change` 按 lead 内位置计）。
- 所有方法的钟数必须一致（以第 1 段为准）；切换只发生在 lead 边界；
  下一段**接着上一段末行**展开，不会重置为各方法的 `start_row`
  （touch 整体只有一个起始排列 `start_row`，缺省 rounds）。
- truth 对**整段 touch** 统一判定：重复 row、提前回到起始排列
  （`premature_rounds`）、结尾是否回到起始排列（`closed`）与 row 上限，
  绝不用各方法单独的 truth 代替跨区段结论。
- 总行数上限 `max_rows` 缺省为一个 extent（`stage!`）；累计行数超限即报错。

校验错误按区段定位（`segment` 为 1 起的区段序号），且**不落库**：

```json
{"error": "segment 2: method 99 not found", "code": "not_found",
 "segment": 2, "method_id": 99}
{"error": "stage mismatch: segment has 5 bells, the touch is on 6",
 "code": "bad_segment", "segment": 2, "stage": 5, "expected": 6}
{"error": "override change must be between 1 and 12 (the lead length)",
 "code": "bad_segment", "segment": 1, "override": 0, "change": 99, "lead_length": 12}
{"error": "total rows 732 exceed the limit of 720",
 "code": "bad_segment", "segment": 1, "total_rows": 732, "max_rows": 720}
```

touch 报告要点：

```json
{
  "status": "not_closed",          // ok | untrue | premature_rounds | not_closed
  "closed": false,
  "total_rows": 60, "total_leads": 5, "segment_count": 3,
  "truth": {"true": true, "first_repeat": null},
  "premature_rounds": null,
  "switches": [{"at_index": 24, "from_segment": 1, "to_segment": 2,
                "from_method": "Plain Bob Minor", "to_method": "...",
                "before_row": "156342", "after_row": "513624"}],
  "methods_used": [{"method_id": 1, "name": "Plain Bob Minor", "version": 1,
                    "leads": 3, "rows": 36, "segments": [1, 3]}, ...],
  "segments": [{"index": 1, "method_id": 1, "leads": 2, "lead_length": 12,
                "rows": 24, "from_index": 0, "to_index": 24,
                "start_row": "123456", "end_row": "156342",
                "overrides": [...]}, ...],
  "unapplied_overrides": [],
  "rows": [{"index": 25, "segment": 2, "method_id": 2, "method": "...",
            "version": 1, "lead": 1, "change": 1, "row": "513624",
            "token": "x", "override": null, "repeat": false}, ...]
}
```

- 逐行条目标明区段、方法版本（`method_id`/`method`/`version`）、`lead`、`change`
  与覆盖来源（`override` 为 `null` 或
  `{"segment","lead","change","notation","replaces_token"}`）。
- `truth.first_repeat` 的两处位置均含 `segment`/`lead`/`change`/`index`。
- 同一 change 被多个覆盖指定时后者生效，前者进入 `unapplied_overrides`
  （`reason: "superseded by a later override for the same change"`）。
- `GET /api/touches/{id}/rows?segment=2` 只取第 2 段的逐行结果；
  `GET /api/touches/compare?a=1&b=2` 比较两次 touch 的规模、闭合、truth、
  首次重复位置与方法交集（`methods_overlap`）。

## 存储

SQLite（默认 `ringing.db`）三张表：

- `methods`：每个方法版本一行（`name`+`version` 唯一），保存 stage、notation、
  start_row、创建时间。
- `analyses`：每次分析一行，保存所属方法版本、overrides、max_rows、状态与
  完整报告 JSON。
- `touches`：每次 spliced touch 一行，保存 stage、区段编排（segments 请求原文）、
  max_rows、状态与完整报告 JSON。

## 文件

| 文件 | 说明 |
|---|---|
| `ringing.py` | 核心引擎：记号解析、rows 展开、truth 检查、spliced touch、比较 |
| `db.py` | SQLite 持久化（方法版本 + 分析报告 + touch 报告） |
| `server.py` | HTTP API（`http.server`）与文档页 |
| `tests.py` | 单元测试 + 接口测试（44 个） |
| `examples.py` | 端到端演示客户端 |
