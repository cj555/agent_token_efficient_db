"""合约校验：实际 parquet 是否符合 contract.yaml（schema + 质量规则）。"""
from __future__ import annotations

import re
from typing import Any

from .config import Paths, paths
from .contract import Contract, load_contract
from . import io as dwio

# 合约类型 -> polars dtype 名的宽松匹配
_TYPE_ALIASES = {
    "string": {"String", "Utf8", "LargeString"},
    "bool": {"Boolean"},
    "date32": {"Date"},
    "binary": {"Binary"},
}


def _dtype_ok(declared: str, actual: str) -> bool:
    base = declared.split("<")[0].strip()
    if base in _TYPE_ALIASES:
        return actual in _TYPE_ALIASES[base]
    if base.startswith("timestamp"):
        return actual.startswith("Datetime")
    if base.startswith(("int", "uint", "float")):
        return actual.lower().startswith(base.lower())
    if base in {"list", "fixed_size_list"}:
        return actual.startswith(("List", "Array", "FixedSizeList"))
    return base.lower() in actual.lower()


def check(dataset: str, p: Paths | None = None, sample: int | None = None) -> dict[str, Any]:
    """返回 {dataset, ok, issues:[{level, code, msg}], rows}。"""
    p = p or paths()
    issues: list[dict] = []
    c: Contract = load_contract(dataset, p)

    if not dwio.exists(dataset, p):
        return {"dataset": dataset, "ok": None, "rows": 0,
                "issues": [{"level": "skip", "code": "no_data",
                            "msg": "尚无 curated 数据，先 `dw run`"}]}

    lf = dwio.scan(dataset, p)
    schema = lf.collect_schema()
    actual = {k: str(v) for k, v in schema.items()}
    declared = {col.name: col for col in c.columns}

    for name, col in declared.items():
        if name not in actual:
            issues.append({"level": "error", "code": "missing_column", "msg": f"缺列 {name}"})
        elif not _dtype_ok(col.type, actual[name]):
            issues.append({"level": "error", "code": "type_mismatch",
                           "msg": f"{name}: 合约 {col.type} vs 实际 {actual[name]}"})
    for name in actual:
        if name not in declared:
            issues.append({"level": "warn", "code": "extra_column",
                           "msg": f"多出未声明列 {name}（跑 `/change-contract` 补进合约）"})

    df = lf.collect()
    rows = df.height

    # grain 唯一性
    if c.grain and all(g in df.columns for g in c.grain):
        dup = rows - df.select(c.grain).unique().height
        if dup:
            issues.append({"level": "error", "code": "grain_duplicate",
                           "msg": f"grain {c.grain} 重复 {dup} 行"})

    # 列级 not_null / unique
    for col in c.columns:
        if col.name not in df.columns:
            continue
        if not col.nullable:
            n = df[col.name].null_count()
            if n:
                issues.append({"level": "error", "code": "null_violation",
                               "msg": f"{col.name} 声明非空但有 {n} 个 null"})
        if col.unique and df[col.name].n_unique() != rows:
            issues.append({"level": "error", "code": "unique_violation",
                           "msg": f"{col.name} 声明唯一但有重复"})

    issues += _quality_rules(c, df, rows)
    ok = not any(i["level"] == "error" for i in issues)
    return {"dataset": dataset, "ok": ok, "rows": rows, "issues": issues}


def _quality_rules(c: Contract, df, rows: int) -> list[dict]:
    out: list[dict] = []
    for r in c.quality:
        lvl = r.severity
        try:
            if r.rule == "row_count_between":
                if (r.min is not None and rows < r.min) or (r.max is not None and rows > r.max):
                    out.append({"level": lvl, "code": r.rule,
                                "msg": f"行数 {rows} 不在 [{r.min}, {r.max}]"})
            elif r.rule == "not_null" and r.column in df.columns:
                n = df[r.column].null_count()
                if n:
                    out.append({"level": lvl, "code": r.rule, "msg": f"{r.column} 有 {n} 个 null"})
            elif r.rule == "unique" and r.column in df.columns:
                if df[r.column].n_unique() != rows:
                    out.append({"level": lvl, "code": r.rule, "msg": f"{r.column} 不唯一"})
            elif r.rule == "accepted_values" and r.column in df.columns:
                bad = set(df[r.column].drop_nulls().unique().to_list()) - set(r.values or [])
                if bad:
                    out.append({"level": lvl, "code": r.rule,
                                "msg": f"{r.column} 出现未允许值 {sorted(bad)[:5]}"})
            elif r.rule == "value_between" and r.column in df.columns:
                s = df[r.column].drop_nulls()
                if len(s) and ((r.min is not None and s.min() < r.min)
                               or (r.max is not None and s.max() > r.max)):
                    out.append({"level": lvl, "code": r.rule,
                                "msg": f"{r.column} 范围 [{s.min()}, {s.max()}] 越界"})
            elif r.rule == "column_regex" and r.column in df.columns:
                pat = re.compile(r.pattern or ".*")
                bad = sum(1 for v in df[r.column].drop_nulls().to_list() if not pat.match(str(v)))
                if bad:
                    out.append({"level": lvl, "code": r.rule,
                                "msg": f"{r.column} 有 {bad} 行不匹配 {r.pattern}"})
            elif r.rule == "freshness_within" and r.column in df.columns:
                import datetime as dt
                from .external import parse_duration
                s = df[r.column].drop_nulls()
                if len(s):
                    newest = s.max()
                    if isinstance(newest, dt.date) and not isinstance(newest, dt.datetime):
                        newest = dt.datetime.combine(newest, dt.time())
                    age = dt.datetime.now() - newest
                    if age > parse_duration(r.window or "7d"):
                        out.append({"level": lvl, "code": r.rule,
                                    "msg": f"{r.column} 最新值已过期 {age.days} 天"})
        except Exception as e:
            out.append({"level": "warn", "code": "rule_error", "msg": f"{r.rule}: {e}"})
    return out
