"""外部数据源健康监控 —— 供 OS 定时任务周期性调用。

    python scripts/monitor_sources.py            # 检查全部源
    python scripts/monitor_sources.py --source X # 只查一个

产物：
    .health/report.json    最新一轮结果（/fix-source skill 直接读这个文件，
                           不需要 Claude 自己去探测，省 token 也省时间）
    .health/history.jsonl  逐次追加的历史，便于看「什么时候开始坏的」

退出码：0 全绿；1 有 fail；0 但有 warn 时 stdout 会给出提示。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dwlib.config import paths                    # noqa: E402
from dwlib.external import run_health             # noqa: E402
from dwlib import graph as G                      # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="外部数据源健康监控")
    ap.add_argument("--source", help="只检查指定 source id")
    ap.add_argument("--quiet", action="store_true", help="全绿时不输出")
    a = ap.parse_args(argv)

    p = paths()
    rep = run_health(p, only=a.source)
    s = rep["summary"]
    g = G.load(p)

    if s["fail"] == 0 and s["warn"] == 0:
        if not a.quiet:
            print(f"[{rep['checked_at']}] 外部源全部正常（{s['total']} 个）")
        return 0

    print(f"[{rep['checked_at']}] 外部源异常：fail={s['fail']} warn={s['warn']} / {s['total']}")
    for r in rep["results"]:
        if r["status"] == "ok":
            continue
        affected = G.external_consumers(g, r["source"])
        print(f"  [{r['status'].upper()}] {r['source']}: {r['reason']}")
        print(f"        受影响 dataset: {', '.join(affected) or '（无）'}")
    print()
    print("→ 打开 Claude Code 运行 `/fix-source`：它会读取 .health/report.json，")
    print("  先给出修复计划，经你批准后再改 ingest.py / external_sources.yaml。")
    return 1 if s["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
