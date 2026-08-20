"""数据读写 API —— 内部 transform 与外部消费方共用的唯一入口。

读：
    import dwlib as dw
    dw.load("sec__mdna", columns=[...])   # polars LazyFrame
    dw.arrow("sec__mdna_vectors")         # pyarrow Table（零拷贝转 numpy）
    dw.sql("select ... from a join b ...")# DuckDB，所有 curated 自动注册为视图

写（transform 的收尾动作，二选一）：
    dw.write_curated(lf, ds, partition_by=["year"])   # 传 LazyFrame = 流式落盘
    dw.write_curated_chunks(chunks, ds, "year")       # 大表按分区逐块落盘

两者的区别只在内存：sink 省掉的是「写」那一半，`chunks` 才能省掉「算」那一半。
整表要做 join / sort / group_by 时必须用后者 —— 见 write_curated_chunks 的文档。
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


_HIVE_TYPES = {
    "int8": "Int8", "int16": "Int16", "int32": "Int32", "int64": "Int64",
    "uint8": "UInt8", "uint16": "UInt16", "uint32": "UInt32", "uint64": "UInt64",
    "float32": "Float32", "float64": "Float64", "string": "String",
    "bool": "Boolean", "date32": "Date",
}


def _hive_schema(dataset: str, p: Paths):
    """按合约声明给分区列定类型。

    不给的话 polars 会从目录名 `year=2024` 猜，整数一律猜成 Int64，
    合约里写的 int32 就对不上，dw validate 会报 type_mismatch。
    分区列名和类型都在 contract.yaml 里写着，直接用它，别猜。
    """
    pl = _pl()
    try:
        from .contract import load_contract
        c = load_contract(dataset, p)
    except Exception:
        return None
    if not c.partitions:
        return None
    types = {col.name: col.type for col in c.columns}
    out = {}
    for name in c.partitions:
        dtype = _HIVE_TYPES.get(str(types.get(name, "")).lower())
        if dtype is None:
            return None
        out[name] = getattr(pl, dtype)
    return out or None


def scan(dataset: str, p: Paths | None = None, columns: Iterable[str] | None = None):
    """惰性扫描 curated 表 -> polars LazyFrame。"""
    pl = _pl()
    pp = p or paths()
    d = curated_path(dataset, pp)
    if not d.is_dir():
        raise FileNotFoundError(f"'{dataset}' 尚无 curated 数据：{d}（先跑 `dw run {dataset}`）")
    kw: dict[str, Any] = {"hive_partitioning": True}
    hs = _hive_schema(dataset, pp)
    if hs:
        kw["hive_schema"] = hs
    lf = pl.scan_parquet(str(d / "**" / "*.parquet"), **kw)
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

    **传 LazyFrame 时走流式落盘（sink_parquet），不会把整表拉进内存。**
    这是本仓库控制内存峰值的主要手段：2500 万行的表用 collect() 要几个 GB，
    用 sink 只占几百 MB。所以 transform.py 的 build() 请返回 LazyFrame，
    不要自己先 .collect()。传 DataFrame 仍然可以（已经在内存里了，直接写）。
    """
    import shutil
    import warnings

    pl = _pl()
    pp = p or paths()
    out = pp.curated(dataset)
    comp = pp.cfg.get("defaults", {}).get("compression", "zstd")
    row_group = pp.cfg.get("defaults", {}).get("row_group_size")

    # LazyFrame 有 collect 没 height；DataFrame 两者都有
    is_lazy = hasattr(df, "collect") and not hasattr(df, "height")

    if mode == "overwrite" and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    if is_lazy:
        if partition_by and mode == "append":
            raise NotImplementedError(
                "分区表暂不支持 append：流式分区写会覆盖同名分区文件。"
                "请用 mode='overwrite' 整表重算 —— 本仓库的 transform 都是确定性的，"
                "重算比增量更安全也够快。"
            )
        kw: dict[str, Any] = {"compression": comp, "engine": "streaming", "mkdir": True}
        if row_group:
            kw["row_group_size"] = int(row_group)
        if partition_by:
            # include_key=False：分区列只编码在目录名里，与 write_parquet(partition_by=)
            # 的产物布局一致，dw.scan 的 hive_partitioning=True 能原样读回
            target = pl.PartitionBy(out, key=list(partition_by), include_key=False)
        else:
            n = len(list(out.glob("part-*.parquet")))
            target = out / f"part-{n:05d}.parquet"
        with warnings.catch_warnings():
            # PartitionBy 在 polars 里标着 unstable，只是 API 可能变，落盘格式是标准 parquet
            warnings.simplefilter("ignore")
            df.sink_parquet(target, **kw)
    else:
        if partition_by:
            df.write_parquet(out, partition_by=partition_by, compression=comp)
        else:
            n = len(list(out.glob("part-*.parquet")))
            df.write_parquet(out / f"part-{n:05d}.parquet", compression=comp)

    stats = {"dataset": dataset, **_curated_stats(out, pp),
             "path": pp.rel(out)}
    _write_run_state(dataset, stats, pp)
    return stats


def write_curated_chunks(
    chunks,
    dataset: str,
    partition_col: str,
    p: Paths | None = None,
    skip_empty: bool = False,
) -> dict[str, Any]:
    """逐块流式落盘：内存峰值 = **单块**大小，而不是整表大小。

    chunks: 可迭代的 (分区值, LazyFrame)。每块单独 sink 成
            <curated>/<partition_col>=<值>/part-NNNNN.parquet。

    **同一个分区值可以出现多次**，会写成同一目录下的多个 part 文件。
    这是把内存压到预算以内的关键：分区粒度是给下游查询用的（比如按年裁剪），
    而处理粒度只服务于内存。想按年分区、但一年的数据仍然超预算时，
    就按季/按月切成多块喂进来，落盘仍然是 year=YYYY/ 一个目录。

    什么时候用它而不是 write_curated：
      当整表要做 join / sort / group_by 这类需要全量物化的操作时。
      流式 sink 只能省掉「写」的那一半内存，省不掉「算」的那一半 ——
      25M 行的 join_asof + 全表 sort 照样要吃 7-8 GB。按年切开算，
      每块 2-3M 行，峰值就降到几百 MB。

    代价：分区之间不能互相引用。所以跨分区的东西（比如累积复权因子）
    必须先在小表上算好，再逐块 join 进来。polygon__stk_eod_adj 就是这么做的。
    """
    import shutil

    pl = _pl()
    pp = p or paths()
    out = pp.curated(dataset)
    comp = pp.cfg.get("defaults", {}).get("compression", "zstd")
    row_group = pp.cfg.get("defaults", {}).get("row_group_size")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    kw: dict[str, Any] = {"compression": comp, "engine": "streaming", "mkdir": True}
    if row_group:
        kw["row_group_size"] = int(row_group)

    n_chunks = 0
    seen: dict[Any, int] = {}
    for value, lf in chunks:
        i = seen.get(value, 0)
        seen[value] = i + 1
        target = out / f"{partition_col}={value}" / f"part-{i:05d}.parquet"
        # 分区列**保留在文件里**：只靠 hive 目录名的话 polars 一律推成 Int64，
        # 合约里声明的 int32 就对不上了。文件里有值时 scan 以文件为准。
        lf.sink_parquet(target, **kw)
        if skip_empty and _parquet_rows(target) == 0:
            # 细粒度切块常会切出空块（比如数据起始年的前几个季度），
            # 落个空文件没坏处但碍眼，顺手删掉并把序号让出来
            target.unlink()
            seen[value] = i
            if not any((out / f"{partition_col}={value}").iterdir()):
                (out / f"{partition_col}={value}").rmdir()
            continue
        n_chunks += 1

    if n_chunks == 0:
        raise ValueError(f"{dataset}: chunks 为空，没有任何数据可写")

    stats = {"dataset": dataset, **_curated_stats(out, pp),
             "path": pp.rel(out), "chunks": n_chunks}
    _write_run_state(dataset, stats, pp)
    return stats


def _parquet_rows(f: Path) -> int:
    """只读 parquet footer 的行数，不碰数据。"""
    import pyarrow.parquet as pq
    try:
        return int(pq.ParquetFile(f).metadata.num_rows)
    except Exception:
        return -1


def _curated_stats(out: Path, p: Paths) -> dict[str, Any]:
    """行数/列数只读 parquet 元数据，不把数据读进内存。"""
    pl = _pl()
    lf = pl.scan_parquet(str(out / "**" / "*.parquet"), hive_partitioning=True)
    rows = lf.select(pl.len()).collect().item()
    return {"rows": int(rows), "columns": len(lf.collect_schema().names()),
            "bytes": sum(f.stat().st_size for f in out.rglob("*.parquet"))}


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
