# ingest.py 常用写法

所有片段都遵守一条：**ingest 只负责原样落地 + 写 manifest，不做清洗**。

## 幂等 + 指纹跳过（多 dataset 共享 source 时必备）

```python
digest = hashlib.sha256(content).hexdigest()
if cfg["ingest"].get("skip_if_fingerprint_match") and manifest.get("sha256") == digest:
    return {"skipped": True, "reason": "指纹未变", "sha256": digest[:16]}
```

## 退避重试 + 限流

```python
import time, httpx

def get_with_retry(cli, url, *, headers=None, tries=4, base=1.5):
    for i in range(tries):
        r = cli.get(url, headers=headers or {})
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("Retry-After", base ** i))
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"重试 {tries} 次仍失败: {url} ({r.status_code})")
```

限速（例如 SEC 要求 ≤10 req/s）：每次请求后 `time.sleep(0.11)`，或用信号量控制并发。

## 需要 User-Agent 的源（SEC EDGAR 等）

```yaml
# external_sources.yaml
  sec_edgar:
    kind: http
    url: https://www.sec.gov/Archives/edgar/full-index/
    headers:
      User-Agent: "${SEC_USER_AGENT}"     # .env: SEC_USER_AGENT="Name email@example.com"
```

## 分页

```python
rows, page = [], 1
while True:
    r = get_with_retry(cli, f"{base}?page={page}&per_page=500")
    payload = r.json()
    batch = payload[1] if isinstance(payload, list) else payload.get("results", [])
    if not batch:
        break
    rows.extend(batch)
    page += 1
    if page > max_pages:      # 兜底，别写死循环
        break
```

## 增量下载（只取新增）

```python
state = dw.run_state(DATASET)               # 上次跑到哪
since = state.get("watermark")
...
manifest["watermark"] = new_max_date        # 落进 manifest，下次接着来
```
增量的水位线写进 raw 的 `manifest.json`，不要写进 curated —— curated 是 transform 的产物。

## blob 类源（原文/PDF/图片）

```python
blob_dir = dw.paths().blob(SOURCE_ID)
blob_dir.mkdir(parents=True, exist_ok=True)

records = []
for item in items:
    target = blob_dir / f"{item['id']}.html"
    if target.exists():                      # 幂等：已有就不重下
        continue
    r = get_with_retry(cli, item["url"], headers=headers)
    target.write_bytes(r.content)
    records.append({
        "id": item["id"],
        "blob_path": str(target.relative_to(dw.paths().storage)),
        "sha256": hashlib.sha256(r.content).hexdigest(),
        "bytes": len(r.content),
    })
```
原文进 blob，**清单进 manifest 表 dataset**（这是拆分规则 4）。

## 本地文件源

```yaml
  my_local_dump:
    kind: local
    path: D:/exports/latest.parquet
    freshness: 1d
```
```python
src = dw.get_source(SOURCE_ID)
shutil.copy2(src["path"], dw.paths().raw(SOURCE_ID) / "latest.parquet")
```

## 凭据

```yaml
    headers:
      Authorization: "Bearer ${MY_API_TOKEN}"
```
`dw.get_source()` 会自动把 `${VAR}` 展开成环境变量。`.env` 已被 `.gitignore` 忽略。
**永远不要把真实 token 写进 yaml 或 py 文件。**
