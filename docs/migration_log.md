# 移植实验日志

每做一次真实数据集移植，追加一条。这是改进本框架的**唯一依据** —— 避免凭感觉改。

模板：

```markdown
## YYYY-MM-DD  <源项目名>

- **源形态**：一个大脚本 / notebook + 散落 parquet / Airflow DAG / 其他
- **规模**：N 个 py 文件，M 行，已有数据 X GB
- **拆成**：`fam__a`(grain) / `fam__b`(grain) / ...
- **耗时**：盘点 __ 分钟，计划 __ 分钟，落地 __ 分钟
- **摩擦点**：
  - （哪一步 Claude 走偏了？哪条命令的输出不够用？哪个模板不合适？）
- **token 观察**：盘点阶段约 __k，落地阶段约 __k（对比 `docs/token_budget.md` 的基线）
- **对框架的改进项**：
  - [ ] ...
```

---

## 2026-08-20  框架初始化（非移植，作为基线）

- **源形态**：无，从零搭建
- **拆成**：`example__gdp`(country_code+year) / `example__gdp_growth`(country_code+year)
- **摩擦点**：
  - `dw index` 早期版本会回写 `contract.yaml` 来填 `consumers`，把人写的注释全冲掉了。
    → 已修：`consumers` 改为纯派生字段，只进 `graph.json` / `registry.json`。
  - `dw new --family` 早期用 `dump_contract` 写 grain/upstream，同样丢注释。
    → 已修：改为把 grain/upstream 渲染进模板占位符。
  - `dw new --family` 遇到已存在的 dataset 会整族中断。
    → 已修：跳过已存在的，支持断点续做。
- **对框架的改进项**：
  - [ ] 等真实移植实验暴露更多问题
