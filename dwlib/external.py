"""外部数据源：登记、凭据解析、健康监控。external_sources.yaml 是唯一真源。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from html.parser import HTMLParser
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


def _load_state(name: str, p: Paths | None = None) -> dict:
    """.health/ 下的某份运行状态（指纹 / schema / 文档索引）。

    这些都存 .health/ 而不是合约文件，理由见 run_health 里的说明。
    """
    f = (p or paths()).health_dir / name
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_fingerprints(p: Paths | None = None) -> dict[str, str]:
    return _load_state("fingerprints.json", p)


def save_sources(sources: dict[str, dict], p: Paths | None = None) -> None:
    """⚠ 会把 external_sources.yaml 整个重写，**注释会全部丢失**。

    这个文件是人写人读的真源（见 CLAUDE.md），注释是它一半的价值。
    只有在用户明确要求程序化改写外部源时才调用它 —— 例行的状态回写
    （指纹、探测时间之类）一律写 .health/，别碰这里。
    """
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


# ---------------- schema 探针 ----------------
#
# 为什么不能只靠 head_sha256：那是探测 URL 前 2KB 的**原始字节**指纹，对 JSON API
# 几乎是噪声（任何一个值变了就翻），而 polygon 那几个源的探测 URL 是 marketstatus/now，
# 跟真正取数的 url_template 根本不是一个端点 —— 上游把 results[].value 改名成 amount
# 这种事，字节指纹一点都测不出来，只能等 ingest 炸了才知道。
#
# 所以另开一条可选的小样本探针：源里写 schema_probe.url（真实取数端点，limit=1），
# 抓一条记录，只取**结构**（key 路径集合）做哈希，值怎么变都不影响。
# 没配探针的源保持现状，面板标灰「未监控」——诚实地承认测不到，好过绿着骗人。

_TOK = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
SCHEMA_MAX_DEPTH = 3
SCHEMA_MAX_KEYS = 200


def _dig(obj: Any, node: str | None) -> Any:
    """按 `results[0].items` / `[1][0]` / `0` 这样的路径下钻。取不到就抛 KeyError。"""
    if not node:
        return obj
    cur = obj
    for name, idx in _TOK.findall(str(node)):
        if name:
            if not isinstance(cur, dict) or name not in cur:
                raise KeyError(f"schema_probe.node 取不到 '{name}'")
            cur = cur[name]
        else:
            i = int(idx)
            if not isinstance(cur, list) or len(cur) <= i:
                raise KeyError(f"schema_probe.node 取不到 '[{i}]'（不是数组或越界）")
            cur = cur[i]
    return cur


def json_key_paths(obj: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """收集 JSON 的 key 路径集合。数组只看第 0 个元素，路径里写 []。

    只看结构不看值：同结构不同值 → 同一份路径集合 → 同一个 hash。
    """
    out: list[str] = []
    if depth >= SCHEMA_MAX_DEPTH:
        return out
    if isinstance(obj, dict):
        for k in sorted(obj)[:SCHEMA_MAX_KEYS]:
            path = f"{prefix}.{k}" if prefix else str(k)
            out.append(path)
            out += json_key_paths(obj[k], path, depth + 1)
    elif isinstance(obj, list) and obj:
        out += json_key_paths(obj[0], f"{prefix}[]", depth + 1)
    return out[:SCHEMA_MAX_KEYS]


def schema_hash(keys: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(keys))).encode()).hexdigest()[:16]


def probe_schema(source_id: str, src: dict, timeout: float = 30.0) -> dict:
    """抓一条样本，返回 {schema_keys, schema_hash}；失败只降级不判死。"""
    import httpx

    from .http import RateLimiter, get

    probe = src.get("schema_probe")
    # 写成 `schema_probe: ~ # 原因` 表示「明确声明不监控」——与「压根没想过这件事」
    # 区分开：前者 dw doctor 不再唠叨，面板标「未监控（已声明）」。
    if not isinstance(probe, dict) or not probe.get("url"):
        return {}
    url = probe["url"]
    # 限流额度按 API key 算：polygon 五个源共用一份，探针必须挤进同一个限流器，
    # 不然 dw health 一轮就能把 5 次/分钟的额度吃光，把 ingest 顶到 429。
    per_min = src.get("rate_limit_per_min")
    lim = (RateLimiter.shared(src.get("rate_limit_key") or source_id.split("_")[0],
                              int(per_min)) if per_min else None)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as cli:
            r = get(cli, url, headers=src.get("headers") or {}, limiter=lim,
                    max_retries=2, label=f"schema_probe:{source_id}")
        node = _dig(r.json(), probe.get("node"))
        # 有的源把列名放在**值**里而不是 key 里（如 SEC 的 company_tickers_mf.json
        # 是 {fields: [...], data: [[...]]}），这时 node 直接指向 fields，
        # 把这串字符串当作结构本身 —— 列改名同样能测出来。
        keys = (sorted(str(x) for x in node)
                if isinstance(node, list) and node
                and all(isinstance(x, (str, int, float, bool)) for x in node)
                else json_key_paths(node))
        if not keys:
            return {"schema_probe_error": "样本里没有任何 key（node 取空了？）"}
        return {"schema_keys": keys, "schema_hash": schema_hash(keys)}
    except Exception as e:                     # 探针挂了不代表源挂了
        return {"schema_probe_error": f"{type(e).__name__}: {e}"}


# ---------------- 上游技术文档变更追踪 ----------------

class _TextExtractor(HTMLParser):
    """扒纯文本。用 stdlib 就够，不为这个引 bs4/jinja2 之类的新依赖。"""

    _SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(" ".join(data.split()))


def html_to_text(html: str) -> str:
    ex = _TextExtractor()
    try:
        ex.feed(html)
    except Exception:
        pass
    return "\n".join(ex.parts)


def _slug(url: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", url.split("://", 1)[-1]).strip("-")
    return (s[:60] or "doc") + "-" + hashlib.sha256(url.encode()).hexdigest()[:8]


DOCS_TTL_HOURS = 24


def check_docs(source_id: str, src: dict, p: Paths, index: dict,
               force: bool = False, timeout: float = 30.0) -> list[dict]:
    """抓取该源登记的技术文档页，对比正文哈希。返回未确认的变更条目（可能为空）。

    **状态按 URL 存，不按源存** —— 一个 family 常常共用一份文档
    （polygon 五个源共用 Massive changelog、sec 三个源共用 EDGAR 技术规格页）。
    按源存就会同一页抓 5 遍、出 5 份一模一样的 diff、要确认 5 次。

    快照 .health/docs/<slug>.txt（基线那一版）与 <slug>.current.txt（当前版），
    差异 <slug>.diff。基线只在「首次抓到」和「人 ack 过」时推进（见 run_health），
    所以一处变更会一直挂着「待确认」，不会因为跑了第二轮就自己消失。
    默认 24h 内不重复抓 —— 文档页一天变不了几回，别去锤人家服务器。
    """
    import difflib

    import httpx

    urls = src.get("docs") or []
    if not urls:
        return []
    d = p.health_dir / "docs"
    changed: list[dict] = []
    now = dt.datetime.now()
    for url in urls:
        entry = dict(index.get(url) or {})
        base_hash = entry.get("baseline")
        snap = d / f"{_slug(url)}.txt"
        if not force and entry.get("checked_at"):
            try:
                fresh = now - dt.datetime.fromisoformat(entry["checked_at"]) < \
                    dt.timedelta(hours=DOCS_TTL_HOURS)
            except ValueError:
                fresh = False
            if fresh:
                if entry.get("current") and entry["current"] != base_hash:
                    changed.append({"url": url, "hash": entry["current"],
                                    "diff_path": entry.get("diff_path", ""),
                                    "lines_changed": entry.get("lines_changed", 0)})
                continue
        entry["checked_at"] = now.isoformat(timespec="seconds")
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as cli:
                r = cli.get(url, headers=src.get("headers") or {})
            if r.status_code >= 400:
                entry["error"] = f"HTTP {r.status_code}"
                index[url] = entry
                continue
            text = html_to_text(r.text)
            entry.pop("error", None)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            index[url] = entry
            continue

        cur_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        d.mkdir(parents=True, exist_ok=True)
        if not base_hash:                       # 首次抓到：直接收作基线
            snap.write_text(text, encoding="utf-8")
            entry.update(baseline=cur_hash, baseline_at=entry["checked_at"])
            entry.pop("current", None)
        elif cur_hash != base_hash:
            old = snap.read_text(encoding="utf-8") if snap.is_file() else ""
            diff = list(difflib.unified_diff(
                old.splitlines(), text.splitlines(),
                fromfile=f"{url} @ {entry.get('baseline_at', '?')}（基线）",
                tofile=f"{url} @ {entry['checked_at']}（当前）", lineterm="", n=2))
            dp = d / f"{_slug(url)}.diff"
            dp.write_text("\n".join(diff), encoding="utf-8")
            (d / f"{_slug(url)}.current.txt").write_text(text, encoding="utf-8")
            n = sum(1 for ln in diff
                    if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
            entry.update(current=cur_hash, diff_path=p.rel(dp), lines_changed=n)
            changed.append({"url": url, "hash": cur_hash,
                            "diff_path": p.rel(dp), "lines_changed": n})
        else:
            entry.pop("current", None)          # 又变回基线那一版了
        index[url] = entry
    return changed


# ---------------- 健康监控 ----------------

def check_source(source_id: str, src: dict, timeout: float = 20.0,
                 probe: bool = True) -> dict:
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
                if r.status_code >= 400:
                    # HEAD 报错不等于源坏了 —— 很多 API 压根不路由 HEAD：
                    # 实测 Kalshi 的 /trade-api/v2/events 对 HEAD 回 404、对 GET 回 200，
                    # 只认 403/405/501 会把这种源常年误判成故障。一律退化为 Range GET，
                    # GET 也不行才算真的坏。
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

    if probe and src.get("schema_probe"):
        res.update(probe_schema(source_id, src, timeout=max(timeout, 30.0)))
    return res


def _parse_http_date(text: str) -> dt.datetime:
    from email.utils import parsedate_to_datetime
    try:
        d = parsedate_to_datetime(text)
        return d.replace(tzinfo=None)
    except Exception:
        return dt.datetime.fromisoformat(text)


def run_health(p: Paths | None = None, only: str | None = None,
               probe: bool = True, force_docs: bool = False) -> dict:
    p = p or paths()
    sources = load_sources(p)
    prev = _load_fingerprints(p)          # 上次的指纹，来自 .health/ 而不是合约文件
    prev_schema = _load_state("schemas.json", p)
    docs_index = _load_state("docs.json", p)
    # 已确认的变更不再报 warn。确认记的是**当时那个 hash**，上游再变一次就又对不上、
    # 重新报 warn —— 所以这不是永久静音（见 health.py 的说明）。
    from .health import ack_for, ack_load       # 局部导入：health 依赖 external
    ack = ack_load(p)
    results = []
    for sid, src in sources.items():
        if only and sid != only:
            continue
        esrc = expand_env(src)
        r = check_source(sid, esrc, probe=probe)
        if prev.get(sid) and r.get("fingerprint") and prev[sid] != r["fingerprint"]:
            r["fingerprint_changed"] = True

        # schema 变更：给出**具名字段**差异。「指纹变了」这种话对修复没有帮助，
        # 「results[].value 没了、多了 results[].amount」才有。
        #
        # ⚠ 比较基准是 **baseline**（最后一次被人确认的结构），不是「上一轮的结构」。
        # 拿上一轮当基准的话，第一轮报了 warn、第二轮基准就跟着变了，warn 自己消失 ——
        # 等于没人看也自动放行。baseline 只在「首次见到」和「人确认过」时才推进。
        cur_hash, cur_keys = r.get("schema_hash"), set(r.get("schema_keys") or [])
        if cur_hash:
            old = prev_schema.get(sid) or {}
            base_hash, base_keys = old.get("baseline"), set(old.get("keys") or [])
            acked = ack_for(ack, sid, "schema")
            if not base_hash or acked.get("hash") == cur_hash:
                if acked.get("hash") == cur_hash and base_hash and base_hash != cur_hash:
                    r["schema_acked"] = acked          # 人确认过这个版本，收作新基线
                prev_schema[sid] = {"baseline": cur_hash, "keys": sorted(cur_keys),
                                    "at": r["checked_at"]}
            elif cur_hash != base_hash:
                r["schema_changed"] = True
                r["schema_added"] = sorted(cur_keys - base_keys)
                r["schema_removed"] = sorted(base_keys - cur_keys)
                r["schema_baseline_at"] = old.get("at", "")
                if r["status"] == "ok":
                    # 只报 warn 不报 fail：加字段往往无害，是不是问题交给人/skill 判断
                    r["status"] = "warn"
                    r["reason"] = (f"schema 变更（+{len(r['schema_added'])} "
                                   f"-{len(r['schema_removed'])} 字段），待确认")
                prev_schema[sid] = {**old, "current": cur_hash,
                                    "current_keys": sorted(cur_keys),
                                    "at_current": r["checked_at"]}

        if probe and esrc.get("docs"):
            ch = check_docs(sid, esrc, p, docs_index, force=force_docs)
            # 人确认过的那一版收作新基线，下一轮不再报；没确认的继续挂着「待确认」。
            # 确认按 URL 记（跟文档状态一样）—— 一个 family 共用一份文档时，
            # 判断一次就够了，不该逼着人对着 5 个源确认 5 遍同一件事。
            for url in esrc["docs"]:
                entry = docs_index.get(url) or {}
                a = ack_for(ack, sid, "docs", url)
                if a.get("hash") and a["hash"] == entry.get("current"):
                    entry.update(baseline=a["hash"], baseline_at=a.get("at", ""))
                    entry.pop("current", None)
                    cur_f = p.health_dir / "docs" / f"{_slug(url)}.current.txt"
                    if cur_f.is_file():        # 确认过的那一版成为新的比对底本
                        (cur_f.parent / f"{_slug(url)}.txt").write_text(
                            cur_f.read_text(encoding="utf-8"), encoding="utf-8")
                        cur_f.unlink()
            ch = [d for d in ch
                  if ack_for(ack, sid, "docs", d["url"]).get("hash") != d["hash"]]
            if ch:
                r["docs_changed"] = ch
                if r["status"] == "ok":
                    r["status"] = "warn"
                    r["reason"] = f"上游技术文档有变更（{len(ch)} 处），待确认"
        results.append(r)

    # 全量 key 列表留在 schemas.json 里就够了，report 只带差异（省 token）
    for r in results:
        r.pop("schema_keys", None)
    fresh = list(results)                  # 这一轮真探过的（history 只记这些）
    if only:
        # 只查一个源时别把 report.json 覆盖成「只有这一个源」——
        # /fix-source 验证时常跑 --source X，覆盖了面板就残了。其余源沿用上轮结果。
        order = list(sources)
        checked = {r["source"] for r in results}
        results = results + [r for r in load_report(p).get("results", [])
                             if r.get("source") not in checked]
        results.sort(key=lambda r: order.index(r["source"])
                     if r["source"] in order else 999)
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
    # history 只增不减，每行别塞 schema_keys（200 个路径 × 每轮 × 每源，几天就涨爆了）
    _SLIM = ("schema_keys", "current_keys")
    with (p.health_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        for r in fresh:
            fh.write(json.dumps({k: v for k, v in r.items() if k not in _SLIM},
                                ensure_ascii=False) + "\n")

    # 指纹存 .health/，**不要回写 external_sources.yaml**。
    # 那个文件是人写人读的真源，注释是它一半的价值；经 PyYAML 往返一次
    # 注释就全没了（本仓踩过：整份文件的注释被 dw health 悄悄清空）。
    # 指纹本来就是运行状态，属于 .health/，跟 report.json 放一起才对。
    fps = {**prev, **{r["source"]: r["fingerprint"] for r in fresh if r.get("fingerprint")}}
    (p.health_dir / "fingerprints.json").write_text(
        json.dumps(fps, ensure_ascii=False, indent=1), encoding="utf-8")
    # schema 基线与文档索引同理，一律留在 .health/
    for fname, obj in (("schemas.json", prev_schema), ("docs.json", docs_index)):
        (p.health_dir / fname).write_text(
            json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def load_report(p: Paths | None = None) -> dict:
    f = (p or paths()).health_dir / "report.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
