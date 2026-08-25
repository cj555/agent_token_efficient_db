"""dwlib.http —— 带限流与重试的 HTTP 取数工具。

为什么放在 dwlib 而不是各 dataset 自己写一份：
**限流额度是按 API key 算的，不是按 dataset 算的**。polygon 的 5 个 dataset
共用同一个 key，`dw run --family polygon` 又是在同一个进程里按拓扑序跑
（见 runner.run_many），所以限流器必须是进程级共享的单例，各写各的会超额。

用法：
    from dwlib.http import RateLimiter, get_json, paginate

    lim = RateLimiter.shared("polygon", per_min=5)
    payload = get_json(cli, url, params, limiter=lim)
    for page in paginate(cli, url, params, limiter=lim, api_key=key):
        ...
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from typing import Any, Iterator

import httpx

DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 6

# 可重试的状态码：429 限流 + 5xx 服务端抖动。其余 4xx 是请求本身有问题，重试没意义。
_RETRYABLE = {429, 500, 502, 503, 504, 509, 520, 522, 524}


class RateLimiter:
    """滚动窗口限流：60 秒内最多 N 次调用。线程安全。"""

    _registry: dict[str, "RateLimiter"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, per_min: int) -> None:
        self.per_min = max(1, int(per_min))
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    @classmethod
    def shared(cls, key: str, per_min: int) -> "RateLimiter":
        """按 key 取进程级单例（同一个 API key / 同一个供应商共用一份额度）。"""
        with cls._registry_lock:
            lim = cls._registry.get(key)
            if lim is None or lim.per_min != per_min:
                lim = cls(per_min)
                cls._registry[key] = lim
            return lim

    def acquire(self) -> None:
        """阻塞直到有空位，然后占掉一个名额。"""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and (now - self._calls[0]) >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.per_min:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0]) + 0.05
            time.sleep(max(0.0, wait))


def get(cli: httpx.Client, url: str, params: dict | None = None,
        limiter: RateLimiter | None = None, headers: dict | None = None,
        max_retries: int = MAX_RETRIES, label: str = "") -> httpx.Response:
    """一次 GET，自带指数退避重试。返回 2xx 响应，否则抛 RuntimeError。"""
    backoff = 1.0
    last = ""
    for attempt in range(1, max_retries + 1):
        if limiter is not None:
            limiter.acquire()
        try:
            r = cli.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
            if attempt == max_retries:
                break
            time.sleep(backoff + random.uniform(0, 0.3))
            backoff = min(backoff * 2, 30.0)
            continue

        if r.status_code < 400:
            return r

        last = f"HTTP {r.status_code}: {r.text[:500]}"
        if r.status_code not in _RETRYABLE:
            raise RuntimeError(f"[{label or url}] {last}")
        if attempt == max_retries:
            break
        # 429 优先听服务端的 Retry-After
        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 120.0))
            except ValueError:
                time.sleep(backoff)
        else:
            time.sleep(backoff + random.uniform(0, 0.5))
        backoff = min(backoff * 2, 60.0)

    raise RuntimeError(f"[{label or url}] {max_retries} 次重试后仍失败：{last}")


def get_json(cli: httpx.Client, url: str, params: dict | None = None, **kw) -> Any:
    return get(cli, url, params, **kw).json()


def stream_to_file(cli: httpx.Client, url: str, target, *, gzip_out: bool = False,
                   limiter: RateLimiter | None = None, headers: dict | None = None,
                   max_retries: int = MAX_RETRIES, label: str = "",
                   chunk: int = 1 << 16) -> int:
    """把响应体**边收边写**到文件，返回写入的字节数。

    为什么不用 `get(...).text`：那会把整个响应先塞进内存。抓 8.6 万份 SEC 备案
    （单份最大 50 MB）时实测进程峰值冲到 **15.76 GB** —— 不是某一份特别大，
    而是反复分配/释放大块字符串把堆撑起来了，Windows 不会把它还给系统。
    流式写入把峰值钉在 chunk 大小上，与文件多大、抓多少份都无关。

    gzip_out=True 时直接压缩落盘（SEC 的 XML 实测 23.6:1）。
    写的是 `<target>.part`，成功后原子替换 —— 中断不会留半截文件。
    """
    import gzip as _gzip
    import shutil
    from pathlib import Path

    target = Path(target)
    tmp = target.with_name(target.name + ".part")
    backoff = 1.0
    last = ""

    for attempt in range(1, max_retries + 1):
        if limiter is not None:
            limiter.acquire()
        try:
            with cli.stream("GET", url, headers=headers, timeout=DEFAULT_TIMEOUT) as r:
                if r.status_code >= 400:
                    last = f"HTTP {r.status_code}"
                    if r.status_code not in _RETRYABLE:
                        raise RuntimeError(f"[{label or url}] {last}")
                    r.close()
                    if attempt == max_retries:
                        break
                    time.sleep(backoff + random.uniform(0, 0.5))
                    backoff = min(backoff * 2, 60.0)
                    continue
                opener = (lambda: _gzip.open(tmp, "wb", compresslevel=6)) if gzip_out \
                    else (lambda: tmp.open("wb"))
                n = 0
                with opener() as fh:
                    for buf in r.iter_bytes(chunk):
                        fh.write(buf)
                        n += len(buf)
            tmp.replace(target)
            return n
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
            if attempt == max_retries:
                break
            time.sleep(backoff + random.uniform(0, 0.3))
            backoff = min(backoff * 2, 30.0)
        finally:
            if tmp.exists() and not target.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    raise RuntimeError(f"[{label or url}] {max_retries} 次重试后仍失败：{last}")


def paginate(cli: httpx.Client, url: str, params: dict, api_key: str,
             limiter: RateLimiter | None = None, max_records: int | None = None,
             label: str = "") -> Iterator[list[dict]]:
    """跟着 Polygon 的 next_url 翻页，逐页 yield results。

    next_url 已经带全部分页参数，续page 只需再补 apiKey。
    max_records 用于「只要最近 N 条」的增量场景 —— 结果按时间倒序时，
    翻够就停，不必把十年历史重新拉一遍。
    """
    seen = 0
    cur_url, cur_params = url, dict(params)
    while cur_url:
        payload = get_json(cli, cur_url, cur_params, limiter=limiter, label=label)
        results = payload.get("results") or []
        seen += len(results)
        yield results

        nxt = payload.get("next_url")
        if not nxt:
            return
        if max_records is not None and seen >= max_records:
            return
        cur_url, cur_params = nxt, {"apiKey": api_key}
