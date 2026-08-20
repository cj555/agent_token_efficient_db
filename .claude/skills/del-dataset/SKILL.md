---
name: del-dataset
description: 彻底删除一个 dataset 及其全部关联对象（代码、parquet 数据、合约、定时任务、注册项、代码引用）。当用户说「删掉这个数据集/不要这份数据了/清理掉某个表」时使用。会先做依赖分析和 dry-run 清单，有下游时阻止删除并给出替代方案，批准后才执行。
---

# del-dataset

删除一个 dataset，不留残骸。

## 铁律

1. **先 dry-run，后动手。** `dw rm <ds>` 默认只列清单不删；批准前不加 `--apply`。
2. **有下游就不删。** 先迁移下游，或标 deprecated 观察一段时间。
3. 删除会连带 **OS 定时任务**和**存储**，这两样 git 回滚不回来 —— 计划里必须写清楚。
4. 共享同一外部源的 raw/blob **不删**（别的 dataset 还在用），`dw rm` 会自动判断。

## 步骤

### 1. 盘点
```bash
dw rm <dataset>              # dry-run：待删对象、体积、定时任务、下游、代码引用点
dw deps <dataset> --down     # 下游闭包
dw refs <dataset>            # 谁在代码里引用它
```
一条 `dw rm` 就够了，**不要自己去翻目录找残留**。

### 2. 有下游时：停下来给选项
不要直接删。把三个选项摆给用户：

| 选项 | 适用 | 动作 |
|---|---|---|
| 先迁移下游 | 下游还要继续用这份数据 | 走 `/change-contract` 把下游改到新上游，再回来删 |
| 标 deprecated 观察 | 不确定还有没有人用 | `status: deprecated` + changelog 写原因和替代品，过一阵再删 |
| 强制删除 | 确认下游也不要了 | 通常意味着要**一起删掉下游**，那就按拓扑逆序逐个删 |

### 3. 输出删除计划并等待批准
必须包含：
- 待删文件/目录清单与**释放的存储体积**
- 要卸载的定时任务名
- 下游与代码引用现状
- **不可恢复的部分**：`storage/` 里的 parquet（git 里没有）、blob 原文（重新下载可能很贵，甚至源已下线）
- 回滚方式：代码可 `git revert`；数据需要 `dw run <ds>` 重跑，**成本估计要写出来**

**停在这里等批准。** 这是不可逆操作，批准必须明确。

### 4. 执行（批准后）
```bash
dw rm <dataset> --apply
```
它会：删代码目录 → 删 curated/tmp → 删不再被共享的 raw/blob → 卸载 `dw-<dataset>-*` 计划任务 → `dw index`。

有下游时会被拒绝；确认要连坐删除时，**按拓扑逆序**（先删最下游）逐个执行，而不是加 `--force`。

### 5. 收尾验证
```bash
dw doctor                # 有没有悬空上游引用
dw validate              # 全仓校验
dw index                 # 确认 INDEX.md / graph.json / registry.json 已更新
grep -rn "<dataset>" datasets/ dwlib/ docs/ 2>/dev/null   # 兜底：残留的字符串引用
```
再检查 `data_contracts/external_sources.yaml`：如果某个 source 已经没有任何 dataset 引用了，
问用户是否一并删除该源条目和它的监控。

### 6. 报告
告诉用户：删了什么、释放多少空间、卸载了哪些任务、有没有残留需要人工处理。

## 只想清数据、保留代码？

那不是删除 dataset：
```bash
rm -rf storage/curated/<dataset>     # 之后 dw run 可重建
```
提醒用户这条更轻，问清楚他要的是哪一种。
