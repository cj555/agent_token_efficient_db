"""健康面板：把 .health/ 里的 json 渲染成一张自包含的 HTML。

为什么是单文件、数据内联：面板用 file:// 打开，浏览器会把同目录 json 的 fetch
当跨域拦掉，所以数据必须内联进 HTML。也不引 jinja2 —— 一张固定版式的页面
用 f-string 拼就够了，多一个依赖不如少一个。

产物 .health/dashboard.html（.health/ 在 .gitignore 里，数据不进 git）。
"""
from __future__ import annotations

import datetime as dt
import html
import json
from typing import Any

from .config import Paths, paths

_COLORS = {"ok": "#16a34a", "warn": "#d97706", "fail": "#dc2626", "none": "#94a3b8"}
_LABEL = {"ok": "正常", "warn": "警告", "fail": "故障", "none": "未监控"}

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;}
@media (prefers-color-scheme:dark){
  :root{--bg:#0f1115;--card:#171a21;--fg:#e5e7eb;--muted:#9ca3af;--line:#2b303b;}
}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px;color:var(--muted);
  text-transform:none;letter-spacing:.02em}
a{color:inherit}
.sum{display:flex;gap:18px;align-items:center;flex-wrap:wrap;padding:14px 18px;border-radius:10px;
  background:var(--card);border:1px solid var(--line);border-left:6px solid var(--line)}
.sum b{font-size:22px;font-weight:600;margin-right:4px}
.muted{color:var(--muted)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--line);
  border-radius:10px;padding:12px 14px}
.card h3{margin:0 0 6px;font-size:15px;font-family:ui-monospace,Consolas,monospace}
.q{background:repeating-linear-gradient(45deg,transparent,transparent 6px,
  rgba(220,38,38,.10) 6px,rgba(220,38,38,.10) 12px)}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.b{font-size:11.5px;padding:1px 8px;border-radius:999px;border:1px solid currentColor;
  white-space:nowrap}
.reason{margin:6px 0 0}
.kv{color:var(--muted);font-size:12.5px;margin-top:6px}
.kv code{font-family:ui-monospace,Consolas,monospace}
details{margin-top:6px}summary{cursor:pointer;color:var(--muted);font-size:12.5px}
ul.f{margin:6px 0 0;padding-left:18px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
  border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-weight:500}tr:last-child td{border-bottom:0}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:2px}
.tl{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.pend{background:var(--card);border:1px solid var(--line);border-left:6px solid #d97706;
  border-radius:10px;padding:10px 14px;margin-bottom:8px}
.plan{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.plan select,.plan input,.plan button{font:inherit;font-size:12.5px;padding:2px 6px;
  border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.plan button{cursor:pointer}
.plan button:hover{border-color:var(--muted)}
.plan .jobline{flex-basis:100%;font-size:12px}
"""


def _e(x: Any) -> str:
    return html.escape(str(x if x is not None else ""))


def _badge(text: str, status: str) -> str:
    return f'<span class="b" style="color:{_COLORS[status]}">{_e(text)}</span>'


def _fields_block(title: str, items: list[str]) -> str:
    if not items:
        return ""
    li = "".join(f"<li>{_e(k)}</li>" for k in items[:40])
    return (f"<details><summary>{_e(title)}（{len(items)}）</summary>"
            f"<ul class='f'>{li}</ul></details>")


def _source_card(r: dict, consumers: list[str], att: dict, docs_idx: dict) -> str:
    st = r.get("status", "ok")
    sid = r.get("source", "?")
    q = bool(att.get("quarantined"))
    badges = [_badge(_LABEL.get(st, st), st)]

    # schema 徽章：没配探针就诚实标灰「未监控」，别绿着骗人
    if r.get("schema_changed"):
        badges.append(_badge(f"schema +{len(r.get('schema_added') or [])} "
                             f"-{len(r.get('schema_removed') or [])} 待确认", "warn"))
    elif r.get("schema_probe_error"):
        badges.append(_badge("schema 探针失败", "warn"))
    elif r.get("schema_hash"):
        badges.append(_badge("schema 一致", "ok"))
    else:
        badges.append(_badge("schema 未监控（已声明）" if r.get("_probe_declared")
                             else "schema 未监控", "none"))

    docs = r.get("_docs") or []
    if r.get("docs_changed"):
        badges.append(_badge(f"文档变更 {len(r['docs_changed'])} 处 待确认", "warn"))
    elif docs:
        badges.append(_badge("文档一致", "ok"))
    else:
        badges.append(_badge("未登记文档", "none"))

    if q:
        badges.append(_badge(f"已熔断（连续 {att.get('fails', 0)} 次修复失败）", "fail"))
    elif att.get("fails"):
        badges.append(_badge(f"修复尝试 {att['fails']}/3", "warn"))

    parts = [f'<div class="card{" q" if q else ""}" style="border-left-color:'
             f'{_COLORS[st]}"><h3>{_e(sid)}</h3>',
             f'<div class="badges">{"".join(badges)}</div>']
    if r.get("reason"):
        parts.append(f'<div class="reason">{_e(r["reason"])}</div>')
    if r.get("schema_acked"):
        a = r["schema_acked"]
        parts.append(f'<div class="kv" style="color:{_COLORS["ok"]}">已确认 '
                     f'{_e(a.get("at", "")[:10])}：{_e(a.get("note", ""))}</div>')
    parts.append(_fields_block("新增字段", r.get("schema_added") or []))
    parts.append(_fields_block("消失字段", r.get("schema_removed") or []))
    if r.get("schema_probe_error"):
        parts.append(f'<div class="kv">探针错误：{_e(r["schema_probe_error"])}</div>')

    meta = []
    if r.get("http_status"):
        meta.append(f"HTTP {r['http_status']}")
    if r.get("last_modified"):
        meta.append(f"上游更新 {_e(r['last_modified'])}")
    if r.get("checked_at"):
        meta.append(f"探测 {_e(r['checked_at'])}")
    if meta:
        parts.append(f'<div class="kv">{" · ".join(meta)}</div>')
    parts.append(f'<div class="kv">受影响 dataset：'
                 f'{_e("、".join(consumers)) or "（无）"}</div>')

    if docs:
        changed = {d["url"]: d for d in (r.get("docs_changed") or [])}
        rows = []
        for u in docs:
            idx = docs_idx.get(u) or {}       # 文档状态按 URL 存（family 常共用一份）
            when = (idx.get("checked_at") or "")[:10]
            if u in changed:
                d = changed[u]
                rows.append(f'<div><a href="{_e(u)}" style="color:{_COLORS["warn"]}">'
                            f'{_e(u)}</a> <span class="muted">已变更 '
                            f'{d.get("lines_changed", 0)} 行 · diff: '
                            f'<code>{_e(d.get("diff_path", ""))}</code></span></div>')
            else:
                err = f'（抓取失败：{_e(idx.get("error"))}）' if idx.get("error") else ""
                rows.append(f'<div><a href="{_e(u)}">{_e(u)}</a> '
                            f'<span class="muted">核对 {_e(when)}{err}</span></div>')
        parts.append(f'<div class="kv">技术文档：{"".join(rows)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _next_run(d: dict, tasks: dict) -> tuple[dt.datetime | None, str]:
    """这张表下次什么时候会被跑。(排序键, 展示文本)

    优先信 schtasks 报的时间（那是系统的实际安排），任务还没注册时按 cron 推算，
    手动维护的返回 None —— 排序时沉到最后。
    """
    from .schedule import next_run_from_cron, parse_task_time, task_name

    cron = d.get("schedule") or ""
    runner = d.get("runner", "family") if cron else "manual"
    who = {"family": f"dw-family-{d.get('family', '')}",
           "own": task_name(d["dataset"])}.get(runner, "")
    task = tasks.get(who) or {}
    when = parse_task_time(task.get("next_run", ""))
    if when:
        return when, when.strftime("%m-%d %H:%M")
    if runner == "manual":
        return None, "—"
    when = next_run_from_cron(cron)
    if when:                                   # 任务没注册上，只是「合约说该这时候跑」
        return when, when.strftime("%m-%d %H:%M") + "?"
    return None, "—"


_RUNNER_LABEL = {"family": "跟族定时", "own": "独立定时", "manual": "手动"}


def _plan_cell(d: dict, tasks: dict, live: bool) -> str:
    """新鲜度表的「计划」列。只读模式给一行说明，控制台模式给控件。"""
    from .schedule import parse_cron, task_name

    ds = d["dataset"]
    cron = d.get("schedule") or ""
    # 没排 cron 就没人会定时跑它，不管 runner 写的是什么 —— 显示按实际来，别骗人
    runner = d.get("runner", "family") if cron else "manual"
    try:
        _, hhmm = parse_cron(cron) if cron else (None, "")
    except ValueError:
        hhmm = ""                              # cron 太复杂，转不成 HH:MM
    who = {"family": f"dw-family-{d.get('family', '')}",
           "own": task_name(ds)}.get(runner, "")
    task = tasks.get(who) or {}
    tip = (f"{_RUNNER_LABEL.get(runner, runner)}"
           + (f" · {cron}" if cron else "")
           + (f" · {who}" if who else ""))
    if not live:
        return f'<span class="muted">{_e(tip)}</span>'
    opts = "".join(
        f'<option value="{k}"{" selected" if k == runner else ""}>{v}</option>'
        for k, v in _RUNNER_LABEL.items())
    return (
        f'<div class="plan" data-ds="{_e(ds)}">'
        f'<select class="runner">{opts}</select>'
        f'<input class="time" type="time" value="{_e(hhmm or "15:00")}">'
        f'<button class="save">保存计划</button>'
        f'<button class="run">立即运行</button>'
        f'<div class="jobline muted"></div>'
        f'<div class="muted" style="font-size:11.5px">{_e(tip)}</div></div>')


_LIVE_JS = """
const TOKEN = "__TOKEN__";
async function api(path, body) {
  const r = await fetch(path, {method: "POST",
    headers: {"Content-Type": "application/json", "X-DW-Token": TOKEN},
    body: JSON.stringify(body)});
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
function hhmmToCron(t) { const [h, m] = t.split(":"); return `${+m} ${+h} * * *`; }
document.querySelectorAll(".plan").forEach(el => {
  const ds = el.dataset.ds, line = el.querySelector(".jobline");
  el.querySelector(".save").onclick = async () => {
    const runner = el.querySelector(".runner").value;
    const payload = {dataset: ds, runner: runner,
                     schedule: runner === "manual" ? "" : hhmmToCron(el.querySelector(".time").value)};
    try {
      const dry = await api("/api/sla", {...payload, dry_run: true});
      if (!confirm("将执行：\\n\\n" + dry.plan.join("\\n") + "\\n\\n确定？")) return;
      line.textContent = "执行中…";
      const done = await api("/api/sla", {...payload, dry_run: false});
      line.textContent = "已保存：" + (done.contract_changed || []).join("；")
        + (done.task && !done.task.ok ? "（任务命令返回非 0，见控制台输出）" : "");
    } catch (e) { line.textContent = "失败：" + e.message; }
  };
  el.querySelector(".run").onclick = async () => {
    if (!confirm(`现在后台跑一次 dw run ${ds}？（大表可能要几小时）`)) return;
    try {
      const j = await api("/api/run", {dataset: ds, stage: "all"});
      line.textContent = `已启动，日志 ${j.log}`;
      const poll = setInterval(async () => {
        const s = await (await fetch("/api/jobs")).json();
        const me = s.jobs.find(x => x.id === j.id);
        if (!me) return;
        line.textContent = `${me.status} · 日志 ${me.log}`;
        if (me.status !== "running") clearInterval(poll);
      }, 5000);
    } catch (e) { line.textContent = "失败：" + e.message; }
  };
});
"""


def _timeline(history: list[str], sources: list[str], days: int = 7) -> str:
    """近 N 天每个源的状态点阵。history.jsonl 只读末尾，别整文件吞。"""
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    per: dict[str, list[tuple[str, str]]] = {s: [] for s in sources}
    for line in history:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid, at = r.get("source"), r.get("checked_at", "")
        if sid not in per:
            continue
        try:
            if dt.datetime.fromisoformat(at) < cutoff:
                continue
        except ValueError:
            continue
        per[sid].append((at, r.get("status", "ok")))
    rows = []
    for sid in sources:
        dots = "".join(
            f'<span class="dot" style="background:{_COLORS.get(st, "#888")}" '
            f'title="{_e(at)} {_e(st)}"></span>' for at, st in per[sid][-60:])
        rows.append(f"<tr><td class='tl'>{_e(sid)}</td><td>{dots or '<span class=muted>无记录</span>'}"
                    f"</td></tr>")
    return (f"<table><tr><th style='width:230px'>源</th>"
            f"<th>近 {days} 天每轮探测</th></tr>{''.join(rows)}</table>")


def render(st: dict, live_token: str = "") -> str:
    """把一份 state 渲染成 HTML。live_token 非空 = 控制台模式，计划列出现按钮。"""
    report = st.get("report") or {}
    freshness = st.get("freshness") or []
    attempts = st.get("attempts") or {}
    history = st.get("history") or []
    digest_ = st.get("digest") or {}
    consumers = st.get("consumers") or {}
    docs_idx = st.get("docs") or {}
    tasks = st.get("tasks") or {}
    results = report.get("results", [])
    s = report.get("summary", {})
    stale = [d for d in freshness if d["status"] != "ok"]
    worst = ("fail" if s.get("fail") or any(d["status"] == "fail" for d in stale)
             else "warn" if s.get("warn") or stale else "ok")
    pending = digest_.get("pending_ack", [])

    cards = "".join(_source_card(r, consumers.get(r["source"], []),
                                 attempts.get(r["source"]) or {}, docs_idx)
                    for r in results)

    pend_html = ""
    if pending:
        items = []
        for x in pending:
            if x["kind"] == "schema":
                what = (f"schema 变更：新增 {len(x.get('schema_added') or [])}、"
                        f"消失 {len(x.get('schema_removed') or [])} 个字段")
                cmd = f"dw ack {x['source']} --schema --note \"...\""
            else:
                what = (f"技术文档变更 {x.get('lines_changed', 0)} 行 —— "
                        f"{x.get('diff_path', '')}")
                cmd = f"dw ack {x['source']} --docs {x.get('url', '')} --note \"...\""
            items.append(f'<div class="pend"><b>{_e(x["source"])}</b> · {_e(what)}'
                         f'<div class="kv">判完记一笔：<code>{_e(cmd)}</code></div></div>')
        pend_html = (f"<h2>待确认（{len(pending)}）—— 要判断，不一定要修</h2>"
                     + "".join(items))

    # 按「下次什么时候会被跑」排序：马上要跑的排最前，手动维护的沉到最后
    _FAR = dt.datetime.max
    ordered = sorted(((d, *_next_run(d, tasks)) for d in freshness),
                     key=lambda t: (t[1] or _FAR, t[0]["dataset"]))
    frows = "".join(
        f'<tr><td class="tl">{_e(d["dataset"])}</td>'
        f'<td><span class="dot" style="background:{_COLORS[d["status"]]}"></span>'
        f'{_LABEL[d["status"]]}</td>'
        f'<td class="tl">{_e(nxt_text)}</td>'
        f'<td>{_e(d.get("last_run") or "—")}</td><td>{_e(d["freshness"])}</td>'
        f'<td>{_plan_cell(d, tasks, bool(live_token))}</td>'
        f'<td class="muted">{_e(d.get("reason") or "")}</td></tr>'
        for d, _nxt, nxt_text in ordered)

    data = json.dumps({"report": report, "freshness": freshness,
                       "attempts": attempts}, ensure_ascii=False)
    live_js = _LIVE_JS.replace("__TOKEN__", live_token) if live_token else ""
    head_note = ("本页由 <code>dw panel</code> 提供，计划列可直接开关/改时间/立即运行"
                 if live_token else
                 "本页由 <code>dw health --html</code> 生成，数据内联，可离线打开"
                 "（要在页面上改计划，跑 <code>dw panel --open</code>）")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据仓库健康面板</title><style>{_CSS}</style></head><body>
<h1>数据仓库健康面板</h1>
<div class="muted" style="margin-bottom:14px">
  外部源探测 {_e(report.get("checked_at", "—"))} · {head_note}</div>
<div class="sum" style="border-left-color:{_COLORS[worst]}">
  <span><b style="color:{_COLORS['ok']}">{s.get('ok', 0)}</b>正常</span>
  <span><b style="color:{_COLORS['warn']}">{s.get('warn', 0)}</b>警告</span>
  <span><b style="color:{_COLORS['fail']}">{s.get('fail', 0)}</b>故障</span>
  <span class="muted">/ 共 {s.get('total', 0)} 个外部源</span>
  <span style="margin-left:auto"><b style="color:{
      _COLORS['fail'] if stale else _COLORS['ok']}">{len(stale)}</b>个 dataset 未如期更新</span>
  <span><b style="color:{_COLORS['warn'] if pending else _COLORS['ok']}"
    >{len(pending)}</b>项待确认</span>
</div>
{pend_html}
<h2>外部数据源</h2>
<div class="grid">{cards}</div>
<h2>dataset 新鲜度（curated 表有没有如期产出）</h2>
<table><tr><th>dataset</th><th>状态</th><th>下次运行 ↑</th><th>最后成功</th><th>SLA</th><th>计划</th><th></th></tr>
{frows}</table>
<h2>探测时间线</h2>
{_timeline(history, [r["source"] for r in results])}
<script type="application/json" id="dw-data">{data}</script>
<script>{live_js}</script>
</body></html>
"""


def state(p: Paths | None = None) -> dict:
    """面板要的全部数据。静态渲染和 dw panel 的 /api/state 共用这一份。"""
    from . import graph as G
    from .external import _load_state, load_report, load_sources
    from .health import attempts_load, dataset_freshness, digest, tail_lines
    from .schedule import list_tasks

    p = p or paths()
    rep = load_report(p)
    sources = load_sources(p)
    g = G.load(p)
    for r in rep.get("results", []):
        src = sources.get(r["source"]) or {}
        r["_docs"] = src.get("docs") or []
        r["_probe_declared"] = "schema_probe" in src
    return {
        "report": rep,
        "freshness": dataset_freshness(p),
        "attempts": attempts_load(p),
        "history": tail_lines(p.health_dir / "history.jsonl", n=800),
        "digest": digest(p),
        "consumers": {r["source"]: G.external_consumers(g, r["source"])
                      for r in rep.get("results", [])},
        "docs": _load_state("docs.json", p),
        "tasks": list_tasks(),
    }


def build_html(p: Paths | None = None, live_token: str = "") -> str:
    return render(state(p), live_token)


def build(p: Paths | None = None) -> str:
    """渲染只读面板并写出 dashboard.html，返回文件路径。"""
    p = p or paths()
    p.health_dir.mkdir(parents=True, exist_ok=True)
    f = p.health_dir / "dashboard.html"
    f.write_text(build_html(p), encoding="utf-8")
    return str(f)
