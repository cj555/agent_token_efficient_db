# 老项目形态 → 本仓结构的映射套路

## 形态 A：一个大脚本从头跑到尾

```
run_all.py:
  download_filings()      → sec__filings.ingest.py
  parse_html()            → sec__filings.transform.py（写 manifest 表）
  extract_mdna()          → sec__mdna.transform.py
  chunk_text()            → sec__mdna_chunks.transform.py
  embed()                 → sec__mdna_vectors.transform.py
```
判断切点的信号：**函数之间落盘了中间文件**、**某一步明显更慢/更贵**、**主键粒度变了**。

## 形态 B：notebook + 散落的 csv/parquet

- 已有产物 → `dw infer` 反推合约 → `dw adopt` 纳管，先让数据可查。
- notebook 里的清洗逻辑 → `transform.py`；探索性绘图代码不迁。
- 之后再补 `ingest.py` 恢复可重跑能力（可以先留 `enabled: false`）。

## 形态 C：已有 Airflow / prefect DAG

- 一个 task ≈ 一个 dataset 的一个阶段。DAG 依赖关系直接翻译成 `contract.yaml` 的 `upstream`。
- 调度改为 `config.yaml.sla.schedule` + `scripts/install_schedule.ps1`。
- operator 里的重试/超时 → `config.yaml.runtime`。

## 代码改写对照

| 老写法 | 本仓写法 |
|---|---|
| `pd.read_parquet(path)` | `dw.load("<dataset>")`（LazyFrame） |
| `pd.read_csv(...)` 读原始文件 | `pl.scan_csv(dw.paths().raw("<sid>") / "x.csv")` |
| `df.to_parquet(out)` | `dw.write_curated(df, "<dataset>")` |
| `requests.get` | `httpx`（超时/重试写进 `config.yaml.runtime`） |
| 硬编码路径 `"D:/data/x.parquet"` | `dw.paths().raw(...)` / `dw.paths().curated(...)` |
| 跨表 join 的手写循环 | `dw.sql("select ... join ...")` 或 polars `join` |
| `df.values` 喂给 numpy | `dw.arrow(...)` / `dw.vectors(...)`（零拷贝） |
| 自己写的 schema 校验 | `contract.yaml` 的 `columns` + `quality`，`dw validate` 执行 |
| `os.environ["TOKEN"]` 散落各处 | `external_sources.yaml` 里 `${TOKEN}` 占位，`dw.get_source()` 展开 |

## pandas → polars 常见对照

| pandas | polars（lazy） |
|---|---|
| `df[df.a > 0]` | `lf.filter(pl.col("a") > 0)` |
| `df.assign(b=df.a*2)` | `lf.with_columns((pl.col("a") * 2).alias("b"))` |
| `df.groupby("k").sum()` | `lf.group_by("k").agg(pl.all().sum())` |
| `df.sort_values("t")` | `lf.sort("t")` |
| `df.merge(o, on="k")` | `lf.join(o, on="k")` |
| `df.drop_duplicates(["k"])` | `lf.unique(subset=["k"])` |
| `df.groupby("k").a.shift(1)` | `pl.col("a").shift(1).over("k")` |
| `df.head()` 触发计算 | `lf.head().collect()` |

`.collect()` 只在最后调用一次；中间保持 lazy，polars 才能做谓词下推和并行。

## 对账清单（移植后必须报给用户）

```bash
# 行数
dw sql "select count(*) from <dataset>"
# 关键数值列的总和 / 唯一键数
dw sql "select sum(amount), count(distinct id) from <dataset>"
```
与旧产物同口径对比，差异要能解释（例如「旧脚本没去重，新版按 grain 去重掉 12 行」）。
