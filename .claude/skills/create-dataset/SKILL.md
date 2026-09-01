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

登记新源时，除了 url / freshness / schedule，还要**在计划里给出这两项并请用户自检**
（漏了的后果是：上游哪天改字段、改文档，本仓完全不知道，只能等 ingest 炸）：

| 字段 | 作用 | 怎么给 |
|---|---|---|
| `schema_probe` | 结构基线探针：抓一条真实样本（`limit=1`），只对 key 路径集合做哈希，值变不误报 | 你**推荐**一个廉价取数 URL 与 `node`（如 `results[0]`），标明推荐依据；用户确认后写入 |
| `docs` | 上游技术文档页 URL，监控会抓正文比对，变了在面板上标出并存 diff | 你**推荐**官方文档地址并说明来源（搜到的/文档里链的）；**没把握就留空**，不要拿看着像的凑数 |

规矩：推荐值必须标明「这是推荐，请你确认」，**不要当成既定事实直接写死**。
用户拿不准就留空，并在计划里写清留空的后果（该源的 schema / 文档变更测不到，
面板会标灰「未监控」）。取数要带具体日期/大参数、或取的是非表格资产（纯文本、PDF）的源，
本来就配不了探针，直接在 `note` 里写明为什么没配。

### 4. 做 family 拆分设计 ★ 最关键的一步
按 `references/splitting.md` 的四问决定拆几个 dataset。产出一张表交给用户看：

| dataset | grain | 上游 | 阶段 | 触网 |
|---|---|---|---|---|

### 5. 确定增量/回补策略并**询问用户** ★ 必做，不可跳过

`ingest.py` 对外部 API 的抓取永远不能无界——`max_records: null` + 分页循环
等于"每次跑都拉全部历史"，在低速率限制下会把"每日增量"跑成几小时的任务，
还可能在写盘前把整批结果攒进内存（同类风险见 pm__event 的 Kalshi 修复）。
本仓库把这拆成两件独立的事，两个脚本各管一段：`ingest.py` = 水位线增量
（每天/每次只补"上次到现在"这一小段），`backfill.py` = 游标式历史回补
（一次性/低频任务，按预算慢慢把更早的历史补齐）。**没有历史可回补的数据源
可以跳过第二步、不生成 `backfill.py`**——先判断这一点，不要不分青红皂白
两个脚本都生成。

#### 第一步：这份数据有没有"可回补的历史"

- **有**：源头本身存着比"当前状态"更深的历史（比如按 accession/id 分页能
  往回翻很多年的事件记录）→ 走下面第二步，两个脚本都要。
- **没有**：源头只是"当前快照"或"滚动 feed"，压根不存在"更早的版本"可以
  拉回来 → 只做增量，不生成 `backfill.py`，`contract.yaml` 的 `purpose`
  里补一句原因（本仓已有三种典型说法，照着抄，别自己现造）：
  - 当前快照类（如 `sec__ticker_cik`）：「源头只提供当前快照，没有历史
    查询接口，不建 backfill.py」
  - RSS 滚动 feed 类（如 `news__bigmoney`）：「RSS 是滚动 feed，源头不
    提供更早历史，不建 backfill.py」
  - API 对旧对象覆盖差类（如 `pm__event`）：「API 对已关闭/过期对象覆盖差，
    回补价值低于实现成本，本次决定跳过，不建 backfill.py」

#### 第二步（有历史的）：增量怎么判断范围 + 回补怎么走

**增量**：`contract.yaml` 顶层加 `watermark: <列名>`（跟 `grain`/`partitions`
同级），`ingest.py` 用 `since = dw.watermark(DATASET) or <合理兜底>`
（没有存量数据时通常兜底成"今天"或"昨天"）判断要抓的时间范围。**下面这张
表描述的是"怎么取数"（分页机制），不再是"时间范围"**——时间范围现在交给
`watermark` 决定，表里这些字段全部降级成"单次请求量的上限保护"：

| 取数机制 | 适用场景 | 参考实现 |
|---|---|---|
| 按天回看 | 有一个天然的"日期"文件/请求单元，文件存在即跳过 | `polygon__stk_eod/ingest.py` |
| 按日期过滤 + 二级上限 | 候选来自另一张表，按日期列过滤，`max_records` 兜底 | `sec__filing_item/ingest.py` |
| 分页 API（结果倒序） | 结果按时间倒序，拉够 `max_records`/`limit` 就停 | `polygon__stk_dividend/ingest.py` |
| 无边界（全量） | 仅当数据源体量小、一次全量几秒内完事（如 `example__gdp`） | 注释写明原因，仍要给一个显式小整数兜底 |

不要保留模板里的占位注释就直接落地——必须替换成选定的具体字段和数值，
并在 ingest.py 里真正读取使用（`dw doctor` 会查 config.yaml 声明的 ingest.*
键有没有在 ingest.py 源码里出现，出现死字段会直接报出来）。

**回补**：`config.yaml` 加 `backfill:` 段。`enabled`/`history_floor` 是
通用字段；**预算字段名跟着上面选的取数机制走，不是死记一个名字**——按天
回看用 `days_per_run`，分页 API 用 `page_limit`（单页条数，见下面的踩坑
提醒）或 `max_records_per_run`，按"详情页数量"计的用
`max_details_per_run`。`backfill.py` 的骨架：

```python
st = dw.backfill_state(DATASET)              # dwlib/backfill.py
cursor = st.cursor or dt.date.today()
if cursor <= floor:
    return {"skipped": True, "done": True}
# ...按选定的取数机制往回走一段（一批天数 / 一页记录 / 一批详情页）...
# 落盘优先复用 write_curated_chunks 的 (value, key, lf) 三元组 + wipe="touched"：
#   key 用当天日期字符串（或等价的稳定单元标识）。不同 key 各写各的文件，
#   互不清空；同一个 key 被重复写只是覆盖它自己那一个文件——天然幂等，
#   天然支持"同一个分区跨很多次独立回补运行安全累积"。
st.advance(new_cursor, done=(new_cursor <= floor))
```

`history_floor` 由用户/LLM 商量给一个具体日期，写进计划里给用户看、批准
后才落地。**如果数据源是商用订阅**（比如 Polygon 这类按订阅层级限制历史
深度的 API），要在计划里提醒用户核实当前订阅到底能拿到多深的历史，
`history_floor` 别一开始就设得比订阅深度更深——不确定就先按保守值起步，
回补时观察实际返回数据的日期范围，证实能拿到更早的再调深（同一类
"商用数据"里也不是所有端点都受同一深度限制——行情 K 线和事件参考数据
可能是两套订阅边界，不要想当然套用同一个 floor）。

⚠ **如果取数机制要用日期过滤参数（`<field>.lt=` 这类）叠加分页接口**，
先用一次最小请求单独验证：过滤条件在翻到第二页之后是不是依然生效，
不要假设"第一页生效 = 全部页都生效"就直接写多页循环——曾在真实的商业
API 上踩过"第一页按条件正确过滤、跟着 `next_url` 翻到第二页过滤条件就
失效"的坑（返回的记录日期整个乱套）。`backfill.py` 里对拿到的结果做一次
运行时核实（出现不满足过滤条件的记录就直接抛错，不要静默把"过滤失效"
当成"补完了"）；如果分页机制不可靠，改成"每次只发单页请求 + 游标手动
往回退"，不要依赖多页 `next_url` 续传。

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
计划必须包含：family 拆分表、每个 dataset 的列草案、**增量/回补策略**（含
有没有可回补历史的判断结论）、**内存预算表**、将新建的文件清单、外部源
登记内容、定时任务安排、风险点。
**停在这里，等用户明确说「可以/批准/继续」再往下。**

### 8. 落地（批准后）
```bash
dw new --example-spec > /tmp/spec.yaml   # 看格式；然后按拆分表写 spec
dw new --family /tmp/spec.yaml           # 一次生成全族脚手架
```
然后按**拓扑序**（上游在前）逐个：
1. 补完 `contract.yaml` 的 `columns` / `quality`（grain 与 upstream 已由 spec 填好）
2. 实现 `ingest.py`（只有触网的 dataset 有）和 `transform.py`
3. **`backfill.py` 是否需要生成**，跟 `ingest.py` 并列检查——第 5 步判断
   "有可回补历史"的 dataset，两个脚本都要有；判断"无历史可回补"的，
   `contract.yaml` 的 `purpose` 里那句说明不能漏，`backfill.py` 不生成。
4. 写 `tests/test_logic.py` 的业务断言（`test_contract.py` 是生成物，别动）
5. 更新 `README.md` 的一句话说明
6. 把第 5 步确认的取数机制/回补预算、第 6 步确认的内存估算值都填进 `config.yaml`
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

合约里用 `sla.runner` 声明谁负责跑它（`family` 默认 / `own` 自己一个任务 / `manual` 只手动），
`dw run --family` 只会带上 `runner: family` 且排了 `schedule` 的成员 —— 声明和实际必须一致，
否则「改成手动」只是句空话。

```bash
dw sla <ds> --runner family --schedule "0 15 * * *"              # 跟族跑：只写声明，任务在族那一层
.\scripts\install_schedule.ps1 -Family <name> -Time <HH:mm>      # 推荐：整族一起
dw sla <ds> --runner own --schedule "30 9 * * *" --install       # 独立 dataset：一条命令改声明 + 注册任务
dw sla <ds> --manual --uninstall                                 # 改手动 + 卸掉它自己的任务
.\scripts\install_schedule.ps1 -Monitor -Time 07:00              # 外部源健康监控（全仓只需一次）
```
执行前把**任务名、时间、为什么选这个时间（相对哪个上游/哪些已有任务错开）**
念给用户确认，再动手注册（`dw sla ... --dry-run` 会只打印将要执行的任务
命令，但 sla 声明本身的改动是真落盘的，不受 `--dry-run` 影响——预览完再
决定要不要真的注册任务）。任务输出会自动写进 `logs/<TaskName>.log`。
用户也可以自己开 `dw panel --open` 在面板上改这些。

**回补任务默认不装定时任务。** 跑法是 `dw run <ds> --stage backfill`
手动/按需触发，一直到 `_meta/backfill_state.json` 的 `backfill_done: true`
为止。只有用户明确要"断更后自动追赶历史"，才帮忙装一个低频（比如每周
一次）的独立任务：

```bash
dw sla <ds> --stage backfill --runner own --schedule "0 3 * * 0" --install
```

`--stage backfill` 让这条命令只管 `dw-<ds>-backfill` 这一个独立任务，跟这个
dataset 正常 ingest/transform 该由谁跑（`sla.runner`/`sla.schedule`）互不
干扰——一个 `runner: family` 的表完全可以同时有一个独立的回补任务。

## 参考
- `references/splitting.md` —— 拆分四问 + SEC 案例（需要判断拆几个表时读）
- `references/contract_fields.md` —— 合约字段与质量规则清单（写 contract.yaml 时读）
