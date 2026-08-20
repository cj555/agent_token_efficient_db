# Token 基线

记录典型操作的实测 token 量，改动框架前后可对比。用 `/context` 或会话统计粗估即可，
重点是**同一操作在改动前后的相对变化**，不必追求绝对精确。

| 操作 | 命令序列 | 基线 (2026-08-20, 2 个 dataset) | 备注 |
|---|---|---|---|
| 看全仓有什么 | `dw ls` | ~200 tok | 每个 dataset 约 40 tok；100 个表约 4k |
| 找可复用数据源 | `dw search <kw>` | ~150 tok | 只返回命中项 |
| 看一个表的结构 | `dw show <ds> --fields schema` | ~250 tok | 整份 contract.yaml 约 900 tok |
| 依赖闭包 | `dw deps <ds> --down` | ~80 tok | |
| 变更影响面 | `dw impact <ds> --column c` | ~300 tok | 替代全仓 grep + 读命中文件（易破 5k） |
| 全仓校验 | `dw validate` | ~150 tok | |
| 外部源健康 | `cat .health/report.json` | ~400 tok | 替代 Claude 自己去探测 |

## 反模式（这些操作应该被上面的命令替代）

| 反模式 | 大致代价 |
|---|---|
| 读整个 `data_contracts/` 目录 | 随 dataset 数线性膨胀 |
| 通读一个 dataset 的全部代码 | 1.5–4k tok/个 |
| 全仓 `grep` 后逐个打开命中文件 | 3–10k tok |
| 让 LLM 逐字输出脚手架模板 | 2–3k tok/dataset（`dw new` 为 0） |

## 怎么量

移植实验时，在 `docs/migration_log.md` 里记下每阶段的近似 token 消耗。
如果某个操作明显超出基线，说明该操作缺一条 `dw` 子命令 —— 那就是下一个要加的功能。
