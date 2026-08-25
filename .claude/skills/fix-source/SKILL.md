---
name: fix-source
description: 诊断并修复外部数据源故障（下载失败、404、认证失效、限流、上游改版导致的 schema drift、技术文档变更、数据超期未更新）。当监控报警、dw health 报 fail/warn，或用户说「数据下不下来了/源挂了/接口变了」时使用。会先跑 dw health --broken --json 定位，给出修复计划，批准后才改代码。
---

# fix-source

外部数据源出问题时的修复流程。**一次只处理一个源。**

## 铁律

### 1. 不编造数据
事实只能有三个来源：`dw health --broken --json` 的输出、`dw source show <sid>` 的输出、
你**亲自跑过**的命令回显（含 `.health/docs/<sid>/*.diff`）。

- **禁止**凭印象猜新的 URL、字段名、参数名、限速数字。上游改了什么，抓一条真实样本看
  （`limit=1`，绝不下载全量）；抓不到就**停下来把情况报给用户**，不要写进代码。
- 计划里每条判断都要标出处（哪条命令、哪个文件的哪一行）。写不出出处的，就是猜的。
- 报告故障时照抄 reason 原文，不要润色成「大概是限流」。

### 2. 不死锁
- 开工前先看 digest 里的 `quarantined`：为 `true` 就**直接停**，输出历次尝试纪要
  （`dw fixlog <sid>`）交人工，不再重试。
- 一轮 = 改一次 + 验证一次。失败就记一笔：`dw fixlog <sid> fail --note "<试了什么、为什么不行>"`。
  **连续第 3 次失败自动熔断**，此后本流程一律拒绝再动这个源。
- 单条验证命令别挂着等：ingest 抽样验证给 10 分钟上限，超时就算这一轮失败，记 fail。
- 修好了记 `dw fixlog <sid> ok --note "..."` 清零。

### 3. 省 token
- 入口**只跑一条命令**：`dw health --broken --json`。它已经把「谁坏了 + 从什么时候坏的 +
  受影响 dataset + 要改哪个 ingest.py + 试过几次」都算好了。
- **不要** `cat .health/report.json`、**不要**读整份 `external_sources.yaml`
  （用 `dw source show <sid> --fields url,url_template,headers,schema_probe,docs`）、
  **不要** `dw health --verbose` 全量重跑（复查只跑 `dw health --source <sid>`）。
- `ingest.py` 先 Grep 定位相关行再局部读，别整文件吞。
- 文档变更看 `.health/docs/<sid>/*.diff`，别自己去重新抓文档页。

### 4. 局部维护
改动面严格限定在两处：**该源在 `external_sources.yaml` 里的那一段** + **它的消费者 dataset 的
`ingest.py`**。

- 用 Edit 做外科手术式改动（那份 yaml 的注释是它一半的价值），**永远不要调 `save_sources()`**。
- 一旦发现必须改 `transform.py` / `contract.yaml`，说明是 schema drift 波及了合约 ——
  转 `/change-contract`，本流程到此为止。
- 凭据永远只写 `${ENV_VAR}` 占位，真值让用户自己放进 `.env`（**不要替用户填值**）。

### 5. 先给计划，人批准再动手
批准前不改任何文件。

## 步骤

### 1. 定位（一条命令）
```bash
dw health --broken --json
```
输出里每个坏源都带：`status` / `reason` / `since`（哪天开始坏的）/ `consumers` /
`ingest_files` / `attempts` / `quarantined`。另外两块：
- `pending_ack` —— schema 或技术文档变了、还没人判断过的。**这是要判断，不一定要修。**
- `stale_datasets` —— curated 表没如期产出（外部源可能好好的，是本地任务没跑）。

要看某个源的配置再跑 `dw source show <sid>`。面板 `.health/dashboard.html` 是给人看的，
你不用读它。

### 2. 分类

| 症状 | 可能原因 | 修法 |
|---|---|---|
| HTTP 404 / 路径不存在 | 上游改了 URL 结构或归档了旧文件 | 更新 `external_sources.yaml` 的 url；必要时改 ingest 的路径拼接 |
| HTTP 401 / 403 | token 过期、需要 UA、需要注册 | 让用户更新 `.env`；补 `headers`（如 SEC 要求 User-Agent） |
| HTTP 429 / 超时 | 限流 | ingest 加退避重试、降并发（`dwlib.http` 已有 RateLimiter/退避，别自己造） |
| `schema 变更` 且只有 `schema_added` | 上游加了字段，我们不一定用 | 多半无影响 → 判定后 `dw ack` |
| `schema 变更` 有 `schema_removed` | **我们在用的字段没了** | 先 `dw refs <dataset> --column <c>` 看有没有人用；用了就走 `/change-contract` |
| `技术文档有变更` | 上游改了 API 说明 | 读 `.health/docs/<sid>/*.diff`，判断是否影响本仓用法 |
| `逾期未更新`（stale_datasets） | 上游停更，或**本地计划任务没跑** | 先查任务还在不在：`schtasks /query \| findstr dw-`；再看 `logs/` |
| 本地路径不存在 | 盘符/目录被移动 | 改 `external_sources.yaml` 的 `path` |

### 3. schema drift 的特殊处理
1. 先看清楚变了什么 —— digest 里的 `schema_added` / `schema_removed` 是**具名字段**，
   够判断就别再抓样本；不够就抓一条（`limit=1`）。
2. 判断是否影响本仓合约：
   - 只是多了字段、我们不用 → 只改 `ingest.py` 的解析（或什么都不用改）
   - 我们用的字段改名/消失 → **必须走 `/change-contract`**，先算影响面
3. 真改了源配置（url / headers / 解析口径）才在 `external_sources.yaml` 该源下追加 `history`。

### 4. 输出修复计划并等待批准
包含：
- 故障源与症状（**附出处**）
- 根因判断与置信度
- 受影响 dataset
- 逐文件改法
- 是否需要回填历史数据
- 验证命令
- **本次是第几轮 / 还剩几轮**（见铁律 2）

**停在这里等批准。**

### 5. 执行（批准后）
1. Edit `data_contracts/external_sources.yaml` 里**该源那一段**（url / headers / freshness /
   schema_probe / docs）
2. Edit 对应 dataset 的 `ingest.py`
3. 需要凭据时，告诉用户在 `.env` 里放什么变量名

### 6. 验证
```bash
dw health --source <sid>                 # 应转绿（只探这一个源，不会覆盖别人的结果）
dw run <dataset> --stage ingest          # 抽样验证能真的下下来
dw run <dataset> --stage transform,test
dw validate <dataset>
```
成了记 `dw fixlog <sid> ok --note "..."`；没成记 `fail`，进入下一轮（最多 3 轮）。

### 7. 收尾：**每个 warn 都要有结论**
`schema 变更` / `技术文档变更` 这类提示型 warn 不会自己消失 —— 上游改完就一直是新样子。
处理完必须三选一：

| 结论 | 动作 |
|---|---|
| 修了 | `dw ack <sid> --schema --note "value→amount，已改 ingest 解析"` |
| 判定无影响 | `dw ack <sid> --schema --note "仅新增 unused 字段，本仓不消费"` |
| 影响合约 | **不 ack**，转 `/change-contract`；合约改完再回来 ack |

文档变更同理：`dw ack <sid> --docs <url> --note "..."`（多条用 `--docs-all`）。
**文档的确认按 URL 记，不按源记** —— 一个 family 共用一份 changelog（polygon 五个源共用
Massive 的 RSS、sec 三个源共用 EDGAR 技术规格页），判断一次就对所有源生效，
不要对着五个源确认五遍同一件事。schema 的确认才是按源记的。
`--note` 必填，它就是留痕 —— 不必再手抄一份到 yaml 的 `history`，
只有**真改了源配置**才写 `history`：

```yaml
    history:
    - date: "2026-08-20"
      event: "上游改版：json 字段 value → amount"
      action: "更新 ingest.py 解析；合约未变"
```

确认记的是**当时那个版本的 hash**，上游再变一次会重新报 warn —— 不是永久静音。
真想彻底不监控某个源的结构，是把它的 `schema_probe` / `docs` 删掉（面板会诚实标灰
「未监控」），而不是拿 ack 把它糊过去。

## 监控本身没在跑？

```powershell
.\scripts\install_schedule.ps1 -Monitor -Time 07:00     # 注册每日检查
.\scripts\install_schedule.ps1 -List                    # 看已注册的任务
```
监控每轮都会刷新 `.health/dashboard.html`（给人看的面板）。

## 参考
- `references/ingest_patterns.md` —— 重试/限流/分页/增量下载的写法
