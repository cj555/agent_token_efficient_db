---
name: fix-source
description: 诊断并修复外部数据源故障（下载失败、404、认证失效、限流、上游改版导致的 schema drift、数据超期未更新）。当监控报警、dw health 报 fail/warn，或用户说「数据下不下来了/源挂了/接口变了」时使用。会先读 .health/report.json 定位，给出修复计划，批准后才改代码。
---

# fix-source

外部数据源出问题时的修复流程。

## 铁律

1. **先读报告，不要自己去探测。** `.health/report.json` 是监控脚本刚跑出来的结果，直接读它。
2. **先计划，后动手。** 批准前不改任何文件。
3. **只改 ingest 层。** 外部源问题的修复面应该只有 `ingest.py` + `external_sources.yaml`。
   如果发现必须改 `transform.py` 或合约，说明是 schema drift —— 转 `/change-contract` 流程。
4. **凭据永远不写进文件。** 只写 `${ENV_VAR}` 占位，实际值让用户放进 `.env`。

## 步骤

### 1. 定位
```bash
cat .health/report.json                  # 最新一轮结果
dw health --verbose                      # 或现场重跑一次
tail -20 .health/history.jsonl           # 什么时候开始坏的
```
找出受影响的 dataset：
```bash
dw ls --json | ...    # 或直接看 report 里 monitor 打印的「受影响 dataset」
dw deps <dataset> --down                 # 波及范围
```

### 2. 分类

| 症状 | 可能原因 | 修法 |
|---|---|---|
| HTTP 404 / 路径不存在 | 上游改了 URL 结构或归档了旧文件 | 更新 `external_sources.yaml` 的 url；必要时改 ingest 的路径拼接 |
| HTTP 401 / 403 | token 过期、需要 UA、需要注册 | 让用户更新 `.env`；补 `headers`（如 SEC 要求 User-Agent） |
| HTTP 429 / 超时 | 限流 | ingest 加退避重试、降并发；`config.yaml.runtime.timeout_seconds` 调大 |
| 指纹变了 + 解析报错 | **schema drift**：上游改字段 | 见下 |
| `超过 freshness` | 上游停更或本地任务没跑 | 先查计划任务是否还在（`schtasks /query \| findstr dw-`） |
| 本地路径不存在 | 盘符/目录被移动 | 改 `external_sources.yaml` 的 `path` |

### 3. schema drift 的特殊处理
上游加/删/改字段时：
1. 先看清楚变了什么 —— 抓一小段样本，**不要下载全量**
2. 判断是否影响本仓合约：
   - 只是多了字段、我们不用 → 只改 `ingest.py`/`transform.py` 的解析，合约不变
   - 我们用的字段改名/消失 → **必须走 `/change-contract`**，先算影响面
3. 在 `external_sources.yaml` 该源下追加 `history` 记录这次变更

### 4. 输出修复计划并等待批准
包含：故障源与症状、根因判断、受影响 dataset、要改的文件与改法、是否需要回填历史数据、验证方式。
**停在这里等批准。**

### 5. 执行（批准后）
1. 改 `data_contracts/external_sources.yaml`（url / headers / freshness）
2. 改对应 dataset 的 `ingest.py`
3. 需要凭据时，告诉用户在 `.env` 里放什么变量名（**不要替用户填值**）

### 6. 验证
```bash
dw health --source <sid>                 # 应转绿
dw run <dataset> --stage ingest          # 抽样验证能真的下下来
dw run <dataset> --stage transform,test
dw validate <dataset>
```

### 7. 留痕
在 `external_sources.yaml` 对应源下追加：
```yaml
    history:
    - date: "2026-08-20"
      event: "上游改版：json 字段 value → amount"
      action: "更新 ingest.py 解析；合约未变"
```

## 监控本身没在跑？

```powershell
.\scripts\install_schedule.ps1 -Monitor -Time 07:00     # 注册每日检查
.\scripts\install_schedule.ps1 -List                    # 看已注册的任务
```

## 参考
- `references/ingest_patterns.md` —— 重试/限流/分页/增量下载的写法
