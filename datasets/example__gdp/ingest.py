"""example__gdp — 阶段 1：世界银行 API → storage/raw/example_worldbank_gdp/

职责边界（不要越界）：
  * 只负责「把外部数据原样落地」+ 写 manifest（指纹/时间/条数）。
  * 不做清洗、不做类型转换、不做业务逻辑 —— 那些属于 transform.py。
  * 必须幂等：靠 manifest 里的 sha256 跳过重复下载（多个 dataset 共享同一 source 时尤其重要）。
"""
from __future__ import annotations

import hashlib
import json

import httpx

import dwlib as dw

DATASET = "example__gdp"
SOURCE_ID = "example_worldbank_gdp"


def _fetch_all_pages(cli: httpx.Client, base_url: str, headers: dict) -> tuple[dict, list]:
    """世界银行 API 响应是 `[分页元信息, 数据行...]`，`per_page=500` 时全库
    17490 条要翻 35 页——**这里必须翻完**，不能只读第一页。

    ⚠ 这是本函数存在的直接原因：旧版 ingest.py 只请求了 base_url 一次、只取
    `payload[1]`，实测这样只拿到第 1 页（500/17490 ≈ 2.9%），历史数据一直
    缺 97%，不是设计选择而是 bug。全量 35 次请求量很小（几十毫秒/次），
    没必要做增量/游标，每次运行直接翻完。
    """
    page, total_pages = 1, 1
    meta: dict = {}
    records: list = []
    while page <= total_pages:
        sep = "&" if "?" in base_url else "?"
        r = cli.get(f"{base_url}{sep}page={page}", headers=headers)
        r.raise_for_status()
        payload = json.loads(r.content)
        meta = payload[0] if isinstance(payload, list) and payload else {}
        page_records = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        if page_records:
            records.extend(page_records)
        total_pages = int(meta.get("pages") or 1)
        page += 1
    return meta, records


def main() -> dict:
    cfg = dw.dataset_config(DATASET)
    src = dw.get_source(SOURCE_ID)          # 已展开 ${ENV_VAR} 凭据
    raw_dir = dw.paths().raw(SOURCE_ID)
    raw_dir.mkdir(parents=True, exist_ok=True)

    manifest_f = raw_dir / "manifest.json"
    manifest = json.loads(manifest_f.read_text(encoding="utf-8")) if manifest_f.is_file() else {}

    with httpx.Client(timeout=60, follow_redirects=True) as cli:
        meta, records = _fetch_all_pages(cli, src["url"], src.get("headers", {}) or {})

    # gdp.json 保持跟改造前一样的 [元信息, 全部记录] 包装形状——transform.py
    # 读 payload[1] 的逻辑不用跟着改，只是这次 payload[1] 真的是全量而不是一页。
    content = json.dumps([meta, records], ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    if cfg["ingest"].get("skip_if_fingerprint_match") and manifest.get("sha256") == digest:
        return {"skipped": True, "reason": "指纹未变，跳过落地", "sha256": digest[:16]}

    target = raw_dir / "gdp.json"
    target.write_bytes(content)

    manifest = {
        "sha256": digest,
        "bytes": len(content),
        "records": len(records),
        "pages": int(meta.get("pages") or 1),
        "files": [target.name],
        "url": src["url"],
    }
    manifest_f.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(main())
