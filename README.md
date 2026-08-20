# agent_token_efficient_db

一个给 **Claude Code** 用的本地数据仓库管理框架：token 高效、高度解耦。

clone 下来就是你自己的数据仓库骨架 —— 5 个 skill 负责新建/移植/改合约/修数据源/删除数据集，
一个 `dw` CLI 负责把「有什么数据、数据之间什么关系、改一处会影响谁」压缩成几行输出，
而不是让 Claude 去读几十个文件。

---

## 它解决什么问题

让 LLM 管理数据仓库，最大的成本是**理解现状**：有哪些表、谁依赖谁、改一列会波及哪里。
朴素做法是把代码和 schema 读进 context，token 消耗随仓库规模线性膨胀，很快就不可用。

本框架用两条主线压住它：

1. **合约驱动 + 三层渐进披露**
   `INDEX.md`（全仓概览，一行一个 dataset）→ `graph.json`（依赖图 + 代码引用表）→ 单个 `contract.yaml`。
   默认只读第一层。
2. **确定性 CLI 取代 LLM 浏览**
   盘点、检索、依赖、影响面、校验、删除清单，全部由 `dw` 命令算好。Claude 读的是结论，不是文件。

配套的结构约束（1 dataset = 1 张表、ingest/transform 分离、每个 dataset 自洽）让「改一处」
真的只需要打开一处。

---

## 安装

需要 Python ≥ 3.10。

```bash
git clone <this-repo> my_warehouse
cd my_warehouse
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # Linux/macOS
```

验证：

```bash
.venv/Scripts/dw.exe doctor
```

然后在这个目录打开 Claude Code。`CLAUDE.md` 会自动加载，`.claude/skills/` 里的 5 个 skill 即刻可用。

可选（推荐）：

```bash
git config core.hooksPath scripts/hooks     # 拦截误提交的数据文件与凭据
```

---

## 五分钟上手

仓库自带一个可跑通的示例（世界银行 GDP，公开 API 无需认证）：

```bash
dw ls                          # 看有哪些 dataset
dw run --family example        # ingest → transform → test，按拓扑序
dw validate                    # 合约校验
dw sql "select country_name, year, gdp_usd from example__gdp order by gdp_usd desc limit 5"
dw impact example__gdp --column gdp_usd    # 改这列会影响谁
```

在 Claude Code 里试试：

```
/create-dataset      我要接入 xxx 数据
/migrate-dataset     把 D:/old_project 移植进来
/change-contract     example__gdp 要加一列人均 GDP
/fix-source          数据下不下来了
/del-dataset         删掉 example__gdp_growth
```

每个 skill 都会**先给计划、等你批准，再动手**。

---

## 结构

```
CLAUDE.md                    # 仓库宪法 + token 纪律（Claude 每次会话自动读）
warehouse.yaml               # 全局配置：storage 根目录、命名规范
.claude/skills/              # 5 个 skill
data_contracts/
  INDEX.md                   # 生成物：一行一个 dataset，Claude 的第一层视野
  graph.json                 # 生成物：依赖图 + 代码引用表
  registry.json              # 生成物：对外调用注册表
  external_sources.yaml      # 真源：外部数据源清单（凭据写 ${ENV_VAR} 占位）
datasets/<name>/             # 每个 dataset 一个自洽子文件夹
  contract.yaml              # ★ 真源：数据合约
  config.yaml                # 运行参数
  ingest.py                  # 阶段1：外部源 → raw/blob（只有触网的才有）
  transform.py               # 阶段2：raw/上游 → curated parquet
  schema.py                  # 生成物：pyarrow schema
  tests/                     # test_contract.py 生成 + test_logic.py 手写
dwlib/                       # 薄共享库 + dw CLI
storage/                     # parquet 湖（.gitignore，不进 git）
scripts/                     # 外部源监控 + 计划任务安装 + git hooks
```

**1 个 dataset = 1 张 curated 表。** 需要多张表就拆多个 dataset，共享同一个外部源 id
（raw 层按 source 组织，不会重复下载）。判定规则见
`.claude/skills/create-dataset/references/splitting.md`。

---

## `dw` 命令

| 命令 | 作用 |
|---|---|
| `dw index` | 重建 INDEX.md / graph.json / registry.json + 生成物 |
| `dw ls` | 一行一个 dataset |
| `dw search <kw>` | 找可复用的内部数据源 |
| `dw show <ds> --fields schema` | 只输出合约的指定片段 |
| `dw deps <ds> --up/--down` | 依赖闭包 |
| `dw refs <ds>` | 谁在代码里引用它 |
| `dw impact <ds> --column c` | 变更影响面：下游 + `文件:行` 清单 |
| `dw validate [ds]` | 实际 parquet vs 合约 |
| `dw new <ds>` / `--family spec.yaml` | 生成脚手架 |
| `dw infer <path>` | 从既有数据反推合约草案 |
| `dw adopt <ds> <path>` | 纳管既有数据，免重跑 |
| `dw run <ds> --stage ingest\|transform\|test` | 执行流水线 |
| `dw rm <ds> [--apply]` | 删除（默认 dry-run） |
| `dw health` | 外部源健康检查 |
| `dw sql "..."` | 跨 dataset DuckDB 查询 |
| `dw doctor` | 仓库体检 |

全部支持 `--json`。

---

## 在代码里读数据

```python
import dwlib as dw

lf   = dw.load("example__gdp")                      # polars LazyFrame（惰性）
df   = dw.frame("example__gdp", columns=["year"])   # 立即物化
tbl  = dw.arrow("sec__mdna_vectors")                # pyarrow Table
mat  = dw.vectors("sec__mdna_vectors")              # (n, dim) numpy，零拷贝
out  = dw.sql("select * from a join b using (id)")  # DuckDB 跨表
meta = dw.describe("example__gdp")                  # 合约摘要
```

跨 dataset 读数据请**只用这些 API**：`dw refs` 靠这些调用形态维护引用表，
直接拼 parquet 路径会让影响分析失灵。

---

## 外部源监控

```powershell
.\scripts\install_schedule.ps1 -Monitor -Time 07:00              # 每日检查外部源
.\scripts\install_schedule.ps1 -Dataset example__gdp -Time 06:30 # 每日刷新某个 dataset
.\scripts\install_schedule.ps1 -List                             # 查看已注册任务
```

Linux / macOS 用 crontab：

```cron
0 7 * * *  cd /path/to/repo && .venv/bin/python scripts/monitor_sources.py
30 6 * * * cd /path/to/repo && .venv/bin/dw run example__gdp
```

监控结果写进 `.health/report.json`；出现异常时提示你运行 `/fix-source`，
Claude 直接读这个文件定位，不必自己去探测。

---

## 数据不进 git

本仓库是**代码与合约仓库**，数据留在本地：`storage/`、所有 parquet/duckdb/csv、`.env`、`.health/`
都已在 `.gitignore` 中。换机器时用 `dw run --all` 重建。

如果你要一边用它管自己的数据、一边改进这个框架，推荐两个仓库：

- **public**（本仓库）：只有框架 + 示例 dataset，改框架在这里改
- **private**：从 public clone，加自己的 `datasets/`，`git remote add upstream <public>` 后用
  `git pull upstream main` 拉框架升级

框架改动单向从 public 流向 private，数据代码物理上不可能反向泄漏。

---

## License

MIT
