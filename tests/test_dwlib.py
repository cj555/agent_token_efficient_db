"""dwlib 核心逻辑的单元测试（不依赖网络，不依赖示例数据）。"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import polars as pl
import pytest
import yaml

from dwlib import graph as G
from dwlib.adopt import adopt, infer
from dwlib.config import Paths, find_repo_root, load_dotenv
from dwlib.contract import Contract, bump, load_contract, parse_contract_file
from dwlib.dashboard import render
from dwlib.external import (
    _dig, expand_env, html_to_text, json_key_paths, parse_duration, schema_hash,
)
from dwlib.health import (
    ack_for, ack_load, ack_record, attempts_clear, attempts_load, attempts_record,
    dataset_freshness,
)
from dwlib.quality import check
from dwlib.registry import reindex
from dwlib.schedule import parse_cron, set_sla
from dwlib.remove import plan as rm_plan
from dwlib.scaffold import arrow_expr, gen_schema_py, new_dataset, new_family


@pytest.fixture
def repo(tmp_path: Path) -> Paths:
    """搭一个最小可用的临时仓库。"""
    (tmp_path / "warehouse.yaml").write_text(
        yaml.safe_dump({
            "version": 1,
            "storage": {"root": "storage", "raw": "raw", "blob": "blob",
                        "curated": "curated", "tmp": "tmp"},
            "naming": {"pattern": "^[a-z][a-z0-9_]*(__[a-z][a-z0-9_]*)?$"},
            "defaults": {"compression": "zstd"},
        }), encoding="utf-8")
    (tmp_path / "datasets").mkdir()
    (tmp_path / "data_contracts").mkdir()
    return Paths(tmp_path)


def _write_curated(p: Paths, name: str, df: pl.DataFrame) -> None:
    d = p.curated(name)
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / "part-00000.parquet")


# ---------------- 脚手架与生成物 ----------------

def test_new_dataset_creates_expected_files(repo: Paths):
    files = new_dataset("demo__a", repo, purpose="测试用", source_id="src1")
    names = {f.split("/")[-1] for f in files}
    assert {"contract.yaml", "config.yaml", "ingest.py", "transform.py",
            "schema.py", "README.md"} <= names
    c = load_contract("demo__a", repo)
    assert c.family == "demo"
    assert c.upstream_externals == ["src1"]


def test_no_ingest_when_no_source(repo: Paths):
    """纯内部派生 dataset 不该生成 ingest.py —— 不生成空壳是设计要求。"""
    new_dataset("demo__a", repo, source_id="src1")
    new_dataset("demo__b", repo, upstream_datasets=["demo__a"])
    assert not (repo.dataset_dir("demo__b") / "ingest.py").exists()
    assert (repo.dataset_dir("demo__b") / "transform.py").exists()
    assert load_contract("demo__b", repo).upstream_datasets == ["demo__a"]


def test_contract_template_keeps_comments(repo: Paths):
    """合约是人写人读的真源，脚手架必须保留注释。"""
    new_dataset("demo__a", repo, source_id="src1")
    text = repo.contract_file("demo__a").read_text(encoding="utf-8")
    assert "# 数据合约" in text
    assert "grain 变了就该拆新 dataset" in text


def test_scaffold_has_no_tests_init(repo: Paths):
    """dataset 的 tests/ 不能有 __init__.py —— 多个 dataset 同名包会让 pytest 收集冲突。"""
    new_dataset("demo__a", repo, source_id="s")
    assert not (repo.dataset_dir("demo__a") / "tests" / "__init__.py").exists()


def test_name_validation(repo: Paths):
    with pytest.raises(ValueError):
        new_dataset("BadName", repo)


def test_family_scaffold_and_resume(repo: Paths, tmp_path: Path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump({
        "family": "fam",
        "datasets": [
            {"name": "fam__a", "grain": ["id"], "source_id": "s1", "purpose": "上游"},
            {"name": "fam__b", "grain": ["id"], "upstream": ["fam__a"], "purpose": "派生"},
        ],
    }), encoding="utf-8")
    created = new_family(spec, repo)
    assert set(created) == {"fam__a", "fam__b"}
    assert load_contract("fam__a", repo).grain == ["id"]

    # 断点续做：再跑一次应跳过已存在的，而不是报错
    again = new_family(spec, repo)
    assert again.get("_skipped_existing") == ["fam__a", "fam__b"]
    assert (repo.plans_dir / "fam.yaml").is_file()


def test_arrow_expr_handles_vectors():
    assert arrow_expr("fixed_size_list<float32,768>") == "pa.list_(pa.float32(), 768)"
    assert arrow_expr("int64") == "pa.int64()"
    assert arrow_expr("list<string>") == "pa.list_(pa.string())"


def test_gen_schema_py_is_valid_python():
    c = Contract(name="x", columns=[
        {"name": "id", "type": "string", "nullable": False},
        {"name": "vec", "type": "fixed_size_list<float32,4>", "nullable": False},
    ])
    src = gen_schema_py(c)
    ns: dict = {}
    exec(compile(src, "schema.py", "exec"), ns)
    assert [f.name for f in ns["SCHEMA"]] == ["id", "vec"]
    assert ns["SCHEMA"].field("vec").type.list_size == 4


# ---------------- 合约 ----------------

def test_contract_none_lists_are_tolerated(tmp_path: Path):
    """模板里只有注释的 columns: 会被 YAML 解析成 None，不能炸。"""
    f = tmp_path / "c.yaml"
    f.write_text("name: x\ncolumns:\nquality:\nupstream:\n", encoding="utf-8")
    c = parse_contract_file(f)
    assert c.columns == [] and c.quality == [] and c.upstream == []


def test_bump():
    assert bump("1.2.3", "breaking") == "2.0.0"
    assert bump("1.2.3", "additive") == "1.3.0"
    assert bump("1.2.3", "fix") == "1.2.4"


def test_unknown_column_type_rejected():
    with pytest.raises(Exception):
        Contract(name="x", columns=[{"name": "a", "type": "nosuchtype"}])


# ---------------- 依赖图与索引 ----------------

def test_graph_closure_and_topo(repo: Paths):
    new_dataset("a", repo, source_id="s")
    new_dataset("b", repo, upstream_datasets=["a"])
    new_dataset("c", repo, upstream_datasets=["b"])
    g = G.build(repo)
    assert [n for n, _ in G.closure(g, "a", "down")] == ["b", "c"]
    assert [n for n, _ in G.closure(g, "c", "up")] == ["b", "a"]
    assert G.topo(g) == ["a", "b", "c"]
    assert G.closure(g, "a", "down", depth=1) == [("b", 1)]


def test_refs_scan_finds_dw_load(repo: Paths):
    new_dataset("a", repo, source_id="s")
    new_dataset("b", repo, upstream_datasets=["a"])
    g = G.build(repo)
    refs = g["refs"].get("a", [])
    assert any(r["file"].endswith("b/transform.py") and r["in_dataset"] == "b" for r in refs)


def test_reindex_writes_generated_files_and_consumers(repo: Paths):
    new_dataset("a", repo, source_id="s")
    new_dataset("b", repo, upstream_datasets=["a"])
    st = reindex(repo)
    assert st["datasets"] == 2
    assert repo.index_md.is_file() and repo.registry_json.is_file()
    g = json.loads(repo.graph_json.read_text(encoding="utf-8"))
    assert g["nodes"]["a"]["consumers"] == ["b"]
    # consumers 是派生字段，绝不能被写回合约文件
    assert "consumers" not in repo.contract_file("a").read_text(encoding="utf-8")


def test_reindex_reports_dangling_upstream(repo: Paths):
    new_dataset("b", repo, upstream_datasets=["ghost"])
    assert reindex(repo)["dangling"] == ["ghost"]


# ---------------- 质量校验 ----------------

def _set_contract(repo: Paths, name: str, **contract_kw) -> None:
    """按字段覆盖合约并写盘（经过 pydantic 校验，避免塞裸 dict）。"""
    from dwlib.contract import dump_contract
    data = load_contract(name, repo).model_dump()
    data.update(contract_kw)
    dump_contract(Contract.model_validate(data), repo.contract_file(name))


def _dataset_with_data(repo: Paths, name: str, df: pl.DataFrame, **contract_kw) -> None:
    new_dataset(name, repo, source_id="s")
    _set_contract(repo, name, **contract_kw)
    _write_curated(repo, name, df)


def test_check_detects_missing_column_and_type_mismatch(repo: Paths):
    _dataset_with_data(
        repo, "a", pl.DataFrame({"id": ["x"], "n": [1]}),
        columns=[{"name": "id", "type": "string", "nullable": False},
                 {"name": "n", "type": "string"},
                 {"name": "gone", "type": "int64"}],
    )
    res = check("a", repo)
    codes = {i["code"] for i in res["issues"]}
    assert "missing_column" in codes and "type_mismatch" in codes
    assert res["ok"] is False


def test_check_detects_grain_duplicate(repo: Paths):
    _dataset_with_data(
        repo, "a", pl.DataFrame({"id": ["x", "x"]}),
        columns=[{"name": "id", "type": "string", "nullable": False}], grain=["id"],
    )
    res = check("a", repo)
    assert any(i["code"] == "grain_duplicate" for i in res["issues"])


def test_check_passes_clean_data(repo: Paths):
    _dataset_with_data(
        repo, "a", pl.DataFrame({"id": ["x", "y"], "v": [1.0, 2.0]}),
        columns=[{"name": "id", "type": "string", "nullable": False, "unique": True},
                 {"name": "v", "type": "float64"}],
        grain=["id"],
        quality=[{"rule": "row_count_between", "min": 1, "max": 10}],
    )
    res = check("a", repo)
    assert res["ok"] is True and res["rows"] == 2


def test_check_quality_rules(repo: Paths):
    _dataset_with_data(
        repo, "a", pl.DataFrame({"id": ["x"], "v": [999.0]}),
        columns=[{"name": "id", "type": "string", "nullable": False},
                 {"name": "v", "type": "float64"}],
        quality=[{"rule": "value_between", "column": "v", "min": 0, "max": 10}],
    )
    assert any(i["code"] == "value_between" for i in check("a", repo)["issues"])


def test_check_skips_when_no_data(repo: Paths):
    new_dataset("a", repo, source_id="s")
    assert check("a", repo)["ok"] is None


# ---------------- infer / adopt ----------------

def test_infer_types_and_grain(repo: Paths, tmp_path: Path):
    f = tmp_path / "x.parquet"
    pl.DataFrame({"id": ["a", "b"], "n": [1, 2], "f": [1.0, 2.0]}).write_parquet(f)
    c = infer(f, "x")
    types = {col.name: col.type for col in c.columns}
    assert types == {"id": "string", "n": "int64", "f": "float64"}
    assert c.grain == ["id"]


def test_adopt_rejects_schema_mismatch(repo: Paths, tmp_path: Path):
    _dataset_with_data(repo, "a", pl.DataFrame({"id": ["x"]}),
                       columns=[{"name": "id", "type": "int64"}])
    shutil.rmtree(repo.curated("a"))
    f = tmp_path / "src.parquet"
    pl.DataFrame({"id": ["x"]}).write_parquet(f)
    res = adopt("a", f, repo)
    assert res["ok"] is False and res["issues"]


def test_adopt_copies_when_schema_matches(repo: Paths, tmp_path: Path):
    new_dataset("a", repo, source_id="s")
    _set_contract(repo, "a", columns=[{"name": "id", "type": "string"}])
    f = tmp_path / "src.parquet"
    pl.DataFrame({"id": ["x", "y"]}).write_parquet(f)
    res = adopt("a", f, repo)
    assert res["ok"] and res["rows"] == 2
    assert any(repo.curated("a").glob("*.parquet"))


# ---------------- 删除 ----------------

def test_rm_plan_blocks_on_downstream(repo: Paths):
    new_dataset("a", repo, source_id="s")
    new_dataset("b", repo, upstream_datasets=["a"])
    reindex(repo)
    pl_ = rm_plan("a", repo)
    assert pl_["blocking"] is True and "b" in pl_["downstream"]


def test_rm_plan_keeps_shared_source(repo: Paths):
    """两个 dataset 共享同一外部源时，raw/blob 不能跟着删。"""
    new_dataset("a", repo, source_id="shared")
    new_dataset("b", repo, source_id="shared")
    repo.raw("shared").mkdir(parents=True, exist_ok=True)
    (repo.raw("shared") / "f.txt").write_text("x", encoding="utf-8")
    reindex(repo)
    pl_ = rm_plan("a", repo)
    assert pl_["removable_sources"] == []
    assert "b" in pl_["shared_sources"]["shared"]
    assert not any(t["kind"] == "raw" for t in pl_["targets"])


def test_rm_plan_lists_storage(repo: Paths):
    new_dataset("a", repo, source_id="solo")
    _write_curated(repo, "a", pl.DataFrame({"id": ["x"]}))
    repo.raw("solo").mkdir(parents=True, exist_ok=True)
    (repo.raw("solo") / "f.txt").write_text("x", encoding="utf-8")
    reindex(repo)
    kinds = {t["kind"] for t in rm_plan("a", repo)["targets"]}
    assert {"code", "curated", "raw"} <= kinds


# ---------------- 外部源工具 ----------------

def test_parse_duration():
    assert parse_duration("7d").days == 7
    assert parse_duration("30m").total_seconds() == 1800
    with pytest.raises(ValueError):
        parse_duration("nope")


def test_expand_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret")
    src = {"headers": {"Authorization": "Bearer ${MY_TOKEN}"}, "list": ["${MY_TOKEN}"]}
    assert expand_env(src)["headers"]["Authorization"] == "Bearer secret"
    assert expand_env(src)["list"] == ["secret"]
def test_expand_env_missing_var_raises_named_error(monkeypatch):
    """缺变量必须报出变量名。

    回归：以前静默替换成空串，源照样「展开成功」，然后在 httpx 层炸出
    `Illegal header value b'... '` —— 与根因毫无关系，极难定位。
    """
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    with pytest.raises(KeyError, match="NOPE_TOKEN"):
        expand_env({"headers": {"X": "Bearer ${NOPE_TOKEN}"}})


def _write_env(d: Path, *lines: str) -> None:
    (d / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_dotenv_reads_repo_root(tmp_path: Path, monkeypatch):
    """回归：.env 在 CLAUDE.md / .gitignore 里都有约定，但从来没被加载过。"""
    for k in ("DW_A", "DW_B", "DW_C", "DW_D"):
        monkeypatch.delenv(k, raising=False)
    _write_env(
        tmp_path,
        "# 注释行",
        "",
        "DW_A=plain",
        "DW_B='单引号'",
        'DW_C="双引号"',
        "DW_D = 两边留空格 ",
        "这行没有等号，应被跳过",
    )
    load_dotenv.cache_clear()
    load_dotenv(tmp_path)
    assert os.environ["DW_A"] == "plain"
    assert os.environ["DW_B"] == "单引号"
    assert os.environ["DW_C"] == "双引号"
    assert os.environ["DW_D"] == "两边留空格"
    for k in ("DW_A", "DW_B", "DW_C", "DW_D"):
        monkeypatch.delenv(k, raising=False)


def test_load_dotenv_does_not_override_process_env(tmp_path: Path, monkeypatch):
    """CI 里注入的真 secret 必须赢过仓库里的 .env。"""
    monkeypatch.setenv("DW_WINS", "from-process")
    _write_env(tmp_path, "DW_WINS=from-dotenv")
    load_dotenv.cache_clear()
    load_dotenv(tmp_path)
    assert os.environ["DW_WINS"] == "from-process"


def test_load_dotenv_tolerates_missing_file(tmp_path: Path):
    load_dotenv.cache_clear()
    load_dotenv(tmp_path)          # 没有 .env 不该抛异常


def test_find_repo_root_loads_dotenv(tmp_path: Path, monkeypatch):
    """接线检查：走 find_repo_root 就该把 .env 载进来（这才是真实调用路径）。"""
    monkeypatch.delenv("DW_WIRED", raising=False)
    monkeypatch.delenv("DW_REPO", raising=False)
    (tmp_path / "warehouse.yaml").write_text("version: 1\n", encoding="utf-8")
    _write_env(tmp_path, "DW_WIRED=yes")
    sub = tmp_path / "datasets" / "x"
    sub.mkdir(parents=True)
    load_dotenv.cache_clear()
    assert find_repo_root(sub) == tmp_path.resolve()
    assert os.environ["DW_WIRED"] == "yes"
    monkeypatch.delenv("DW_WIRED", raising=False)


# ---------------- 健康监控：schema 结构探针 ----------------

def test_schema_hash_ignores_values():
    """同结构不同值必须是同一个 hash —— 否则每次探测都在误报。"""
    a = json_key_paths({"results": [{"ticker": "AAPL", "n": 1}]})
    b = json_key_paths({"results": [{"ticker": "MSFT", "n": 999}]})
    assert schema_hash(a) == schema_hash(b)


def test_schema_hash_catches_renamed_field():
    a = json_key_paths({"results": [{"value": 1}]})
    b = json_key_paths({"results": [{"amount": 1}]})
    assert schema_hash(a) != schema_hash(b)
    assert set(b) - set(a) == {"results[].amount"}
    assert set(a) - set(b) == {"results[].value"}


def test_dig_supports_index_and_key_paths():
    doc = {"results": [{"x": {"y": 1}}]}
    assert _dig(doc, "results[0].x") == {"y": 1}
    assert _dig([{"a": 1}, [{"b": 2}]], "[1][0]") == {"b": 2}
    with pytest.raises(KeyError):
        _dig(doc, "nope[0]")


def test_html_to_text_drops_script_and_markup():
    t = html_to_text("<html><body><h1>Rate limits</h1>"
                     "<script>var x=1</script><p>10 req/s</p></body></html>")
    assert "Rate limits" in t and "10 req/s" in t and "var x" not in t


# ---------------- 健康监控：新鲜度 / 熔断 / 确认 ----------------

def _contract(p: Paths, name: str, freshness: str, schedule: str | None = "0 7 * * *"):
    d = p.dataset_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "contract.yaml").write_text(yaml.safe_dump({
        "name": name, "grain": ["id"],
        "columns": [{"name": "id", "type": "int64"}],
        "sla": {"freshness": freshness, "schedule": schedule},
    }, allow_unicode=True), encoding="utf-8")


def _run_state(p: Paths, name: str, hours_ago: float):
    import datetime as dt
    meta = p.dataset_dir(name) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    when = dt.datetime.now() - dt.timedelta(hours=hours_ago)
    (meta / "run_state.json").write_text(
        json.dumps({"last_run": when.isoformat(timespec="seconds")}), encoding="utf-8")


def test_dataset_freshness_levels(repo: Paths):
    """只有真超了才报。日更的表在下次跑之前必然接近上限，预警等于天天黄。"""
    _contract(repo, "demo__fresh", "1d"); _run_state(repo, "demo__fresh", 2)
    _contract(repo, "demo__near", "1d"); _run_state(repo, "demo__near", 22)
    _contract(repo, "demo__late", "1d"); _run_state(repo, "demo__late", 50)
    _contract(repo, "demo__never", "1d")
    got = {d["dataset"]: d["status"] for d in dataset_freshness(repo)}
    assert got == {"demo__fresh": "ok", "demo__near": "ok",
                   "demo__late": "fail", "demo__never": "warn"}


def test_manual_dataset_never_fails_on_freshness(repo: Paths):
    """没排计划任务的表没有「该跑没跑」一说，最多提醒，不该标成故障。"""
    _contract(repo, "demo__manual", "1d", schedule=None)
    _run_state(repo, "demo__manual", 100)
    assert dataset_freshness(repo)[0]["status"] == "warn"


def test_attempts_quarantine_after_three_fails(repo: Paths):
    for _ in range(2):
        rec = attempts_record("src1", "fail", "试了", repo)
    assert rec["quarantined"] is False
    rec = attempts_record("src1", "fail", "又试了", repo)
    assert rec["fails"] == 3 and rec["quarantined"] is True
    assert attempts_record("src1", "ok", "修好了", repo)["quarantined"] is False


def test_attempts_clear_keeps_log(repo: Paths):
    attempts_record("src1", "fail", "x", repo)
    attempts_clear("src1", repo)
    rec = attempts_load(repo)["src1"]
    assert rec["fails"] == 0 and len(rec["log"]) == 2


def test_ack_requires_note(repo: Paths):
    with pytest.raises(ValueError):
        ack_record("src1", "schema", "h1", "  ", p=repo)


def test_ack_is_version_scoped_not_permanent_mute(repo: Paths):
    """确认只对当时那个版本生效：上游再变一次必须重新报。"""
    ack_record("src1", "schema", "hash-v2", "只新增了不用的字段", p=repo)
    ack = ack_load(repo)
    assert ack_for(ack, "src1", "schema")["hash"] == "hash-v2"
    assert ack_for(ack, "src1", "schema")["hash"] != "hash-v3"


def test_dashboard_renders_with_empty_state():
    assert "<title>数据仓库健康面板</title>" in render({})


def test_dashboard_live_mode_adds_controls():
    st = {"freshness": [{"dataset": "demo__a", "status": "ok", "freshness": "1d",
                         "schedule": "0 15 * * *", "runner": "family",
                         "family": "demo", "last_run": "2026-08-25T15:00:00",
                         "reason": ""}]}
    assert "<button class=\"save\">" not in render(st)          # 只读模式不给按钮
    live = render(st, live_token="t0k3n")
    assert "<button class=\"save\">" in live and "t0k3n" in live


# ---------------- 调度：cron 转换与合约行级改写 ----------------

def test_parse_cron_daily_and_weekly():
    assert parse_cron("0 15 * * *") == (None, "15:00")
    assert parse_cron("30 23 * * 1-5") == ("MON,TUE,WED,THU,FRI", "23:30")
    assert parse_cron("5 6 * * 0") == ("SUN", "06:05")


def test_parse_cron_rejects_what_schtasks_cannot_express():
    """Windows 任务计划表达不了的 cron 直接报错，不偷偷降级成别的时间。"""
    for bad in ("*/5 * * * *", "0 15 1 * *", "", "0 99 * * *"):
        with pytest.raises(ValueError):
            parse_cron(bad)


def test_set_sla_edits_only_those_lines_and_keeps_comments(repo: Paths):
    d = repo.dataset_dir("demo__sla")
    d.mkdir(parents=True)
    (d / "contract.yaml").write_text(
        "name: demo__sla\n"
        "grain: [id]\n"
        "sla:\n"
        "  freshness: 1d\n"
        '  schedule: "0 15 * * *"   # 每天下午跑\n'
        "  stage: all\n"
        "changelog: []\n", encoding="utf-8")
    changed = set_sla("demo__sla", repo, schedule=None, freshness="30d",
                      runner="manual")
    text = (d / "contract.yaml").read_text(encoding="utf-8")
    # 值变了，行尾那句「每天下午跑」就成了假话 —— 连注释一起去掉
    assert "  schedule: null" in text
    assert "每天下午跑" not in text
    assert any("去掉过时的行尾注释" in c for c in changed)
    assert "  freshness: 30d" in text
    assert "  runner: manual" in text                    # 缺的字段补进 sla 块
    assert "  stage: all" in text and "grain: [id]" in text
    assert len(changed) == 3
