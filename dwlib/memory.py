"""dwlib.memory —— 内存峰值的测量与预算约束。

为什么需要这个：数据流水线通常要和机器上的其他工作共存，不能想吃多少吃多少。
但「省内存」如果只是口号，写代码时没人记得住 —— 所以把它做成**可测量、
可申报、可拦截**的三件套：

  1. 测量：`peak_rss()` 上下文管理器，采样本进程的峰值工作集。
     `dw run` 每个阶段都包在里面，结果写进 _meta/run_state.json。
  2. 申报：每个 dataset 的 config.yaml 写 `runtime.memory_estimate_gb`。
     新建/迁移 dataset 时必须先估算并经用户确认（见 create-dataset / migrate-dataset skill）。
  3. 拦截：warehouse.yaml 的 `engine.memory_budget_gb` 是全仓上限。
     实测峰值超预算时按 `engine.memory_on_exceed`（warn | fail）处理。

估算方法见 `estimate_bytes` —— 从 schema 的 dtype 宽度推每行字节数，
再乘行数。粗，但足以区分「几百 MB」和「十几 GB」，而这正是要防的量级差。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
from contextlib import contextmanager
from typing import Any

from .config import Paths, paths

# dtype 名 -> 每行大致字节数。变长类型按经验值给，宁可高估。
_WIDTH = {
    "Boolean": 1, "Int8": 1, "UInt8": 1, "Int16": 2, "UInt16": 2,
    "Int32": 4, "UInt32": 4, "Float32": 4, "Date": 4,
    "Int64": 8, "UInt64": 8, "Float64": 8, "Time": 8, "Datetime": 8, "Duration": 8,
    "Categorical": 8, "Enum": 4,
    "String": 48,        # 32 字节内容 + 偏移/长度开销，短代码类的列会高估
    "Binary": 64,
    "List": 128, "Array": 128, "Struct": 128,
}
_DEFAULT_WIDTH = 32

# polars 执行期的放大系数：join / sort / groupby 要额外的中间缓冲与副本。
# 3 是实测 polygon__stk_eod_adj（25M 行 × 12 列）反推出来的保守值。
EXEC_MULTIPLIER = 3.0


def row_bytes(schema: dict[str, Any]) -> int:
    """按 schema 估每行字节数。schema 是 {列名: polars dtype}。"""
    total = 0
    for dtype in schema.values():
        name = str(dtype).split("(")[0].strip()
        total += _WIDTH.get(name, _DEFAULT_WIDTH)
    return total


def estimate_bytes(schema: dict[str, Any], rows: int,
                   multiplier: float = EXEC_MULTIPLIER) -> int:
    """估算「物化这张表 + 执行期开销」的峰值字节数。"""
    return int(row_bytes(schema) * max(rows, 0) * multiplier)


def fmt_gb(n_bytes: float) -> str:
    return f"{n_bytes / 1e9:.2f} GB"


# ---------------------------------------------------------------- 实测

class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _win_sampler():
    """返回一个「读当前进程峰值工作集（字节）」的函数；非 Windows 返回 None。

    注意只查**自己**：GetProcessMemoryInfo 查别的进程会返回全零
    （OpenProcess / Popen._handle 都试过），所以采样必须在被测进程内进行。
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]   # 不设会把句柄截成 32 位
        fn.restype = wt.BOOL
        cur = ctypes.windll.kernel32.GetCurrentProcess
        cur.restype = wt.HANDLE
        handle = cur()
    except Exception:
        return None

    def read() -> int:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        if not fn(handle, ctypes.byref(pmc), pmc.cb):
            return 0
        # 读**当前**占用而不是 PeakWorkingSetSize —— 后者是进程生命周期的高水位，
        # `dw run --family` 把好几个 dataset 跑在同一个进程里时，
        # 后面的小表会白白继承前面大表的峰值，报出来的数字全是错的。
        return int(max(pmc.WorkingSetSize, pmc.PagefileUsage))

    return read


def _posix_peak() -> int:
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(rss if sys.platform == "darwin" else rss * 1024)
    except Exception:
        return 0


class PeakTracker:
    """记录一段代码执行期间的进程峰值内存（字节）。"""

    def __init__(self) -> None:
        self.peak = 0
        self.baseline = 0


@contextmanager
def peak_rss(interval: float = 0.1):
    """上下文管理器，退出后 tracker.peak 是这段时间内的进程峰值内存（字节）。

    两个已知的坑，都踩过了：
      * 采样间隔别调太小 —— 0.05 秒会因为 GIL 争用把被测代码拖慢好几倍。
        代价是极短的尖峰可能采不到，所以这个数字是「下界」不是「上界」。
      * 只能测自己。GetProcessMemoryInfo 查别的进程会返回全零
        （OpenProcess 和 Popen._handle 都试过），所以采样必须在被测进程内。
    """
    tracker = PeakTracker()
    read = _win_sampler()

    if read is None:                       # 非 Windows：用 rusage 的高水位
        tracker.baseline = _posix_peak()
        try:
            yield tracker
        finally:
            tracker.peak = _posix_peak()
        return

    tracker.baseline = read()
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            tracker.peak = max(tracker.peak, read())
            stop.wait(interval)

    t = threading.Thread(target=sample, daemon=True)
    t.start()
    try:
        yield tracker
    finally:
        stop.set()
        t.join(timeout=2)
        tracker.peak = max(tracker.peak, read())


# ---------------------------------------------------------------- 预算

def budget(p: Paths | None = None) -> dict[str, Any]:
    """读 warehouse.yaml 的内存预算配置。"""
    pp = p or paths()
    eng = pp.cfg.get("engine", {}) or {}
    return {
        "budget_gb": eng.get("memory_budget_gb"),
        "on_exceed": (eng.get("memory_on_exceed") or "warn").lower(),
    }


def check(peak_bytes: int, dataset: str, declared_gb: float | None = None,
          p: Paths | None = None, baseline_bytes: int = 0) -> dict[str, Any]:
    """把实测值与「全仓预算」「dataset 自己的申报值」比对。

    两个数字量的是两回事，别混：
      * **绝对峰值** vs 全仓预算 —— 关心的是「这个进程会不会把机器吃满」，
        所以要看整个进程当时占了多少。
      * **增量（峰值 − 入场时基线）** vs 该 dataset 的申报值 —— 关心的是
        「这个 dataset 自己吃了多少」。`dw run --family` 把多个 dataset 跑在同一个
        进程里，polars 的分配器又不会把内存还给系统，所以后面的小表一进场
        基线就已经很高了；不减基线的话它们会背上前面大表的锅。

    返回 {ok, level, msg, peak_gb, delta_gb, budget_gb, declared_gb}。
    level: ok | warn | fail
    """
    cfg = budget(p)
    peak_gb = peak_bytes / 1e9
    delta_gb = max(peak_bytes - baseline_bytes, 0) / 1e9
    res: dict[str, Any] = {"ok": True, "level": "ok", "msg": "",
                           "peak_gb": round(peak_gb, 2), "delta_gb": round(delta_gb, 2),
                           "budget_gb": cfg["budget_gb"], "declared_gb": declared_gb}

    problems: list[str] = []
    if cfg["budget_gb"] and peak_gb > float(cfg["budget_gb"]):
        problems.append(
            f"峰值 {peak_gb:.2f} GB 超出全仓预算 {cfg['budget_gb']} GB"
            f"（warehouse.yaml: engine.memory_budget_gb）"
        )
    # 申报值给 25% 容差 —— 数据自然增长不该天天报警
    if declared_gb and delta_gb > float(declared_gb) * 1.25:
        problems.append(
            f"本阶段增量 {delta_gb:.2f} GB 超出 {dataset} 申报的 {declared_gb} GB 逾 25%"
            f"（config.yaml: runtime.memory_estimate_gb，请重新估算并更新）"
        )

    if problems:
        res["msg"] = "；".join(problems)
        res["level"] = "fail" if cfg["on_exceed"] == "fail" else "warn"
        res["ok"] = res["level"] != "fail"
    return res
