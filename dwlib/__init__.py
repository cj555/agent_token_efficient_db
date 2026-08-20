"""dwlib —— token 高效、高度解耦的本地数据仓库工具库。

给 dataset 代码和外部消费方用的公共 API：

    import dwlib as dw
    dw.load("sec__mdna")                 # polars LazyFrame
    dw.frame("sec__mdna")                # polars DataFrame（立即物化）
    dw.arrow("sec__mdna_vectors")        # pyarrow Table
    dw.vectors("sec__mdna_vectors")      # (n, dim) numpy 矩阵
    dw.sql("select ... from a join b")   # DuckDB 跨 dataset 查询
    dw.describe("sec__mdna")             # 合约摘要（读 registry.json）
    dw.write_curated(df, "sec__mdna")    # transform 的收尾动作
"""
from .config import Paths, find_repo_root, load_config, paths
from .contract import Contract, list_datasets, load_all, load_contract
from .external import get_source, load_sources, run_health
from .io import (
    arrow, connect, curated_path, describe, exists, frame, load, run_state,
    scan, sql, vectors, write_curated,
)
from .quality import check
from .runner import dataset_config

__version__ = "0.1.0"

__all__ = [
    "Contract", "Paths",
    "arrow", "check", "connect", "curated_path", "dataset_config", "describe",
    "exists", "find_repo_root", "frame", "get_source", "list_datasets", "load",
    "load_all", "load_config", "load_contract", "load_sources", "paths",
    "run_health", "run_state", "scan", "sql", "vectors", "write_curated",
]
