---
name: migrate-dataset
description: 把外部/老项目里的数据处理代码移植进本数据仓库，按 1 dataset = 1 张表的约束重组并精简依赖。当用户说「把这个文件夹/这个老项目/这些脚本移植过来、迁移进仓库、纳入管理」并给出路径时使用。会先盘点、给出拆分与映射计划，批准后才落地。
---

# migrate-dataset

把一个外部文件夹里的数据处理逻辑，移植成本仓库的 dataset family。

## 铁律

1. **先计划，后动手。** 批准前不写任何文件。
2. **只读盘点，不通读代码。** 先看目录树和 import/IO 调用，按需再读具体函数。老项目动辄上万行，通读会烧光 context。
3. 老项目常常是**一个大脚本从头跑到尾**（下载→解析→抽取→算向量）。移植的核心工作是**把它切成多个 dataset**。
4. 能复用的既有产物用 `dw adopt` 纳管，**不要重跑昂贵步骤**（重新下载全部 10-K、重算全部向量）。

## 步骤

### 1. 只读盘点（低 token）
```bash
ls -R <路径> | head -100                       # 目录树
grep -rn "^import\|^from" <路径> --include=*.py | sort | uniq -c | sort -rn | head -30
grep -rln "read_csv\|read_parquet\|requests\|httpx\|urlopen\|to_parquet\|open(" <路径>
ls -la <路径>/**/*.parquet 2>/dev/null | head   # 已有数据产物？
```
目标是搞清楚：入口脚本是哪个、触网的地方在哪、产出了哪些数据文件。**此时不要打开每个 .py。**

### 2. 问用户
- 要迁移哪些部分？哪些逻辑保留、哪些丢弃？
- 历史数据要不要一并接管（能省掉重跑）？
- 有哪些依赖是必须保留的（专有解析库、私有 SDK）？

### 3. 切分成 family
用 `references/../../create-dataset/references/splitting.md` 的四问，把旧流程切成多个 dataset。
标出**每段旧代码对应新 dataset 的哪个阶段**：

| 新 dataset | grain | 来自旧代码 | 阶段 |
|---|---|---|---|
| `x__raw_list` | id | `fetch.py:20-90` | ingest |
| `x__parsed` | id | `parse.py:全部` | transform |

### 4. 依赖精简
把 `pip freeze` / import 清单分成三类，写进计划：

| 依赖 | 处置 |
|---|---|
| pandas / numpy 的表操作 | → 改写为 polars lazy |
| sqlalchemy / 自建 DB 层 | → 去掉，改用 `dw.load` / `dw.sql` |
| requests | → httpx（本仓已有） |
| 专有解析库（如 pdfminer） | 保留，加进 `pyproject.toml` |
| 死代码 / 未被入口引用 | 删 |

### 5. 确定迁移进来的抓取逻辑的边界策略并**询问用户** ★ 必做，不可跳过

老项目里常见的"每次全量重跑"写法一律要改成下面某种边界策略，**不能原样照搬**——
`ingest.py` 对外部 API 的抓取不能无界，`max_records: null` + 分页循环等于
"每次跑都拉全部历史"，在低速率限制下会把"每日增量"跑成几小时的任务，还
可能在写盘前把整批结果攒进内存（同类风险见 pm__event 的 Kalshi 修复）。

先看这份数据的边界怎么天然形成，从下面选一种写进 config.yaml 的 ingest 段
（选完在计划里给用户看，别自己拍板）：

| 策略 | 适用场景 | 参考实现 |
|---|---|---|
| ndays | 按最近 N 个自然/工作日回看，文件存在即跳过 | polygon__stk_eod/ingest.py |
| lookback_days | 按日期字段过滤候选，可叠加二级上限 | sec__etf_holding/ingest.py |
| max_records | 分页 API，结果按时间倒序，拉够最近 N 条就停 | polygon__stk_dividend/ingest.py |
| 无边界（全量） | 仅当数据源体量小、一次全量几秒内完事 | 需在注释里写明原因，仍要给 max_records 一个显式小整数兜底 |

不要保留模板里的占位注释就直接落地——必须替换成选定的具体字段和数值，
并在 ingest.py 里真正读取使用（`dw doctor` 会查 config.yaml 声明的 ingest.*
键有没有在 ingest.py 源码里出现，出现死字段会直接报出来）。

迁移带进来的外部源登记进 `external_sources.yaml` 时，除了 url / freshness / schedule，
还要**在计划里给出 `schema_probe` 与 `docs` 两项并请用户自检**：

- `schema_probe`（`{url, node}`）—— 结构基线探针，抓一条真实样本（`limit=1`），
  只对 key 路径集合做哈希，值变不误报。你推荐一个廉价取数 URL 与 node，标明推荐依据。
- `docs`（URL 列表）—— 上游技术文档页，监控会抓正文比对，变了在面板标出并存 diff。
  你推荐官方地址并说明来源，**没把握就留空，不要拿看着像的凑数**。

推荐值必须标明「这是推荐，请你确认」。用户拿不准就留空，并在计划里写清后果：
该源的 schema / 文档变更测不到，面板标灰「未监控」。配不了探针的源（取数要带具体日期、
或取的是纯文本/PDF 之类非表格资产）在 `note` 里写明原因。

### 6. 估算内存峰值并**询问用户** ★ 必做，不可跳过
这台机器的数据流水线要与视频渲染共存，`warehouse.yaml` 的
`engine.memory_budget_gb` 是硬约束。**在写计划之前**先算一遍，别等跑挂了再说。

估算方法（`dwlib.memory` 里有同样的公式）：
```
每行字节 ≈ Σ 各列宽度（数值 4-8；日期 4；字符串按 48 算）
峰值 ≈ 每行字节 × 行数 × 3        # ×3 是 join/sort/groupby 的中间副本开销
```
拿这个数对照预算，然后在计划里给用户一张表：

| dataset | 预估行数 | 每行字节 | 预估峰值 | 预算内？ | 超了怎么办 |
|---|---|---|---|---|---|

**超预算时的标准手段，按顺序试：**
1. `build()` 返回 **LazyFrame**（不要自己 `.collect()`），`write_curated` 会流式落盘。
2. 按分区切块，用 `dw.write_curated_chunks(chunks, ds, "year")` 逐块喂 ——
   同一个分区值可以喂多块，所以「按年分区、按季处理」是合法且常用的组合。
3. 别对大表做全表 `sort` / `join_asof`：先在小表上把要 join 的东西算好，再逐块 join。
4. 调小 `warehouse.yaml` 的 `engine.polars_max_threads`（16→4 实测省一半内存）。

估算结果和应对方案**必须让用户确认**，确认后写进每个 dataset 的
`config.yaml` 的 `runtime.memory_estimate_gb`。之后每次 `dw run` 都会实测核对，
超申报值 25% 就告警；`dw doctor` 也会查有没有漏报。

### 7. 输出迁移计划并等待批准
必须包含：family 拆分表、**「旧文件:行段 → 新文件」映射表**、依赖去留表、**增量抓取边界策略**、**内存预算表**、历史数据接管方案、验证方式。
**停在这里等批准。**

### 8. 落地（批准后）
```bash
dw new --family /tmp/spec.yaml       # 已存在的 dataset 会自动跳过，可断点续做
```
按拓扑序逐个移植。合约可以从既有数据反推，省掉逐列人肉推断：
```bash
dw infer <旧数据路径>                       # 先看一眼推断结果
dw infer <旧数据路径> --write <dataset>     # 写进 contract.yaml（保留人写字段）
```
然后**手工收紧**：补 `purpose`、确认 `grain`、把该 NOT NULL 的列改掉、加 `quality` 规则。
`dw infer` 只是起点，不是终点。

### 9. 接管历史数据（避免重跑）
```bash
dw adopt <dataset> <旧parquet路径>            # schema 不符会拒绝
dw adopt <dataset> <路径> --mode move         # 确认无误后可以移动而非复制
```

### 10. 验证 + 与旧产物对账
```bash
dw index && dw run --family <family> && dw validate
```
对账：行数、关键列的 sum/校验和是否与旧产物一致。**把对账结果告诉用户**，不要只说「迁移完成」。

### 11. 留痕
在 `docs/migration_log.md` 追加一条：源项目形态、遇到的摩擦点、对框架的改进建议。
这是后续优化这个 agent 的唯一依据。

## 参考
- `references/mapping.md` —— 老项目常见形态 → 本仓结构的映射套路
- `../create-dataset/references/splitting.md` —— 拆分四问
