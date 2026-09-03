"""健康面板的本地控制服务：`dw panel`。

面板本来是 file:// 打开的静态页，只能看不能动。要在页面上开关定时、改时间、
点一下就跑，就得有个能执行动作的后端 —— 这里就是那个后端。

安全边界（这东西能改合约、注册系统任务、跑流水线，边界必须写死）：
- **只绑 127.0.0.1**，不监听外部网卡；
- 启动时生成一次性 token，嵌进页面，每个写接口都要带（跨源页面读不到响应体，
  拿不到 token，也就驱动不了它）；
- dataset 名一律拿本仓合约表校验，时间/cron 走 schedule.parse_cron 校验，
  子进程一律 list 传参、不过 shell；
- 每个写动作先返回「将要执行的命令」，页面确认后再执行（dry_run 两段式）。

用完关掉即可（Ctrl-C），不是常驻服务。
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Paths, paths

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _datasets(p: Paths) -> list[str]:
    from .contract import list_datasets
    return list_datasets(p)


def start_job(dataset: str, stage: str, p: Paths) -> dict:
    """后台起一次 dw run。这张表可能跑几小时，页面不能同步等着。"""
    logs = p.root / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / f"panel-{dataset}.log"
    cmd = [sys.executable, "-m", "dwlib.cli", "run", dataset]
    if stage and stage != "all":
        cmd += ["--stage", stage]
    jid = f"{dataset}:{secrets.token_hex(4)}"
    fh = log.open("a", encoding="utf-8")
    fh.write(f"\n===== {_now()} panel 触发：{' '.join(cmd)}\n")
    fh.flush()
    proc = subprocess.Popen(cmd, cwd=str(p.root), stdout=fh, stderr=subprocess.STDOUT)
    job = {"id": jid, "dataset": dataset, "stage": stage or "all", "status": "running",
           "started": _now(), "log": p.rel(log), "cmd": " ".join(cmd)}
    with _LOCK:
        _JOBS[jid] = job

    def _watch() -> None:
        rc = proc.wait()
        fh.close()
        with _LOCK:
            job["status"] = "ok" if rc == 0 else "fail"
            job["finished"] = _now()
            job["returncode"] = rc

    threading.Thread(target=_watch, daemon=True).start()
    return job


def jobs() -> list[dict]:
    with _LOCK:
        return sorted(_JOBS.values(), key=lambda j: j["started"], reverse=True)[:20]


def apply_sla(body: dict, p: Paths, dry_run: bool = True) -> dict:
    """面板上的「保存计划」：改合约 + 注册/卸载 Windows 任务。

    dry_run=True 时只回将要做什么，页面确认后再来一次真做 —— 改系统的事
    不该在用户没看清命令之前就发生。
    """
    from . import schedule as S

    ds = body.get("dataset", "")
    if ds not in _datasets(p):
        raise ValueError(f"没有这个 dataset：{ds}")
    runner = body.get("runner", "family")
    if runner not in S.RUNNERS:
        raise ValueError(f"runner 只能是 {'/'.join(S.RUNNERS)}")
    cron = (body.get("schedule") or "").strip() or None
    freshness = (body.get("freshness") or "").strip() or None
    if runner == "manual":
        cron = None
    elif not cron:
        raise ValueError("定时模式必须给 schedule（cron，如 0 15 * * *）")
    if cron:
        S.parse_cron(cron)                     # 先校验，别写了合约再失败

    plan = [f"合约 {p.rel(p.contract_file(ds))}：runner={runner}"
            f"，schedule={cron or 'null'}"
            + (f"，freshness={freshness}" if freshness else "")]
    task_cmd = None
    if runner == "own":
        task_cmd = " ".join(S.task_command(ds, cron, p))
        plan.append(f"注册计划任务 {S.task_name(ds)}：{task_cmd}")
    else:
        # family / manual 都不该留着自己的任务，否则两头跑
        task_cmd = " ".join(S.task_command(ds, None, p, remove=True))
        plan.append(f"卸载可能存在的独立任务 {S.task_name(ds)}（若没有则忽略报错）")
    if runner == "family":
        plan.append(f"由 dw-family-<族> 带着跑（dw run --family 会选中它）")
    if dry_run:
        return {"dry_run": True, "plan": plan, "task_cmd": task_cmd}

    changed = S.set_sla(ds, p, schedule=cron, freshness=freshness, runner=runner)
    task = S.apply_task(ds, cron, p, remove=(runner != "own"))
    return {"dry_run": False, "plan": plan, "contract_changed": changed, "task": task}


class _Handler(BaseHTTPRequestHandler):
    token = ""
    paths_: Paths

    def log_message(self, fmt, *args):        # 别把每个请求都刷到控制台
        pass

    # ---- 小工具 ----
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def _authed(self) -> bool:
        if self.headers.get("X-DW-Token") == self.token:
            return True
        self._json({"error": "token 不对：请从 dw panel 打开的页面上操作"}, 403)
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # ---- 路由 ----
    def do_GET(self) -> None:
        from . import dashboard
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            html = dashboard.build_html(self.paths_, live_token=self.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(dashboard.state(self.paths_))
        elif path == "/api/jobs":
            self._json({"jobs": jobs()})
        elif path == "/api/export":
            self._export(parse_qs(parsed.query))
        else:
            self._json({"error": "no such route"}, 404)

    def _export(self, qs: dict[str, list[str]]) -> None:
        """只读、不改任何状态，所以跟 /api/state 一样不要求 token——面板本来
        就只绑 127.0.0.1，本机内浏览器点一下就该能直接下载，不用先过 fetch。
        """
        from .io import export_last_n

        ds = (qs.get("dataset") or [""])[0]
        if ds not in _datasets(self.paths_):
            self._json({"error": f"没有这个 dataset：{ds}"}, 400)
            return
        try:
            n = int((qs.get("limit") or ["100"])[0])
        except ValueError:
            n = 100
        try:
            df = export_last_n(ds, n, self.paths_)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 400)
            return
        import io as _io
        buf = _io.BytesIO()
        df.write_csv(buf)
        fname = f"{ds}_last{df.height}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(buf.tell()))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(buf.getvalue())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authed():
            return
        try:
            body = self._body()
            if path == "/api/sla":
                self._json(apply_sla(body, self.paths_,
                                     dry_run=bool(body.get("dry_run", True))))
            elif path == "/api/run":
                ds = body.get("dataset", "")
                if ds not in _datasets(self.paths_):
                    raise ValueError(f"没有这个 dataset：{ds}")
                self._json(start_job(ds, body.get("stage", "all"), self.paths_))
            else:
                self._json({"error": "no such route"}, 404)
        except Exception as e:                 # 页面要看得见错在哪
            self._json({"error": f"{type(e).__name__}: {e}"}, 400)


def serve(p: Paths | None = None, port: int = 8787, open_browser: bool = False) -> None:
    p = p or paths()
    _Handler.token = secrets.token_urlsafe(16)
    _Handler.paths_ = p
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)   # 只对本机
    url = f"http://127.0.0.1:{port}/"
    print(f"健康面板控制台：{url}")
    print("  只监听 127.0.0.1；改合约/注册任务前页面会先让你确认命令。Ctrl-C 结束。")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已关闭。")
    finally:
        srv.server_close()
