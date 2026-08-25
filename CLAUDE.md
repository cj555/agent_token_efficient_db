# 本仓库的工作约定

这是一个 **token 高效、高度解耦**的本地数据仓库。数据以 parquet 存在本地，
Polars 做 ETL、DuckDB 做跨表 SQL、PyArrow schema 作为类型真源。

## Token 纪律（最重要）

**默认动作是「跑 `dw` 命令」，不是「读文件」。**

| 想知道什么 | 跑这个 | 不要这样 |
|---|---|---|
| 有哪些数据 | `dw ls` | `ls datasets/` 后逐个读 README |
| 有没有现成的数据能用 | `dw search <关键词>` | 通读所有 contract.yaml |
| 某个表的结构 | `dw show <ds> --fields schema` | 打开整份 contract.yaml |
| 谁依赖它 / 它依赖谁 | `dw deps <ds> --down` / `--up` | 递归读合约 |
| 改这一列会影响谁 | `dw impact <ds> --column c` | 全仓 grep |
| 数据对不对 | `dw validate <ds>` | 读数据抽样目测 |
| 外部源健康吗 | `dw health --broken --json` | `cat .health/report.json` / 自己去 curl |
| 某个外部源怎么配的 | `dw source show <sid>` | 读整份 external_sources.yaml |
| 整体状况给人看 | `dw health --html --open`（面板 `.health/dashboard.html`） | 逐个文件翻 |
| 在面板上改计划/立即跑 | `dw panel --open`（本地控制台，只绑 127.0.0.1） | 手写 schtasks |
| 改某表的调度声明 | `dw sla <ds> --manual` / `--runner own --schedule "30 9 * * *" --install` | 手改 contract.yaml |
| 仓库整体有没有问题 | `dw doctor` | 逐个目录检查 |

三层渐进披露：`data_contracts/INDEX.md`（全仓概览，最先读）→ `graph.json`（局部查询，用 `dw deps/impact` 而非直接读）→ 单个 `contract.yaml`（确有必要时才展开）。
**永远不要一次性读整个 `data_contracts/` 或所有 dataset 的代码。**

## 谁负责按时跑一张表

合约 `sla.runner` 三档，决定 `dw run --family` 会不会带上它、以及该不该有独立任务：

| runner | 含义 | 独立 Windows 任务 |
|---|---|---|
| `family`（默认） | 跟着 `dw-family-<族>` 一起跑（同进程按拓扑序，天然避免上下游竞态） | 不该有 |
| `own` | 自己一个 `dw-<ds>-<stage>` 任务，族任务不带它（适合节奏不同的大表） | 有 |
| `manual` | 都不跑，只有点名 `dw run <ds>` 才动；`schedule` 写 null | 不该有 |

改这三档用 `dw sla`（会同时改合约和注册/卸载任务），或在 `dw panel` 的面板上点。
手动/独立的表不会被族任务带跑 —— 要一起跑加 `dw run --family X --include-manual`。

## 结构不变量

- **1 个 dataset = 1 张 curated 表**。grain（主键）变了就该拆新 dataset。
- 每个 dataset 是自洽单元：`datasets/<name>/` 下有 `contract.yaml`、`config.yaml`、
  `ingest.py`（仅触网的有）、`transform.py`、`schema.py`、`README.md`、`tests/`。
  改一个 dataset **不需要打开任何其他 dataset 的文件**。
- `ingest` 与 `transform` 严格分离：ingest 触网、幂等、可跳过；transform 纯本地、确定性、可无限重放。
  改口径只重跑 transform，不要重新下载。
- 存储分层：`storage/raw/<source_id>/`（按外部源，可共享）、`storage/blob/<source_id>/`（非表格资产）、
  `storage/curated/<dataset>/`（唯一承诺产物）、`storage/tmp/<dataset>/`（可删）。

## 真源与生成物

| 文件 | 性质 |
|---|---|
| `datasets/<ds>/contract.yaml` | ★ 真源，人写人读，**保留注释**。除 `dw new` / `dw infer --write` 外不要程序化重写 |
| `data_contracts/external_sources.yaml` | ★ 真源，外部源清单。凭据写 `${ENV_VAR}` 占位，真值放仓库根 `.env`（`dwlib` 自动加载，见 `.env.example`） |
| `datasets/<ds>/schema.py` | 生成物，`dw index` 从合约重生成，**不要手改** |
| `datasets/<ds>/tests/test_contract.py` | 生成物，同上 |
| `data_contracts/INDEX.md` / `graph.json` / `registry.json` | 生成物，`dw index` 产出 |
| `datasets/<ds>/_meta/run_state.json` | 运行状态，程序写 |

## 数据不进 git

本仓库是**代码与合约仓库**。`storage/`、所有 parquet/duckdb/csv、`.env`、`.health/` 都在
`.gitignore` 里。换机器时用 `dw run --all` 重建数据，不要把数据提交上去。
可选第二道防线：`git config core.hooksPath scripts/hooks`。

## 改动流程

有对应 skill 的事情就走 skill，它们都会**先给计划、经你批准再动手**：

| 需求 | skill |
|---|---|
| 新建数据集 | `/create-dataset` |
| 从老项目移植 | `/migrate-dataset` |
| 改合约/加列/改口径 | `/change-contract` |
| 外部源坏了 | `/fix-source` |
| 删除数据集 | `/del-dataset` |

任何会写盘的操作（生成脚手架、改合约、删除、注册定时任务），**必须先把计划给用户看并等批准**。

## 内存纪律

**流水线通常要和这台机器上的其他工作共存，不能想吃多少吃多少。**
`warehouse.yaml` 的 `engine.memory_budget_gb` 是全仓上限（当前 1 GB），
每个 dataset 在 `config.yaml` 的 `runtime.memory_estimate_gb` 申报自己的用量。

| 想知道什么 | 跑这个 |
|---|---|
| 某个 dataset 实际吃多少内存 | `dw run <ds> --stage transform`（每次都实测并打印峰值） |
| 历史峰值 | `datasets/<ds>/_meta/run_state.json` 的 `peak_gb` |
| 谁没申报 / 谁超了 | `dw doctor` |

写 transform 的三条硬规矩：
1. `build()` **返回 LazyFrame，不要自己 `.collect()`** —— `dw.write_curated` 会流式落盘。
2. 大表别做全表 `sort` / `join_asof`。先把要 join 的东西在小表上算好（比如累积复权因子），
   再按分区逐块 join，用 `dw.write_curated_chunks(chunks, ds, "year")`。
   **分区粒度是给下游查询用的，处理粒度只服务于内存** —— 同一个分区值可以喂多块，
   所以「按年分区、按季处理」是标准做法。
3. 新建 / 迁移 dataset 时必须**先估算内存、给用户看、经确认**再动手
   （`/create-dataset` 和 `/migrate-dataset` 的第 5 步）。

参考量级：2510 万行 × 12 列的复权表，一把梭 7.8 GB，按年×季切块后 1.0 GB。

## 代码风格

- Python ≥3.10，polars 用 lazy API（`scan_*` / `LazyFrame`），`.collect()` 只在最后调一次。
- 路径一律走 `dw.paths()`，不要硬编码。
- transform 的收尾统一用 `dw.write_curated(...)`，它会写 parquet 并更新 `run_state.json`。
- 跨 dataset 读数据只用 `dw.load()` / `dw.arrow()` / `dw.sql()`，不要直接拼 parquet 路径 ——
  `dw refs` 靠这些调用形态来维护引用表，绕过它会让影响分析失灵。

## 环境

```bash
.venv/Scripts/dw.exe <cmd>        # Windows；或先激活 venv 后直接用 dw
```

## LSP（可选）

本项目已配置 Claude Code 内置 LSP 代码智能（Pyright），换机器需要重新装：

1. 交互式终端里跑一次 `/plugin install pyright-lsp@claude-plugins-official`
2. `npm install -g pyright`（提供 `pyright-langserver` 二进制，需在系统 PATH 上）
3. 若 `/plugin` 的 Errors 标签出现 `Executable not found in $PATH`，说明第 2 步没生效
