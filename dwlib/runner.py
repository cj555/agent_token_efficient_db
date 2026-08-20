"""`dw run` —— 分阶段执行 dataset 流水线。

阶段划分对应 ingest / transform 的职责分离：
  ingest    触网、幂等、可跳过（指纹命中）
  transform 纯本地、确定性、可无限重放
  test      pytest 该 dataset 的 tests/
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .config import Paths, paths
from . import graph as G

STAGES = ("ingest", "transform", "test")


def dataset_config(dataset: str, p: Paths | None = None) -> dict:
    p = p or paths()
    f = p.dataset_dir(dataset) / "config.yaml"
    cfg = yaml.safe_load(f.read_text(encoding="utf-8")) if f.is_file() else {}
    cfg = cfg or {}
    cfg.setdefault("ingest", {})
    cfg.setdefault("transform", {})
    cfg.setdefault("runtime", {})
    return cfg


def _load_module(path: Path, modname: str):
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def run_stage(dataset: str, stage: str, p: Paths | None = None) -> dict:
    p = p or paths()
    d = p.dataset_dir(dataset)
    cfg = dataset_config(dataset, p)
    t0 = time.time()

    if stage == "test":
        r = subprocess.run(
            [sys.executable, "-m", "pytest", str(d / "tests"), "-q", "--no-header"],
            cwd=str(p.root), capture_output=True, text=True,
        )
        tail = (r.stdout or r.stderr).strip().splitlines()[-3:]
        return {"stage": stage, "ok": r.returncode == 0,
                "secs": round(time.time() - t0, 1), "detail": " | ".join(tail)}

    f = d / f"{stage}.py"
    if not f.is_file():
        return {"stage": stage, "ok": True, "skipped": True,
                "secs": 0.0, "detail": f"无 {stage}.py（该 dataset 不需要此阶段）"}
    if cfg.get(stage, {}).get("enabled") is False:
        return {"stage": stage, "ok": True, "skipped": True,
                "secs": 0.0, "detail": f"config.yaml 中 {stage}.enabled=false"}

    try:
        mod = _load_module(f, f"dw_{dataset}_{stage}")
        result = mod.main()
        return {"stage": stage, "ok": True, "secs": round(time.time() - t0, 1),
                "detail": _brief(result)}
    except NotImplementedError as e:
        return {"stage": stage, "ok": False, "secs": round(time.time() - t0, 1),
                "detail": f"未实现: {e}"}
    except Exception as e:
        return {"stage": stage, "ok": False, "secs": round(time.time() - t0, 1),
                "detail": f"{type(e).__name__}: {e}"}


def _brief(result) -> str:
    if isinstance(result, dict):
        keys = ("rows", "bytes", "files", "skipped", "reason")
        parts = [f"{k}={result[k]}" for k in keys if k in result]
        return ", ".join(parts) or str(result)[:120]
    return str(result)[:120]


def run(dataset: str, stages: tuple[str, ...] = STAGES, p: Paths | None = None) -> dict:
    p = p or paths()
    out = {"dataset": dataset, "stages": [], "ok": True}
    for st in stages:
        r = run_stage(dataset, st, p)
        out["stages"].append(r)
        if not r["ok"]:
            out["ok"] = False
            break        # 上游阶段失败即停，不浪费时间也不产生半成品
    return out


def run_many(names: list[str], stages: tuple[str, ...] = STAGES,
             p: Paths | None = None, stop_on_error: bool = True) -> list[dict]:
    """按依赖拓扑序批量执行（dw run --family / --all）。"""
    p = p or paths()
    g = G.load(p)
    ordered = G.topo(g, names) if g.get("nodes") else names
    results = []
    for n in ordered:
        r = run(n, stages, p)
        results.append(r)
        if not r["ok"] and stop_on_error:
            break
    return results


def select_family(prefix: str, p: Paths | None = None) -> list[str]:
    from .contract import load_all
    p = p or paths()
    out = []
    for name, c in load_all(p).items():
        fam = c.family or (name.split("__")[0] if "__" in name else "")
        if fam == prefix or name.startswith(prefix + "__") or name == prefix:
            out.append(name)
    return sorted(out)
