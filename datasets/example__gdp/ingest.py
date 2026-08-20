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


def main() -> dict:
    cfg = dw.dataset_config(DATASET)
    src = dw.get_source(SOURCE_ID)          # 已展开 ${ENV_VAR} 凭据
    raw_dir = dw.paths().raw(SOURCE_ID)
    raw_dir.mkdir(parents=True, exist_ok=True)

    manifest_f = raw_dir / "manifest.json"
    manifest = json.loads(manifest_f.read_text(encoding="utf-8")) if manifest_f.is_file() else {}

    with httpx.Client(timeout=60, follow_redirects=True) as cli:
        r = cli.get(src["url"], headers=src.get("headers", {}) or {})
        r.raise_for_status()
        content = r.content

    digest = hashlib.sha256(content).hexdigest()
    if cfg["ingest"].get("skip_if_fingerprint_match") and manifest.get("sha256") == digest:
        return {"skipped": True, "reason": "指纹未变，跳过落地", "sha256": digest[:16]}

    target = raw_dir / "gdp.json"
    target.write_bytes(content)

    payload = json.loads(content)
    records = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    manifest = {
        "sha256": digest,
        "bytes": len(content),
        "records": len(records),
        "files": [target.name],
        "url": src["url"],
    }
    manifest_f.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(main())
