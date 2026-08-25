---
name: create-dataset
description: 在本数据仓库中新建一个或一族 dataset。当用户说「新建数据集/加一份数据/我要接入某个数据源/把这份数据纳入仓库」时使用。会先做拆分设计与计划、经用户批准后才生成脚手架、填代码、注册并配置定时任务。
---

# create-dataset

在这个仓库新增数据。产出单位是 **dataset family**（一个需求常常对应多个表），不是单个表。

## 铁律

1. **先计划，后动手。** 在用户明确批准前，不创建/修改任何文件。
2. **先跑 `dw`，再读文件。** 找现有数据用 `dw search` / `dw ls`，不要去 `ls datasets/` 或通读合约。
3. **1 个 dataset = 1 张 curated 表。** 拆分标准见下。
4. 脚手架由 `dw new` 生成，**不要手写样板文件**。

## 步骤

### 1. 采集需求（问用户）
- 这份数据是什么、**为什么需要**、最终拿它做什么？（这段会写进 `contract.yaml` 的 `purpose`，也是以后 `dw search` 的检索面，务必具体）
- 数据从哪来：外部 URL/API/本地文件？需要认证吗？
- 更新频率、可接受的陈旧度（freshness）。
- 谁会消费它、以什么粒度消费。

### 2. 找现有数据（省掉重复造数据）
```bash
dw search <关键词>          # 在 purpose/tags/列名上检索
dw ls --domain <领域>
dw show <候选> --fields meta,schema   # 只在确有候选时展开
```
如果已有 dataset 能满足或作为上游，直接告诉用户，并把它作为 upstream 而不是重新下载。

### 3. 确认外部源
新外部源要登记进 `data_contracts/external_sources.yaml`（先看一眼已有条目，可能已存在可复用的 source id —— 共享 source 的多个 dataset 不会重复下载）。
凭据一律写 `${ENV_VAR}` 占位，**绝不写明文**。

### 4. 做 family 拆分设计 ★ 最关键的一步
按 `references/splitting.md` 的四问决定拆几个 dataset。产出一张表交给用户看：

| dataset | grain | 上游 | 阶段 | 触网 |
|---|---|---|---|---|

### 5. 确定增量抓取的边界策略并**询问用户** ★ 必做，不可跳过

`ingest.py` 对外部 API 的抓取永远不能无界——`max_records: null` + 分页循环
等于"每次跑都拉全部历史"，在低速率限制下会把"每日增量"跑成几小时的任务，
还可能在写盘前把整批结果攒进内存（同类风险：把所有页攒进内存再一次性落盘，量一大就会撑爆预算，应边拉边落盘）。

先看这份数据的边界怎么天然形成，从下面选一种写进 config.yaml 的 ingest 段
（选完在计划里给用户看，别自己拍板）：

| 策略 | 适用场景 | 参考实现 |
|---|---|---|
| ndays | 按最近 N 个自然/工作日回看，文件存在即跳过 | 适合"按天产出"的源（如日线行情） |
| lookback_days | 按日期字段过滤候选，可叠加二级上限 | 适合按日期字段筛选的备案/记录类源 |
| max_records | 分页 API，结果按时间倒序，拉够最近 N 条就停 | 适合无日期参数、只能翻页的 API |
| 无边界（全量） | 仅当数据源体量小、一次全量几秒内完事 | 需在注释里写明原因，仍要给 max_records 一个显式小整数兜底 |

不要保留模板里的占位注释就直接落地——必须替换成选定的具体字段和数值，
并在 ingest.py 里真正读取使用（`dw doctor` 会查 config.yaml 声明的 ingest.*
键有没有在 ingest.py 源码里出现，出现死字段会直接报出来）。

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

### 7. 输出计划并等待批准
计划必须包含：family 拆分表、每个 dataset 的列草案、**增量抓取边界策略**、**内存预算表**、将新建的文件清单、外部源登记内容、定时任务安排、风险点。
**停在这里，等用户明确说「可以/批准/继续」再往下。**

### 8. 落地（批准后）
```bash
dw new --example-spec > /tmp/spec.yaml   # 看格式；然后按拆分表写 spec
dw new --family /tmp/spec.yaml           # 一次生成全族脚手架
```
然后按**拓扑序**（上游在前）逐个：
1. 补完 `contract.yaml` 的 `columns` / `quality`（grain 与 upstream 已由 spec 填好）
2. 实现 `ingest.py`（只有触网的 dataset 有）和 `transform.py`
3. 写 `tests/test_logic.py` 的业务断言（`test_contract.py` 是生成物，别动）
4. 更新 `README.md` 的一句话说明
5. 把第 5 步确认的边界策略、第 6 步确认的内存估算值都填进 `config.yaml`
   （`ingest` 段的具体字段、`runtime.memory_estimate_gb`）

### 9. 注册与验证
```bash
dw index                       # 注册进 INDEX.md / graph.json / registry.json
dw run --family <family>       # 按拓扑序跑 ingest→transform→test
dw validate                    # 合约校验
```

### 10. 维护任务 ★ 必做的收尾，不要漏

**dataset 建完但没有定时任务 = 只会更新这一次。** 这一步不是可选项，
每次 `create-dataset` 落地后都要主动问用户要不要装、什么时候跑——
不要默认"用户会自己记得装"。

优先用 **`-Family`**，不要给同一个 family 里的每个 dataset 各开一个
`-Dataset` 任务：`dw run --family <名>` 在同一进程里按拓扑序跑完整族，
天然避免"上游任务还没跑完、下游任务的定时器已经到点"的竞态。只有
dataset 不属于任何 family、或明确要独立于 family 节奏刷新时才单独用
`-Dataset`。

**排时间前先查 upstream 约束**：
```bash
dw deps <ds-或-family> --up          # 谁是我的上游
dw show <upstream-ds> --fields meta  # 看它的 sla.schedule
schtasks /query /fo table /nh | findstr dw-    # 看这台机器已经在什么时间点跑什么
```
新任务的时间必须**晚于**它依赖的外部数据源/上游 family 的产出时间，
且与已注册的其他任务错开（避免同一时刻抢内存预算——这台机器全仓
`memory_budget_gb` 是共享的，见 `warehouse.yaml`）。没有明确上游约束、
用户也没指定时间时，默认排在夜间空闲时段。

```bash
.\scripts\install_schedule.ps1 -Family <name> -Time <HH:mm>      # 推荐：整族一起
.\scripts\install_schedule.ps1 -Dataset <name> -Time <HH:mm>     # 独立 dataset
.\scripts\install_schedule.ps1 -Monitor -Time 07:00              # 外部源健康监控（全仓只需一次）
```
执行前把**任务名、时间、为什么选这个时间（相对哪个上游/哪些已有任务错开）**
念给用户确认，再动手注册。任务输出会自动写进 `logs/<TaskName>.log`。

## 参考
- `references/splitting.md` —— 拆分四问 + SEC 案例（需要判断拆几个表时读）
- `references/contract_fields.md` —— 合约字段与质量规则清单（写 contract.yaml 时读）
