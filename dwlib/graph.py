"""依赖图 + 代码引用表。graph.json 是 dw deps/impact 的查表基础，避免全仓 grep。"""
from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from pathlib import Path

from .config import Paths, paths
from .contract import Contract, load_all

# 代码里引用 dataset 的写法（dw refs 扫描这些形态）
_REF_PATTERNS = [
    re.compile(r"""dw\.(?:load|arrow|scan|describe)\(\s*["']([a-z][a-z0-9_]*)["']"""),
    re.compile(r"""dwlib\.(?:load|arrow|scan|describe)\(\s*["']([a-z][a-z0-9_]*)["']"""),
    re.compile(r"""\bfrom\s+([a-z][a-z0-9_]*)\b""", re.IGNORECASE),  # SQL: from <table>
    re.compile(r"""\bjoin\s+([a-z][a-z0-9_]*)\b""", re.IGNORECASE),  # SQL: join <table>
]

_SCAN_SUFFIXES = {".py", ".sql", ".yaml", ".yml", ".md"}
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "storage", ".health", ".dw", "node_modules"}


def _iter_code_files(root: Path):
    """按代码后缀遍历仓库。用 os.walk 原地剪掉 _SKIP_DIRS（尤其是 storage），

    不用 Path.rglob("*")——那会先把整棵目录树物理遍历一遍再按后缀/目录过滤，
    对 storage/ 这种可能有几十万个文件的目录代价极高（实测 storage/blob/
    单个外部源下就有 19 万+子目录，rglob 在 Windows 上跑 dw index 能卡到
    一小时以上都遍历不完）。os.walk 允许在遍历途中对 dirnames 就地剪枝，
    从根源上不下钻进 storage/.git 等目录，输出结果与旧实现完全一致。
    """
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            f = Path(dirpath) / name
            if f.suffix in _SCAN_SUFFIXES:
                yield f


def scan_refs(p: Paths, known: set[str]) -> dict[str, list[dict]]:
    """扫描全仓，返回 {dataset: [{file, line, text, in_dataset}]}。结果缓存进 graph.json。"""
    refs: dict[str, list[dict]] = defaultdict(list)
    for f in _iter_code_files(p.root):
        rel = p.rel(f)
        owner = rel.split("/")[1] if rel.startswith("datasets/") else None
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for pat in _REF_PATTERNS:
                for m in pat.finditer(line):
                    ds = m.group(1)
                    if ds not in known or ds == owner:
                        continue
                    refs[ds].append(
                        {"file": rel, "line": i, "text": line.strip()[:160], "in_dataset": owner}
                    )
    return dict(refs)


def build(p: Paths | None = None, scan: bool = True) -> dict:
    p = p or paths()
    contracts = load_all(p)
    known = set(contracts)

    nodes = {}
    edges = []          # {from, to, kind}  from -> to 表示 to 依赖 from
    for name, c in contracts.items():
        nodes[name] = {
            "domain": c.domain,
            "family": c.family or (name.split("__")[0] if "__" in name else ""),
            "status": c.status,
            "grain": c.grain,
            "columns": c.column_names,
            "tags": c.tags,
            "network": c.touches_network(),
            "schedule": c.sla.schedule,
        }
        for up in c.upstream:
            edges.append({"from": up.ref, "to": name, "kind": up.kind})

    refs = scan_refs(p, known) if scan else _load(p).get("refs", {})
    return {"nodes": nodes, "edges": edges, "refs": refs}


def _load(p: Paths) -> dict:
    if p.graph_json.is_file():
        return json.loads(p.graph_json.read_text(encoding="utf-8"))
    return {"nodes": {}, "edges": [], "refs": {}}


def load(p: Paths | None = None) -> dict:
    return _load(p or paths())


def save(g: dict, p: Paths | None = None) -> None:
    p = p or paths()
    p.graph_json.parent.mkdir(parents=True, exist_ok=True)
    p.graph_json.write_text(
        json.dumps(g, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def _adj(g: dict, direction: str) -> dict[str, list[str]]:
    a: dict[str, list[str]] = defaultdict(list)
    for e in g["edges"]:
        if e["kind"] != "dataset":
            continue
        if direction == "down":       # 谁依赖我
            a[e["from"]].append(e["to"])
        else:                          # 我依赖谁
            a[e["to"]].append(e["from"])
    return a


def closure(g: dict, start: str, direction: str = "down", depth: int = 99) -> list[tuple[str, int]]:
    """BFS 依赖闭包，返回 [(dataset, 距离)]。"""
    a = _adj(g, direction)
    seen = {start: 0}
    order: list[tuple[str, int]] = []
    q = deque([(start, 0)])
    while q:
        cur, d = q.popleft()
        if d >= depth:
            continue
        for nxt in a.get(cur, []):
            if nxt in seen:
                continue
            seen[nxt] = d + 1
            order.append((nxt, d + 1))
            q.append((nxt, d + 1))
    return order


def topo(g: dict, names: list[str] | None = None) -> list[str]:
    """拓扑序（上游在前）。用于 dw run --family。"""
    nodes = list(names or g["nodes"].keys())
    nset = set(nodes)
    indeg = {n: 0 for n in nodes}
    succ: dict[str, list[str]] = defaultdict(list)
    for e in g["edges"]:
        if e["kind"] != "dataset" or e["from"] not in nset or e["to"] not in nset:
            continue
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    q = deque(sorted(n for n in nodes if indeg[n] == 0))
    out = []
    while q:
        n = q.popleft()
        out.append(n)
        for m in sorted(succ[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    if len(out) != len(nodes):     # 有环
        out += sorted(set(nodes) - set(out))
    return out


def external_consumers(g: dict, source_id: str) -> list[str]:
    return sorted({e["to"] for e in g["edges"] if e["kind"] == "external" and e["from"] == source_id})
