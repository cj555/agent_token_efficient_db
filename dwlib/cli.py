"""`dw` 命令行 —— 让 Claude「跑命令」而不是「读文件」。

每条命令的输出都刻意保持紧凑（几行到几十行），并支持 --json 供程序消费。
用 argparse 而非 typer/click：零额外依赖、启动快。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import paths


# ---------------- 输出helpers ----------------

def out(obj, as_json: bool, text: str = "") -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    else:
        print(text if text else obj)


def _size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}TB"


# ---------------- 各子命令 ----------------

def cmd_index(a) -> int:
    from .registry import reindex
    from .scaffold import regenerate_all
    p = paths()
    regen = regenerate_all(p)
    st = reindex(p, scan=not a.no_scan)
    lines = [f"datasets={st['datasets']} edges={st['edges']} refs={st['refs']}"]
    if regen:
        lines.append(f"regenerated: {len(regen)} 个生成物")
    if st["dangling"]:
        lines.append(f"[warn] 悬空上游引用: {', '.join(st['dangling'])}")
    lines.append(f"已更新 {p.rel(p.index_md)} / graph.json / registry.json")
    out({**st, "regenerated": regen}, a.json, "\n".join(lines))
    return 0


def cmd_ls(a) -> int:
    from .contract import load_all
    p = paths()
    rows = []
    for name, c in sorted(load_all(p).items()):
        fam = c.family or (name.split("__")[0] if "__" in name else "")
        if a.family and fam != a.family:
            continue
        if a.domain and c.domain != a.domain:
            continue
        if a.tag and a.tag not in c.tags:
            continue
        if a.status and c.status != a.status:
            continue
        rows.append({
            "name": name, "domain": c.domain, "family": fam, "status": c.status,
            "grain": c.grain, "columns": len(c.columns),
            "upstream": c.upstream_datasets + [f"@{e}" for e in c.upstream_externals],
            "purpose": c.purpose.strip().splitlines()[0][:70] if c.purpose.strip() else "",
        })
    text = "\n".join(
        f"{r['name']:<28} {r['status']:<10} grain={'+'.join(r['grain']) or '-':<20} "
        f"cols={r['columns']:<4} up={','.join(r['upstream']) or '-':<28} {r['purpose']}"
        for r in rows
    ) or "（无 dataset。用 /create-dataset 创建，或 dw new <name>）"
    out(rows, a.json, text + f"\n共 {len(rows)} 个")
    return 0


def cmd_search(a) -> int:
    """在 purpose/tags/列名/名称上检索。找可复用的内部数据源，不必通读合约。"""
    from .contract import load_all
    p = paths()
    kws = [k.lower() for k in a.keywords]
    hits = []
    for name, c in load_all(p).items():
        hay_name = name.lower() + " " + (c.family or "").lower()
        hay_purpose = (c.purpose + " " + " ".join(c.tags) + " " + c.domain).lower()
        hay_cols = " ".join(c.column_names).lower()
        score = 0
        where = []
        for k in kws:
            if k in hay_name:
                score += 5
                where.append("name")
            if k in hay_purpose:
                score += 3
                where.append("purpose")
            if k in hay_cols:
                score += 1
                where.append("column")
        if score:
            hits.append({"name": name, "score": score, "matched": sorted(set(where)),
                         "purpose": c.purpose.strip().splitlines()[0][:80],
                         "columns": [cn for cn in c.column_names
                                     if any(k in cn.lower() for k in kws)][:6]})
    hits.sort(key=lambda h: -h["score"])
    hits = hits[: a.limit]
    text = "\n".join(
        f"{h['score']:>3}  {h['name']:<28} [{','.join(h['matched'])}] {h['purpose']}"
        + (f"  cols:{','.join(h['columns'])}" if h["columns"] else "")
        for h in hits
    ) or "无匹配（可以放心新建，不会重复造数据）"
    out(hits, a.json, text)
    return 0


def cmd_show(a) -> int:
    from .contract import load_contract
    from . import io as dwio
    p = paths()
    c = load_contract(a.dataset, p)
    fields = a.fields.split(",") if a.fields else ["meta", "schema", "upstream", "sla"]
    data, lines = {}, []

    if "meta" in fields or "all" in fields:
        data["meta"] = {"name": c.name, "version": c.version, "status": c.status,
                        "domain": c.domain, "family": c.family, "tags": c.tags,
                        "grain": c.grain, "partitions": c.partitions,
                        "owner": c.owner, "purpose": c.purpose.strip()}
        lines += [f"{c.name}  v{c.version}  {c.status}  domain={c.domain or '-'}",
                  f"grain: {'+'.join(c.grain) or '-'}   partitions: {c.partitions or '-'}",
                  f"purpose: {c.purpose.strip()}"]
    if "schema" in fields or "all" in fields:
        data["columns"] = [col.model_dump() for col in c.columns]
        lines.append(f"columns ({len(c.columns)}):")
        lines += [f"  {col.name:<28} {col.type:<28} "
                  f"{'NULL' if col.nullable else 'NOT NULL':<9}"
                  f"{'UNIQUE ' if col.unique else ''}{col.desc}"
                  for col in c.columns]
    if "upstream" in fields or "all" in fields:
        from . import graph as G
        g = G.load(p)
        consumers = g.get("nodes", {}).get(c.name, {}).get("consumers", [])
        data["upstream"] = [u.model_dump() for u in c.upstream]
        data["consumers"] = consumers
        lines.append("upstream: " + (", ".join(
            f"{u.ref}({u.kind})" for u in c.upstream) or "-"))
        lines.append("consumers: " + (", ".join(consumers) or "-"))
    if "sla" in fields or "all" in fields:
        data["sla"] = c.sla.model_dump()
        lines.append(f"sla: freshness={c.sla.freshness} schedule={c.sla.schedule or '手动'} "
                     f"stage={c.sla.stage}")
    if "quality" in fields or "all" in fields:
        data["quality"] = [q.model_dump(exclude_none=True) for q in c.quality]
        lines.append("quality: " + (", ".join(
            f"{q.rule}({q.column or ''})" for q in c.quality) or "-"))
    if "changelog" in fields or "all" in fields:
        data["changelog"] = [e.model_dump() for e in c.changelog]
        lines += [f"  {e.version} {e.date} [{e.kind}] {e.note}" for e in c.changelog[-5:]]
    if "state" in fields or "all" in fields:
        state = dwio.run_state(a.dataset, p)
        data["state"] = state
        lines.append(f"state: {state or '未运行'}  data_exists={dwio.exists(a.dataset, p)}")

    out(data, a.json, "\n".join(lines))
    return 0


def cmd_deps(a) -> int:
    from . import graph as G
    p = paths()
    g = G.load(p)
    direction = "up" if a.up else "down"
    items = G.closure(g, a.dataset, direction, a.depth)
    data = {"dataset": a.dataset, "direction": direction,
            "items": [{"name": n, "distance": d} for n, d in items]}
    label = "下游（谁依赖我）" if direction == "down" else "上游（我依赖谁）"
    text = f"{a.dataset} 的{label}：\n" + ("\n".join(
        f"  {'  ' * (d - 1)}└ {n}" for n, d in items) or "  （无）")
    if direction == "up":
        node = g["nodes"].get(a.dataset, {})
        ext = [e["from"] for e in g["edges"]
               if e["kind"] == "external" and e["to"] == a.dataset]
        if ext:
            data["external"] = ext
            text += f"\n  外部源: {', '.join(ext)}"
    out(data, a.json, text)
    return 0


def cmd_refs(a) -> int:
    from . import graph as G
    p = paths()
    g = G.load(p)
    refs = g.get("refs", {}).get(a.dataset, [])
    if a.column:
        refs = [r for r in refs if a.column in r["text"]]
    text = "\n".join(f"{r['file']}:{r['line']}  {r['text']}" for r in refs) \
        or "（无代码引用）"
    out(refs, a.json, text + f"\n共 {len(refs)} 处")
    return 0


def cmd_impact(a) -> int:
    """变更影响面：下游 dataset + 精确的 文件:行 清单。change-contract 的核心依据。"""
    from . import graph as G
    from .contract import load_all
    p = paths()
    g = G.load(p)
    contracts = load_all(p)

    downs = G.closure(g, a.dataset, "down")
    all_refs = g.get("refs", {}).get(a.dataset, [])
    # 指定列时：既给出直接提到该列的行，也保留「引用了本 dataset」的行——
    # 后者多半是 dw.load(...) 之后再用到该列，同样需要人过目。
    refs = [r for r in all_refs if a.column in r["text"]] if a.column else all_refs
    indirect = [r for r in all_refs if r not in refs] if a.column else []

    # 下游合约里是否也声明了同名列（列改名/删列时必须一起改）
    col_hits = []
    if a.column:
        for n, _d in downs:
            c = contracts.get(n)
            if c and a.column in c.column_names:
                col_hits.append(n)

    data = {"dataset": a.dataset, "column": a.column,
            "downstream": [n for n, _ in downs],
            "downstream_with_same_column": col_hits,
            "code_refs": refs,
            "indirect_refs": indirect,
            "files_to_edit": sorted({r["file"] for r in refs + indirect}
                                    | {f"datasets/{n}/transform.py" for n, _ in downs}
                                    | {f"datasets/{n}/contract.yaml" for n in col_hits})}
    lines = [f"变更 {a.dataset}" + (f".{a.column}" if a.column else "") + " 的影响面：",
             f"  下游 dataset ({len(downs)}): " + (", ".join(n for n, _ in downs) or "无")]
    if col_hits:
        lines.append(f"  下游合约含同名列: {', '.join(col_hits)}  ← 必须同步改合约")
    lines.append(f"  直接引用该列 ({len(refs)}):")
    lines += [f"    {r['file']}:{r['line']}  {r['text']}" for r in refs] or ["    无"]
    if indirect:
        lines.append(f"  引用了本 dataset（需人工确认是否用到该列）({len(indirect)}):")
        lines += [f"    {r['file']}:{r['line']}  {r['text']}" for r in indirect]
    lines.append(f"  建议编辑的文件 ({len(data['files_to_edit'])}):")
    lines += [f"    {f}" for f in data["files_to_edit"]]
    out(data, a.json, "\n".join(lines))
    return 0


def cmd_validate(a) -> int:
    from .contract import list_datasets
    from .quality import check
    p = paths()
    names = [a.dataset] if a.dataset else list_datasets(p)
    results, bad = [], 0
    for n in names:
        try:
            r = check(n, p)
        except Exception as e:
            r = {"dataset": n, "ok": False, "rows": 0,
                 "issues": [{"level": "error", "code": "check_failed", "msg": str(e)}]}
        results.append(r)
        if r["ok"] is False:
            bad += 1
    lines = []
    for r in results:
        mark = {True: "OK  ", False: "FAIL", None: "SKIP"}[r["ok"]]
        lines.append(f"{mark} {r['dataset']:<28} rows={r['rows']}")
        lines += [f"       [{i['level']}] {i['code']}: {i['msg']}" for i in r["issues"]]
    lines.append(f"—— {len(results)} 个检查，{bad} 个失败")
    out(results, a.json, "\n".join(lines))
    return 1 if bad else 0


def cmd_new(a) -> int:
    from .scaffold import FAMILY_SPEC_EXAMPLE, new_dataset, new_family
    p = paths()
    if a.example_spec:
        print(FAMILY_SPEC_EXAMPLE)
        return 0
    if a.family:
        created = new_family(Path(a.family), p, force=a.force)
        text = "\n".join(f"{name}: {len(files)} 个文件\n  " + "\n  ".join(files)
                         for name, files in created.items())
        out(created, a.json, text + "\n下一步：按拓扑序填 ingest/transform，然后 dw index")
        return 0
    if not a.dataset:
        print("需要 <dataset> 或 --family <spec.yaml>（`dw new --example-spec` 看样例）")
        return 2
    files = new_dataset(
        a.dataset, p, purpose=a.purpose or "", domain=a.domain or "",
        owner=a.owner or "", source_id=a.source or "",
        has_ingest=None if a.source else False,
        has_transform=not a.no_transform,
        upstream_datasets=a.upstream.split(",") if a.upstream else None,
        force=a.force,
    )
    out(files, a.json, "\n".join(files) + f"\n共 {len(files)} 个文件；下一步：填 contract.yaml 的 "
                                          f"columns/grain，再 dw index")
    return 0


def cmd_infer(a) -> int:
    from .adopt import infer_report, write_inferred_contract
    p = paths()
    if a.write:
        f = write_inferred_contract(a.write, Path(a.path), p, merge=not a.overwrite)
        print(f"已写入 {f}（保留了人写字段；请补 purpose/grain/quality）")
        return 0
    rep = infer_report(Path(a.path), a.name or "")
    head = f"{rep['name']}  {rep['n_columns']} 列  grain猜测={rep['grain_guess'] or '?'}"
    body = "\n".join(f"  {n:<28} {t:<28} {nl}" for n, t, nl in rep["columns"])
    out(rep, a.json, head + "\n" + body)
    return 0


def cmd_adopt(a) -> int:
    from .adopt import adopt
    p = paths()
    r = adopt(a.dataset, Path(a.path), p, mode=a.mode, strict=not a.no_strict)
    if not r["ok"]:
        text = "拒绝纳管：\n  " + "\n  ".join(r["issues"]) + f"\n{r['hint']}"
        out(r, a.json, text)
        return 1
    text = f"已纳管 {r['files']} 个文件 / {r['rows']} 行 → {r['path']}"
    if r["issues"]:
        text += "\n  [warn] " + "; ".join(r["issues"])
    out(r, a.json, text)
    return 0


def cmd_run(a) -> int:
    from .contract import list_datasets
    from .runner import STAGES, run, run_many, select_family
    p = paths()
    stages = tuple(a.stage.split(",")) if a.stage else STAGES

    if a.all:
        names = list_datasets(p)
    elif a.family:
        names = select_family(a.family, p)
    elif a.dataset:
        names = [a.dataset]
    else:
        print("需要 <dataset> 或 --family <名> 或 --all")
        return 2

    results = run_many(names, stages, p) if len(names) > 1 else [run(names[0], stages, p)]
    lines = []
    failed = 0
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        if not r["ok"]:
            failed += 1
        lines.append(f"{mark} {r['dataset']}")
        for s in r["stages"]:
            tag = "skip" if s.get("skipped") else ("ok" if s["ok"] else "FAIL")
            lines.append(f"       {s['stage']:<10} {tag:<5} {s['secs']}s  {s['detail']}")
    if failed:
        lines.append(f"—— 中断于失败（{failed} 个）。上游失败即停，避免产生半成品。")
    out(results, a.json, "\n".join(lines))
    return 1 if failed else 0


def cmd_rm(a) -> int:
    from .remove import apply, plan
    p = paths()
    if a.apply:
        r = apply(a.dataset, p, force=a.force)
        if not r["ok"]:
            text = (f"拒绝删除：{r['reason']}\n  下游: {', '.join(r['downstream']) or '无'}\n"
                    f"  代码引用: {len(r['code_refs'])} 处\n{r['hint']}")
            out(r, a.json, text)
            return 1
        text = ("已删除:\n  " + "\n  ".join(r["removed"]) +
                f"\n卸载定时任务: {', '.join(r['unscheduled']) or '无'}"
                f"\n释放 {_size(r['freed_bytes'])}；已重建索引")
        out(r, a.json, text)
        return 0

    pl = plan(a.dataset, p)
    lines = [f"dry-run: 删除 {a.dataset} 将影响 ——"]
    lines.append("  待删对象:")
    lines += [f"    [{t['kind']}] {t['path']}  {_size(t['bytes'])}" for t in pl["targets"]] \
        or ["    无"]
    lines.append(f"  定时任务: {', '.join(pl['scheduled_tasks']) or '无'}")
    lines.append(f"  下游 dataset: {', '.join(pl['downstream']) or '无'}")
    lines.append(f"  代码引用点: {len(pl['code_refs'])} 处")
    lines += [f"    {r['file']}:{r['line']}" for r in pl["code_refs"][:20]]
    if pl["shared_sources"]:
        lines.append("  以下外部源被其他 dataset 共享，raw/blob 将保留:")
        lines += [f"    {s} ← {', '.join(v)}" for s, v in pl["shared_sources"].items()]
    lines.append(f"  合计释放 {_size(pl['total_bytes'])}")
    if pl["blocking"]:
        lines.append("  ⚠ 存在下游/引用，`--apply` 会被拒绝（需先迁移下游，或 --force 强删）")
    else:
        lines.append("  可安全删除：加 --apply 执行")
    out(pl, a.json, "\n".join(lines))
    return 0


def cmd_health(a) -> int:
    from .external import run_health
    p = paths()
    rep = run_health(p, only=a.source)
    s = rep["summary"]
    lines = [f"外部源健康：ok={s['ok']} warn={s['warn']} fail={s['fail']} / {s['total']}"]
    for r in rep["results"]:
        if r["status"] == "ok" and not a.verbose:
            continue
        lines.append(f"  [{r['status'].upper()}] {r['source']}: {r['reason'] or '-'}")
    if s["fail"] or s["warn"]:
        lines.append("→ 在 Claude Code 里运行 `/fix-source` 修复（会先给计划再动手）")
    out(rep, a.json, "\n".join(lines))
    return 1 if s["fail"] else 0


def cmd_sql(a) -> int:
    from .io import sql
    df = sql(a.query)
    if a.json:
        print(json.dumps(df.to_dicts(), ensure_ascii=False, default=str, indent=1))
    else:
        print(df)
    return 0


def _memory_problems(name: str, p) -> list[str]:
    """内存预算体检：有没有申报、实测有没有超。"""
    import json
    from .runner import dataset_config
    from . import memory as mem

    out_: list[str] = []
    declared = dataset_config(name, p).get("runtime", {}).get("memory_estimate_gb")
    if declared is None:
        return [f"{name}: config.yaml 未申报 runtime.memory_estimate_gb"
                f"（新建/迁移时应先估算并与用户确认）"]

    b = mem.budget(p)
    if b["budget_gb"] and float(declared) > float(b["budget_gb"]):
        out_.append(f"{name}: 申报 {declared} GB 超出全仓预算 {b['budget_gb']} GB")

    f = p.dataset_dir(name) / "_meta" / "run_state.json"
    if not f.is_file():
        return out_
    state = json.loads(f.read_text(encoding="utf-8")) or {}
    peaks = state.get("peak_gb") or {}
    deltas = state.get("delta_gb") or {}
    for stage, peak in peaks.items():
        # 与 dw run 同口径：绝对峰值对预算，增量对申报值
        delta = float(deltas.get(stage, peak))
        v = mem.check(float(peak) * 1e9, name, declared, p,
                      baseline_bytes=int((float(peak) - delta) * 1e9))
        if v["msg"]:
            out_.append(f"{name}[{stage}]: {v['msg']}")
    return out_


def cmd_doctor(a) -> int:
    """一次性体检：结构、生成物是否过期、悬空引用、未跑的 dataset。"""
    from .contract import load_all
    from . import graph as G
    from . import io as dwio
    from .scaffold import gen_schema_py
    p = paths()
    contracts = load_all(p)
    g = G.load(p)
    problems = []
    for name, c in contracts.items():
        d = p.dataset_dir(name)
        f = d / "schema.py"
        if not f.is_file() or f.read_text(encoding="utf-8") != gen_schema_py(c):
            problems.append(f"{name}: schema.py 与合约不同步（跑 dw index）")
        if not c.columns:
            problems.append(f"{name}: 合约未定义 columns")
        if not c.grain:
            problems.append(f"{name}: 合约未定义 grain（拆分边界不明）")
        if not dwio.exists(name, p):
            problems.append(f"{name}: 尚无 curated 数据（dw run {name}）")
        for u in c.upstream:
            if u.kind == "dataset" and u.ref not in contracts:
                problems.append(f"{name}: 上游 dataset '{u.ref}' 不存在")
        problems.extend(_memory_problems(name, p))
    if not p.index_md.is_file():
        problems.append("data_contracts/INDEX.md 缺失（跑 dw index）")
    text = "\n".join(f"  - {x}" for x in problems) or "  一切正常"
    out({"problems": problems}, a.json, f"体检 {len(contracts)} 个 dataset：\n{text}")
    return 0


# ---------------- 入口 ----------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="dw", description="token 高效的数据仓库管理 CLI（Claude 先跑命令，再读文件）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", help="重建 INDEX.md / graph.json / registry.json + 生成物")
    s.add_argument("--no-scan", action="store_true", help="跳过代码引用扫描（更快）")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("ls", help="一行一个 dataset")
    s.add_argument("--family"); s.add_argument("--domain")
    s.add_argument("--tag"); s.add_argument("--status")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("search", help="按关键词找可复用的内部数据源")
    s.add_argument("keywords", nargs="+")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("show", help="只输出合约的指定片段")
    s.add_argument("dataset")
    s.add_argument("--fields", help="meta,schema,upstream,sla,quality,changelog,state,all")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("deps", help="依赖闭包")
    s.add_argument("dataset")
    s.add_argument("--up", action="store_true", help="看上游（默认看下游）")
    s.add_argument("--down", action="store_true")
    s.add_argument("--depth", type=int, default=99)
    s.set_defaults(func=cmd_deps)

    s = sub.add_parser("refs", help="谁在代码里引用了它（查 graph.json 缓存）")
    s.add_argument("dataset"); s.add_argument("--column")
    s.set_defaults(func=cmd_refs)

    s = sub.add_parser("impact", help="变更影响面：下游 + 文件:行 清单")
    s.add_argument("dataset"); s.add_argument("--column")
    s.set_defaults(func=cmd_impact)

    s = sub.add_parser("validate", help="实际数据 vs 合约")
    s.add_argument("dataset", nargs="?")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("new", help="生成脚手架（单个或整族）")
    s.add_argument("dataset", nargs="?")
    s.add_argument("--family", help="family spec.yaml 路径，批量生成")
    s.add_argument("--example-spec", action="store_true", help="打印 family spec 样例")
    s.add_argument("--purpose"); s.add_argument("--domain"); s.add_argument("--owner")
    s.add_argument("--source", help="external_sources.yaml 里的 source id（触网才需要）")
    s.add_argument("--upstream", help="逗号分隔的上游 dataset")
    s.add_argument("--no-transform", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("infer", help="从既有数据反推合约草案")
    s.add_argument("path"); s.add_argument("--name")
    s.add_argument("--write", metavar="DATASET", help="写进该 dataset 的 contract.yaml")
    s.add_argument("--overwrite", action="store_true", help="不保留人写字段")
    s.set_defaults(func=cmd_infer)

    s = sub.add_parser("adopt", help="把既有数据纳管进 curated（避免重跑昂贵步骤）")
    s.add_argument("dataset"); s.add_argument("path")
    s.add_argument("--mode", choices=["copy", "move", "link"], default="copy")
    s.add_argument("--no-strict", action="store_true")
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser("run", help="执行流水线")
    s.add_argument("dataset", nargs="?")
    s.add_argument("--family"); s.add_argument("--all", action="store_true")
    s.add_argument("--stage", help="ingest / transform / test，可逗号组合")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("rm", help="删除 dataset（默认 dry-run）")
    s.add_argument("dataset")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="默认行为，仅为可读性")
    s.add_argument("--force", action="store_true", help="有下游也强删")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("health", help="外部源健康监控")
    s.add_argument("--source"); s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_health)

    s = sub.add_parser("sql", help="跨 dataset DuckDB 查询")
    s.add_argument("query")
    s.set_defaults(func=cmd_sql)

    s = sub.add_parser("doctor", help="仓库体检")
    s.set_defaults(func=cmd_doctor)

    return ap


def _force_utf8() -> None:
    """Windows 控制台默认 cp936/cp1252，中文输出会炸。统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _apply_engine_limits() -> None:
    """把 warehouse.yaml 的 engine 限制落成环境变量。

    必须在 polars 被 import 之前做 —— polars 的线程池在 import 时就固定了。
    这里是 `dw` 的第一行，早于任何 dataset 代码，所以是唯一可靠的时机。
    线程数直接影响内存峰值（每个 worker 各持一份 chunk），也决定会不会
    把 CPU 吃满 —— 机器上往往还有别的活儿在跑，所以留了这个旋钮。
    """
    import os
    try:
        from .config import load_config
        eng = (load_config() or {}).get("engine", {}) or {}
    except Exception:
        return
    n = eng.get("polars_max_threads")
    if n and not os.environ.get("POLARS_MAX_THREADS"):
        os.environ["POLARS_MAX_THREADS"] = str(int(n))


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    _apply_engine_limits()
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return a.func(a)
    except FileNotFoundError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    except KeyError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
