---
name: change-contract
description: 修改某个 dataset 的数据合约（加列/删列/改类型/改粒度/改口径/弃用），并同步所有受影响的下游代码与测试。当用户说「这个表要加一列/改字段/改口径/换主键/这个数据集要废弃」时使用。会先用 dw impact 算出精确影响面并给出计划，批准后才改。
---

# change-contract

改数据合约，并把所有受影响的地方一次改干净。

## 铁律

1. **先计划，后动手。** 批准前不改任何文件。
2. **用 `dw impact` 找影响面，不要全仓 grep。** 它查的是 `graph.json` 里缓存的引用表，一条命令给出 `文件:行`。
3. **只读 impact 指出的那几处代码**，不要打开整个下游 dataset。
4. `schema.py` 和 `tests/test_contract.py` 是**生成物**，改完合约跑 `dw index` 自动重生成，**不要手改**。

## 步骤

### 1. 明确变更内容
问清楚：改哪个 dataset、改什么、为什么。把它归类：

| 类型 | 例子 | 版本 | 下游影响 |
|---|---|---|---|
| additive | 加一个可空列、加质量规则、补 desc | minor+1 | 通常无 |
| breaking | 删列、改列名、改类型、**改 grain**、改口径含义 | major+1 | 必须同步改 |
| fix | 修 desc / unit / 纠正错误的 nullable 声明 | patch+1 | 无 |

**改 grain 是最重的变更** —— 先确认它是否其实应该是一个新 dataset（见拆分四问：grain 变了就该拆）。

### 2. 算影响面
```bash
dw impact <dataset> --column <列名>     # 指定列时同时给出直接引用与间接引用
dw deps <dataset> --down                # 完整下游闭包
dw show <dataset> --fields schema       # 当前列定义
```
输出里的「建议编辑的文件」就是这次要动的全部文件。

### 3. 输出变更计划并等待批准
必须包含：
- 合约 diff（哪些列/规则怎么改）
- 变更分级与新版本号
- **受影响文件逐个列出**（来自 `dw impact`），每个说明要怎么改
- breaking 时：兼容策略（是否保留旧列一个周期）、历史数据是否需要回填/重跑、重跑成本估计
- 回滚方式（git revert + 重跑哪几个 dataset）

**停在这里等批准。**

### 4. 执行（批准后）
按顺序：
1. 改 `datasets/<ds>/contract.yaml`：列定义、`version`、追加 `changelog` 条目
2. 改本 dataset 的 `transform.py`，让产出与新合约一致
3. 按 `dw impact` 的清单改下游：先改下游 `contract.yaml`（若含同名列），再改其 `transform.py`
4. 需要的话更新 `tests/test_logic.py` 的业务断言
5. `dw index`（重生成 schema.py / test_contract.py，刷新索引与依赖图）

### 5. 重跑与验证
```bash
dw run <dataset> --stage transform      # 改的是口径就不必重新 ingest
dw run --family <family>                # 或按拓扑序重跑整族
dw validate                             # 全仓合约校验
```
下游是纯派生的，**不要重跑 ingest** —— 那会白白重新下载。

### 6. 报告
告诉用户：改了哪些文件、新版本号、重跑了什么、行数/关键指标变化前后对比。

## 特例：弃用一个 dataset

不要直接删（那是 `/del-dataset` 的活）。先：
1. `contract.yaml` 的 `status: deprecated`，changelog 记原因与替代品
2. `dw deps <ds> --down` 找出下游，逐个通知/迁移
3. 观察期后再走 `/del-dataset`

## 参考
- `references/breaking_changes.md` —— 各类 breaking 变更的标准处理流程
- `../create-dataset/references/contract_fields.md` —— 合约字段与质量规则速查
