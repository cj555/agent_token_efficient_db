"""脚手架生成：`dw new` / `dw new --family`。

模板由脚本渲染，LLM 不必逐字输出样板代码 —— 这是 token 效率的关键一环。
schema.py 与 tests/test_contract.py 是**生成物**，由 contract.yaml 派生，勿手改。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Paths, paths
from .contract import Contract, load_contract, today

TPL_DIR = Path(__file__).parent / "templates"

GEN_HEADER = "# GENERATED from contract.yaml by `dw index` — 请勿手工编辑\n"


def _render(tpl: str, **kw: Any) -> str:
    text = (TPL_DIR / tpl).read_text(encoding="utf-8")
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


# ---------------- 单个 dataset ----------------

def new_dataset(
    name: str,
    p: Paths | None = None,
    purpose: str = "TODO: 描述这份数据是什么、为什么存在、典型用途。",
    domain: str = "",
    owner: str = "",
    source_id: str = "",
    has_ingest: bool | None = None,
    has_transform: bool = True,
    upstream_datasets: list[str] | None = None,
    grain: list[str] | None = None,
    family: str = "",
    force: bool = False,
) -> list[str]:
    """生成一个 dataset 的全部脚手架。

    has_ingest 默认 = 是否有 source_id（触网才需要 ingest.py）。
    """
    p = p or paths()
    p.validate_name(name)
    d = p.dataset_dir(name)
    if d.exists() and not force and any(d.iterdir()):
        raise FileExistsError(f"{p.rel(d)} 已存在（加 --force 覆盖）")

    ups = list(upstream_datasets or [])
    if has_ingest is None:
        has_ingest = bool(source_id)
    family = family or (name.split("__")[0] if "__" in name else "")
    created: list[str] = []

    def write(rel: str, content: str) -> None:
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists() and not force:
            return
        f.write_text(content, encoding="utf-8", newline="\n")
        created.append(p.rel(f))

    upstream_desc = ", ".join(([f"@{source_id}"] if source_id else []) + ups) or "无（源头数据集）"

    up_lines = [f"\n- kind: external\n  ref: {source_id}"] if source_id else []
    up_lines += [f"\n- kind: dataset\n  ref: {u}" for u in ups]
    upstream_block = "".join(up_lines) or (
        "\n  # - kind: external     # 触网，会被 dw health / fix-source 监控"
        "\n  #   ref: some_source_id"
        "\n  # - kind: dataset"
        "\n  #   ref: some_upstream_dataset"
    )
    write("contract.yaml", _render(
        "contract.yaml.tmpl", name=name, owner=owner, domain=domain, family=family,
        purpose=(purpose or "TODO").strip(), grain=list(grain or []),
        upstream_block=upstream_block,
        source_id=source_id or "TODO_source_id", date=today(),
    ))
    write("config.yaml", _render(
        "config.yaml.tmpl", name=name, source_id=source_id,
        has_ingest=str(bool(has_ingest)).lower(),
        has_transform=str(bool(has_transform)).lower(),
    ))
    write("README.md", _render(
        "README.md.tmpl", name=name, purpose=(purpose or "TODO").strip(),
        upstream_desc=upstream_desc))

    if has_ingest:
        write("ingest.py", _render(
            "ingest.py.tmpl", name=name, source_id=source_id or "TODO_source_id"))
    if has_transform:
        if ups:
            lines = [f'    lf = dw.load("{ups[0]}")']
            lines += [f'    lf_{u} = dw.load("{u}")' for u in ups[1:]]
            reads = "\n".join(lines)
            src_desc = " + ".join(ups)
        else:
            sid = source_id or "TODO_source_id"
            reads = (f'    raw_dir = dw.paths().raw("{sid}")\n'
                     f'    lf = pl.scan_parquet(str(raw_dir / "*.parquet"))')
            src_desc = f"raw/{sid}"
        write("transform.py", _render(
            "transform.py.tmpl", name=name, read_upstream=reads, transform_input=src_desc))

    write("tests/test_logic.py", _render("test_logic.py.tmpl", name=name))
    write("_meta/.gitkeep", "")

    created += regenerate(load_contract(name, p), p)
    return created


# ---------------- 生成物：schema.py / test_contract.py ----------------

_ARROW_MAP = {
    "int8": "pa.int8()", "int16": "pa.int16()", "int32": "pa.int32()", "int64": "pa.int64()",
    "uint8": "pa.uint8()", "uint16": "pa.uint16()", "uint32": "pa.uint32()",
    "uint64": "pa.uint64()", "float32": "pa.float32()", "float64": "pa.float64()",
    "bool": "pa.bool_()", "string": "pa.string()", "binary": "pa.binary()",
    "date32": "pa.date32()", "timestamp[us]": 'pa.timestamp("us")',
    "timestamp[ns]": 'pa.timestamp("ns")', "time64[us]": 'pa.time64("us")',
}


def arrow_expr(t: str) -> str:
    t = t.strip()
    if t in _ARROW_MAP:
        return _ARROW_MAP[t]
    if t.startswith("fixed_size_list<"):
        inner, _, size = t[len("fixed_size_list<"):-1].rpartition(",")
        return f"pa.list_({arrow_expr(inner.strip())}, {int(size)})"
    if t.startswith("list<"):
        return f"pa.list_({arrow_expr(t[5:-1].strip())})"
    if t.startswith("decimal128<"):
        prec, _, scale = t[len("decimal128<"):-1].partition(",")
        return f"pa.decimal128({int(prec)}, {int(scale)})"
    return "pa.string()"


def gen_schema_py(c: Contract) -> str:
    fields = "\n".join(
        f'    pa.field("{col.name}", {arrow_expr(col.type)}, nullable={col.nullable}),'
        + (f"  # {col.desc}" if col.desc else "")
        for col in c.columns
    )
    doc = f'"""{c.name} 的 pyarrow schema —— 类型真源，由 contract.yaml 派生。"""'
    return (
        f"{GEN_HEADER}{doc}\n"
        "import pyarrow as pa\n\n"
        f'DATASET = "{c.name}"\n'
        f'VERSION = "{c.version}"\n'
        f"GRAIN = {c.grain!r}\n"
        f"PARTITIONS = {c.partitions!r}\n\n"
        "SCHEMA = pa.schema([\n"
        f"{fields}\n"
        "])\n\n"
        "COLUMNS = [f.name for f in SCHEMA]\n"
    )


def gen_test_contract_py(c: Contract) -> str:
    doc = f'"""{c.name} 的合约一致性测试 —— 由 contract.yaml 生成，勿手改。"""'
    body = '''import pytest

import dwlib as dw

DATASET = "%s"

pytestmark = pytest.mark.skipif(
    not dw.exists(DATASET), reason=f"{DATASET} 尚无 curated 数据"
)


def test_contract_conformance():
    """列/类型/非空/唯一/grain/质量规则，全部按合约执行。"""
    res = dw.check(DATASET)
    errors = [i for i in res["issues"] if i["level"] == "error"]
    assert not errors, "\\n".join(f'{i["code"]}: {i["msg"]}' for i in errors)
''' % c.name
    return f"{GEN_HEADER}{doc}\n{body}"


def regenerate(c: Contract, p: Paths | None = None) -> list[str]:
    """重生成 schema.py 与 tests/test_contract.py（内容不变则不写盘）。"""
    p = p or paths()
    d = p.dataset_dir(c.name)
    out: list[str] = []
    for rel, content in (
        ("schema.py", gen_schema_py(c)),
        ("tests/test_contract.py", gen_test_contract_py(c)),
    ):
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists() or f.read_text(encoding="utf-8") != content:
            f.write_text(content, encoding="utf-8", newline="\n")
            out.append(p.rel(f))
    return out


def regenerate_all(p: Paths | None = None) -> list[str]:
    from .contract import load_all
    p = p or paths()
    out: list[str] = []
    for c in load_all(p).values():
        out += regenerate(c, p)
    return out


# ---------------- family ----------------

FAMILY_SPEC_EXAMPLE = """# dataset family 规划表 —— create-dataset / migrate-dataset 的审批对象。
# 拆分四问：1) grain 变了吗 2) 重算触发/成本差异大吗 3) 总是一起重算+一起被消费吗
#          4) 产物是 blob 不是表吗（blob 用 manifest 表索引）
family: sec
owner: ""
domain: filings
datasets:
  - name: sec__filings
    purpose: SEC 10-K/10-Q 原文清单（manifest 表），原文落 storage/blob/sec_edgar/
    grain: [accession]
    source_id: sec_edgar        # 有 source_id = 触网 = 生成 ingest.py
    upstream: []
  - name: sec__mdna
    purpose: 从原文抽取的 MD&A 段落
    grain: [accession]
    upstream: [sec__filings]    # 纯内部派生 = 只生成 transform.py
"""


def new_family(spec_path: Path, p: Paths | None = None, force: bool = False) -> dict:
    p = p or paths()
    spec = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")) or {}
    family = spec.get("family", "")
    created: dict[str, list[str]] = {}
    skipped: list[str] = []

    for item in spec.get("datasets", []):
        name = item["name"]
        ups = list(item.get("upstream", []) or [])
        sid = item.get("source_id", "") or ""
        if p.dataset_dir(name).exists() and any(p.dataset_dir(name).iterdir()) and not force:
            # 断点续做：已存在的 dataset 直接跳过，不打断整族的生成
            created[name] = []
            skipped.append(name)
            continue
        files = new_dataset(
            name, p,
            purpose=item.get("purpose", ""),
            domain=item.get("domain", spec.get("domain", "")),
            owner=item.get("owner", spec.get("owner", "")),
            source_id=sid,
            has_ingest=bool(sid),
            has_transform=item.get("has_transform", True),
            upstream_datasets=ups,
            grain=item.get("grain") or [],
            family=family,
            force=force,
        )
        files += regenerate(load_contract(name, p), p)
        created[name] = sorted(dict.fromkeys(files))

    _save_plan(spec, family, created, p)
    if skipped:
        created["_skipped_existing"] = skipped
    return created


def _save_plan(spec: dict, family: str, created: dict, p: Paths) -> None:
    """family 计划落盘，支持断点续做（context 断了也能接着干）。"""
    p.plans_dir.mkdir(parents=True, exist_ok=True)
    f = p.plans_dir / f"{family or 'adhoc'}.yaml"
    state = yaml.safe_load(f.read_text(encoding="utf-8")) if f.is_file() else {}
    state = state or {}
    state["family"] = family
    state["spec"] = spec
    status = state.get("status", {})
    for name in created:
        status.setdefault(name, "scaffolded")
    state["status"] = status
    f.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_plan(family: str, p: Paths | None = None) -> dict:
    p = p or paths()
    f = p.plans_dir / f"{family}.yaml"
    return yaml.safe_load(f.read_text(encoding="utf-8")) if f.is_file() else {}


def set_plan_status(family: str, dataset: str, status: str, p: Paths | None = None) -> None:
    p = p or paths()
    f = p.plans_dir / f"{family}.yaml"
    if not f.is_file():
        return
    state = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    state.setdefault("status", {})[dataset] = status
    f.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
