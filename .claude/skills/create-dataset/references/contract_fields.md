# contract.yaml 字段速查

```yaml
name: sec__mdna              # = 文件夹名 = curated 目录名 = SQL 视图名
version: 0.1.0               # 语义化；breaking 变更时 major+1
owner: ""
status: draft                # draft | active | deprecated
domain: filings
family: sec
tags: [nlp, sec]

purpose: |                   # ★ dw search 的主检索面，写具体（是什么/为什么/怎么用）
  从 10-K/10-Q 原文抽取的 MD&A 段落，供下游做分块与向量化。

grain: [accession]           # 主键列。grain 变了就该拆新 dataset
partitions: []               # 例：[year] → hive 分区目录
watermark: filed_date        # 可选；增量更新用的水位线列名，见 dw.watermark()。
                              # 没有可回补历史的数据源不用声明这个字段。

columns:
- name: accession
  type: string
  nullable: false
  unique: true
  desc: SEC 报送编号
  unit: null                 # 有物理单位时写，例：USD / percent / bytes

quality:
- rule: row_count_between
  min: 1000
  severity: error            # error | warn

upstream:
- kind: external             # external = 触网，会被 dw health / fix-source 监控
  ref: sec_edgar
- kind: dataset
  ref: sec__filings

sla:
  freshness: 7d              # 超过则 dw health / freshness_within 报 warn
  schedule: "0 6 * * *"      # cron；null = 手动
  stage: all                 # ingest | transform | all

changelog:
- version: 0.1.0
  date: "2026-08-20"
  kind: init                 # init | additive | breaking | fix
  note: 初始创建
```

`consumers` 是**派生字段**，由 `dw index` 算进 graph/registry，不写进本文件。

## 列类型

`int8/16/32/64` · `uint8/16/32/64` · `float32/64` · `bool` · `string` · `binary`
`date32` · `timestamp[us]` · `timestamp[ns]` · `time64[us]`
`list<T>` · `fixed_size_list<float32,768>`（向量）· `decimal128<38,9>`

## 质量规则

| rule | 需要的字段 | 含义 |
|---|---|---|
| `row_count_between` | min / max | 行数区间 |
| `not_null` | column | 该列无 null |
| `unique` | column | 该列唯一 |
| `accepted_values` | column, values | 枚举白名单 |
| `value_between` | column, min / max | 数值区间 |
| `column_regex` | column, pattern | 正则匹配 |
| `freshness_within` | column, window | 该时间列的最新值不超过 window（如 `7d`） |

`severity: error` 会让 `dw validate` 和 `pytest` 失败；`warn` 只提示。

grain 的唯一性、列的 `nullable`/`unique` 由框架自动检查，**不必**再写成质量规则。
