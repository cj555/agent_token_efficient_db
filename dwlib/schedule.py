"""调度落地：改合约的 sla、注册/卸载 Windows 计划任务、查已注册的任务。

`dw sla` 和健康面板的按钮共用这一份实现 —— 两条 UI、一条代码路径，
省得两边规则跑偏。

⚠ 改 contract.yaml 是**行级外科手术**（只动 sla 块里那几行，保留行尾注释）。
那份文件是人写人读的真源，注释是它一半的价值，绝不整份 yaml.safe_dump 回写。
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

from .config import Paths, paths

RUNNERS = ("family", "own", "manual")
_DOW = {"0": "SUN", "7": "SUN", "1": "MON", "2": "TUE", "3": "WED",
        "4": "THU", "5": "FRI", "6": "SAT"}
_CRON = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(\S+)\s*$")

_MISSING = object()          # 与 None 区分：None = 手动（显式清空 schedule）


def parse_cron(cron: str) -> tuple[str | None, str]:
    """cron → (schtasks 的 /d 参数或 None, "HH:MM")。

    只支持本仓实际用得到的两种：`M H * * *`（每天）与 `M H * * <星期>`（每周）。
    再复杂的 cron（分钟粒度、多时间点）Windows 任务计划本来也表达不了，
    与其偷偷降级不如直接报错。
    """
    m = _CRON.match(cron or "")
    if not m:
        raise ValueError(f"无法转成 Windows 计划任务的 cron：'{cron}'"
                         f"（只支持 'M H * * *' 或 'M H * * <星期>'）")
    minute, hour, dow = int(m.group(1)), int(m.group(2)), m.group(3)
    if not (0 <= minute < 60 and 0 <= hour < 24):
        raise ValueError(f"cron 时间越界：'{cron}'")
    time_ = f"{hour:02d}:{minute:02d}"
    if dow == "*":
        return None, time_
    days = []
    for part in dow.split(","):
        if "-" in part:                       # 1-5 → MON..FRI
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"看不懂的星期段：'{part}'")
            days += [_DOW[str(d)] for d in range(int(a), int(b) + 1)]
        elif part.isdigit():
            days.append(_DOW[part])
        elif part.upper()[:3] in _DOW.values():
            days.append(part.upper()[:3])
        else:
            raise ValueError(f"看不懂的星期：'{part}'")
    return ",".join(dict.fromkeys(days)), time_


def task_name(dataset: str, stage: str = "all") -> str:
    """与 scripts/install_schedule.ps1 的命名规范一致（del-dataset 也依赖它）。"""
    return f"dw-{dataset}-{stage}"


# ---------------- 合约里的 sla 块 ----------------

def set_sla(dataset: str, p: Paths | None = None, *, schedule=_MISSING,
            freshness: str | None = None, runner: str | None = None,
            dry_run: bool = False) -> list[str]:
    """只改 contract.yaml 的 sla 块里那几行，返回改动摘要。

    dry_run=True 时只计算、不写盘——`dw sla --dry-run` 原来只让任务注册命令
    "只打印不执行"，这里的合约改动却照写不误（实测踩过：跑一次
    `--dry-run` 就把 schedule 真的改掉了）。传 True 时改动摘要照常返回，
    给调用方预览用，只是最后不落盘。
    """
    p = p or paths()
    if runner is not None and runner not in RUNNERS:
        raise ValueError(f"runner 只能是 {'/'.join(RUNNERS)}")
    f = p.contract_file(dataset)
    if not f.is_file():
        raise FileNotFoundError(f"没有 {p.rel(f)}")
    lines = f.read_text(encoding="utf-8").splitlines()

    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "sla:")
    except StopIteration:
        raise ValueError(f"{p.rel(f)} 里没有 sla: 块（先手工补一个）") from None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i] and not lines[i][0].isspace()), len(lines))

    want: dict[str, str] = {}
    if schedule is not _MISSING:
        want["schedule"] = "null" if schedule is None else f'"{schedule}"'
    if freshness is not None:
        want["freshness"] = str(freshness)
    if runner is not None:
        want["runner"] = runner

    changed = []
    for key, value in want.items():
        pat = re.compile(rf"^(\s+){re.escape(key)}:\s*(.*?)(\s+#.*)?$")
        for i in range(start + 1, end):
            m = pat.match(lines[i])
            if not m:
                continue
            old = m.group(2).strip()
            if old == value:
                break
            # 值变了就把行尾注释一并去掉：那句话是给旧值写的，留着就成了假话
            # （实测踩过：schedule 从 "0 15 * * *" 改成 null，行尾还写着
            #   「每天本机 15:00，随 dw-family-polygon 一起跑」）。
            # 想留说明就写在块上方的整行注释里，那种不会被动。
            dropped = (m.group(3) or "").strip()
            lines[i] = f"{m.group(1)}{key}: {value}"
            changed.append(f"{key}: {old} → {value}"
                           + (f"（去掉过时的行尾注释 {dropped}）" if dropped else ""))
            break
        else:                                  # sla 块里还没有这一行，补在块首
            lines.insert(start + 1, f"  {key}: {value}")
            end += 1
            changed.append(f"{key}: （新增）→ {value}")
    if changed and not dry_run:
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


# ---------------- Windows 计划任务 ----------------

def _ps1(p: Paths) -> Path:
    return p.root / "scripts" / "install_schedule.ps1"


def task_command(dataset: str, cron: str | None, p: Paths | None = None,
                 stage: str = "all", remove: bool = False) -> list[str]:
    """返回将要执行的 powershell 命令（给人先看一眼，别偷偷改系统）。"""
    p = p or paths()
    args = ["-Dataset", dataset, "-Stage", stage]
    if remove:
        args.append("-Remove")
    else:
        weekly, time_ = parse_cron(cron or "")
        args += ["-Time", time_]
        if weekly:
            args += ["-Weekly", weekly]
    # 走 -Command 而不是 -File，为的是先把控制台输出编码设成 UTF-8 ——
    # 否则 ps1 里的中文经由 OEM 代码页出来就是乱码（本机实测 cp437 一路吃掉）。
    # 参数名要裸着传（加引号 PowerShell 会当成值），只给值加引号
    inner = " ".join([f"& '{_ps1(p)}'"]
                     + [a if a.startswith("-") else f"'{a}'" for a in args])
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + inner]


def _decode(raw: bytes) -> str:
    """命令里已经把控制台设成 UTF-8；老路径进来的话退回系统代码页。"""
    import locale
    for enc in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


_NO_SUCH_TASK = ("cannot find the file", "找不到", "does not exist", "系统找不到")


def apply_task(dataset: str, cron: str | None, p: Paths | None = None,
               stage: str = "all", remove: bool = False) -> dict:
    """真正去注册/卸载任务。返回 {cmd, ok, output, note}。"""
    cmd = task_command(dataset, cron, p, stage, remove)
    r = subprocess.run(cmd, capture_output=True)
    text = (_decode(r.stdout or b"") + _decode(r.stderr or b"")).strip()
    ok = r.returncode == 0
    note = ""
    # schtasks 找不到任务时只往 stderr 写 ERROR，退出码仍是 0（ps1 也不拦），
    # 所以只看返回码会把「本来就没这个任务」报成「已卸载」。按文本判。
    if remove and any(s in text.lower() for s in _NO_SUCH_TASK):
        ok, note = True, f"{task_name(dataset, stage)} 本来就没注册过，无需卸载"
    return {"cmd": " ".join(cmd), "ok": ok, "note": note, "output": text[-800:]}


def list_tasks() -> dict[str, dict]:
    """本仓注册过的计划任务：{任务名: {next_run, status}}。查不到就返回空。"""
    try:
        r = subprocess.run(["schtasks", "/query", "/fo", "csv", "/nh"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[str, dict] = {}
    for row in (r.stdout or "").splitlines():
        cols = [c.strip('"') for c in row.split('","')]
        if len(cols) < 3:
            continue
        name = cols[0].strip('"').lstrip("\\")
        if not name.startswith("dw-"):
            continue
        out[name] = {"next_run": cols[1], "status": cols[2].strip('"')}
    return out


# ---------------- 下次运行时间 ----------------

_TASK_TIME_FMTS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
                   "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                   "%d/%m/%Y %H:%M:%S")


def parse_task_time(text: str) -> dt.datetime | None:
    """schtasks 报的「下次运行时间」。格式随系统区域设置变，挨个试。"""
    s = (text or "").strip()
    if not s or s.upper().startswith("N/A"):
        return None
    for fmt in _TASK_TIME_FMTS:
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def next_run_from_cron(cron: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """按 cron 推下次触发时刻。任务还没注册时用它兜底，免得表里一片空白。"""
    try:
        weekly, hhmm = parse_cron(cron)
    except ValueError:
        return None
    now = now or dt.datetime.now()
    base = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]),
                       second=0, microsecond=0)
    if not weekly:
        return base if base > now else base + dt.timedelta(days=1)
    days = {_WEEKDAYS.index(d) for d in weekly.split(",") if d in _WEEKDAYS}
    for i in range(8):
        cand = base + dt.timedelta(days=i)
        if cand > now and cand.weekday() in days:
            return cand
    return None
