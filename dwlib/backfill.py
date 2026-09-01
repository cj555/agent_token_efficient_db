"""共享历史回补游标 —— 收 `sec__filing_index/ingest.py` 已经写好的模式成库函数。

与水位线（增量更新的"存量到哪了"）相对，回补游标记录的是"历史往回补到哪了"。
状态落在每个 dataset 自己的 `_meta/backfill_state.json`，字段名沿用
`sec__filing_index` 已经在用的那套：`backfill_cursor` / `backfill_done` /
`last_run`，这样以后可以直接把它换成调这里的共享函数而不改存储格式。

用法（backfill.py 参考）：
    st = dw.backfill_state(DATASET)
    cursor = st.cursor or dt.date.today()   # 从未回补过 = 从今天开始往回走
    ... 处理 cursor 这一天/这一段 ...
    st.advance(new_cursor, done=(new_cursor <= floor))
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .config import Paths, paths


def _state_file(dataset: str, p: Paths) -> Path:
    return p.dataset_dir(dataset) / "_meta" / "backfill_state.json"


class BackfillState:
    """`backfill_state()` 的返回值。`cursor`/`done` 是只读快照，落盘只能通过 advance()。"""

    def __init__(self, dataset: str, p: Paths, cursor: dt.date | None, done: bool):
        self.dataset = dataset
        self.cursor = cursor
        self.done = done
        self._p = p

    def advance(self, new_cursor: dt.date, done: bool = False) -> None:
        """推进游标并落盘。new_cursor 是"下次回补从这里继续往回走"的位置。"""
        f = _state_file(self.dataset, self._p)
        f.parent.mkdir(parents=True, exist_ok=True)
        state = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        state["backfill_cursor"] = new_cursor.isoformat()
        state["backfill_done"] = done
        state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
        f.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        self.cursor = new_cursor
        self.done = done


def backfill_state(dataset: str, p: Paths | None = None) -> BackfillState:
    """读当前回补游标状态。没有存量状态时 cursor=None ——
    意味着"还没开始回补过"，起点（today() 还是合约里约定的历史下界）由调用方决定。
    """
    p = p or paths()
    f = _state_file(dataset, p)
    st = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    cursor = st.get("backfill_cursor")
    return BackfillState(
        dataset=dataset,
        p=p,
        cursor=dt.date.fromisoformat(cursor) if cursor else None,
        done=bool(st.get("backfill_done", False)),
    )
