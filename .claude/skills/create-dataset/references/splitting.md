# 拆分判定：一个需求要拆成几个 dataset？

## 核心不变量

**1 个 dataset = 1 张 curated 表**（物理上可按分区拆成多个 `part-*.parquet`，逻辑上仍是一张表）。

之所以强制 1:1：`contract.yaml`、`schema.py`、`test_contract.py`、registry 条目、`graph.json` 节点、
`dw deps/impact/rm` 的粒度，全都以「表」为单位。一个文件夹产两张表会让依赖图和影响分析立刻失真 ——
删除、变更、验证都无法精确到表。

## 四问

1. **grain 变了吗？**（新产物的主键与上游不同）→ **必须拆**。
2. grain 相同，但**重算触发条件或成本差异大**（换模型要重跑、换口径不用重跑）→ **拆**。
3. grain 相同、总是一起重算、总是一起被消费 → **不拆，加列**。
4. 产物**不是表**（原文、PDF、图片等 blob）→ 不做成表体，落 `storage/blob/<source_id>/`，
   再建一张 **manifest 表** dataset 索引它（列含 `blob_path` + `sha256` + 业务键）。

## 分层与目录

| 层 | 位置 | 组织方式 | 承诺 schema |
|---|---|---|---|
| raw | `storage/raw/<source_id>/` | 按**外部源**，多 dataset 共享，不重复下载 | 否，可随时清空重拉 |
| blob | `storage/blob/<source_id>/` | 按外部源，非表格资产 | 否，由 manifest 表索引 |
| curated | `storage/curated/<dataset>/` | 按 dataset，恰好 1 张表 | 是，即 `contract.yaml.columns` |
| tmp | `storage/tmp/<dataset>/` | 过程产物，随时可删 | 不进合约、不进 registry |

## 案例：SEC 10-K/10-Q → 原文 → MD&A → 词向量

答案是 **4 个 dataset**（不做分块则 3 个）：

| dataset | grain | 阶段 | 说明 |
|---|---|---|---|
| `sec__filings` | `accession` | ingest + transform | 原文落 `storage/blob/sec_edgar/`；表里存 `cik, form, filed_date, period, url, blob_path, sha256, bytes, fetched_at`。**唯一触网的 dataset** |
| `sec__mdna` | `accession` | transform only | 上游 `sec__filings`。列：`cik, period, mdna_text, char_len, extract_method, extract_status`。抽取失败也留行并标 status，便于统计成功率 |
| `sec__mdna_chunks` | `chunk_id`(= accession+idx) | transform only | 上游 `sec__mdna`。列：`accession, chunk_idx, text, token_len` |
| `sec__mdna_vectors` | `(chunk_id, model)` | transform only | 上游 `sec__mdna_chunks`。列：`vector: fixed_size_list<float32,768>, model, dim, embedded_at` |

要点：
- `sec__mdna` 与 `sec__filings` grain 相同（都是 accession），**仍然拆**：抽取逻辑改动频繁、重算成本高，
  且下载不该被抽取失败连累（规则 2）。
- `sec__mdna_vectors` 的 grain 是 `(chunk_id, model)` 而非 `chunk_id` —— 一段文本可能有多个模型的向量。
  这是**必须拆表的硬约束**（规则 1）。若确定永远只用一个模型，才可以合并进 chunks 加一列 `vector`。
- 原文是 blob 不是表（规则 4），所以 `sec__filings` 是 manifest 表。
- 只有 `sec__filings` 有 `ingest.py`。`dw health` / `/fix-source` 只盯它；EDGAR 改版只需修一处。
- 换 embedding 模型 = 只重跑最后一个 dataset，前三个的 parquet 原封不动。**这就是拆分的实际收益。**

## 命名

`<family>__<table>`，全小写下划线。同 family 可用 `dw ls --family <name>` 一起看。
单表需求可以不带 family 前缀。

## 向量列写法

```yaml
- name: vector
  type: fixed_size_list<float32,768>
  nullable: false
  desc: 句向量
```
读出来做矩阵计算：
```python
import dwlib as dw
mat = dw.vectors("sec__mdna_vectors")   # (n, 768) numpy，零拷贝
```
