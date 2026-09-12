#!/usr/bin/env python3
"""Change-ringing method validation API.

Stdlib only (http.server + sqlite3); works fully offline.

Run:  python3 server.py [--host 127.0.0.1] [--port 8000] [--db ringing.db]
Docs: http://127.0.0.1:8000/  (or README.md)
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from db import Store
from ringing import (HARD_MAX_ROWS, LimitRequiredError, NotationError,
                     SchemeError, SplicedError, analyze, analyze_spliced,
                     compact_bells, compare_music, compare_reports,
                     compare_touch_reports, parse_notation, parse_row,
                     parse_scheme, row_str, rule_to_dict, score_analysis,
                     score_touch, SCORING_VERSION)

DOCS_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>换位法校验 API · Change Ringing Method Validator</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#222}
code,pre{background:#f4f4f4;padding:.12em .35em;border-radius:4px;font-size:.95em}
pre{padding:1em;overflow:auto;border:1px solid #e2e2e2}
table{border-collapse:collapse;margin:1em 0}
td,th{border:1px solid #ccc;padding:.35em .7em;text-align:left;vertical-align:top}
h1,h2{border-bottom:1px solid #ddd;padding-bottom:.25em}
.tag{font-family:monospace;font-weight:bold}
.get{color:#0a7d2c}.post{color:#0a5bc0}
</style>
</head>
<body>
<h1>换位法校验 API <small>Change Ringing Method Validator</small></h1>
<p>英式鸣钟（change ringing）换位法校验服务。纯 Python 标准库（http.server + sqlite3），断网可用。
支持 <strong>4–12 口钟</strong>。所有接口（除本页外）均接收/返回 JSON；错误返回
<code>{"error": …, "code": …}</code>，记号错误另附 <code>token</code>（原记号）与
<code>offset</code>（在记号串中的位置），绝不自动修正。</p>

<h2>10–12 口钟的符号</h2>
<p>钟号 10、11、12 在 place notation 与紧凑 row 中分别写作单字符
<code>0</code>、<code>E</code>、<code>T</code>；rounds（12 口）为
<code>1234567890ET</code>，<code>row_str([1,…,12]) == "1234567890ET"</code>。
数组输入仍用整数：<code>[1,2,…,10,11,12]</code>；分隔串两种都接受：
<code>"1 2 … 10 11 12"</code> 或 <code>"1 2 … 0 E T"</code>。</p>
<ul>
<li>place notation 中每个字符是一个 place：<code>"10"</code> 表示位置 <strong>1 与 10</strong>，
绝不是两位数“十”；单独表示 10 号位请写 <code>0</code>（如 <code>02</code>＝places 2 与 10）。
11 口用 <code>E</code>、12 口用 <code>T</code>，例如 Maximus 的 <code>1T</code>＝places 1 与 12。</li>
<li>10 口起一个 extent（<code>stage!</code> 行）超过硬上限
1,000,000（10! = 3,628,800）：创建分析/touch 时<strong>必须显式传
<code>max_rows</code></strong>（1..1,000,000），否则拒绝创建（错误码
<code>limit_required</code>）；显式值超过 1,000,000 一律拒绝。</li>
<li>触顶（未闭合且未发现重复）时<strong>不给完整 truth 结论</strong>：
<code>truth.true</code> 为 <code>null</code>、<code>truth.conclusive</code> 为
<code>false</code>，并给出 <code>truth.checked_rows</code>（已检查行数）与
<code>problems</code> 中的 <code>truth_inconclusive</code>；发现重复则照常判 <code>untrue</code>。</li>
</ul>

<h2>Place notation 语法</h2>
<table>
<tr><th>记号</th><th>含义</th></tr>
<tr><td><code>x</code> / <code>X</code> / <code>-</code></td><td>全换（cross）：所有相邻位置交换，无 place</td></tr>
<tr><td><code>1</code>…<code>9</code> <code>0</code> <code>E</code> <code>T</code></td><td>
place：该位置的钟不动；每个字符一个 place，如 <code>16</code>、<code>1256</code>、
<code>10</code>（1 与 10）、<code>1T</code>（1 与 12）</td></tr>
<tr><td><code>.</code></td><td>分隔各个 change（<code>x</code>/<code>-</code> 前后可省略）；空白字符忽略</td></tr>
<tr><td><code>,</code></td><td>对称展开：<code>a,b</code> → <code>a + reverse(a[:-1]) + b</code>（a 的最后一个
change 是 half-lead 支点，镜像不重复；b 是 lead end）。如 6 口 <code>x16x16x16,12</code> →
Plain Bob Minor 的 12 变；10 口 <code>x10x10x10x10x10,12</code> → Plain Bob Royal 的 20 变；
12 口 <code>x1Tx1Tx1Tx1Tx1Tx1T,12</code> → Plain Bob Maximus 的 24 变；至多一个逗号</td></tr>
</table>
<p>解析规则：先按钟数补全可推断的首尾 place（如 6 口钟 <code>3</code> → <code>36</code>、
12 口钟 <code>3</code> → <code>3T</code>）；其余位置必须组成相邻交换对。非法字符、place 越界、
同一 change 内 place 重复、无法配对的 place 都会报错并定位到原记号。紧凑 row 出现
非法符号、重复钟、超出 stage 或缺钟（长度不符/非 1..stage 的排列）时，同样定位原 token 与
offset（字符串为字符偏移，数组为元素下标）。</p>

<h2>分析语义</h2>
<ul>
<li>展开 rows：给出 lead 长度、lead head（结束排列）、回到起始排列（rounds）的周期（leads/rows）、hunt bells（lead head 中位置未变的钟）。</li>
<li>truth 仅在一个 extent（stage! 行）内按 row 唯一性检查；首个重复 row 标出两处位置（index/lead/change），
逐行条目以 <code>repeat</code> 标记；发现重复后仍继续展开，直到闭合或达到上限。</li>
<li>lead 中途回到 rounds → <code>premature_rounds</code>，同时视为对第 0 行的重复（标出 0 与该行）；
达到上限仍未闭合 → <code>exceeded_limit</code> + <code>not_closed</code>；有重复 → <code>untrue</code>，分别报告。
闭合（lead 边界回到起始排列）时的周期 leads/rows 总会给出。</li>
<li>上限语义：默认上限是一个 extent（<code>stage!</code>）。达到上限仍未闭合时，
若已出现重复则结论为 <code>untrue</code>；若尚未重复，<strong>不下“true”结论</strong>——
<code>truth.true</code> 为 <code>null</code>、<code>truth.conclusive</code> 为 <code>false</code>、
<code>truth.checked_rows</code> 给出已检查行数，<code>problems</code> 另加
<code>truth_inconclusive</code>。4–8 口的 extent 均不超硬上限，既有行为与响应不变。</li>
<li>组合（composition）：分析时可用 <code>overrides</code> 在指定 lead 的某一变以 notation 覆盖（如 bob/single），报告保留覆盖点前后轨迹。</li>
</ul>

<h2>音乐性评分（music scoring）</h2>
<p>方案（scheme）是一套<strong>带权规则</strong>，对已存储的 analysis 或 touch 的逐行 rows
打分（<code>scoring_version</code> 随评分模型固定，当前 <code>1.0</code>）。规则三类：</p>
<table>
<tr><th>kind</th><th>字段</th><th>含义与计分</th></tr>
<tr><td><code>run</code></td><td><code>direction</code>（<code>up</code>/<code>down</code>/<code>both</code>，缺省 both）、
<code>position</code>（<code>front</code> 前排 / <code>back</code> 后排 / <code>any</code> 任意位置，缺省 any）、
<code>min_length</code>（2..stage，缺省 4）、<code>weight</code>（缺省 1）</td>
<td>连续升序/降序钟号 run。<strong>同一 row、同一方向、同一位置范围内只取最长的一段</strong>
（并列取最靠前）；<code>direction:"both"</code> 可同时给出升、降各一次命中。每次命中计
<code>weight × run 长度</code>。</td></tr>
<tr><td><code>sequence</code></td><td><code>bells</code>（紧凑串 <code>"4321"</code>/<code>"90ET"</code>、分隔串或整数数组）、
<code>position</code>（front/back/any）</td>
<td>指定钟号序列在 row 中连续出现（row 是排列，至多一处）；每次命中计 <code>weight</code>。</td></tr>
<tr><td><code>row</code></td><td><code>row</code>（必须是 1..stage 的完整排列，沿用 0/E/T）</td>
<td>整行完全一致才命中；每次命中计 <code>weight</code>。</td></tr>
</table>
<ul>
<li><strong>stroke 固定约定：index 0（起始行）永远是 handstroke（手绳）</strong>，此后随奇偶交替
（偶 index = hand，奇 index = back）。可用 <code>strokes:["hand"]</code>/<code>["back"]</code>
（缺省两者）限定；<code>lead_end</code> 取 <code>lead_end</code>/<code>not_lead_end</code>/<code>any</code>
（缺省 any，lead end = 该行 change 等于 lead 长度）；<code>segments:[2]</code> 只在 touch 的指定
1 起区段内计分（analysis 无区段，带该过滤的规则在 analysis 上保留零值）。</li>
<li>每条命中记录 <code>rule_id/rule/kind</code>、<code>row</code>、<code>index</code>、<code>stroke</code>、
<code>lead_end</code>、<code>position/start/length/matched</code>（start 为 1 起位置）与上下文
<code>method_id/method/version</code>、<code>segment</code>、<code>lead</code>、<code>change</code> 及本次得分。</li>
<li>按规则汇总 <code>hits/score</code>，并细分 <code>by_stroke</code>、<code>by_method</code>，
touch 还有 <code>by_segment</code>；<strong>未命中规则保留零值</strong>，规则顺序即方案顺序。</li>
<li><strong>max_rows 截断时只分析实际已生成的 rows</strong>：结果带 <code>partial:true</code>、
<code>rows_analyzed</code>、<code>checked_rows</code> 与 truth 状态（<code>truth.true</code> 可为
<code>null</code>），绝不能把局部得分当作全量结果；touch 总是完整展开（规模创建时已校验），
故永不为 partial。</li>
<li><strong>只比较同 stage、同方案（name）同方案版本（version）、同 scoring_version 的两个结果</strong>；
否则 <code>GET /api/music/compare</code> 返回 400 <code>incomparable</code> 并列出失配项。
可比较时逐条列出各规则双方 hits/score 及 <code>hits_delta/score_delta</code>（一方缺规则按零值），
并给出总分增减与双方 partial/checked_rows。</li>
<li>方案同名再次创建自动递增 <code>version</code>；规则错误按 <code>rule</code>（0 起）、<code>field</code>
定位，row/sequence 的符号错误另带 <code>token</code>/<code>offset</code>，错误不落库。</li>
</ul>

<h2>Spliced touch（多方法拼接）</h2>
<ul>
<li>按区段（segment）编排：每段指定 <code>method_id</code>、lead 数（<code>leads</code>）与段内
change 覆盖（<code>overrides</code>，lead 从 1 起按段内计）。所有方法钟数必须一致；
切换只发生在 lead 边界，下一段<strong>接着上一段末行</strong>展开，不会重置为各方法的 start_row。</li>
<li>校验按区段报错且不落库：方法不存在（404）、钟数不一、leads 非法、覆盖越界
（lead 超出段内 lead 数或 change 超出 lead 长度）、累计总行数超过上限。
默认上限为一个 extent = <code>stage!</code>；10 口起 extent 超过 1,000,000，
必须显式传 <code>max_rows</code> 才能创建（<code>limit_required</code>）。
错误体带 <code>segment</code>（及 <code>override</code>）定位。</li>
<li>整段 touch 统一判定 truth：重复 row、提前回到起始排列（premature_rounds）、
结尾是否回到起始排列（closed）、row 上限——绝不用各方法单独的 truth 代替跨区段结论。</li>
<li>逐行条目标明区段、方法版本（method_id/name/version）、lead、change 与覆盖来源
（<code>override</code> 为 null 或 <code>{"segment","lead","change","notation","replaces_token"}</code>）。</li>
<li>报告汇总：各方法使用的 leads/rows（<code>methods_used</code>）、切换点前后 row
（<code>switches</code>）、首次重复的两处位置（<code>truth.first_repeat</code>，含 segment）、
未应用覆盖（<code>unapplied_overrides</code>，如同一 change 被后续覆盖取代）。</li>
</ul>

<h2>接口一览</h2>
<table>
<tr><th>方法</th><th>路径</th><th>说明</th></tr>
<tr><td class="tag post">POST</td><td><code>/api/parse</code></td><td>仅解析记号。Body: <code>{"stage":6,"notation":"x.16.x.16.x.16,x.12"}</code></td></tr>
<tr><td class="tag post">POST</td><td><code>/api/methods</code></td><td>创建方法版本（同名自动递增 version）。Body: <code>{"name":"Plain Bob Minor","stage":6,"notation":"…","start_row":"123456"?}</code></td></tr>
<tr><td class="tag get">GET</td><td><code>/api/methods</code></td><td>列出所有方法版本</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/methods/{id}</code></td><td>方法详情 + 解析后的 changes</td></tr>
<tr><td class="tag post">POST</td><td><code>/api/analyses</code></td><td>创建分析。Body: <code>{"method_id":1,"overrides":[{"lead":1,"change":12,"notation":"14"}]?,"max_rows":720?}</code></td></tr>
<tr><td class="tag get">GET</td><td><code>/api/analyses</code></td><td>列出分析（可选 <code>?method_id=</code>）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/analyses/{id}</code></td><td>完整报告（含逐行 rows）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/analyses/{id}/rows?from=0&amp;to=60</code></td><td>逐行结果（可切片）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/analyses/{id}/download</code></td><td>下载报告 JSON（attachment）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/compare?a=1&amp;b=2</code></td><td>比较两个分析的周期与重复位置；也支持 POST <code>{"a":1,"b":2}</code> 或 <code>{"method_a":1,"method_b":2}</code>（自动建分析）</td></tr>
<tr><td class="tag post">POST</td><td><code>/api/touches</code></td><td>创建 spliced touch。Body: <code>{"segments":[{"method_id":1,"leads":2,"overrides":[{"lead":1,"change":12,"notation":"14"}]?},{"method_id":2,"leads":3}],"start_row":"123456"?,"max_rows":720?}</code></td></tr>
<tr><td class="tag get">GET</td><td><code>/api/touches</code></td><td>列出全部 touch（摘要）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/touches/{id}</code></td><td>完整 touch 报告（含逐行 rows）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/touches/{id}/rows?segment=2&amp;from=0&amp;to=60</code></td><td>逐行结果，可按区段过滤、按行号切片</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/touches/{id}/download</code></td><td>下载 touch 报告 JSON（attachment）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/touches/compare?a=1&amp;b=2</code></td><td>比较两次 touch 的规模、闭合、truth 与首次重复位置；也支持 POST <code>{"a":1,"b":2}</code></td></tr>
<tr><td class="tag post">POST</td><td><code>/api/schemes</code></td><td>创建音乐性评分<strong>方案版本</strong>（同名自动递增 version）。Body: <code>{"name":"minor-music","stage":6,"rules":[…]}</code>（stage 也可改传 <code>analysis_id</code>/<code>touch_id</code> 继承）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/schemes?stage=6</code></td><td>列出方案版本（可按 stage 过滤）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/schemes/{id}</code></td><td>方案详情（含规范化后的全部规则）</td></tr>
<tr><td class="tag post">POST</td><td><code>/api/music</code></td><td>对一个已存 analysis 或 touch 打分。Body: <code>{"analysis_id":1,"scheme_id":1}</code>（二者二选一）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/music?kind=&amp;subject_id=&amp;scheme_id=&amp;stage=</code></td><td>列出音乐分析结果（摘要 + 各规则得分）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/music/{id}</code></td><td>完整音乐分析（规则汇总 + 全部命中）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/music/{id}/hits?rule_id=&amp;kind=&amp;stroke=&amp;lead_end=&amp;segment=&amp;method_id=&amp;from=&amp;to=</code></td><td>命中筛选：按规则/类型/stroke/lead end/区段/方法与行号区间过滤</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/music/{id}/download</code></td><td>下载音乐分析 JSON（含完整方案规则，attachment）</td></tr>
<tr><td class="tag get">GET</td><td><code>/api/music/compare?a=1&amp;b=2</code></td><td>比较两个音乐分析（须同 stage、同方案版本）；也支持 POST <code>{"a":1,"b":2}</code></td></tr>
</table>

<h2>示例</h2>
<pre># 创建方法（Plain Bob Minor）
curl -s -X POST localhost:8000/api/methods -d '{"name":"Plain Bob Minor","stage":6,
  "notation":"x.16.x.16.x.16.x.16.x.16.x.12"}'

# 创建分析（lead 1 第 12 变用 14 覆盖，即一个 bob）
curl -s -X POST localhost:8000/api/analyses -d '{"method_id":1,
  "overrides":[{"lead":1,"change":12,"notation":"14"}]}'

# 逐行查看前 13 行
curl -s 'localhost:8000/api/analyses/1/rows?from=0&amp;to=12'

# 比较两版并下载报告
curl -s 'localhost:8000/api/compare?a=1&amp;b=2'
curl -sOJ localhost:8000/api/analyses/1/download</pre>

<h2>示例：spliced touch</h2>
<pre># 三段拼接：2 leads 方法1 → 2 leads 方法2（段内 bob 覆盖）→ 1 lead 方法1
curl -s -X POST localhost:8000/api/touches -d '{
  "segments": [
    {"method_id": 1, "leads": 2},
    {"method_id": 2, "leads": 2,
     "overrides": [{"lead": 1, "change": 12, "notation": "14"}]},
    {"method_id": 1, "leads": 1}
  ]}'

# 按区段读取逐行结果（第 2 段）
curl -s 'localhost:8000/api/touches/1/rows?segment=2'

# 比较两次 touch / 下载报告
curl -s 'localhost:8000/api/touches/compare?a=1&amp;b=2'
curl -sOJ localhost:8000/api/touches/1/download</pre>
<p>校验错误按区段定位（<code>segment</code> 为 1 起的区段序号），且不落库：</p>
<pre>{"error": "segment 2: method 99 not found", "code": "not_found",
 "segment": 2, "method_id": 99}
{"error": "stage mismatch: segment has 5 bells, the touch is on 6",
 "code": "bad_segment", "segment": 2, "stage": 5, "expected": 6}
{"error": "total rows 84 exceed the limit of 60", "code": "bad_segment",
 "segment": 3, "total_rows": 84, "max_rows": 60}</pre>
<h2>示例：10 口（Royal）与 12 口（Maximus）</h2>
<p>新建数据库后方法 id 取决于自增序列，下面的命令<strong>直接接续使用创建响应里的
<code>id</code></strong>（用标准库 <code>python3</code> 解析 JSON），可整段复制执行：</p>
<pre># 创建 Plain Bob Royal（10 口）：x10 的“10”是 places 1 与 10；
# 10! 超过硬上限，必须显式给 max_rows
RID=$(curl -s -X POST localhost:8000/api/methods -d '{"name":"Plain Bob Royal","stage":10,
  "notation":"x10x10x10x10x10,12","start_row":"1234567890"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/api/analyses \
  -d '{"method_id":'"$RID"',"max_rows":180}'
# lead head 为 1352749608；整 course 180 行，闭合且 true

# 12 口 Plain Bob Maximus；只检查前 240 行 -> 未闭合、truth 不下结论
MID=$(curl -s -X POST localhost:8000/api/methods -d '{"name":"Plain Bob Maximus","stage":12,
  "notation":"x1Tx1Tx1Tx1Tx1Tx1T,12"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/api/analyses \
  -d '{"method_id":'"$MID"',"max_rows":240}'
# -> {"status":"exceeded_limit","closed":false,
#     "truth":{"true":null,"conclusive":false,"checked_rows":240,"first_repeat":null},
#     "problems":["exceeded_limit","not_closed","truth_inconclusive"]}

# 不传 max_rows 会被拒绝（错误码 limit_required）
curl -s -X POST localhost:8000/api/analyses -d '{"method_id":'"$MID"'}'
# -> {"code":"limit_required","error":"stage 12: one extent is 479,001,600 rows
#     which exceeds the hard limit of 1,000,000; pass an explicit max_rows ...",
#     "stage":12,"extent_rows":479001600,"hard_max_rows":1000000}

# 紧凑 row 的重复/缺漏定位（数组输入用整数）；offset 按原始字符串计，前导空白也算
curl -s -X POST localhost:8000/api/methods -d '{"name":"X","stage":12,
  "notation":"x","start_row":" 1234567890EE"}'
# -> {"code":"notation_error","token":"E","offset":12,"error":"bell 11 appears ..."}</pre>

<h2>示例：音乐性评分</h2>
<pre># 1) 创建评分方案（同名再发即新版本）：带权 run + 钟号序列 + 整 row
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
     "lead_end": "lead_end", "weight": 3}
  ]}'

# 2) 对已存分析（id=1）打分；max_rows 截断的分析只计已检查 rows 并标 partial
curl -s -X POST localhost:8000/api/music -d '{"analysis_id":1,"scheme_id":1}'

# 3) 命中筛选：只看 handstroke 的 lead-end 命中 / 只看某规则的 run 命中
curl -s 'localhost:8000/api/music/1/hits?stroke=hand&amp;lead_end=true'
curl -s 'localhost:8000/api/music/1/hits?rule_id=front-up&amp;kind=run&amp;from=0&amp;to=24'

# 4) 比较两个结果（必须同 stage、同方案版本）：各规则得分与命中增减
curl -s 'localhost:8000/api/music/compare?a=1&amp;b=2'
# 方案版本不同 -> 400 {"code":"incomparable","reasons":["scheme_version"]}

# 5) touch 评分可按区段限定规则；命中可按 segment/method_id 过滤
curl -s -X POST localhost:8000/api/schemes -d '{"name":"seg2","stage":6,"rules":[
  {"name":"segment 2 runs","kind":"run","direction":"both","position":"any",
   "min_length":4,"segments":[2]}]}'
curl -s -X POST localhost:8000/api/music -d '{"touch_id":1,"scheme_id":2}'
curl -s 'localhost:8000/api/music/3/hits?segment=2'

# 6) 下载（内含完整方案规则，自包含）
curl -sOJ localhost:8000/api/music/1/download</pre>
<p>规则错误定位到 0 起的 <code>rule</code>（及 <code>field</code>），row/sequence 符号错误另带
<code>token</code>/<code>offset</code>，不落库：</p>
<pre>{"code":"bad_rule","error":"direction must be 'up', 'down' or 'both'",
 "rule":1,"field":"direction"}
{"code":"bad_rule","error":"bell 5 appears more than once in the row",
 "rule":0,"field":"row","token":"5","offset":5}</pre>

<p>更多说明见仓库 <code>README.md</code>；演示脚本：<code>python3 examples.py</code>。</p>
</body>
</html>
"""

API_INDEX = {
    "name": "Change Ringing Method Validator API",
    "version": "1.2",
    "min_stage": 4,
    "max_stage": 12,
    "hard_max_rows": HARD_MAX_ROWS,
    "scoring_version": SCORING_VERSION,
    "index0_stroke": "hand",
    "bell_symbols": {"10": "0", "11": "E", "12": "T"},
    "docs": "/",
    "endpoints": [
        "POST /api/parse",
        "POST /api/methods",
        "GET  /api/methods",
        "GET  /api/methods/{id}",
        "POST /api/analyses",
        "GET  /api/analyses",
        "GET  /api/analyses/{id}",
        "GET  /api/analyses/{id}/rows?from=&to=",
        "GET  /api/analyses/{id}/download",
        "GET  /api/compare?a=&b=  (or POST)",
        "POST /api/touches",
        "GET  /api/touches",
        "GET  /api/touches/{id}",
        "GET  /api/touches/{id}/rows?segment=&from=&to=",
        "GET  /api/touches/{id}/download",
        "GET  /api/touches/compare?a=&b=  (or POST)",
        "POST /api/schemes",
        "GET  /api/schemes?stage=",
        "GET  /api/schemes/{id}",
        "POST /api/music",
        "GET  /api/music?kind=&subject_id=&scheme_id=&stage=",
        "GET  /api/music/{id}",
        "GET  /api/music/{id}/hits?rule_id=&kind=&stroke=&lead_end=&segment=&method_id=&from=&to=",
        "GET  /api/music/{id}/download",
        "GET  /api/music/compare?a=&b=  (or POST)",
    ],
}


class ApiError(Exception):
    def __init__(self, status, message, code="error", extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.extra = extra or {}


# --------------------------------------------------------------------- helpers
def _require(body, key):
    if not isinstance(body, dict) or key not in body:
        raise ApiError(400, f"missing required field {key!r}", "missing_field")
    return body[key]


def _method_json(store, method, with_changes=True):
    out = {k: method[k] for k in
           ("id", "name", "stage", "notation", "start_row", "version", "created_at")}
    if with_changes:
        changes = parse_notation(method["notation"], method["stage"])
        out["changes"] = [c.to_dict() for c in changes]
        out["lead_length"] = len(changes)
    return out


def _analysis_summary(rec):
    rep = rec["report"]
    keys = ("status", "closed", "period_leads", "period_rows", "lead_length",
            "lead_head", "hunt_bells", "problems", "rows_generated")
    out = {k: rep[k] for k in keys}
    out.update({"id": rec["id"], "method_id": rec["method_id"],
                "created_at": rec["created_at"], "truth": rep["truth"]})
    return out


def _get_method_or_404(store, method_id):
    method = store.get_method(method_id)
    if method is None:
        raise ApiError(404, f"method {method_id} not found", "not_found")
    return method


def _get_analysis_or_404(store, analysis_id):
    rec = store.get_analysis(analysis_id)
    if rec is None:
        raise ApiError(404, f"analysis {analysis_id} not found", "not_found")
    return rec


def _get_touch_or_404(store, touch_id):
    rec = store.get_touch(touch_id)
    if rec is None:
        raise ApiError(404, f"touch {touch_id} not found", "not_found")
    return rec


def _touch_summary(rec):
    rep = rec["report"]
    keys = ("status", "closed", "stage", "total_rows", "total_leads",
            "segment_count", "problems")
    out = {k: rep[k] for k in keys}
    out.update({"id": rec["id"], "created_at": rec["created_at"],
                "truth": rep["truth"], "methods_used": rep["methods_used"]})
    return out


def _run_analysis(store, method_id, overrides=None, max_rows=None):
    """Run an analysis for a method version and persist the report."""
    method = _get_method_or_404(store, method_id)
    report = analyze(method["stage"], method["notation"],
                     start_row=method["start_row"],
                     overrides=overrides, max_rows=max_rows)
    return store.create_analysis(method_id, overrides or [],
                                 report["max_rows"], report["status"], report)


# --------------------------------------------------------------------- music
def _scheme_json(rec, with_rules=True):
    out = {k: rec[k] for k in
           ("id", "name", "stage", "version", "created_at")}
    out["scoring_version"] = SCORING_VERSION
    out["rule_count"] = len(rec["rules"])
    if with_rules:
        out["rules"] = rec["rules"]
    return out


def _normalized_scheme(rec):
    """Rebuild a parse_scheme() form (rule bells as tuples) from a record."""
    rules = []
    for rule in rec["rules"]:
        norm = {k: rule[k] for k in
                ("id", "name", "kind", "weight", "strokes",
                 "lead_end", "segments")}
        if rule["kind"] == "run":
            norm.update({"direction": rule["direction"],
                         "position": rule["position"],
                         "min_length": rule["min_length"]})
        elif rule["kind"] == "sequence":
            # a sequence is only a contiguous bell fragment, not a full row
            bells = compact_bells(rule["bells"], rec["stage"])
            norm.update({"bells": bells, "pattern": rule["bells"],
                         "position": rule["position"]})
        else:
            bells = parse_row(rule["row"], rec["stage"])
            norm.update({"bells": bells, "pattern": rule["row"]})
        rules.append(norm)
    return {"id": rec["id"], "name": rec["name"], "stage": rec["stage"],
            "version": rec["version"], "rules": rules}


def _music_summary(rec):
    res = rec["result"]
    return {"id": rec["id"], "created_at": rec["created_at"],
            "kind": rec["kind"], "stage": rec["stage"],
            "subject_id": rec["subject_id"], "scheme_id": rec["scheme_id"],
            "scheme": {"id": rec["scheme_id"],
                       "version": rec["scheme_version"]},
            "scheme_version": rec["scheme_version"],
            "status": rec["status"], "partial": rec["partial"],
            "rows_analyzed": res["rows_analyzed"],
            "checked_rows": res["checked_rows"],
            "total_hits": res["total_hits"],
            "total_score": res["total_score"],
            "truth": res["truth"],
            "rule_scores": [{"id": r["id"], "name": r["name"],
                             "kind": r["kind"], "hits": r["hits"],
                             "score": r["score"]} for r in res["rules"]]}


def _get_scheme_or_404(store, scheme_id):
    scheme = store.get_scheme(scheme_id)
    if scheme is None:
        raise ApiError(404, f"scheme {scheme_id} not found", "not_found")
    return scheme


def _get_music_or_404(store, music_id):
    rec = store.get_music_analysis(music_id)
    if rec is None:
        raise ApiError(404, f"music analysis {music_id} not found", "not_found")
    return rec


def _filtered_hits(hits, query):
    """Filter stored hits: rule_id, kind, stroke, lead_end, segment, method,
    plus a global row-index slice (from/to inclusive)."""
    out = hits

    def _one(name):
        return query[name][0] if name in query else None

    rule_id = _one("rule_id")
    if rule_id is not None:
        out = [h for h in out if str(h["rule_id"]) == rule_id]
    kind = _one("kind")
    if kind is not None:
        out = [h for h in out if h["kind"] == kind]
    stroke = _one("stroke")
    if stroke is not None:
        if stroke not in ("hand", "back"):
            raise ApiError(400, "stroke must be 'hand' or 'back'", "bad_field")
        out = [h for h in out if h["stroke"] == stroke]
    lead_end = _one("lead_end")
    if lead_end is not None:
        wanted = {"1": True, "true": True, "0": False, "false": False}.get(
            lead_end.lower())
        if wanted is None:
            raise ApiError(400, "lead_end must be true or false", "bad_field")
        out = [h for h in out if h["lead_end"] is wanted]
    segment = _one("segment")
    if segment is not None:
        try:
            segment = int(segment)
        except ValueError:
            raise ApiError(400, "segment must be an integer", "bad_field")
        out = [h for h in out if h["segment"] == segment]
    method_id = _one("method_id")
    if method_id is not None:
        try:
            method_id = int(method_id)
        except ValueError:
            raise ApiError(400, "method_id must be an integer", "bad_field")
        out = [h for h in out if h["method_id"] == method_id]
    try:
        start = int(query["from"][0]) if "from" in query else 0
        end = int(query["to"][0]) if "to" in query else None
    except ValueError:
        raise ApiError(400, "from/to must be integers", "bad_field")
    out = [h for h in out if h["index"] >= start
           and (end is None or h["index"] <= end)]
    return out


# --------------------------------------------------------------------- routes
def api_parse(handler, query, body):
    stage = _require(body, "stage")
    notation = _require(body, "notation")
    changes = parse_notation(notation, stage)
    out = {"stage": stage, "notation": notation,
           "lead_length": len(changes),
           "changes": [c.to_dict() for c in changes]}
    if "start_row" in body:
        out["start_row"] = row_str(parse_row(body["start_row"], stage))
    return out, 200


def api_create_method(handler, query, body):
    name = _require(body, "name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "name must be a non-empty string", "bad_field")
    stage = _require(body, "stage")
    notation = _require(body, "notation")
    parse_notation(notation, stage)  # validates stage and notation (located errors)
    start_row = row_str(parse_row(body.get("start_row"), stage))
    method = handler.server.store.create_method(name.strip(), stage, notation, start_row)
    return _method_json(handler.server.store, method), 201


def api_list_methods(handler, query, body):
    store = handler.server.store
    return {"methods": [_method_json(store, m, with_changes=False)
                        for m in store.list_methods()]}, 200


def api_get_method(handler, query, body, method_id):
    store = handler.server.store
    return _method_json(store, _get_method_or_404(store, method_id)), 200


def api_create_analysis(handler, query, body):
    store = handler.server.store
    method_id = _require(body, "method_id")
    overrides = body.get("overrides")
    if overrides is not None and not isinstance(overrides, list):
        raise ApiError(400, "overrides must be a list", "bad_field")
    max_rows = body.get("max_rows")
    if max_rows is not None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) \
                or not 1 <= max_rows <= HARD_MAX_ROWS:
            raise ApiError(400, f"max_rows must be an integer in 1..{HARD_MAX_ROWS}",
                           "bad_field")
    rec = _run_analysis(store, method_id, overrides=overrides, max_rows=max_rows)
    out = _analysis_summary(rec)
    out["links"] = {
        "report": f"/api/analyses/{rec['id']}",
        "rows": f"/api/analyses/{rec['id']}/rows",
        "download": f"/api/analyses/{rec['id']}/download",
    }
    return out, 201


def api_list_analyses(handler, query, body):
    store = handler.server.store
    method_id = None
    if "method_id" in query:
        try:
            method_id = int(query["method_id"][0])
        except ValueError:
            raise ApiError(400, "method_id must be an integer", "bad_field")
    return {"analyses": [_analysis_summary(r)
                         for r in store.list_analyses(method_id)]}, 200


def api_get_analysis(handler, query, body, analysis_id):
    store = handler.server.store
    rec = _get_analysis_or_404(store, analysis_id)
    method = _get_method_or_404(store, rec["method_id"])
    return {"id": rec["id"], "created_at": rec["created_at"],
            "method": _method_json(store, method, with_changes=False),
            "overrides": rec["overrides"], "max_rows": rec["max_rows"],
            "status": rec["status"], "report": rec["report"]}, 200


def api_get_rows(handler, query, body, analysis_id):
    store = handler.server.store
    rec = _get_analysis_or_404(store, analysis_id)
    rows = rec["report"]["rows"]

    def _int_param(name, default):
        if name not in query:
            return default
        try:
            return int(query[name][0])
        except ValueError:
            raise ApiError(400, f"{name} must be an integer", "bad_field")

    start = max(0, _int_param("from", 0))
    end = _int_param("to", len(rows) - 1)  # inclusive row index
    sliced = [r for r in rows if start <= r["index"] <= end]
    return {"id": rec["id"], "total": len(rows), "from": start,
            "to": min(end, len(rows) - 1), "rows": sliced}, 200


def api_download(handler, query, body, analysis_id):
    store = handler.server.store
    rec = _get_analysis_or_404(store, analysis_id)
    method = _get_method_or_404(store, rec["method_id"])
    payload = {"id": rec["id"], "created_at": rec["created_at"],
               "method": _method_json(store, method, with_changes=False),
               "overrides": rec["overrides"], "report": rec["report"]}
    return payload, 200, f"analysis-{rec['id']}.json"


def api_compare(handler, query, body):
    store = handler.server.store

    def _param(name, sources):
        for src in sources:
            if isinstance(src, dict) and name in src:
                value = src[name]
                return value[0] if isinstance(value, list) else value
        return None

    a = _param("a", (body, query))
    b = _param("b", (body, query))
    ma = _param("method_a", (body, query))
    mb = _param("method_b", (body, query))
    try:
        if a is not None and b is not None:
            rec_a = _get_analysis_or_404(store, int(a))
            rec_b = _get_analysis_or_404(store, int(b))
        elif ma is not None and mb is not None:
            rec_a = _run_analysis(store, int(ma))
            rec_b = _run_analysis(store, int(mb))
        else:
            raise ApiError(400, "provide analysis ids a & b (or method_a & method_b)",
                           "missing_field")
    except ValueError:
        raise ApiError(400, "ids must be integers", "bad_field")

    def _side(rec):
        method = _get_method_or_404(store, rec["method_id"])
        return {"analysis_id": rec["id"], "method_id": method["id"],
                "method": method["name"], "version": method["version"],
                "created_at": rec["created_at"]}

    comparison = compare_reports(rec_a["report"], rec_b["report"])
    return {"a": _side(rec_a), "b": _side(rec_b), "comparison": comparison}, 200


def api_create_touch(handler, query, body):
    """Create a spliced touch: segments of method versions run back to back.

    Every segment is validated (method exists, same stage, leads >= 1,
    overrides inside the segment, cumulative rows within the limit) and any
    failure is reported against its 1-based segment index; nothing is stored
    unless the whole touch expands successfully.
    """
    store = handler.server.store
    segments = _require(body, "segments")
    if not isinstance(segments, list) or not segments:
        raise ApiError(400, "segments must be a non-empty list", "bad_field")
    max_rows = body.get("max_rows")
    if max_rows is not None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) \
                or not 1 <= max_rows <= HARD_MAX_ROWS:
            raise ApiError(400, f"max_rows must be an integer in 1..{HARD_MAX_ROWS}",
                           "bad_field")
    resolved = []
    for i, spec in enumerate(segments, start=1):
        if not isinstance(spec, dict):
            raise ApiError(400, f"segment {i} must be an object", "bad_segment",
                           {"segment": i})
        method_id = spec.get("method_id")
        if isinstance(method_id, bool) or not isinstance(method_id, int):
            raise ApiError(400, f"segment {i}: method_id must be an integer",
                           "bad_segment", {"segment": i})
        method = store.get_method(method_id)
        if method is None:
            raise ApiError(404, f"segment {i}: method {method_id} not found",
                           "not_found", {"segment": i, "method_id": method_id})
        overrides = spec.get("overrides") or []
        if not isinstance(overrides, list):
            raise ApiError(400, f"segment {i}: overrides must be a list",
                           "bad_segment", {"segment": i})
        resolved.append({"method_id": method["id"], "name": method["name"],
                         "version": method["version"], "stage": method["stage"],
                         "notation": method["notation"],
                         "leads": spec.get("leads"), "overrides": overrides})
    try:
        report = analyze_spliced(resolved, start_row=body.get("start_row"),
                                 max_rows=max_rows)
    except SplicedError as err:
        extra = {k: v for k, v in err.to_dict().items() if k != "error"}
        raise ApiError(400, err.message, "bad_segment", extra)
    rec = store.create_touch(report["stage"], segments, report["max_rows"],
                             report["status"], report)
    out = _touch_summary(rec)
    out["links"] = {
        "report": f"/api/touches/{rec['id']}",
        "rows": f"/api/touches/{rec['id']}/rows",
        "download": f"/api/touches/{rec['id']}/download",
    }
    return out, 201


def api_list_touches(handler, query, body):
    store = handler.server.store
    return {"touches": [_touch_summary(r) for r in store.list_touches()]}, 200


def api_get_touch(handler, query, body, touch_id):
    store = handler.server.store
    rec = _get_touch_or_404(store, touch_id)
    return {"id": rec["id"], "created_at": rec["created_at"],
            "stage": rec["stage"], "segments": rec["segments"],
            "max_rows": rec["max_rows"], "status": rec["status"],
            "report": rec["report"]}, 200


def api_touch_rows(handler, query, body, touch_id):
    store = handler.server.store
    rec = _get_touch_or_404(store, touch_id)
    report = rec["report"]
    rows = report["rows"]
    segment = None
    if "segment" in query:
        try:
            segment = int(query["segment"][0])
        except ValueError:
            raise ApiError(400, "segment must be an integer", "bad_field")
        if not 0 <= segment <= report["segment_count"]:
            raise ApiError(404, f"touch {touch_id} has no segment {segment}",
                           "not_found",
                           {"segment": segment,
                            "segment_count": report["segment_count"]})
        rows = [r for r in rows if r["segment"] == segment]

    def _int_param(name, default):
        if name not in query:
            return default
        try:
            return int(query[name][0])
        except ValueError:
            raise ApiError(400, f"{name} must be an integer", "bad_field")

    start = max(0, _int_param("from", 0))
    end = _int_param("to", rows[-1]["index"] if rows else 0)  # inclusive
    sliced = [r for r in rows if start <= r["index"] <= end]
    return {"id": rec["id"], "segment": segment,
            "total": len(report["rows"]), "matched": len(rows),
            "from": start, "to": min(end, rows[-1]["index"] if rows else 0),
            "rows": sliced}, 200


def api_touch_download(handler, query, body, touch_id):
    store = handler.server.store
    rec = _get_touch_or_404(store, touch_id)
    payload = {"id": rec["id"], "created_at": rec["created_at"],
               "stage": rec["stage"], "segments": rec["segments"],
               "report": rec["report"]}
    return payload, 200, f"touch-{rec['id']}.json"


def api_compare_touches(handler, query, body):
    store = handler.server.store

    def _param(name):
        for src in (body, query):
            if isinstance(src, dict) and name in src:
                value = src[name]
                return value[0] if isinstance(value, list) else value
        return None

    a, b = _param("a"), _param("b")
    if a is None or b is None:
        raise ApiError(400, "provide touch ids a & b", "missing_field")
    try:
        rec_a = _get_touch_or_404(store, int(a))
        rec_b = _get_touch_or_404(store, int(b))
    except ValueError:
        raise ApiError(400, "ids must be integers", "bad_field")

    def _side(rec):
        return {"touch_id": rec["id"], "created_at": rec["created_at"],
                "methods_used": rec["report"]["methods_used"]}

    comparison = compare_touch_reports(rec_a["report"], rec_b["report"])
    return {"a": _side(rec_a), "b": _side(rec_b),
            "comparison": comparison}, 200


# --------------------------------------------------------------- music routes
def api_create_scheme(handler, query, body):
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object", "bad_field")
    name = _require(body, "name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "name must be a non-empty string", "bad_field")
    rules = body.get("rules")
    store = handler.server.store
    # the scheme may carry its own stage, or inherit from a referenced report
    stage = body.get("stage")
    if stage is None:
        if body.get("analysis_id"):
            ref = _get_analysis_or_404(
                store, _validate_id(body["analysis_id"], "analysis_id"))
            stage = ref["report"]["stage"]
        elif body.get("touch_id"):
            ref = _get_touch_or_404(store,
                                    _validate_id(body["touch_id"], "touch_id"))
            stage = ref["report"]["stage"]
    try:
        parsed = parse_scheme({"name": name, "stage": stage, "rules": rules})
    except SchemeError as err:
        raise ApiError(400, err.message, "bad_rule",
                       {k: v for k, v in err.to_dict().items()
                        if k != "error" and v is not None})
    except NotationError as err:
        raise ApiError(400, err.message, "notation_error",
                       {k: v for k, v in err.to_dict().items() if k != "error"})
    stored_rules = [rule_to_dict(rule) for rule in parsed["rules"]]
    rec = store.create_scheme(parsed["name"], parsed["stage"], stored_rules)
    return _scheme_json(rec), 201


def api_list_schemes(handler, query, body):
    store = handler.server.store
    stage = None
    if "stage" in query:
        try:
            stage = int(query["stage"][0])
        except ValueError:
            raise ApiError(400, "stage must be an integer", "bad_field")
    return {"schemes": [_scheme_json(s, with_rules=False)
                        for s in store.list_schemes(stage)]}, 200


def api_get_scheme(handler, query, body, scheme_id):
    return _scheme_json(_get_scheme_or_404(handler.server.store, scheme_id)), 200


def _validate_id(value, field):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} must be an integer", "bad_field")


def api_create_music(handler, query, body):
    """Score one stored analysis OR one stored touch against a scheme version.

    Body: {"analysis_id"|"touch_id", "scheme_id"}.  The stage must match the
    scheme's stage; a report stopped by max_rows is scored over the rows
    actually checked and returned as partial=true.
    """
    store = handler.server.store
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object", "bad_field")
    analysis_id = body.get("analysis_id")
    touch_id = body.get("touch_id")
    if (analysis_id is None) == (touch_id is None):
        raise ApiError(400,
                       "provide exactly one of 'analysis_id' and 'touch_id'",
                       "missing_field")
    scheme_id = _validate_id(_require(body, "scheme_id"), "scheme_id")
    scheme_rec = _get_scheme_or_404(store, scheme_id)
    normalized = _normalized_scheme(scheme_rec)
    if touch_id is None:
        rec = _get_analysis_or_404(store, _validate_id(analysis_id, "analysis_id"))
        method = _get_method_or_404(store, rec["method_id"])
        result = score_analysis(
            rec["report"], normalized,
            method={"id": method["id"], "name": method["name"],
                    "version": method["version"]})
        subject_id = rec["id"]
    else:
        rec = _get_touch_or_404(store, _validate_id(touch_id, "touch_id"))
        result = score_touch(rec["report"], normalized)
        subject_id = rec["id"]
    music = store.create_music_analysis(
        result["kind"], result["stage"], subject_id, scheme_rec["id"],
        scheme_rec["version"], result)
    out = _music_summary(music)
    out["links"] = {
        "report": f"/api/music/{music['id']}",
        "hits": f"/api/music/{music['id']}/hits",
        "download": f"/api/music/{music['id']}/download",
    }
    return out, 201


def api_list_music(handler, query, body):
    store = handler.server.store

    def _opt_int(name):
        if name not in query:
            return None
        try:
            return int(query[name][0])
        except ValueError:
            raise ApiError(400, f"{name} must be an integer", "bad_field")

    kind = query["kind"][0] if "kind" in query else None
    if kind is not None and kind not in ("analysis", "touch"):
        raise ApiError(400, "kind must be 'analysis' or 'touch'", "bad_field")
    recs = store.list_music_analyses(kind=kind,
                                     subject_id=_opt_int("subject_id"),
                                     scheme_id=_opt_int("scheme_id"),
                                     stage=_opt_int("stage"))
    return {"music_analyses": [_music_summary(r) for r in recs]}, 200


def _music_full(rec, scheme_rec):
    return {"id": rec["id"], "created_at": rec["created_at"],
            "kind": rec["kind"], "stage": rec["stage"],
            "subject_id": rec["subject_id"],
            "scheme": _scheme_json(scheme_rec, with_rules=False),
            "result": rec["result"]}


def api_get_music(handler, query, body, music_id):
    rec = _get_music_or_404(handler.server.store, music_id)
    scheme_rec = _get_scheme_or_404(handler.server.store, rec["scheme_id"])
    return _music_full(rec, scheme_rec), 200


def api_music_hits(handler, query, body, music_id):
    rec = _get_music_or_404(handler.server.store, music_id)
    hits = _filtered_hits(rec["result"]["hits"], query)
    return {"id": rec["id"], "kind": rec["kind"],
            "total_hits": rec["result"]["total_hits"],
            "matched": len(hits), "hits": hits}, 200


def api_music_download(handler, query, body, music_id):
    rec = _get_music_or_404(handler.server.store, music_id)
    scheme_rec = _get_scheme_or_404(handler.server.store, rec["scheme_id"])
    payload = _music_full(rec, scheme_rec)
    # include the complete normalized scheme rules for a self-contained file
    payload["scheme"] = _scheme_json(scheme_rec, with_rules=True)
    return payload, 200, f"music-{rec['kind']}-{rec['id']}.json"


def api_compare_music(handler, query, body):
    store = handler.server.store

    def _param(name):
        for src in (body, query):
            if isinstance(src, dict) and name in src:
                value = src[name]
                return value[0] if isinstance(value, list) else value
        return None

    a, b = _param("a"), _param("b")
    if a is None or b is None:
        raise ApiError(400, "provide music result ids a & b", "missing_field")
    rec_a = _get_music_or_404(store, _validate_id(a, "a"))
    rec_b = _get_music_or_404(store, _validate_id(b, "b"))
    res_a, res_b = dict(rec_a["result"]), dict(rec_b["result"])
    res_a["id"], res_b["id"] = rec_a["id"], rec_b["id"]
    comparison = compare_music(res_a, res_b)
    if not comparison["comparable"]:
        # only same-stage, same scheme version results may be compared
        raise ApiError(
            400,
            "music results are not comparable: " + ", ".join(comparison["reasons"]),
            "incomparable", {"reasons": comparison["reasons"],
                             "a": comparison["a"], "b": comparison["b"]})

    def _side(rec):
        return {"music_id": rec["id"], "kind": rec["kind"],
                "stage": rec["stage"], "subject_id": rec["subject_id"],
                "scheme_id": rec["scheme_id"],
                "scheme_version": rec["scheme_version"],
                "created_at": rec["created_at"]}

    return {"a": _side(rec_a), "b": _side(rec_b),
            "comparison": comparison}, 200


ROUTES = [
    ("GET", re.compile(r"^/$"), lambda h, q, b: (DOCS_HTML, 200, None, "html")),
    ("GET", re.compile(r"^/api$"), lambda h, q, b: (API_INDEX, 200)),
    ("POST", re.compile(r"^/api/parse$"), api_parse),
    ("POST", re.compile(r"^/api/methods$"), api_create_method),
    ("GET", re.compile(r"^/api/methods$"), api_list_methods),
    ("GET", re.compile(r"^/api/methods/(\d+)$"),
     lambda h, q, b, mid: api_get_method(h, q, b, int(mid))),
    ("POST", re.compile(r"^/api/analyses$"), api_create_analysis),
    ("GET", re.compile(r"^/api/analyses$"), api_list_analyses),
    ("GET", re.compile(r"^/api/analyses/(\d+)$"),
     lambda h, q, b, aid: api_get_analysis(h, q, b, int(aid))),
    ("GET", re.compile(r"^/api/analyses/(\d+)/rows$"),
     lambda h, q, b, aid: api_get_rows(h, q, b, int(aid))),
    ("GET", re.compile(r"^/api/analyses/(\d+)/download$"),
     lambda h, q, b, aid: api_download(h, q, b, int(aid))),
    ("GET", re.compile(r"^/api/compare$"), api_compare),
    ("POST", re.compile(r"^/api/compare$"), api_compare),
    ("POST", re.compile(r"^/api/touches$"), api_create_touch),
    ("GET", re.compile(r"^/api/touches$"), api_list_touches),
    ("GET", re.compile(r"^/api/touches/compare$"), api_compare_touches),
    ("POST", re.compile(r"^/api/touches/compare$"), api_compare_touches),
    ("GET", re.compile(r"^/api/touches/(\d+)$"),
     lambda h, q, b, tid: api_get_touch(h, q, b, int(tid))),
    ("GET", re.compile(r"^/api/touches/(\d+)/rows$"),
     lambda h, q, b, tid: api_touch_rows(h, q, b, int(tid))),
    ("GET", re.compile(r"^/api/touches/(\d+)/download$"),
     lambda h, q, b, tid: api_touch_download(h, q, b, int(tid))),
    ("POST", re.compile(r"^/api/schemes$"), api_create_scheme),
    ("GET", re.compile(r"^/api/schemes$"), api_list_schemes),
    ("GET", re.compile(r"^/api/schemes/(\d+)$"),
     lambda h, q, b, sid: api_get_scheme(h, q, b, int(sid))),
    ("POST", re.compile(r"^/api/music$"), api_create_music),
    ("GET", re.compile(r"^/api/music$"), api_list_music),
    ("GET", re.compile(r"^/api/music/compare$"), api_compare_music),
    ("POST", re.compile(r"^/api/music/compare$"), api_compare_music),
    ("GET", re.compile(r"^/api/music/(\d+)$"),
     lambda h, q, b, mid: api_get_music(h, q, b, int(mid))),
    ("GET", re.compile(r"^/api/music/(\d+)/hits$"),
     lambda h, q, b, mid: api_music_hits(h, q, b, int(mid))),
    ("GET", re.compile(r"^/api/music/(\d+)/download$"),
     lambda h, q, b, mid: api_music_download(h, q, b, int(mid))),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "RingingAPI/1.2"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ------------------------------------------------------------- plumbing
    def _dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            path = parsed.path if parsed.path == "/" else parsed.path.rstrip("/")
            query = parse_qs(parsed.query)
            body = None
            for route_method, pattern, func in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                if method == "POST":
                    body = self._read_json()
                result = func(self, query, body, *match.groups())
                self._respond(result)
                return
            raise ApiError(404, f"no such route: {method} {path}", "not_found")
        except ApiError as err:
            self._send_json({"error": err.message, "code": err.code, **err.extra},
                            err.status)
        except NotationError as err:
            self._send_json({"code": "notation_error", **err.to_dict()}, 400)
        except LimitRequiredError as err:
            self._send_json({"code": "limit_required", **err.to_dict()}, 400)
        except SchemeError as err:
            self._send_json({"code": "bad_rule", **err.to_dict()}, 400)
        except ValueError as err:
            self._send_json({"error": str(err), "code": "bad_request"}, 400)
        except BrokenPipeError:
            pass
        except Exception as err:  # pragma: no cover - defensive
            self._send_json({"error": f"internal error: {err}", "code": "internal"}, 500)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body must be valid JSON", "bad_json")

    def _respond(self, result):
        download = None
        content_type = "json"
        if len(result) == 4:
            obj, status, download, content_type = result
        elif len(result) == 3:
            obj, status, download = result
        else:
            obj, status = result
        if content_type == "html":
            self._send_html(obj, status)
        else:
            self._send_json(obj, status, download_name=download)

    def _send_json(self, obj, status=200, download_name=None):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if download_name:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host="127.0.0.1", port=8000, db_path="ringing.db"):
    store = Store(db_path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Change-ringing method validation API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="ringing.db", help="SQLite database path")
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port, args.db)
    print(f"Change Ringing Method Validator API")
    print(f"  listening on http://{args.host}:{args.port}  (docs at /)")
    print(f"  database: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
        server.store.close()


if __name__ == "__main__":
    main()
