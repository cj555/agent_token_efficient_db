# GENERATED from contract.yaml by `dw index` — 请勿手工编辑
"""example__gdp_growth 的 pyarrow schema —— 类型真源，由 contract.yaml 派生。"""
import pyarrow as pa

DATASET = "example__gdp_growth"
VERSION = "0.1.0"
GRAIN = ['country_code', 'year']
PARTITIONS = []

SCHEMA = pa.schema([
    pa.field("country_code", pa.string(), nullable=False),  # ISO 国家/地区代码
    pa.field("year", pa.int32(), nullable=False),  # 年份
    pa.field("gdp_usd", pa.float64(), nullable=True),  # 当年 GDP（现价美元）
    pa.field("gdp_growth_pct", pa.float64(), nullable=True),  # 相对上一年的名义增长率；上一年缺失时为 null
])

COLUMNS = [f.name for f in SCHEMA]
