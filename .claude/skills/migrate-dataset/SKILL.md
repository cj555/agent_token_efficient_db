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

### 5. 输出迁移计划并等待批准
必须包含：family 拆分表、**「旧文件:行段 → 新文件」映射表**、依赖去留表、历史数据接管方案、验证方式。
**停在这里等批准。**

### 6. 落地（批准后）
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

### 7. 接管历史数据（避免重跑）
```bash
dw adopt <dataset> <旧parquet路径>            # schema 不符会拒绝
dw adopt <dataset> <路径> --mode move         # 确认无误后可以移动而非复制
```

### 8. 验证 + 与旧产物对账
```bash
dw index && dw run --family <family> && dw validate
```
对账：行数、关键列的 sum/校验和是否与旧产物一致。**把对账结果告诉用户**，不要只说「迁移完成」。

### 9. 留痕
在 `docs/migration_log.md` 追加一条：源项目形态、遇到的摩擦点、对框架的改进建议。
这是后续优化这个 agent 的唯一依据。

## 参考
- `references/mapping.md` —— 老项目常见形态 → 本仓结构的映射套路
- `../create-dataset/references/splitting.md` —— 拆分四问
