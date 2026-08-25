"""`dw run` —— 分阶段执行 dataset 流水线。

阶段划分对应 ingest / transform 的职责分离：
  ingest    触网、幂等、可跳过（指纹命中）
  transform 纯本地、确定性、可无限重放
  test      pytest 该 dataset 的 tests/
"""
from __future__ import annotations

import importlib.util
import os
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


def apply_thread_override(cfg: dict) -> str:
    """按 dataset 覆盖 polars 线程数（config.yaml 的 runtime.polars_max_threads）。

    为什么需要：线程数是内存与速度的直接权衡 —— 实测 event__stk_daily 4 线程
    1.01 GB / 26s、2 线程 0.71 GB / 43s。少数吃内存的 dataset 不该逼着全仓降速，
    所以给它们一个局部开关，全仓默认仍走 warehouse.yaml 的 engine.polars_max_threads。

    坑：polars 的线程池在 **import 时**固定。所以只能在加载 stage 模块之前设环境变量，
    且同一个进程里只有第一次生效（`dw run --all` 跑多个 dataset 时后面的覆盖不了）。
    覆盖没生效时返回一句提示，如实写进 run 结果，不假装成功。
    """
    want = cfg.get("runtime", {}).get("polars_max_threads")
    if not want:
        return ""
    want = int(want)
    if "polars" in sys.modules:
        import polars as pl
        if pl.thread_pool_size() != want:
            return (f"[warn] runtime.polars_max_threads={want} 未生效："
                    f"polars 已在本进程 import（线程池 {pl.thread_pool_size()}）。"
                    f"单独跑 `dw run {cfg.get('dataset', '')}` 可生效")
        return ""
    os.environ["POLARS_MAX_THREADS"] = str(want)
    return ""


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
        from . import memory as mem
        declared = cfg.get("runtime", {}).get("memory_estimate_gb")
        note = apply_thread_override(cfg)
        mod = _load_module(f, f"dw_{dataset}_{stage}")
        with mem.peak_rss() as tracker:
            result = mod.main()
        verdict = mem.check(tracker.peak, dataset, declared, p,
                            baseline_bytes=tracker.baseline)
        out = {"stage": stage, "ok": verdict["level"] != "fail",
               "secs": round(time.time() - t0, 1),
               "peak_gb": verdict["peak_gb"], "detail": _brief(result)}
        if note:
            out["detail"] += f" | {note}"
        if verdict["msg"]:
            out["detail"] += f" | [内存{verdict['level']}] {verdict['msg']}"
        _record_peak(dataset, stage, verdict, p)
        return out
    except NotImplementedError as e:
        return {"stage": stage, "ok": False, "secs": round(time.time() - t0, 1),
                "detail": f"未实现: {e}"}
    except Exception as e:
        return {"stage": stage, "ok": False, "secs": round(time.time() - t0, 1),
                "detail": f"{type(e).__name__}: {e}"}


def _record_peak(dataset: str, stage: str, verdict: dict, p: Paths) -> None:
    """把实测峰值写进 _meta/run_state.json，供 dw doctor 事后核对申报值。"""
    import json
    meta = p.dataset_dir(dataset) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    f = meta / "run_state.json"
    state = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    state.setdefault("peak_gb", {})[stage] = verdict["peak_gb"]
    state.setdefault("delta_gb", {})[stage] = verdict["delta_gb"]
    f.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


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


def select_family(prefix: str, p: Paths | None = None,
                  include_manual: bool = False) -> list[str]:
    """族内成员。默认只带 **sla.runner == "family" 且排了 schedule** 的。

    family 定时任务跑的就是 `dw run --family X`，如果不看这两个字段，把某张表
    「改成手动」或「挪去自己的任务」就只是句空话 —— 它照样每天被带着跑（还可能
    跟自己的独立任务两头跑）。点名跑（`dw run <ds>`）永远照常，
    要在族里带上非 family 成员加 --include-manual。
    """
    from .contract import load_all
    p = p or paths()
    out = []
    for name, c in load_all(p).items():
        fam = c.family or (name.split("__")[0] if "__" in name else "")
        if not (fam == prefix or name.startswith(prefix + "__") or name == prefix):
            continue
        if not include_manual and (c.sla.runner != "family" or not c.sla.schedule):
            continue
        out.append(name)
    return sorted(out)
