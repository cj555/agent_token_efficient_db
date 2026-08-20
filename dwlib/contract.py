"""数据合约模型：datasets/<name>/contract.yaml 是唯一真源。"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .config import Paths, paths

# 合约类型 -> pyarrow 构造表达式（schema.py 生成用）
ARROW_TYPES = {
    "int8", "int16", "int32", "int64",
    "uint8", "uint16", "uint32", "uint64",
    "float32", "float64", "bool", "string", "binary",
    "date32", "timestamp[us]", "timestamp[ns]", "time64[us]",
}


class Column(BaseModel):
    name: str
    type: str = "string"
    nullable: bool = True
    unique: bool = False
    desc: str = ""
    unit: str | None = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        base = v.split("<")[0].strip()
        if base in ARROW_TYPES or base in {"decimal128", "list", "fixed_size_list", "struct", "map"}:
            return v
        raise ValueError(f"未知列类型 '{v}'（支持 {sorted(ARROW_TYPES)} 以及 list/fixed_size_list<...>）")


class QualityRule(BaseModel):
    rule: Literal[
        "row_count_between", "not_null", "unique", "accepted_values",
        "value_between", "freshness_within", "column_regex",
    ]
    column: str | None = None
    min: float | None = None
    max: float | None = None
    values: list[Any] | None = None
    pattern: str | None = None
    window: str | None = None
    severity: Literal["error", "warn"] = "error"


class Upstream(BaseModel):
    kind: Literal["dataset", "external"]
    ref: str
    note: str = ""


class SLA(BaseModel):
    freshness: str = "7d"          # 数据最大允许陈旧度
    schedule: str | None = None    # cron 表达式；None = 手动
    stage: Literal["ingest", "transform", "all"] = "all"


class ChangeLogEntry(BaseModel):
    version: str
    date: str
    kind: Literal["additive", "breaking", "fix", "init"]
    note: str


class Contract(BaseModel):
    name: str
    version: str = "0.1.0"
    owner: str = ""
    status: Literal["draft", "active", "deprecated"] = "draft"
    domain: str = ""
    family: str = ""
    tags: list[str] = Field(default_factory=list)
    purpose: str = ""                      # 自然语言，dw search 的主检索面
    grain: list[str] = Field(default_factory=list)   # 主键列，决定拆分边界
    partitions: list[str] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)
    quality: list[QualityRule] = Field(default_factory=list)
    upstream: list[Upstream] = Field(default_factory=list)
    sla: SLA = Field(default_factory=SLA)
    consumers: list[str] = Field(default_factory=list)   # 由 dw index 自动回填
    changelog: list[ChangeLogEntry] = Field(default_factory=list)

    @field_validator("columns", "quality", "upstream", "tags", "grain",
                     "partitions", "consumers", "changelog", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        """YAML 里只有注释的 `columns:` 会解析成 None —— 统一当空列表。"""
        return [] if v is None else v

    # ---- 派生属性 ----
    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def upstream_datasets(self) -> list[str]:
        return [u.ref for u in self.upstream if u.kind == "dataset"]

    @property
    def upstream_externals(self) -> list[str]:
        return [u.ref for u in self.upstream if u.kind == "external"]

    def touches_network(self) -> bool:
        return bool(self.upstream_externals)

    def summary_line(self) -> str:
        """INDEX.md 的一行。控制在 ~40 token。"""
        grain = "+".join(self.grain) or "-"
        up = ",".join(self.upstream_datasets + [f"@{e}" for e in self.upstream_externals]) or "-"
        # purpose 是多行的，压成一行且去掉 | 才不会撑坏 markdown 表格
        purpose = " ".join(self.purpose.split()).replace("|", "/")[:70]
        return (
            f"| {self.name} | {self.domain or '-'} | {grain} | {len(self.columns)} | "
            f"{self.status} | {up} | {purpose} |"
        )


def load_contract(name: str, p: Paths | None = None) -> Contract:
    p = p or paths()
    f = p.contract_file(name)
    if not f.is_file():
        raise FileNotFoundError(f"dataset '{name}' 无 contract.yaml：{p.rel(f)}")
    return parse_contract_file(f)


def parse_contract_file(f: Path) -> Contract:
    with f.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Contract.model_validate(data)


def list_datasets(p: Paths | None = None) -> list[str]:
    p = p or paths()
    if not p.datasets.is_dir():
        return []
    return sorted(
        d.name for d in p.datasets.iterdir()
        if d.is_dir() and (d / "contract.yaml").is_file()
    )


def load_all(p: Paths | None = None) -> dict[str, Contract]:
    p = p or paths()
    out: dict[str, Contract] = {}
    for n in list_datasets(p):
        try:
            out[n] = load_contract(n, p)
        except Exception as e:  # 合约损坏不应让整个 CLI 挂掉
            print(f"[warn] 合约解析失败 {n}: {e}")
    return out


def _prune(obj):
    """递归剪掉 None 值，让写出的 YAML 保持干净可读。"""
    if isinstance(obj, dict):
        return {k: _prune(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_prune(v) for v in obj]
    return obj


def dump_contract(c: Contract, f: Path) -> None:
    """写合约文件。注意：只在 dw new / dw infer --write 等**创建**场景调用；
    dw index 等日常命令绝不回写，否则会抹掉人写的注释。"""
    data = _prune(c.model_dump(mode="json", exclude={"consumers"}))
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, width=100)


def bump(version: str, kind: str) -> str:
    major, minor, patch = (list(map(int, version.split("."))) + [0, 0, 0])[:3]
    if kind == "breaking":
        return f"{major + 1}.0.0"
    if kind == "additive":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def today() -> str:
    return _dt.date.today().isoformat()
