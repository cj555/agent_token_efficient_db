# example__gdp

世界银行各国年度 GDP（现价美元）。框架的端到端冒烟样例：演示 http JSON 源 -> raw -> curated parquet 的完整链路。

- **grain**：待填（见 contract.yaml）
- **上游**：@example_worldbank_gdp
- **产物**：`storage/curated/example__gdp/`（1 dataset = 1 张表）

```bash
dw run example__gdp          # ingest + transform + test
dw validate example__gdp     # 合约校验
```

```python
import dwlib as dw
df = dw.load("example__gdp").collect()
```
