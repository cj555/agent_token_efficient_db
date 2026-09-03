"""`dw run` —— 分阶段执行 dataset 流水线。

阶段划分对应 ingest / transform 的职责分离：
  ingest    触网、幂等、可跳过（指纹命中）
  transform 纯本地、确定性、可无限重放
  test      pytest 该 dataset 的 tests/
"""
from __future__ import annotations

import importlib.util
import json
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


_GUARDED_STAGES = ("ingest", "transform", "backfill")   # test 阶段不熔断：失败可见、成本低、重跑无副作用


def run_stage(dataset: str, stage: str, p: Paths | None = None) -> dict:
    """`_run_stage_inner` 的薄包装：跑之前查有没有被熔断，跑完把结果记进
    运行台账（`dwlib.health.run_attempts_record`）。真正的执行逻辑全部在
    `_run_stage_inner` 里，本函数只管熔断检查和结果记录，不碰业务逻辑。

    为什么要熔断：ingest 的水位线、backfill 的游标都是"只在成功时前进"的
    设计，一个 stage 如果连续失败，不管是外部循环脚本还是每天一次的定时
    任务，都会对着同一个坏窗口静默重试下去（真实事故：一次回补循环卡在
    同一个游标连跑 42 轮才被发现）。连续失败达到
    `health.RUN_ATTEMPT_LIMIT` 次后直接短路，不再调用 main()，需要人工
    诊断后 `dw run-reset` 清零才会再尝试。
    """
    guarded = stage in _GUARDED_STAGES
    if guarded:
        from . import health
        rec = health.run_attempts_load(p).get(f"{dataset}:{stage}", {})
        if rec.get("quarantined"):
            last_note = (rec.get("log") or [{}])[-1].get("note", "")
            return {"stage": stage, "ok": False, "quarantined": True, "secs": 0.0,
                    "detail": f"已连续失败 {rec.get('fails')} 次熔断，需要人工诊断后 "
                              f"`dw run-reset {dataset} --stage {stage}` 清零再继续"
                              f"（最近错误：{last_note}）"}
    out = _run_stage_inner(dataset, stage, p)
    if guarded:
        from . import health
        health.run_attempts_record(dataset, stage, "ok" if out.get("ok") else "fail",
                                    note=out.get("detail", ""), p=p)
    return out


def _run_stage_inner(dataset: str, stage: str, p: Paths | None = None) -> dict:
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
        if stage == "backfill":
            detail = "无 backfill.py（可能是这个 dataset 不需要历史回补，也可能是还没建）"
        else:
            detail = f"无 {stage}.py（该 dataset 不需要此阶段）"
        return {"stage": stage, "ok": True, "skipped": True, "secs": 0.0, "detail": detail}
    if cfg.get(stage, {}).get("enabled") is False:
        return {"stage": stage, "ok": True, "skipped": True,
                "secs": 0.0, "detail": f"config.yaml 中 {stage}.enabled=false"}

    try:
        from . import memory as mem
        declared = cfg.get("runtime", {}).get("memory_estimate_gb")
        note = ""
        if os.environ.get("DW_INPROCESS"):
            # 进程内路径：只给调试和 pytest 用，峰值会被同进程里跑过的表污染
            note = apply_thread_override(cfg)
            mod = _load_module(f, f"dw_{dataset}_{stage}")
            with mem.peak_rss() as tracker:
                result = mod.main()
            peak, baseline = tracker.peak, tracker.baseline
        else:
            got = _run_in_subprocess(dataset, stage, cfg, p)
            if "error" in got:
                detail = (f"未实现: {got['error']}" if got.get("not_implemented")
                          else got["error"])
                return {"stage": stage, "ok": False,
                        "secs": round(time.time() - t0, 1), "detail": detail}
            result, peak, baseline = got["result"], got["peak"], got["baseline"]

        verdict = mem.check(peak, dataset, declared, p, baseline_bytes=baseline)
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


def _run_in_subprocess(dataset: str, stage: str, cfg: dict, p: Paths) -> dict:
    """fork 一个干净的解释器跑这个 stage —— 与 test 阶段一样的做法。

    这样每张表跑完内存彻底归还 OS，`dw run --family` 的绝对峰值 = 最大的**单张**表，
    而不是全族累加（polars 的分配器不还页，同进程里只涨不落）。
    """
    from ._stage_worker import MARKER

    env = dict(os.environ)
    want = cfg.get("runtime", {}).get("polars_max_threads")
    if want:
        # 子进程里 polars 还没 import，线程池尚未固定，这里设才真的生效
        env["POLARS_MAX_THREADS"] = str(int(want))

    r = subprocess.run(
        [sys.executable, "-m", "dwlib._stage_worker", dataset, stage],
        cwd=str(p.root), capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace",
    )
    lines = (r.stdout or "").splitlines()
    # 被测代码自己的输出原样透传（子进程捕获后一次性吐出，顺序保持不变）
    for line in lines:
        if not line.startswith(MARKER):
            print(line)
    for line in reversed(lines):
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    # 没拿到结果行 = 子进程被杀（OOM）或启动就失败，把它的话如实带回来
    tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["无输出"])[-3:]
    return {"error": f"子进程异常退出（returncode={r.returncode}）: " + " | ".join(tail)}


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
