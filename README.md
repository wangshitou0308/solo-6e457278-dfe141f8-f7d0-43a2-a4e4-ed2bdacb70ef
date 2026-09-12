# 换位法校验 API（Change Ringing Method Validator）

为英式鸣钟（change ringing）组织者提供的换位法（method）校验服务。
纯 Python 标准库实现（`http.server` + `sqlite3`），**无需联网、无第三方依赖**，
Python 3.8+ 即可运行。支持 **4–12 口钟**（Minimus～Maximus）。

## 10–12 口钟的符号约定

钟号 10、11、12 在 **place notation 与紧凑 row** 中分别写作单字符
`0`、`E`、`T`：

| 钟号 | 紧凑符号 | 数组输入 |
|---|---|---|
| 1–9 | `1`…`9` | `1`…`9` |
| 10 | `0` | `10`（整数） |
| 11 | `E` | `11`（整数） |
| 12 | `T` | `12`（整数） |

因此 12 口 rounds 写作 `1234567890ET`。三种 start_row 形式等价：
`"1234567890ET"`、`[1,2,…,10,11,12]`（始终是整数）、
`"1,2,…,10,11,12"`（分隔串中整数 `10/11/12` 与符号 `0/E/T` 均接受）。

**`"10"` 在 place notation 中表示位置 1 与 10 两个 place，绝不是两位数“十”**；
单独表示 10 号位写 `0`。同理 `1T`＝places 1 与 12。row、lead head、
重复位置与 spliced 切换轨迹的所有输出都使用规范符号（0/E/T）。

### 上限（cap）与 truth 结论

硬上限 `HARD_MAX_ROWS = 1,000,000`。4–8 口的 extent（`stage!`）均不超限，
缺省 `max_rows` 即一个 extent，既有行为与响应完全不变。10! = 3,628,800
已超过硬上限：**10–12 口创建分析或 touch 时必须显式传
`max_rows`（1..1,000,000）**，否则拒绝创建作业，错误码 `limit_required`
（附 `stage`、`extent_rows`、`hard_max_rows` 与原因说明）；显式值超过
1,000,000 一律拒绝。

在显式上限内触顶（未闭合）时：

- 已发现重复 → 照常判 `untrue`（结论确定）；
- 尚未发现重复 → **不下完整 truth 结论**：`truth.true` 为 `null`、
  `truth.conclusive` 为 `false`，`truth.checked_rows` 报告已检查行数，
  `problems` 增加 `truth_inconclusive`；比较接口的 `both_true` 此时也是 `null`。

## 功能

- **Place notation 解析**：支持 `x`/`X`/`-`（全换）、位置符号（`1`…`9`、`0`、
  `E`、`T`）、`.` 分隔、`,` 逗号对称展开（`a,b` → `a + reverse(a[:-1]) + b`，
  如 6 口 `x16x16x16,12` 即 Plain Bob Minor 的 12 变；10 口
  `x10x10x10x10x10,12` 即 Plain Bob Royal 的 20 变；12 口
  `x1Tx1Tx1Tx1Tx1Tx1T,12` 即 Plain Bob Maximus 的 24 变）；
  支持 4–12 口钟与自定义起始排列。
- **严格校验**：先按钟数补全可推断的首尾 place（如 6 口钟 `3` → `36`、
  12 口钟 `3` → `3T`），其余位置必须组成相邻交换对。非法字符、place 越界、
  同一 change 内 place 重复、无法配对——以及紧凑 row 的非法符号、重复钟、
  超出 stage 或缺钟——都会**定位到原 token 与 offset** 报错，绝不自动修正。
- **Rows 展开**：lead 长度、lead head（结束排列）、回到 rounds 的周期（leads/rows）、
  hunt bells（lead head 中位置未变的钟）；rows 一律以规范符号输出。
- **Truth 检查**：按 row 唯一性检查；首个重复 row 标出两处位置
  （index/lead/change），逐行条目以 `repeat` 标记；**发现重复后仍继续展开**，
  直到在 lead 边界回到起始排列（闭合）或达到上限，闭合周期与完整轨迹总会给出。
  `premature_rounds`（lead 中途回到 rounds，视为对第 0 行的重复）、
  `exceeded_limit`（超过上限未闭合）、`untrue`（有重复）分别报告；
  触顶且未见重复时只报告已检查范围，`truth.true=null`（见上）。
- **组合（composition）**：分析时可用 `overrides` 在指定 lead 的某一变以 notation
  覆盖（如 bob/single），报告保留覆盖点前后轨迹（`before_row`/`after_row`）。
- **Spliced touch（多方法拼接）**：按区段编排 touch——每段指定 `method_id`、lead 数
  与段内 change 覆盖；各方法钟数必须一致，切换只发生在 lead 边界，下一段接着上一段
  末行展开（不重置为各方法的 start_row）。truth 对整段 touch 统一判定（重复 row、
  提前回到起始排列、结尾闭合、row 上限），逐行标明区段/方法版本/lead/change/覆盖来源；
  报告汇总各方法使用的 leads/rows、切换点前后 row、首次重复两处位置与未应用覆盖。
  方法不存在、钟数不一、覆盖越界或总行数超限时按区段报错且不落库。
- **音乐性评分（music scoring）**：用带权规则的**方案版本（scheme）**对 analysis 或 touch
  的逐行 rows 打分。规则支持前排/后排/任意位置的连续升降序 run、指定钟号序列与完整 row，
  可限定 stroke（index 0 固定 handstroke）、lead end 与 touch 区段；run 命中按
  `weight × 长度`计分，同一 row/方向/位置范围只取最长 run。命中记录规则、row、索引、
  位置及 method/segment/lead/change 上下文；按规则 + stroke + 方法（+ 区段）汇总，
  未命中规则保留零值。`max_rows` 截断时只分析已生成 rows 并标注 `partial`、`checked_rows`
  与 truth 状态，不把局部分数当全量结果。比较仅在同 stage、同方案版本、同评分模型间进行，
  列出各规则得分与命中增减。
- **持久化**：SQLite 保存方法版本（同名自动递增 version）、全部分析报告、spliced touch、
  评分方案版本与音乐分析结果。
- **比较与下载**：比较两版的周期与重复位置、比较两次 touch、比较两个音乐分析结果；
  报告均可下载为 JSON。

## 运行

```bash
python3 server.py --host 127.0.0.1 --port 8000 --db ringing.db
# 文档页: http://127.0.0.1:8000/
```

测试与演示：

```bash
python3 tests.py       # 95 个单元/接口测试
python3 examples.py    # 端到端演示（需先启动 server）
```

## Place notation 语法

| 记号 | 含义 |
|---|---|
| `x` / `X` / `-` | 全换（cross）：所有相邻位置交换，无 place |
| `1`…`9` `0` `E` `T` | place：每个字符是一个位置，如 `16`、`1256`、`10`（places 1 与 10）、`1T`（places 1 与 12）；不得超出钟数 |
| `.` | 分隔 change（`x`/`-` 前后可省略）；空白忽略 |
| `,` | 对称展开：`a,b` → `a + reverse(a[:-1]) + b`（a 的最后一个 change 为 half-lead 支点，镜像不重复；b 为 lead end），至多一个逗号 |

紧凑 row 与 place notation 使用同一套单字符符号：钟号 10/11/12 写 `0`/`E`/`T`。
数组输入仍用整数；分隔串（逗号/空格/点）中 `10`/`11`/`12` 是整数钟号，
`0`/`E`/`T` 是等价符号，二者都接受。

解析规则：

1. 每个 change 先按钟数补全可推断的首尾 place：最低（最高）显式 place 之下（上）
   若有奇数个未占位置，则在第 1 位（末位）补一个隐含 place。
   例：6 口钟 `3`→`36`，`1`→`16`，`2`→`12`，`5`→`56`，`23`→`1236`；
   12 口钟 `3`→`3T`，`E`→`ET`。
2. 其余位置必须两两组成相邻交换对，否则报错（如 6 口钟 `13`：1 与 3 之间只剩
   一个位置，无法配对）。
3. 错误一律定位原 token，不自动修正：

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
| POST | `/api/schemes` | 创建音乐性评分方案版本：`{"name","stage"?, "rules":[…]}`（同名自动递增 version） |
| GET | `/api/schemes?stage=6` | 列出方案版本（可按 stage 过滤） |
| GET | `/api/schemes/{id}` | 方案详情（含规范化后的规则） |
| POST | `/api/music` | 对已存 analysis/touch 打分：`{"analysis_id"|"touch_id","scheme_id"}` |
| GET | `/api/music?kind=&subject_id=&scheme_id=&stage=` | 列出音乐分析结果（摘要 + 各规则得分） |
| GET | `/api/music/{id}` | 完整音乐分析（规则汇总 + 全部命中） |
| GET | `/api/music/{id}/hits?rule_id=&kind=&stroke=&lead_end=&segment=&method_id=&from=&to=` | 命中筛选 |
| GET | `/api/music/{id}/download` | 下载音乐分析 JSON（含完整方案规则，attachment） |
| GET/POST | `/api/music/compare` | 比较两个音乐分析：`?a=1&b=2`（music id；须同 stage、同方案版本） |

## 音乐性评分（music scoring）

音乐性评分回答“这段敲法里有多少好听的 row”。先用**带权规则**定义一个评分方案
（scheme），再对已存储的 analysis 或 touch 打分；方案同名再创建自动递增
`version`，比较只在**同 stage、同方案（name）同版本、同评分模型（`scoring_version`，
当前 `1.0`）**的两个结果间进行。

### 规则

| kind | 必填/可选字段 | 命中与计分 |
|---|---|---|
| `run` | `direction`: `up`/`down`/`both`（缺省 `both`）；`position`: `front`（前排）/`back`（后排）/`any`（任意位置，缺省）；`min_length`: 2..stage（缺省 4）；`weight`（缺省 1） | 连续升序/降序钟号。**同一 row、同一方向、同一位置范围内只计最长一段**（并列取最靠前）；`both` 可各计升、降一次。得分 `weight × 长度` |
| `sequence` | `bells`：紧凑串 `"4321"`/`"90ET"`、分隔串或整数数组；`position`: front/back/any（缺省 any） | 指定钟号在 row 中连续出现（row 是排列，至多一处）；每次 `weight` 分 |
| `row` | `row`：必须是 1..stage 的完整排列（沿用 0/E/T） | 整行完全一致；每次 `weight` 分 |

三类规则共用的过滤字段（均可省略）：

- `strokes`：`["hand","back"]`（缺省两者）。**stroke 约定固定：index 0（起始行）
  永远是 handstroke**，其后按行号奇偶交替（偶 hand、奇 back）。
- `lead_end`：`lead_end`（只计 lead 末行，即 change 等于 lead 长度）/
  `not_lead_end` / `any`（缺省）。
- `segments`：如 `[2]`，仅在 touch 的指定 1 起区段内计分；analysis 没有区段，
  带此过滤的规则在 analysis 上一律零命中（规则仍保留）。

钟号写法与其他接口一致：紧凑串与序列中 10/11/12 仍写 `0`/`E`/`T`
（如 `"90ET"`）；数组用整数。规则错误返回 400 `bad_rule`，带 0 起的 `rule` 与
`field`；row/sequence 的符号错误另带 `token`/`offset`，不落库：

```json
{"code": "bad_rule", "error": "direction must be 'up', 'down' or 'both'",
 "rule": 1, "field": "direction"}
{"code": "bad_rule", "error": "bell 5 appears more than once in the row",
 "rule": 0, "field": "row", "token": "5", "offset": 5}
```

### 打分与结果

```bash
curl -s -X POST localhost:8000/api/schemes -d '{
  "name": "minor-music", "stage": 6, "rules": [
    {"id": "front-up", "name": "front run up >=4", "kind": "run",
     "direction": "up", "position": "front", "min_length": 4, "weight": 2},
    {"id": "back-down", "name": "back run down >=4", "kind": "run",
     "direction": "down", "position": "back", "min_length": 4},
    {"id": "queens", "name": "135 at the front", "kind": "sequence",
     "bells": "135", "position": "front"},
    {"id": "rounds-hand", "name": "rounds (handstroke)", "kind": "row",
     "row": "123456", "strokes": ["hand"]},
    {"id": "le-music", "name": "lead-end runs", "kind": "run",
     "direction": "both", "position": "any", "min_length": 4,
     "lead_end": "lead_end", "weight": 3}]}'

curl -s -X POST localhost:8000/api/music \
  -d '{"analysis_id": 1, "scheme_id": 1}'
```

结果要点：

```json
{
  "id": 1, "kind": "analysis", "stage": 6,
  "scheme": {"id": 1, "version": 1},
  "scoring_version": "1.0",
  "index0_stroke": "hand", "strokes": ["hand", "back"],
  "rows_analyzed": 61, "checked_rows": 60,
  "partial": false, "truncated": false,
  "status": "ok", "closed": true,
  "truth": {"true": true, "conclusive": true, "checked_rows": 60},
  "total_hits": 11, "total_score": 78,
  "rules": [{"id": "front-up", "name": "front run up >=4", "kind": "run",
             "hits": 4, "score": 42,
             "by_stroke": {"hand": {"hits": 2, "score": 24},
                           "back": {"hits": 2, "score": 18}},
             "by_method": [{"method_id": 1, "name": "Plain Bob Minor",
                            "version": 1, "hits": 4, "score": 42}],
             "by_segment": null}],
  "hits": [{"rule_id": "front-up", "rule": "front run up >=4",
            "kind": "run", "row": "123456", "index": 0,
            "stroke": "hand", "lead_end": false,
            "position": "front", "start": 1, "length": 6,
            "matched": "123456", "direction": "up", "score": 12,
            "lead": 0, "change": 0,
            "method_id": 1, "method": "Plain Bob Minor", "version": 1,
            "segment": null}]}
```

- 每条命中记录规则（`rule_id`/`rule`/`kind`）、`row`、`index`、`stroke`、
  `lead_end`、位置（`position`、1 起 `start`、`length`、`matched`；run 还有
  `direction`）与上下文 `method_id`/`method`/`version`、`segment`、`lead`、
  `change` 及该次 `score`。touch 的 index 0 行是 `segment: 0`、无方法。
- 按规则汇总 `hits`/`score`，细分 `by_stroke`、`by_method`，touch 还有
  `by_segment`（含区段 0）；**未命中规则以零值保留**，顺序与方案一致。
- **截断语义**：analysis 达到 `max_rows` 未闭合时，只对实际生成的 rows 打分，
  结果带 `partial: true`、`truncated: true`、`rows_analyzed`、`checked_rows`
  与 truth 状态（触顶且无重复时 `truth.true` 为 `null`）——局部得分**绝不**代表
  全量结果。touch 在创建时已校验规模并完整展开，音乐结果永不为 partial。
- touch 的区段规则：`segments` 只在匹配区段计分；analysis 不识别区段过滤（零值）。

### 命中筛选

```bash
curl -s 'localhost:8000/api/music/1/hits?stroke=hand&lead_end=true'
curl -s 'localhost:8000/api/music/1/hits?rule_id=front-up&kind=run&from=0&to=24'
curl -s 'localhost:8000/api/music/3/hits?segment=2&method_id=5'
```

查询参数：`rule_id`、`kind`（run/sequence/row）、`stroke`（hand/back）、
`lead_end`（true/false）、`segment`、`method_id`、全局行号切片 `from`/`to`（含端点）。

### 比较音乐分析

```bash
curl -s 'localhost:8000/api/music/compare?a=1&b=2'
```

仅当两侧 **stage、方案（name+version）、scoring_version 全部相同**才可比较；否则返回
400 `incomparable` 并在 `reasons` 中列出失配项（`stage`/`scheme`/`scheme_version`/
`scoring_version`）：

```json
{"code": "incomparable", "reasons": ["scheme_version"], "a": {...}, "b": {...}}
```

可比较时逐条规则列出双方 `hits`/`score` 与 `hits_delta`/`score_delta`
（一方没有的规则按零值处理），并给出 `total_hits_delta`/`total_score_delta`、
双方 `partial_*` 与 `checked_rows_*`，便于判断差异是否发生在不同的已检查范围内。

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
  "truth": {"true": true, "conclusive": true, "checked_rows": 60,
            "first_repeat": null},
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
- `exceeded_limit`：达到 `max_rows`（4–8 口缺省为一个 extent = `stage!`）仍未闭合，
  `problems` 含 `not_closed`；此前的重复仍会记录在 `truth.first_repeat`。
- `truth.conclusive=false`：触顶且尚未发现重复，只检查了
  `truth.checked_rows` 行，此时 `truth.true` 为 `null`、`problems` 含
  `truth_inconclusive`——**不代表 true**。闭合或已发现重复时结论都是确定的。
- 10–12 口的 extent 超过 1,000,000：必须显式传 `max_rows`（见上文「上限」）。

### 10 口（Royal）与 12 口（Maximus）示例

新建数据库后方法 id 由自增序列决定，下面的命令**直接接续使用创建响应里的
`id`**（用标准库 `python3` 解析 JSON），可整段复制执行：

```bash
# Plain Bob Royal（10 口）："10" 是 places 1 与 10；必须显式给 max_rows
RID=$(curl -s -X POST localhost:8000/api/methods -d '{
  "name": "Plain Bob Royal", "stage": 10,
  "notation": "x10x10x10x10x10,12", "start_row": "1234567890"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/api/analyses \
  -d '{"method_id":'"$RID"', "max_rows": 180}'
# lead head "1352749608"，整 course 180 行闭合、true

# Plain Bob Maximus（12 口）：1T 是 places 1 与 12
MID=$(curl -s -X POST localhost:8000/api/methods -d '{
  "name": "Plain Bob Maximus", "stage": 12,
  "notation": "x1Tx1Tx1Tx1Tx1Tx1T,12"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
# 只检查前 240 行：未闭合，truth 不下结论
curl -s -X POST localhost:8000/api/analyses \
  -d '{"method_id":'"$MID"', "max_rows": 240}'
# {"status":"exceeded_limit","closed":false,
#  "truth":{"true":null,"conclusive":false,"checked_rows":240,"first_repeat":null},
#  "problems":["exceeded_limit","not_closed","truth_inconclusive"]}

# 不传 max_rows：作业被拒绝（同样接续使用上面的 $MID）
curl -s -X POST localhost:8000/api/analyses -d '{"method_id":'"$MID"'}'
# {"code":"limit_required","stage":12,"extent_rows":479001600,
#  "hard_max_rows":1000000,"error":"stage 12: one extent is 479,001,600 rows ..."}

# 紧凑 row 重复定位（数组输入用整数）；offset 按原始字符串计，前导空白也算
curl -s -X POST localhost:8000/api/methods -d '{
  "name":"X","stage":12,"notation":"x","start_row":" 1234567890EE"}'
# {"code":"notation_error","error":"bell 11 appears more than once in the row",
#  "token":"E","offset":12}
```

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
  10–12 口的 extent 超过硬上限，缺省 `max_rows` 会直接拒绝创建
  （`limit_required`），必须显式给值（1..1,000,000）。

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

SQLite（默认 `ringing.db`）五张表：

- `methods`：每个方法版本一行（`name`+`version` 唯一），保存 stage、notation、
  start_row、创建时间。
- `analyses`：每次分析一行，保存所属方法版本、overrides、max_rows、状态与
  完整报告 JSON。
- `touches`：每次 spliced touch 一行，保存 stage、区段编排（segments 请求原文）、
  max_rows、状态与完整报告 JSON。
- `schemes`：每个音乐性评分方案版本一行（`name`+`version` 唯一），保存 stage 与
  规范化规则 JSON。
- `music_analyses`：每次音乐分析一行，保存种类（analysis/touch）、stage、
  被评分对象 id、方案 id/版本、partial 标志、总分与完整结果 JSON。

## 文件

| 文件 | 说明 |
|---|---|
| `ringing.py` | 核心引擎：记号解析、rows 展开、truth 检查、spliced touch、**音乐性评分方案/打分/比较**、比较 |
| `db.py` | SQLite 持久化（方法版本 + 分析报告 + touch 报告 + 评分方案 + 音乐分析） |
| `server.py` | HTTP API（`http.server`）与文档页 |
| `tests.py` | 单元测试 + 接口测试（95 个） |
| `examples.py` | 端到端演示客户端（含 10/12 口演示） |
| `analysis-1.json` | 样例报告：6 口 Plain Bob Minor plain course |
| `analysis-royal-10.json` | 样例报告：10 口 Plain Bob Royal，180 行闭合、true |
| `analysis-maximus-12.json` | 样例报告：12 口 Plain Bob Maximus，上限 240 行触顶、truth 不确定（`true: null`） |
