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


_PREVIEW_CELL_MAX = 60


def _latest_part_dir(root: Path) -> Path | None:
    """挑「最新」的那个分区目录。

    分区目录名是 year=2024 / date=2024-01-01 这种零填充形式，字典序即时间序，
    所以直接取排序后的最后一个。非分区表只有 root 自己，行为一致。
    """
    dirs = {f.parent for f in root.rglob("*.parquet")}
    return max(sorted(dirs), default=None)


def preview(dataset: str, n: int = 5, max_cols: int = 8,
            p: Paths | None = None) -> dict:
    """取 curated 表最新分区末尾 n 行，做成可直接渲染的片段。

    只扫最新一个分区、惰性 tail，不做全表 sort —— 面板要的是「看一眼数据长啥样」，
    不值得为它把大表拉进内存（见 CLAUDE.md 内存纪律）。
    行序即写入序，所以措辞是「最新分区末尾 N 行」，不承诺按时间排序。
    """
    pl = _pl()
    pp = p or paths()
    root = curated_path(dataset, pp)
    if not exists(dataset, pp):
        return {"error": f"尚无 curated 数据（先跑 `dw run {dataset}`）"}
    part = _latest_part_dir(root)
    if part is None:
        return {"error": f"尚无 curated 数据（先跑 `dw run {dataset}`）"}

    kw: dict[str, Any] = {"hive_partitioning": True}
    hs = _hive_schema(dataset, pp)
    if hs:
        kw["hive_schema"] = hs
    lf = pl.scan_parquet(str(part / "*.parquet"), **kw)
    names = lf.collect_schema().names()
    cols = names[:max_cols]
    df = lf.select(cols).tail(n).collect()

    def cell(v: Any) -> str:
        if v is None:
            return "—"
        s = str(v)
        return s[:_PREVIEW_CELL_MAX] + "…" if len(s) > _PREVIEW_CELL_MAX else s

    rel = part.relative_to(root).as_posix()
    return {
        "columns": cols,
        "rows": [[cell(v) for v in row] for row in df.rows()],
        "total_columns": len(names),
        "truncated_cols": len(names) > len(cols),
        "partition": rel if rel != "." else "",
        "error": None,
    }


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


def _compute_watermark(out: Path, col: str) -> str | None:
    """扫 curated 目录取该列 max()，转成 ISO 字符串（date/timestamp/string 都行）。"""
    pl = _pl()
    v = (pl.scan_parquet(str(out / "**" / "*.parquet"), hive_partitioning=True)
           .select(pl.col(col).max()).collect().item())
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def write_curated(
    df,
    dataset: str,
    partition_by: list[str] | None = None,
    p: Paths | None = None,
    mode: str = "overwrite",
    watermark_col: str | None = None,
) -> dict[str, Any]:
    """把 polars DataFrame/LazyFrame 写入 curated 层。transform.py 的收尾动作。

    mode: overwrite（默认，先清空目录）| append

    **传 LazyFrame 时走流式落盘（sink_parquet），不会把整表拉进内存。**
    这是本仓库控制内存峰值的主要手段：2500 万行的表用 collect() 要几个 GB，
    用 sink 只占几百 MB。所以 transform.py 的 build() 请返回 LazyFrame，
    不要自己先 .collect()。传 DataFrame 仍然可以（已经在内存里了，直接写）。

    watermark_col: 传了就在落盘后扫这一列的 max()，存进 run_state.json 的
    watermark 字段（服务增量更新，见 dw.watermark()）。不传 = 不动这个字段。
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
    wm = _compute_watermark(out, watermark_col) if watermark_col else None
    _write_run_state(dataset, stats, pp, watermark=wm)
    return stats


def write_curated_chunks(
    chunks,
    dataset: str,
    partition_col: str,
    p: Paths | None = None,
    skip_empty: bool = False,
    wipe: str = "all",
    watermark_col: str | None = None,
) -> dict[str, Any]:
    """逐块流式落盘：内存峰值 = **单块**大小，而不是整表大小。

    chunks: 可迭代的 (分区值, LazyFrame) 二元组，或 (分区值, key, LazyFrame) 三元组。
            二元组每块单独 sink 成 <curated>/<partition_col>=<值>/part-NNNNN.parquet
            （NNNNN 是本次调用内的序号）。三元组按 key 落盘成
            <curated>/<partition_col>=<值>/part-key-<key>.parquet，见下方 wipe="touched"
            + key 的说明。

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

    wipe: "all"（默认，行为不变）先 `shutil.rmtree(out)` 整表清空再重写；
          "touched" 不清空整个 out。不带 key 的二元组：**本次 chunks 里第一次
          出现**该 partition_col=value 时把该分区目录整个删了重写（避免重跑
          同一次调用残留旧 part 文件），其余没出现在本次 chunks 里的分区目录
          原样不动。带 key 的三元组：**不做任何目录级清空**，直接按 key 覆盖写
          同名文件 —— 见下方专门说明。

    key（三元组时给）：把"这一份数据要写到哪个文件"从"本次调用的第几个块"
    改成"调用方指定的稳定身份"（比如某一天：`key=str(the_date)`）。用途是
    **跨多次独立调用（甚至跨多次独立进程）安全累积同一个分区**：历史回补
    通常要分很多次运行（比如每次预算 30 天）才能补完一年，每次运行都是一次
    独立的 write_curated_chunks 调用；如果沿用"本次调用第一次触碰该分区就
    整个删掉重写"的语义，后一次回补运行会把前一次运行已经写好的日子全部
    冲掉。key 从根源上避开这个问题：文件名只取决于 key 本身，不取决于"这是
    本次调用第几次见到这个分区值"——同一个 key 被写第二次就是覆盖它自己
    那一个文件（幂等：重跑同一天不留陈旧碎片），不同 key 之间永远是各写各的
    文件、互不清空（不同天的回补写入无论隔了多久、来自多少次不同的运行，
    都能安全共存在同一个 year=YYYY/ 目录下）。二元组的旧行为（含 wipe="touched"
    的整目录清空）完全不受影响 —— 02 交付以来没有任何生产代码用过二元组的
    wipe="touched"，这次只是新增 key 这条路径，不改旧路径的语义。

    watermark_col: 传了就在落盘后扫这一列的 max()，存进 run_state.json（见
    write_curated 同名参数）。
    """
    import re
    import shutil

    pl = _pl()
    pp = p or paths()
    out = pp.curated(dataset)
    comp = pp.cfg.get("defaults", {}).get("compression", "zstd")
    row_group = pp.cfg.get("defaults", {}).get("row_group_size")

    if wipe == "all":
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)
    elif wipe == "touched":
        out.mkdir(parents=True, exist_ok=True)
    else:
        raise ValueError(f"unknown wipe mode: {wipe!r}")

    kw: dict[str, Any] = {"compression": comp, "engine": "streaming", "mkdir": True}
    if row_group:
        kw["row_group_size"] = int(row_group)

    n_chunks = 0
    seen: dict[Any, int] = {}
    for item in chunks:
        if len(item) == 3:
            value, key, lf = item
        else:
            value, lf = item
            key = None
        part_dir = out / f"{partition_col}={value}"
        if key is not None:
            safe_key = re.sub(r"[^A-Za-z0-9._-]", "_", str(key))
            target = part_dir / f"part-key-{safe_key}.parquet"
        else:
            i = seen.get(value, 0)
            if wipe == "touched" and i == 0 and part_dir.exists():
                shutil.rmtree(part_dir)
            seen[value] = i + 1
            target = part_dir / f"part-{i:05d}.parquet"
        # 分区列**保留在文件里**：只靠 hive 目录名的话 polars 一律推成 Int64，
        # 合约里声明的 int32 就对不上了。文件里有值时 scan 以文件为准。
        lf.sink_parquet(target, **kw)
        if skip_empty and _parquet_rows(target) == 0:
            # 细粒度切块常会切出空块（比如数据起始年的前几个季度），
            # 落个空文件没坏处但碍眼，顺手删掉；非 key 模式把序号让出来
            target.unlink()
            if key is None:
                seen[value] = i
            if part_dir.is_dir() and not any(part_dir.iterdir()):
                part_dir.rmdir()
            continue
        n_chunks += 1

    if n_chunks == 0:
        raise ValueError(f"{dataset}: chunks 为空，没有任何数据可写")

    stats = {"dataset": dataset, **_curated_stats(out, pp),
             "path": pp.rel(out), "chunks": n_chunks}
    wm = _compute_watermark(out, watermark_col) if watermark_col else None
    _write_run_state(dataset, stats, pp, watermark=wm)
    return stats


def bucket_by_key(lf, keys, n_buckets: int, tmp_dir, row_group_size: int = 8192):
    """把一个 LazyFrame 按主键 hash **物理重分区**到 tmp_dir，返回逐桶的 LazyFrame。

    为什么必须真的落盘，而不是直接 lf.filter(hash % n == b)：
      「取每个 key 最新的整行」这类归并要写成「先在窄表上求 max/min，再 join 回
      宽表挑那一行」（否则 group_by().agg(pl.exclude(...).last()) 会为每个列各建
      一遍分组列表，pm__market 实测 26 GB）。但这么写 lf 会出现在 join 的**两侧**，
      而 hash % n 这种谓词下推不进 parquet 扫描 —— polars 于是为每个桶把整个上游
      重新物化一遍，桶数开到多少都没用。pm__market 实测：
        filter+sink 单桶 0.58 GB / 同一个桶做 join 5.72 GB
        先把桶落盘、再在小文件上跑同样的归并 0.84 GB
    所以这一步的意义不是「过滤」，是**把上游截断**：后面每个桶读的是自己那份小
    parquet，join 两侧都只有 1/N 的数据。

    row_group_size 默认调小到 8192：宽字符串表用全仓默认的 131072 时，
    单个 row group 缓冲就是好几百 MB，乘上线程数直接打穿预算
    （pm__market 实测 scan+sink：131072 → 3.92 GB，8192 → 1.89 GB）。
    """
    import shutil
    import warnings

    pl = _pl()
    tmp_dir = Path(tmp_dir)
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    col = "_dw_bucket"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")     # PartitionBy 标着 unstable，落盘格式是标准 parquet
        (lf.with_columns((pl.struct(list(keys)).hash(seed=0) % n_buckets).alias(col))
           .sink_parquet(pl.PartitionBy(tmp_dir, key=[col], include_key=False),
                         engine="streaming", compression="zstd",
                         row_group_size=int(row_group_size), mkdir=True))

    # 空桶不会产生目录（键分布不均时很常见），按实际存在的目录返回
    return [pl.scan_parquet(d / "**/*.parquet")
            for d in sorted(tmp_dir.glob(f"{col}=*")) if any(d.rglob("*.parquet"))]


def write_curated_parts(
    chunks,
    dataset: str,
    p: Paths | None = None,
    skip_empty: bool = True,
    row_group_size: int | None = None,
) -> dict[str, Any]:
    """逐块流式落盘到**扁平** part 文件：内存峰值 = 单块大小，且不引入分区列。

    chunks: 可迭代的 LazyFrame。每块单独 sink 成 <curated>/part-NNNNN.parquet。

    与 write_curated_chunks 的区别只在落盘布局：那个按 <col>=<值>/ 建 hive 目录，
    适合本来就要分区的表；这个适合 **partition_by 为空、但整表 sort/group_by
    吃不下**的表 —— 典型做法是按主键 hash 分桶，同一主键必落同一块，
    所以逐块去重 == 全表去重，结果精确等价而不是近似。

    代价：块与块之间没有全局顺序（块内该怎么排还怎么排）。grain 唯一性不受影响，
    dw.load() / dw.sql() 也都不依赖文件顺序。

    为什么不用 write_curated(mode="append") 逐块追加：那样每块都会重跑一遍
    _curated_stats（对整个目录 scan_parquet(pl.len())）和 _write_run_state，
    32 块就是 32 次多余的元数据扫描。这里只在最后统计一次。
    """
    import shutil

    pp = p or paths()
    out = pp.curated(dataset)
    comp = pp.cfg.get("defaults", {}).get("compression", "zstd")
    row_group = row_group_size or pp.cfg.get("defaults", {}).get("row_group_size")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    kw: dict[str, Any] = {"compression": comp, "engine": "streaming", "mkdir": True}
    if row_group:
        kw["row_group_size"] = int(row_group)

    n_parts = 0
    for lf in chunks:
        target = out / f"part-{n_parts:05d}.parquet"
        lf.sink_parquet(target, **kw)
        if skip_empty and _parquet_rows(target) == 0:
            # 分桶常会切出空块（键分布不均、或整块都被判据滤掉），
            # 落个空文件没坏处但碍眼，顺手删掉并把序号让出来
            target.unlink()
            continue
        n_parts += 1

    if n_parts == 0:
        raise ValueError(f"{dataset}: chunks 为空，没有任何数据可写")

    stats = {"dataset": dataset, **_curated_stats(out, pp),
             "path": pp.rel(out), "chunks": n_parts}
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


def _write_run_state(dataset: str, stats: dict, p: Paths, watermark: str | None = None) -> None:
    import datetime as dt
    meta = p.dataset_dir(dataset) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    state_f = meta / "run_state.json"
    state = json.loads(state_f.read_text(encoding="utf-8")) if state_f.is_file() else {}
    state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
    state["rows"] = stats["rows"]
    state["bytes"] = stats["bytes"]
    if watermark is not None:      # None = 调用方没传 watermark_col，保留已有值不动
        state["watermark"] = watermark
    state_f.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def run_state(dataset: str, p: Paths | None = None) -> dict:
    f = (p or paths()).dataset_dir(dataset) / "_meta" / "run_state.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def watermark(dataset: str, p: Paths | None = None):
    """读 _meta/run_state.json 里的水位线（date）。没有就返回 None ——
    可能是数据源没声明 watermark_col，也可能是从未成功写过一次（新建 dataset）。
    调用方按 README 的框架规则处理：`start = dw.watermark(ds) or date.today()`。
    """
    import datetime as dt
    wm = run_state(dataset, p).get("watermark")
    if not wm:
        return None
    try:
        return dt.date.fromisoformat(wm[:10])
    except ValueError:
        return None


def describe(dataset: str, p: Paths | None = None) -> dict:
    """从 registry.json 读合约摘要（不解析 YAML，快且省）。"""
    from .registry import load_registry
    reg = load_registry(p)
    if dataset not in reg["datasets"]:
        raise KeyError(f"registry 中无 '{dataset}'，先跑 `dw index`")
    return reg["datasets"][dataset]
