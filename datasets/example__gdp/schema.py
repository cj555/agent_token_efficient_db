# GENERATED from contract.yaml by `dw index` — 请勿手工编辑
"""example__gdp 的 pyarrow schema —— 类型真源，由 contract.yaml 派生。"""
import pyarrow as pa

DATASET = "example__gdp"
VERSION = "0.1.0"
GRAIN = ['country_code', 'year']
PARTITIONS = []

SCHEMA = pa.schema([
    pa.field("country_code", pa.string(), nullable=False),  # ISO 国家/地区代码
    pa.field("country_name", pa.string(), nullable=True),  # 国家/地区名称
    pa.field("year", pa.int32(), nullable=False),  # 年份
    pa.field("gdp_usd", pa.float64(), nullable=True),  # GDP（现价美元），缺测为 null
    pa.field("fetched_at", pa.timestamp("us"), nullable=False),  # 本行的抓取时间
])

COLUMNS = [f.name for f in SCHEMA]
