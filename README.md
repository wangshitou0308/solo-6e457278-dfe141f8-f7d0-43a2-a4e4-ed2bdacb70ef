# 换位法校验 API（Change Ringing Method Validator）

为英式鸣钟（change ringing）组织者提供的换位法（method）校验服务。
纯 Python 标准库实现（`http.server` + `sqlite3`），**无需联网、无第三方依赖**，
Python 3.8+ 即可运行。

## 功能

- **Place notation 解析**：支持 `x`/`X`/`-`（全换）、位置数字（`1`…`8`）、`.` 分隔、
  `,` 逗号对称展开（`a,b` → `a + b + reverse(a)`）；支持 4–8 口钟与自定义起始排列。
- **严格校验**：先按钟数补全可推断的首尾 place（如 6 口钟 `3` → `36`、`2` → `12`），
  其余位置必须组成相邻交换对。非法字符、place 越界、同一 change 内 place 重复、
  无法配对——都会**定位到原记号**（`token` + `offset`）报错，绝不自动修正。
- **Rows 展开**：lead 长度、lead head（结束排列）、回到 rounds 的周期（leads/rows）、
  hunt bells（lead head 中位置未变的钟）。
- **Truth 检查**：仅在一个 extent（`stage!` 行）内按 row 唯一性检查；首个重复 row
  标出两处位置（index/lead/change）。`premature_rounds`（lead 中途回到 rounds）、
  `exceeded_limit`（超过上限未闭合）、`untrue`（有重复）分别报告。
- **组合（composition）**：分析时可用 `overrides` 在指定 lead 的某一变以 notation
  覆盖（如 bob/single），报告保留覆盖点前后轨迹（`before_row`/`after_row`）。
- **持久化**：SQLite 保存方法版本（同名自动递增 version）与全部分析报告。
- **比较与下载**：比较两版的周期与重复位置；报告可下载为 JSON。

## 运行

```bash
python3 server.py --host 127.0.0.1 --port 8000 --db ringing.db
# 文档页: http://127.0.0.1:8000/
```

测试与演示：

```bash
python3 tests.py       # 29 个单元/接口测试
python3 examples.py    # 端到端演示（需先启动 server）
```

## Place notation 语法

| 记号 | 含义 |
|---|---|
| `x` / `X` / `-` | 全换（cross）：所有相邻位置交换，无 place |
| `1`…`8` | place：该位置的钟不动，如 `16`、`1256`；不得超出钟数 |
| `.` | 分隔 change（`x`/`-` 前后可省略）；空白忽略 |
| `,` | 对称展开：`a,b` → `a + b + reverse(a)`，至多一个逗号 |

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
            "token": null, "override": false}, ...]
}
```

- `untrue`：`truth.first_repeat` 给出重复 row 及两处位置
  （`{"index","lead","change"}`）。
- `premature_rounds`：在 lead 中途回到起始排列，给出位置。
- `exceeded_limit`：达到 `max_rows`（默认一个 extent = `stage!`）仍未闭合，
  `problems` 含 `not_closed`。

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

## 存储

SQLite（默认 `ringing.db`）两张表：

- `methods`：每个方法版本一行（`name`+`version` 唯一），保存 stage、notation、
  start_row、创建时间。
- `analyses`：每次分析一行，保存所属方法版本、overrides、max_rows、状态与
  完整报告 JSON。

## 文件

| 文件 | 说明 |
|---|---|
| `ringing.py` | 核心引擎：记号解析、rows 展开、truth 检查、比较 |
| `db.py` | SQLite 持久化（方法版本 + 报告） |
| `server.py` | HTTP API（`http.server`）与文档页 |
| `tests.py` | 单元测试 + 接口测试（29 个） |
| `examples.py` | 端到端演示客户端 |
