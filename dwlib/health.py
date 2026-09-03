"""本仓这一侧的健康状态：dataset 新鲜度、修复台账（熔断）、变更确认（ack）、坏源摘要。

分工：`external.py` 只管「探外部」（HEAD / schema 探针 / 文档抓取），这里管「本地这一侧」——
数据有没有如期更新、某个源被修过几次、schema/文档的变更有没有人确认过。

所有状态文件一律写 `.health/`，**绝不回写 external_sources.yaml**
（那份是人写人读的真源，PyYAML 往返一次注释就全没了，见 external.save_sources 的注释）。

    .health/fix_attempts.json   {sid: {fails, quarantined, log: [...]}}
    .health/ack.json            {sid: {schema: {...}}, "_docs": {url: {...}}}

⚠ schema 的确认按**源**记（每个源的结构是自己的），文档的确认按 **URL** 记 ——
一个 family 常共用一份文档（polygon 五个源共用 Massive changelog），
判断一次就够了，不该逼着人对同一件事确认五遍。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .config import Paths, paths
from .external import load_report, load_sources, parse_duration

# 连续失败几次就熔断。fix-source skill 见到 quarantined 直接停手交人工，
# 避免同一个源被反复瞎试（死锁）。
ATTEMPT_LIMIT = 3


# ---------------- 小工具 ----------------

def _read_json(f: Path, default: Any) -> Any:
    if not f.is_file():
        return default
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(f: Path, obj: Any) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def tail_lines(f: Path, n: int = 400, max_bytes: int = 512_000) -> list[str]:
    """读文件末尾若干行。history.jsonl 只增不减，整文件读迟早会涨爆上下文。"""
    if not f.is_file():
        return []
    size = f.stat().st_size
    with f.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()                      # 丢掉半行
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]


# ---------------- 修复台账（熔断） ----------------

def attempts_load(p: Paths | None = None) -> dict[str, dict]:
    return _read_json((p or paths()).health_dir / "fix_attempts.json", {})


def attempts_record(source_id: str, outcome: str, note: str = "",
                    p: Paths | None = None) -> dict:
    """记一次修复尝试。outcome ∈ {ok, fail}；连续第 ATTEMPT_LIMIT 次 fail 触发熔断。"""
    if outcome not in ("ok", "fail"):
        raise ValueError("outcome 只能是 ok / fail")
    p = p or paths()
    all_ = attempts_load(p)
    rec = all_.setdefault(source_id, {"fails": 0, "quarantined": False, "log": []})
    rec["log"] = (rec.get("log") or [])[-19:] + [
        {"at": _now(), "outcome": outcome, "note": note}]
    if outcome == "ok":
        rec["fails"] = 0
        rec["quarantined"] = False
    else:
        rec["fails"] = int(rec.get("fails", 0)) + 1
        rec["quarantined"] = rec["fails"] >= ATTEMPT_LIMIT
    _write_json(p.health_dir / "fix_attempts.json", all_)
    return rec


# ---------------- dataset 运行台账（熔断） ----------------
# 跟上面的 fix_attempts 是同一套"连续失败 N 次自动熔断"范式，但保护的是不同
# 的事：fix_attempts 记的是"修外部源的尝试"，这里记的是"dataset 某个 stage
# 本身（ingest/transform/backfill）反复失败"——比如 backfill.py 撞上一个真实
# bug、游标卡在同一个窗口不再前进，外部循环脚本或每天一次的定时任务会对着
# 同一个坏窗口静默重试到天荒地老。两者报警渠道、人工处理流程不一样（一个走
# fix-source skill，一个是"看 dw run-reset 之前先去看代码"），故意不共用
# 同一份 fix_attempts.json，避免 `dw fixlog` 的语义变模糊。

RUN_ATTEMPT_LIMIT = 3   # 跟 ATTEMPT_LIMIT 用同一个阈值，保持两套熔断心智一致


def run_attempts_load(p: Paths | None = None) -> dict[str, dict]:
    return _read_json((p or paths()).health_dir / "run_attempts.json", {})


def run_attempts_record(dataset: str, stage: str, outcome: str, note: str = "",
                        p: Paths | None = None) -> dict:
    """记一次 dataset 某个 stage 的运行结果。键是 f"{dataset}:{stage}"，
    outcome ∈ {ok, fail}；连续第 RUN_ATTEMPT_LIMIT 次 fail 触发熔断
    （quarantined=True）——`dwlib.runner.run_stage` 见到熔断标记会直接跳过、
    不再调用该 stage 的 main()，需要 `dw run-reset` 人工清零才会再尝试。
    """
    if outcome not in ("ok", "fail"):
        raise ValueError("outcome 只能是 ok / fail")
    p = p or paths()
    key = f"{dataset}:{stage}"
    all_ = run_attempts_load(p)
    rec = all_.setdefault(key, {"fails": 0, "quarantined": False, "log": []})
    rec["log"] = (rec.get("log") or [])[-19:] + [
        {"at": _now(), "outcome": outcome, "note": note}]
    if outcome == "ok":
        rec["fails"] = 0
        rec["quarantined"] = False
    else:
        rec["fails"] = int(rec.get("fails", 0)) + 1
        rec["quarantined"] = rec["fails"] >= RUN_ATTEMPT_LIMIT
    _write_json(p.health_dir / "run_attempts.json", all_)
    return rec


def run_attempts_clear(dataset: str, stage: str | None = None,
                       p: Paths | None = None) -> list[str]:
    """人工诊断修好之后手动清零，不自动恢复——保持跟 fix_attempts 一样的
    "遇到熔断必须人工介入"心智，不能因为随手跑一次成功了就悄悄免责。
    不传 stage 就清掉这个 dataset 名下所有阶段的熔断记录。返回清掉的键列表。
    """
    p = p or paths()
    all_ = run_attempts_load(p)
    prefix = f"{dataset}:{stage}" if stage else f"{dataset}:"
    cleared = [k for k in all_ if k == prefix or (stage is None and k.startswith(prefix))]
    for k in cleared:
        all_[k] = {"fails": 0, "quarantined": False,
                   "log": (all_[k].get("log") or [])[-19:] + [
                       {"at": _now(), "outcome": "reset", "note": "人工清零"}]}
    _write_json(p.health_dir / "run_attempts.json", all_)
    return cleared


def attempts_clear(source_id: str, p: Paths | None = None) -> None:
    """人工解除熔断：清零计数，但保留历次尝试的日志（留痕）。"""
    p = p or paths()
    all_ = attempts_load(p)
    rec = all_.get(source_id)
    if not rec:
        return
    rec["fails"] = 0
    rec["quarantined"] = False
    rec["log"] = (rec.get("log") or []) + [
        {"at": _now(), "outcome": "clear", "note": "人工解除熔断"}]
    _write_json(p.health_dir / "fix_attempts.json", all_)


# ---------------- 变更确认（ack） ----------------
#
# schema/文档变更是「提示型 warn」：上游改完就一直是新样子，不像 404 那样自己会好。
# 不给一个「我看过了」的出口，面板会永远挂着黄，久了就没人看了。
# 确认记的是**当时那个 hash**，上游再变一次就又对不上、重新报 warn ——
# 所以这不是永久静音。真想彻底不管某个源，把它的 schema_probe / docs 删掉，
# 面板会诚实地标灰「未监控」，而不是绿着骗人。

def ack_load(p: Paths | None = None) -> dict[str, dict]:
    return _read_json((p or paths()).health_dir / "ack.json", {})


def ack_record(source_id: str, kind: str, hash_: str, note: str,
               url: str | None = None, p: Paths | None = None) -> dict:
    """确认一次变更。kind ∈ {schema, docs}；docs 需给 url。note 必填。"""
    if kind not in ("schema", "docs"):
        raise ValueError("kind 只能是 schema / docs")
    if not (note or "").strip():
        raise ValueError("确认必须写 --note：为什么可以放过 / 怎么修的（这就是留痕）")
    p = p or paths()
    all_ = ack_load(p)
    entry = {"hash": hash_, "at": _now(), "note": note.strip()}
    if kind == "schema":
        all_.setdefault(source_id, {})["schema"] = entry
    else:
        if not url:
            raise ValueError("确认文档变更要指明是哪个 url")
        # 文档按 URL 记（共用文档只需判断一次），但留下是谁判的，便于追溯
        all_.setdefault("_docs", {})[url] = {**entry, "by_source": source_id}
    _write_json(p.health_dir / "ack.json", all_)
    return entry


def ack_for(ack: dict, source_id: str, kind: str, url: str | None = None) -> dict:
    if kind == "schema":
        return (ack.get(source_id) or {}).get("schema") or {}
    return (ack.get("_docs") or {}).get(url or "") or {}


# ---------------- dataset 新鲜度：「有没有如期更新」 ----------------

def dataset_freshness(p: Paths | None = None) -> list[dict]:
    """本地 curated 表有没有按 SLA 更新。

    为什么不能只靠外部源的 freshness：`polygon_grouped_daily` 的 note 已经写明
    本源没有 last-modified 头，HEAD 探测拿不到时间。而 run_state.json 的 last_run
    只有 write_curated 成功时才会写（见 io._write_run_state），所以它天然就是
    「最后一次成功产出」—— 这才是「如期更新」的可靠信号。
    """
    from .contract import load_all

    p = p or paths()
    now = dt.datetime.now()
    out: list[dict] = []
    for name, c in sorted(load_all(p).items()):
        if c.status == "deprecated":
            continue
        f = p.dataset_dir(name) / "_meta" / "run_state.json"
        state = _read_json(f, {})
        last = state.get("last_run")
        row: dict[str, Any] = {
            "dataset": name,
            "last_run": last,
            "freshness": c.sla.freshness,
            "schedule": c.sla.schedule,
            "runner": c.sla.runner,
            "family": c.family or (name.split("__")[0] if "__" in name else ""),
            "rows": state.get("rows"),
            "rows_added": state.get("rows_added"),
            "sources": c.upstream_externals,
            "status": "ok",
            "reason": "",
            "overdue_hours": 0.0,
        }
        if not last:
            row.update(status="warn", reason="从未成功跑过（无 run_state.json）")
            out.append(row)
            continue
        try:
            age = now - dt.datetime.fromisoformat(last)
            limit = parse_duration(c.sla.freshness)
        except ValueError:
            row.update(status="warn", reason=f"无法解析 last_run/freshness: {last}")
            out.append(row)
            continue
        row["age_hours"] = round(age.total_seconds() / 3600, 1)
        if age > limit:
            over = (age - limit).total_seconds() / 3600
            row["overdue_hours"] = round(over, 1)
            # 手动维护的没有「该跑没跑」一说，超了也只是提醒该跑一次，最多 warn
            manual = c.sla.runner == "manual" or not c.sla.schedule
            row.update(status="warn" if manual else "fail",
                       reason=f"逾期未更新 {over / 24:.1f} 天"
                              f"（SLA {c.sla.freshness}，最后成功 {last}）")
        # 刻意不做「接近 SLA 上限」的预警：日更的表在下一次跑之前必然接近上限，
        # 那会让面板天天挂着一排黄的，看多了就没人看了。超了才是问题。
        out.append(row)
    return out


# ---------------- 给 /fix-source 的精简摘要 ----------------

def _first_bad_at(source_id: str, p: Paths) -> str:
    """从 history.jsonl 倒着找这一轮连续故障是从什么时候开始的。"""
    first = ""
    for line in reversed(tail_lines(p.health_dir / "history.jsonl")):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("source") != source_id:
            continue
        if r.get("status") == "ok":
            break
        first = r.get("checked_at") or first
    return first


def digest(p: Paths | None = None) -> dict:
    """`dw health --broken --json` 的输出：只含要人管的东西。

    这是 /fix-source 的唯一入口数据 —— 一条命令换掉「cat report.json +
    tail history.jsonl + dw deps」三连，也省得把整份 external_sources.yaml
    读进上下文。
    """
    from . import graph as G

    p = p or paths()
    rep = load_report(p)
    sources = load_sources(p)
    att = attempts_load(p)
    ack = ack_load(p)
    g = G.load(p)

    broken: list[dict] = []
    pending: list[dict] = []
    for r in rep.get("results", []):
        sid = r["source"]
        consumers = G.external_consumers(g, sid)
        a = att.get(sid) or {}
        base = {
            "source": sid,
            "status": r.get("status"),
            "reason": r.get("reason", ""),
            "consumers": consumers,
            "ingest_files": [p.rel(p.dataset_dir(d) / "ingest.py") for d in consumers
                             if (p.dataset_dir(d) / "ingest.py").is_file()],
            "attempts": int(a.get("fails", 0)),
            "quarantined": bool(a.get("quarantined")),
        }
        # schema / 文档变更是「待确认」，不是「故障」—— 前者要判断，后者要修，别混在一起
        sch_ack = ack_for(ack, sid, "schema")
        if r.get("schema_changed") and sch_ack.get("hash") != r.get("schema_hash"):
            pending.append({**base, "kind": "schema",
                            "schema_added": r.get("schema_added", []),
                            "schema_removed": r.get("schema_removed", []),
                            "schema_hash": r.get("schema_hash")})
        for d in r.get("docs_changed") or []:
            if ack_for(ack, sid, "docs", d.get("url")).get("hash") == d.get("hash"):
                continue
            pending.append({**base, "kind": "docs", **d})
        # 被熔断的源即便探测是绿的也要列出来 —— 熔断说明「修过三次没修好」，
        # 探测绿只是说 URL 还能连上，ingest 那一侧可能仍然是坏的。
        if r.get("status") == "ok" and not base["quarantined"]:
            continue
        broken.append({**base, "since": _first_bad_at(sid, p),
                       "url": (sources.get(sid) or {}).get("url", ""),
                       "docs": (sources.get(sid) or {}).get("docs") or [],
                       "log": (a.get("log") or [])[-3:]})

    stale = [d for d in dataset_freshness(p) if d["status"] != "ok"]
    return {
        "checked_at": rep.get("checked_at", ""),
        "summary": rep.get("summary", {}),
        "broken": broken,
        "pending_ack": pending,
        "stale_datasets": stale,
        "next": "先看 quarantined：为 true 就停手交人工。修完记 `dw fixlog <sid> ok`，"
                "变更判完记 `dw ack <sid> --schema|--docs <url> --note ...`。",
    }
