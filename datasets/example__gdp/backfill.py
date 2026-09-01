"""example__gdp — 历史回补：这张表没有真正的"历史缺口"可回补。

修复 `ingest.py` 的翻页 bug（旧版只读 World Bank API 响应的第 1 页，
500/17490 ≈ 2.9%）后，daily `ingest.py` 每次运行都会翻完全部页拿到完整历史
（几十次请求、几秒钟量级）——World Bank 的年度 GDP 接口本来就不是"只给最近
窗口"的增量源，没有"水位线到今天"之外还需要另外回补的历史。

按 `04-other-datasets.md` 的许可（"如果全量历史本来就能在一次请求循环里
秒级拿到，backfill.py 可以是几行的薄封装"），这里不引入游标/预算概念，
直接复用 `ingest.main()`（daily 增量和"回补"在这张表上其实是同一件事），
跑一次就标记为已完成。
"""
from __future__ import annotations

import datetime as dt

import dwlib as dw

from datasets.example__gdp import ingest

DATASET = "example__gdp"


def main() -> dict:
    st = dw.backfill_state(DATASET)
    if st.done:
        return {"skipped": True, "reason": "已完成（这张表没有需要分批回补的历史）",
                "done": True}

    result = ingest.main()               # daily ingest 本来就拿全量，直接复用
    st.advance(dt.date(1960, 1, 1), done=True)   # World Bank 数据从 1960 年开始，一次性标完成
    return {**result, "cursor": "1960-01-01", "done": True}


if __name__ == "__main__":
    print(main())
