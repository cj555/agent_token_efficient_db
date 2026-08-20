"""example__gdp_growth — 阶段 2：example__gdp → storage/curated/example__gdp_growth/

纯内部派生 dataset：没有 ingest.py，不触网，可无限重放。
上游改口径时只需 `dw run example__gdp_growth --stage transform` 重算这一层，
不必重新下载 —— 这就是 ingest / transform 分离的收益。
"""
from __future__ import annotations

import polars as pl

import dwlib as dw

DATASET = "example__gdp_growth"


def build() -> pl.LazyFrame:
    lf = dw.load("example__gdp")
    return (
        lf.select(["country_code", "year", "gdp_usd"])
        .sort(["country_code", "year"])
        .with_columns(
            (
                (pl.col("gdp_usd") / pl.col("gdp_usd").shift(1).over("country_code") - 1) * 100
            ).alias("gdp_growth_pct")
        )
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
