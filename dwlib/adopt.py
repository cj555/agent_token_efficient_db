"""`dw infer` / `dw adopt` —— 迁移老项目时的两把快刀。

infer：从既有 parquet/JSON/CSV 反推 contract 草案（省掉逐列人肉推断类型）。
adopt：校验 schema 后把既有数据直接纳管进 curated（避免重跑昂贵的下载/向量计算）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import Paths, paths
from .contract import Contract, Column, dump_contract, load_contract, today

# polars dtype -> 合约类型
_PL_TO_CONTRACT = {
    "Int8": "int8", "Int16": "int16", "Int32": "int32", "Int64": "int64",
    "UInt8": "uint8", "UInt16": "uint16", "UInt32": "uint32", "UInt64": "uint64",
    "Float32": "float32", "Float64": "float64", "Boolean": "bool",
    "String": "string", "Utf8": "string", "Binary": "binary", "Date": "date32",
}


def _contract_type(dtype) -> str:
    s = str(dtype)
    if s in _PL_TO_CONTRACT:
        return _PL_TO_CONTRACT[s]
    if s.startswith("Datetime"):
        return "timestamp[us]"
    if s.startswith("Array"):
        inner = getattr(dtype, "inner", None)
        size = getattr(dtype, "size", None) or getattr(dtype, "width", None)
        if inner is not None and size:
            return f"fixed_size_list<{_contract_type(inner)},{size}>"
    if s.startswith("List"):
        inner = getattr(dtype, "inner", None)
        return f"list<{_contract_type(inner)}>" if inner is not None else "list<string>"
    return "string"


def scan_any(path: Path):
    """按后缀惰性读取 parquet / json / ndjson / csv。"""
    import polars as pl

    path = Path(path)
    if path.is_dir():
        for pat, fn in (("**/*.parquet", pl.scan_parquet),
                        ("**/*.jsonl", pl.scan_ndjson),
                        ("**/*.csv", pl.scan_csv)):
            if any(path.glob(pat)):
                return fn(str(path / pat))
        raise FileNotFoundError(f"{path} 下没有 parquet/jsonl/csv")
    suf = path.suffix.lower()
    if suf == ".parquet":
        return pl.scan_parquet(str(path))
    if suf in (".jsonl", ".ndjson"):
        return pl.scan_ndjson(str(path))
    if suf == ".json":
        return pl.read_json(str(path)).lazy()
    if suf in (".csv", ".tsv"):
        return pl.scan_csv(str(path), separator="\t" if suf == ".tsv" else ",")
    raise ValueError(f"不支持的格式: {path.name}")


def infer(path: Path, name: str = "", sample_rows: int = 5000) -> Contract:
    """从数据文件反推合约草案（不写盘，由调用方决定）。"""
    lf = scan_any(Path(path))
    schema = lf.collect_schema()
    df = lf.head(sample_rows).collect()

    cols: list[Column] = []
    for cname, dtype in schema.items():
        nulls = df[cname].null_count() if cname in df.columns else 0
        uniq = False
        try:
            uniq = df.height > 0 and df[cname].n_unique() == df.height
        except Exception:
            pass
        cols.append(Column(
            name=cname, type=_contract_type(dtype),
            nullable=bool(nulls) or df.height == 0, unique=uniq, desc="",
        ))

    # 猜 grain：优先取唯一且非空的第一列
    grain = [c.name for c in cols if c.unique and not c.nullable][:1]
    c = Contract(
        name=name or Path(path).stem,
        purpose=f"（由 dw infer 从 {Path(path).name} 反推，请补充业务描述）",
        grain=grain,
        columns=cols,
        status="draft",
    )
    c.changelog = [{"version": "0.1.0", "date": today(), "kind": "init",
                    "note": f"dw infer 自 {path}"}]  # type: ignore[list-item]
    return c


def infer_report(path: Path, name: str = "") -> dict:
    """给 CLI 用的紧凑摘要，避免整份 YAML 进 context。"""
    c = infer(path, name)
    return {
        "name": c.name,
        "columns": [(col.name, col.type, "null" if col.nullable else "notnull") for col in c.columns],
        "grain_guess": c.grain,
        "n_columns": len(c.columns),
    }


def adopt(dataset: str, src: Path, p: Paths | None = None,
          mode: str = "copy", strict: bool = True) -> dict:
    """把既有数据纳管进 storage/curated/<dataset>/。

    mode: copy（默认，保留原件）| move | link（不动原件，仅校验）
    strict: True 时 schema 与合约不符即拒绝纳管。
    """
    p = p or paths()
    src = Path(src)
    c = load_contract(dataset, p)

    lf = scan_any(src)
    actual = {k: _contract_type(v) for k, v in lf.collect_schema().items()}
    declared = {col.name: col.type for col in c.columns}

    issues = []
    for n, t in declared.items():
        if n not in actual:
            issues.append(f"缺列 {n}")
        elif actual[n] != t:
            issues.append(f"{n}: 合约 {t} vs 实际 {actual[n]}")
    extra = [n for n in actual if n not in declared]

    if issues and strict:
        return {"ok": False, "adopted": False, "issues": issues, "extra_columns": extra,
                "hint": "先用 `dw infer` 校准合约，或加 --no-strict 强行纳管"}

    out = p.curated(dataset)
    out.mkdir(parents=True, exist_ok=True)
    copied = 0
    if mode == "link":
        pass
    else:
        files = [src] if src.is_file() else sorted(src.rglob("*.parquet"))
        if src.is_file() and src.suffix != ".parquet":
            # 非 parquet 源：转换后落地
            df = lf.collect()
            df.write_parquet(out / "part-00000.parquet",
                             compression=p.cfg.get("defaults", {}).get("compression", "zstd"))
            copied = 1
        else:
            for i, f in enumerate(files):
                dst = out / f"part-{i:05d}.parquet"
                shutil.move(str(f), dst) if mode == "move" else shutil.copy2(f, dst)
                copied += 1

    rows = int(lf.select(__import__("polars").len()).collect().item()) if copied else 0
    return {"ok": True, "adopted": True, "files": copied, "rows": rows,
            "path": p.rel(out), "issues": issues, "extra_columns": extra}


def write_inferred_contract(dataset: str, path: Path, p: Paths | None = None,
                            merge: bool = True) -> str:
    """把 infer 结果写进 datasets/<ds>/contract.yaml（保留已有的 purpose/owner 等人写字段）。"""
    p = p or paths()
    new = infer(path, dataset)
    f = p.contract_file(dataset)
    if merge and f.is_file():
        old = load_contract(dataset, p)
        old.columns = new.columns
        if not old.grain:
            old.grain = new.grain
        dump_contract(old, f)
    else:
        dump_contract(new, f)
    return p.rel(f)
