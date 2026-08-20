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

### 5. 输出计划并等待批准
计划必须包含：family 拆分表、每个 dataset 的列草案、将新建的文件清单、外部源登记内容、定时任务安排、风险点。
**停在这里，等用户明确说「可以/批准/继续」再往下。**

### 6. 落地（批准后）
```bash
dw new --example-spec > /tmp/spec.yaml   # 看格式；然后按拆分表写 spec
dw new --family /tmp/spec.yaml           # 一次生成全族脚手架
```
然后按**拓扑序**（上游在前）逐个：
1. 补完 `contract.yaml` 的 `columns` / `quality`（grain 与 upstream 已由 spec 填好）
2. 实现 `ingest.py`（只有触网的 dataset 有）和 `transform.py`
3. 写 `tests/test_logic.py` 的业务断言（`test_contract.py` 是生成物，别动）
4. 更新 `README.md` 的一句话说明

### 7. 注册与验证
```bash
dw index                       # 注册进 INDEX.md / graph.json / registry.json
dw run --family <family>       # 按拓扑序跑 ingest→transform→test
dw validate                    # 合约校验
```

### 8. 维护任务（先问再装）
只给**触网的** dataset 装定时刷新；纯派生表通常跟着上游跑即可。
```bash
.\scripts\install_schedule.ps1 -Dataset <name> -Time 06:30
.\scripts\install_schedule.ps1 -Monitor -Time 07:00    # 外部源健康监控
```
执行前把要注册的任务名和时间念给用户确认。

## 参考
- `references/splitting.md` —— 拆分四问 + SEC 案例（需要判断拆几个表时读）
- `references/contract_fields.md` —— 合约字段与质量规则清单（写 contract.yaml 时读）
