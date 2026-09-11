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
from ringing import (HARD_MAX_ROWS, NotationError, analyze, compare_reports,
                     parse_notation, parse_row, row_str)

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
所有接口（除本页外）均接收/返回 JSON；错误返回 <code>{"error": …, "code": …}</code>，
记号错误另附 <code>token</code>（原记号）与 <code>offset</code>（在记号串中的位置），绝不自动修正。</p>

<h2>Place notation 语法</h2>
<table>
<tr><th>记号</th><th>含义</th></tr>
<tr><td><code>x</code> / <code>X</code> / <code>-</code></td><td>全换（cross）：所有相邻位置交换，无 place</td></tr>
<tr><td><code>1</code>…<code>8</code></td><td>place：该位置的钟不动，如 <code>16</code>、<code>1256</code></td></tr>
<tr><td><code>.</code></td><td>分隔各个 change（<code>x</code>/<code>-</code> 前后可省略）；空白字符忽略</td></tr>
<tr><td><code>,</code></td><td>对称展开：<code>a,b</code> → <code>a + reverse(a[:-1]) + b</code>（a 的最后一个
change 是 half-lead 支点，镜像不重复；b 是 lead end）。如 <code>x16x16x16,12</code> →
<code>x.16.x.16.x.16.x.16.x.16.x.12</code>（12 变，即 Plain Bob Minor）；至多一个逗号</td></tr>
</table>
<p>解析规则：先按钟数补全可推断的首尾 place（如 6 口钟上 <code>3</code> → <code>36</code>、
<code>2</code> → <code>12</code>）；其余位置必须组成相邻交换对。非法字符、place 越界、
同一 change 内 place 重复、无法配对的 place 都会报错并定位到原记号。</p>

<h2>分析语义</h2>
<ul>
<li>展开 rows：给出 lead 长度、lead head（结束排列）、回到起始排列（rounds）的周期（leads/rows）、hunt bells（lead head 中位置未变的钟）。</li>
<li>truth 仅在一个 extent（stage! 行）内按 row 唯一性检查；首个重复 row 标出两处位置（index/lead/change），
逐行条目以 <code>repeat</code> 标记；发现重复后仍继续展开，直到闭合或达到上限。</li>
<li>lead 中途回到 rounds → <code>premature_rounds</code>，同时视为对第 0 行的重复（标出 0 与该行）；
达到上限仍未闭合 → <code>exceeded_limit</code> + <code>not_closed</code>；有重复 → <code>untrue</code>，分别报告。
闭合（lead 边界回到起始排列）时的周期 leads/rows 总会给出。</li>
<li>组合（composition）：分析时可用 <code>overrides</code> 在指定 lead 的某一变以 notation 覆盖（如 bob/single），报告保留覆盖点前后轨迹。</li>
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
<p>更多说明见仓库 <code>README.md</code>；演示脚本：<code>python3 examples.py</code>。</p>
</body>
</html>
"""

API_INDEX = {
    "name": "Change Ringing Method Validator API",
    "version": "1.0",
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


def _run_analysis(store, method_id, overrides=None, max_rows=None):
    """Run an analysis for a method version and persist the report."""
    method = _get_method_or_404(store, method_id)
    report = analyze(method["stage"], method["notation"],
                     start_row=method["start_row"],
                     overrides=overrides, max_rows=max_rows)
    return store.create_analysis(method_id, overrides or [],
                                 report["max_rows"], report["status"], report)


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
]


class Handler(BaseHTTPRequestHandler):
    server_version = "RingingAPI/1.0"
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
