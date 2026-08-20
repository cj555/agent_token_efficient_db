"""`dw rm` —— 彻底删除一个 dataset 的全部关联对象。

清单覆盖：代码目录、curated/tmp 存储、blob/raw（仅当无其他 dataset 共享该 source）、
合约与生成物、registry/graph 条目、OS 定时任务、其他代码里的引用点。
默认 dry-run，只列清单不动手。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import Paths, paths
from .contract import load_all, load_contract
from . import graph as G


def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0


def plan(dataset: str, p: Paths | None = None) -> dict:
    """列出将要删除的一切。这是 del-dataset skill 的审批依据。"""
    p = p or paths()
    g = G.load(p)
    contracts = load_all(p)

    downstream = [n for n, _ in G.closure(g, dataset, "down")]
    refs = g.get("refs", {}).get(dataset, [])
    external_refs = [r for r in refs if r.get("in_dataset") != dataset]

    c = contracts.get(dataset)
    sources = c.upstream_externals if c else []
    # 只有当没有别的 dataset 也用这个 source 时，raw/blob 才可以删
    shared = {
        s: sorted(n for n, cc in contracts.items()
                  if n != dataset and s in cc.upstream_externals)
        for s in sources
    }

    targets = []
    code_dir = p.dataset_dir(dataset)
    if code_dir.is_dir():
        targets.append({"kind": "code", "path": p.rel(code_dir), "bytes": _dir_size(code_dir)})
    for kind, d in (("curated", p.curated(dataset)), ("tmp", p.tmp(dataset))):
        if d.is_dir():
            targets.append({"kind": kind, "path": p.rel(d), "bytes": _dir_size(d)})
    for s in sources:
        if shared[s]:
            continue    # 被别的 dataset 共享，保留
        for kind, d in (("raw", p.raw(s)), ("blob", p.blob(s))):
            if d.is_dir():
                targets.append({"kind": kind, "path": p.rel(d), "bytes": _dir_size(d),
                                "source_id": s})

    tasks = _find_scheduled_tasks(dataset)

    return {
        "dataset": dataset,
        "exists": dataset in contracts,
        "downstream": downstream,
        "blocking": bool(downstream) or bool(external_refs),
        "code_refs": external_refs,
        "shared_sources": {k: v for k, v in shared.items() if v},
        "removable_sources": [s for s in sources if not shared[s]],
        "targets": targets,
        "total_bytes": sum(t["bytes"] for t in targets),
        "scheduled_tasks": tasks,
    }


def _find_scheduled_tasks(dataset: str) -> list[str]:
    """查 Windows 计划任务里 dw-<dataset>-* 的条目。非 Windows 返回空。"""
    try:
        r = subprocess.run(["schtasks", "/query", "/fo", "csv", "/nh"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    prefix = f"dw-{dataset}-"
    found = []
    for line in r.stdout.splitlines():
        name = line.split(",")[0].strip('" ').lstrip("\\")
        if name.startswith(prefix) or name == f"dw-{dataset}":
            found.append(name)
    return sorted(set(found))


def apply(dataset: str, p: Paths | None = None, force: bool = False) -> dict:
    """真正执行删除。存在下游或代码引用时拒绝，除非 force。"""
    p = p or paths()
    pl = plan(dataset, p)
    if pl["blocking"] and not force:
        return {"ok": False, "reason": "存在下游依赖或代码引用，拒绝删除",
                "downstream": pl["downstream"], "code_refs": pl["code_refs"],
                "hint": "先迁移下游 / 标 deprecated 观察期 / 或 --force 强删"}

    removed = []
    for t in pl["targets"]:
        d = p.root / t["path"] if not Path(t["path"]).is_absolute() else Path(t["path"])
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(t["path"])
    for task in pl["scheduled_tasks"]:
        subprocess.run(["schtasks", "/delete", "/tn", task, "/f"],
                       capture_output=True, text=True)

    from .registry import reindex
    stats = reindex(p)
    return {"ok": True, "removed": removed, "unscheduled": pl["scheduled_tasks"],
            "freed_bytes": pl["total_bytes"], "reindex": stats}
