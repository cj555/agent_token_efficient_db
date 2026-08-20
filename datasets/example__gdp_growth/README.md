# example__gdp_growth

由 example__gdp 派生的同比增长率。演示纯内部派生 dataset（只有 transform.py、不触网）与依赖图/影响分析。

- **grain**：待填（见 contract.yaml）
- **上游**：example__gdp
- **产物**：`storage/curated/example__gdp_growth/`（1 dataset = 1 张表）

```bash
dw run example__gdp_growth          # ingest + transform + test
dw validate example__gdp_growth     # 合约校验
```

```python
import dwlib as dw
df = dw.load("example__gdp_growth").collect()
```
