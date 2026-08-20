"""数据读写 API —— 内部 transform 与外部消费方共用的唯一入口。

    import dwlib as dw
    dw.load("sec__mdna", columns=[...])   # polars LazyFrame
    dw.arrow("sec__mdna_vectors")         # pyarrow Table（零拷贝转 numpy）
    dw.sql("select ... from a join b ...")# DuckDB，所有 curated 自动注册为视图
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .config import Paths, paths


def _pl():
    import polars as pl
    return pl


def curated_path(dataset: str, p: Paths | None = None) -> Path:
    return (p or paths()).curated(dataset)


def exists(dataset: str, p: Paths | None = None) -> bool:
    d = curated_path(dataset, p)
    return d.is_dir() and any(d.rglob("*.parquet"))


def scan(dataset: str, p: Paths | None = None, columns: Iterable[str] | None = None):
    """惰性扫描 curated 表 -> polars LazyFrame。"""
    pl = _pl()
    d = curated_path(dataset, p)
    if not d.is_dir():
        raise FileNotFoundError(f"'{dataset}' 尚无 curated 数据：{d}（先跑 `dw run {dataset}`）")
    lf = pl.scan_parquet(str(d / "**" / "*.parquet"), hive_partitioning=True)
    if columns:
        lf = lf.select(list(columns))
    return lf


def load(dataset: str, columns: Iterable[str] | None = None, filters=None, p: Paths | None = None):
    """返回 LazyFrame（默认惰性，避免误把大表拉进内存）。filters 为 polars 表达式。"""
    lf = scan(dataset, p, columns)
    if filters is not None:
        lf = lf.filter(filters)
    return lf


def frame(dataset: str, columns: Iterable[str] | None = None, p: Paths | None = None):
    """立即物化为 polars DataFrame。"""
    return scan(dataset, p, columns).collect()


def arrow(dataset: str, columns: Iterable[str] | None = None, p: Paths | None = None):
    """pyarrow Table —— 向量/矩阵计算走这里，`.to_numpy(zero_copy_only=False)` 转 numpy。"""
    import pyarrow.dataset as pads
    d = curated_path(dataset, p)
    if not d.is_dir():
        raise FileNotFoundError(f"'{dataset}' 尚无 curated 数据：{d}")
    ds = pads.dataset(str(d), format="parquet", partitioning="hive")
    return ds.to_table(columns=list(columns) if columns else None)


def vectors(dataset: str, column: str = "vector", p: Paths | None = None):
    """把 fixed_size_list<float32> 列零拷贝转成 (n, dim) 的 numpy 矩阵。"""
    import numpy as np
    tbl = arrow(dataset, [column], p)
    col = tbl.column(column).combine_chunks()
    if hasattr(col, "chunk"):
        col = col.chunk(0) if col.num_chunks else col
    flat = col.flatten() if hasattr(col, "flatten") else col.values
    dim = col.type.list_size if hasattr(col.type, "list_size") else len(col[0])
    return np.asarray(flat).reshape(-1, dim)


def connect(p: Paths | None = None, datasets: Iterable[str] | None = None):
    """DuckDB 连接，所有 curated 表注册为同名视图。"""
    import duckdb
    from .contract import list_datasets

    pp = p or paths()
    con = duckdb.connect()
    threads = pp.cfg.get("engine", {}).get("duckdb_threads", 0)
    if threads:
        con.execute(f"pragma threads={int(threads)}")
    for name in (datasets or list_datasets(pp)):
        d = pp.curated(name)
        if d.is_dir() and any(d.rglob("*.parquet")):
            glob = (d / "**" / "*.parquet").as_posix()
            con.execute(
                f'create or replace view "{name}" as '
                f"select * from read_parquet('{glob}', hive_partitioning=true)"
            )
    return con


def sql(query: str, p: Paths | None = None):
    """跨 dataset SQL，返回 polars DataFrame。"""
    con = connect(p)
    try:
        return con.execute(query).pl()
    finally:
        con.close()


def write_curated(
    df,
    dataset: str,
    partition_by: list[str] | None = None,
    p: Paths | None = None,
    mode: str = "overwrite",
) -> dict[str, Any]:
    """把 polars DataFrame/LazyFrame 写入 curated 层。transform.py 的收尾动作。

    mode: overwrite（默认，先清空目录）| append
    """
    import shutil

    pl = _pl()
    pp = p or paths()
    if hasattr(df, "collect"):
        df = df.collect()
    out = pp.curated(dataset)
    if mode == "overwrite" and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    comp = pp.cfg.get("defaults", {}).get("compression", "zstd")

    if partition_by:
        df.write_parquet(out, partition_by=partition_by, compression=comp)
    else:
        n = len(list(out.glob("part-*.parquet")))
        df.write_parquet(out / f"part-{n:05d}.parquet", compression=comp)

    stats = {"dataset": dataset, "rows": df.height, "columns": df.width,
             "path": pp.rel(out), "bytes": sum(f.stat().st_size for f in out.rglob("*.parquet"))}
    _write_run_state(dataset, stats, pp)
    return stats


def _write_run_state(dataset: str, stats: dict, p: Paths) -> None:
    import datetime as dt
    meta = p.dataset_dir(dataset) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    state_f = meta / "run_state.json"
    state = json.loads(state_f.read_text(encoding="utf-8")) if state_f.is_file() else {}
    state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
    state["rows"] = stats["rows"]
    state["bytes"] = stats["bytes"]
    state_f.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def run_state(dataset: str, p: Paths | None = None) -> dict:
    f = (p or paths()).dataset_dir(dataset) / "_meta" / "run_state.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def describe(dataset: str, p: Paths | None = None) -> dict:
    """从 registry.json 读合约摘要（不解析 YAML，快且省）。"""
    from .registry import load_registry
    reg = load_registry(p)
    if dataset not in reg["datasets"]:
        raise KeyError(f"registry 中无 '{dataset}'，先跑 `dw index`")
    return reg["datasets"][dataset]
