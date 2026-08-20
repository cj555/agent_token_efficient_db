"""外部数据源：登记、凭据解析、健康监控。external_sources.yaml 是唯一真源。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .config import Paths, paths

_DUR = re.compile(r"^(\d+)\s*([smhdw])$")
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_duration(text: str) -> dt.timedelta:
    m = _DUR.match(str(text).strip())
    if not m:
        raise ValueError(f"无法解析时长 '{text}'（形如 30m / 6h / 7d / 2w）")
    return dt.timedelta(**{_UNIT[m.group(2)]: int(m.group(1))})


def expand_env(value: Any) -> Any:
    """把 ${ENV_VAR} 占位替换为环境变量。凭据永远不写进 YAML 明文。"""
    def _sub(m):
        name = m.group(1)
        if name not in os.environ:
            raise KeyError(
                f"环境变量 {name} 未设置：请在仓库根的 .env 里写 {name}=<值>"
                f"（.env 已被 .gitignore 忽略，不会提交）"
            )
        return os.environ[name]

    if isinstance(value, str):
        return _ENV.sub(_sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def load_sources(p: Paths | None = None) -> dict[str, dict]:
    p = p or paths()
    if not p.external_yaml.is_file():
        return {}
    data = yaml.safe_load(p.external_yaml.read_text(encoding="utf-8")) or {}
    return data.get("sources", {}) or {}


def save_sources(sources: dict[str, dict], p: Paths | None = None) -> None:
    p = p or paths()
    p.external_yaml.parent.mkdir(parents=True, exist_ok=True)
    p.external_yaml.write_text(
        yaml.safe_dump({"version": 1, "sources": sources},
                       allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def get_source(source_id: str, p: Paths | None = None) -> dict:
    src = load_sources(p).get(source_id)
    if src is None:
        raise KeyError(f"external_sources.yaml 中无源 '{source_id}'")
    return expand_env(src)


# ---------------- 健康监控 ----------------

def check_source(source_id: str, src: dict, timeout: float = 20.0) -> dict:
    """单个外部源的健康探测。不下载全量，只做 HEAD / Range 抽样。"""
    import httpx

    now = dt.datetime.now().isoformat(timespec="seconds")
    res = {"source": source_id, "status": "ok", "reason": "", "checked_at": now,
           "kind": src.get("kind", "http")}
    kind = src.get("kind", "http")

    try:
        if kind == "local":
            f = Path(src["path"])
            if not f.exists():
                return {**res, "status": "fail", "reason": f"本地路径不存在: {f}"}
            mtime = dt.datetime.fromtimestamp(f.stat().st_mtime)
            res["last_modified"] = mtime.isoformat(timespec="seconds")
            res["bytes"] = f.stat().st_size if f.is_file() else None
        elif kind == "http":
            url = src["url"]
            headers = src.get("headers", {}) or {}
            with httpx.Client(timeout=timeout, follow_redirects=True) as cli:
                r = cli.head(url, headers=headers)
                if r.status_code in (403, 405, 501):     # 不支持 HEAD，退化为 Range GET
                    r = cli.get(url, headers={**headers, "Range": "bytes=0-2047"})
                if r.status_code >= 400:
                    return {**res, "status": "fail",
                            "reason": f"HTTP {r.status_code} {r.reason_phrase}"}
                res["http_status"] = r.status_code
                lm = r.headers.get("last-modified")
                if lm:
                    res["last_modified"] = lm
                cl = r.headers.get("content-length")
                if cl:
                    res["bytes"] = int(cl)
                if r.content:
                    res["head_sha256"] = hashlib.sha256(r.content[:2048]).hexdigest()[:16]
        else:
            return {**res, "status": "warn", "reason": f"未知 kind '{kind}'，跳过探测"}
    except Exception as e:
        return {**res, "status": "fail", "reason": f"{type(e).__name__}: {e}"}

    # 陈旧度
    freshness = src.get("freshness")
    lm = res.get("last_modified")
    if freshness and lm:
        try:
            when = _parse_http_date(lm)
            if dt.datetime.now() - when > parse_duration(freshness):
                res["status"] = "warn"
                res["reason"] = f"超过 freshness {freshness}（最后更新 {lm}）"
        except Exception:
            pass

    # 指纹变化（可能意味着上游改版）
    prev = src.get("_last_fingerprint")
    cur = res.get("head_sha256")
    if prev and cur and prev != cur:
        res["fingerprint_changed"] = True
    res["fingerprint"] = cur
    return res


def _parse_http_date(text: str) -> dt.datetime:
    from email.utils import parsedate_to_datetime
    try:
        d = parsedate_to_datetime(text)
        return d.replace(tzinfo=None)
    except Exception:
        return dt.datetime.fromisoformat(text)


def run_health(p: Paths | None = None, only: str | None = None) -> dict:
    p = p or paths()
    sources = load_sources(p)
    raw = load_sources(p)   # 未展开 env 的原件，用于回写指纹
    results = []
    for sid, src in sources.items():
        if only and sid != only:
            continue
        results.append(check_source(sid, expand_env(src)))

    report = {
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "warn": sum(1 for r in results if r["status"] == "warn"),
            "fail": sum(1 for r in results if r["status"] == "fail"),
        },
        "results": results,
    }
    p.health_dir.mkdir(parents=True, exist_ok=True)
    (p.health_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with (p.health_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 回写最新指纹，便于下次比对
    changed = False
    for r in results:
        if r.get("fingerprint") and raw.get(r["source"]) is not None:
            if raw[r["source"]].get("_last_fingerprint") != r["fingerprint"]:
                raw[r["source"]]["_last_fingerprint"] = r["fingerprint"]
                changed = True
    if changed:
        save_sources(raw, p)
    return report


def load_report(p: Paths | None = None) -> dict:
    f = (p or paths()).health_dir / "report.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
