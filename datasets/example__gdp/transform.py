"""example__gdp — 阶段 2：raw/example_worldbank_gdp → storage/curated/example__gdp/

职责边界：纯本地、确定性、可无限重放。不触网（触网的事情归 ingest.py）。
"""
from __future__ import annotations

import datetime as dt
import json

import polars as pl

import dwlib as dw

DATASET = "example__gdp"
SOURCE_ID = "example_worldbank_gdp"


def build() -> pl.LazyFrame:
    raw_file = dw.paths().raw(SOURCE_ID) / "gdp.json"
    payload = json.loads(raw_file.read_text(encoding="utf-8"))
    records = payload[1] if isinstance(payload, list) and len(payload) > 1 else []

    rows = [
        {
            "country_code": rec["countryiso3code"] or rec["country"]["id"],
            "country_name": rec["country"]["value"],
            "year": int(rec["date"]),
            "gdp_usd": rec["value"],
        }
        for rec in records
        if rec.get("date")
    ]
    return (
        pl.LazyFrame(rows, schema={"country_code": pl.String, "country_name": pl.String,
                                   "year": pl.Int32, "gdp_usd": pl.Float64})
        .filter(pl.col("country_code").is_not_null() & (pl.col("country_code") != ""))
        .unique(subset=["country_code", "year"], keep="first")   # 守住 grain
        .with_columns(pl.lit(dt.datetime.now()).cast(pl.Datetime("us")).alias("fetched_at"))
        .sort(["country_code", "year"])
    )


def main() -> dict:
    cfg = dw.dataset_config(DATASET)
    return dw.write_curated(
        build(),
        DATASET,
        partition_by=cfg["transform"].get("partition_by") or None,
        mode=cfg["transform"].get("write_mode", "overwrite"),
    )


if __name__ == "__main__":
    print(main())
