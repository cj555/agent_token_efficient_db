# breaking 变更的标准处理

## 加列（additive，最简单）

1. `contract.yaml` 加列定义（新列**必须** `nullable: true`，除非同时回填）
2. `transform.py` 产出该列
3. `version` minor+1，changelog `kind: additive`
4. `dw index && dw run <ds> --stage transform && dw validate`

下游不用动。

## 删列

1. `dw impact <ds> --column <列>` → 若有任何引用，**先改下游**再删
2. 下游改完并跑通后，再从合约和 `transform.py` 里去掉
3. major+1，changelog 写清替代方案

分两次提交更安全：先让下游不再用它，再真正删。

## 改列名

等价于「加新列 + 迁移下游 + 删旧列」。急的话可以在 `transform.py` 里同时产出新旧两列，
标记旧列 `desc: "DEPRECATED，2026-Q4 移除，改用 <新列>"`，一个周期后再删。

## 改类型

| 情况 | 处理 |
|---|---|
| 加宽（int32→int64, float32→float64） | 直接改，additive 级别，重跑 transform 即可 |
| 收窄（int64→int32） | 先 `dw sql "select max(x), min(x) from <ds>"` 确认不溢出 |
| 语义变化（string→timestamp） | breaking：下游所有比较/格式化逻辑都要检查 |
| 向量维度变化 `fixed_size_list<float32,768>` → `1536` | breaking，必须全量重算；考虑改成 grain 含 `model` 的新行而非改列 |

## 改 grain ★ 最重

**先停下来问：这真的是同一个 dataset 吗？**
grain 变了通常意味着这是一张**新表**（拆分四问的规则 1）。典型正确做法是新建一个 dataset，
把旧的标 deprecated，而不是原地改 grain。

确实要原地改（例如从 `[id]` 收紧为 `[id, date]` 修正了原本的错误）时：
1. 改 `contract.yaml` 的 `grain`
2. `transform.py` 的去重/聚合逻辑必须同步改，否则 `dw validate` 会报 `grain_duplicate`
3. 全量重跑（不能增量），major+1
4. 所有下游 join 的 key 都要检查 —— `dw impact` 列出的每个 `transform.py` 都要看

## 改口径（列不变，含义变）

最危险的一类：schema 校验**发现不了**。
1. changelog 必须写清「旧口径 → 新口径」和生效范围
2. 若历史数据不重算，在 `purpose` 或列 `desc` 里注明分界日期
3. 考虑加一个 `quality` 规则守住新口径的取值范围
4. 通知所有 `dw deps --down` 出来的下游 owner

## 回滚

```bash
git revert <commit>
dw index
dw run <dataset> --stage transform    # 只重算，不重新下载
dw validate
```
`storage/raw/` 里的原始数据没被动过，所以回滚不需要重新触网 —— 这是 ingest/transform 分离的又一个收益。
