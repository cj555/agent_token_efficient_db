<#
.SYNOPSIS
    注册/卸载 Windows 计划任务：外部源监控，以及各 dataset / family 的定时刷新。

.DESCRIPTION
    任务命名规范： dw-<dataset>-<stage>   例如 dw-example__gdp-all
                   dw-family-<family>    例如 dw-family-example（-Family 用）
                   dw-monitor            外部源健康监控
    del-dataset skill 依赖这个命名规范来精确卸载，请勿自行改名。

    **优先用 -Family，而不是给同一个 family 里的每个 dataset 各开一个 -Dataset 任务。**
    `dw run --family X` 在同一个进程里按拓扑序跑完整族，天然避免了「上游任务还没
    跑完、下游任务的定时器已经到点」这种竞态 —— 尤其是当 family 内多个 dataset
    的 sla.schedule 写的是同一个时间点时（常见于同一份原始响应派生出多张表的
    family——那是"整族一起跑"的设计意图，不是"每个 dataset 各自的定时器都在
    同一时刻触发"）。
    -Dataset 仍然保留，给不属于任何 family、或明确要单独刷新节奏的场景用。

    日志：会把 `dw run` 的 stdout/stderr 追加写到仓库根目录的 `logs\<TaskName>.log`
    （目录首次使用时自动创建，logs/ 已在 .gitignore 里）。Windows Task Scheduler
    本身不落 stdout，不重定向的话失败了也无从查起。

.EXAMPLE
    # 每天 07:00 检查外部源健康
    .\scripts\install_schedule.ps1 -Monitor -Time 07:00

    # 整族刷新（推荐）：example 每天 15:00
    .\scripts\install_schedule.ps1 -Family example -Time 15:00

    # 只刷新单个 dataset 的 transform 阶段，每周一
    .\scripts\install_schedule.ps1 -Dataset example__gdp_growth -Stage transform -Weekly Monday -Time 06:45

    # 卸载
    .\scripts\install_schedule.ps1 -Family example -Remove
    .\scripts\install_schedule.ps1 -Dataset example__gdp -Remove
    .\scripts\install_schedule.ps1 -Monitor -Remove

    # 查看本仓库注册的全部任务
    .\scripts\install_schedule.ps1 -List

.NOTES
    Linux / macOS 用户请改用 crontab，等价写法见 README.md。
#>
[CmdletBinding()]
param(
    [string]$Dataset,
    [string]$Family,
    [switch]$Monitor,
    [ValidateSet("ingest", "transform", "test", "all")]
    [string]$Stage = "all",
    [string]$Time = "06:00",
    [string]$Weekly,
    [switch]$Remove,
    [switch]$List
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

# 优先用仓库自带的 venv，保证定时任务和交互式运行是同一套依赖
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

function Get-TaskName {
    if ($Monitor) { return "dw-monitor" }
    if ($Family) { return "dw-family-$Family" }
    return "dw-$Dataset-$Stage"
}

if ($List) {
    schtasks /query /fo table /nh | Select-String "^\\?dw-"
    exit 0
}

if (-not $Monitor -and -not $Dataset -and -not $Family) {
    Write-Error "需要 -Dataset <名字> 或 -Family <族名> 或 -Monitor（或 -List）"
}
if ($Dataset -and $Family) {
    Write-Error "-Dataset 和 -Family 只能二选一"
}

$TaskName = Get-TaskName

if ($Remove) {
    schtasks /delete /tn $TaskName /f
    Write-Host "已卸载计划任务 $TaskName"
    exit 0
}

# schtasks /tr 有 261 字符上限，本仓库路径够长时内联 cd+echo+重定向会超限，
# 所以固定逻辑（cd 到仓库根、追加日志）挪进 run_logged.cmd，schtasks 只传短参数。
$Wrapper = Join-Path $RepoRoot "scripts\run_logged.cmd"

if ($Monitor) {
    $FullCommand = "`"$Wrapper`" $TaskName scripts\monitor_sources.py"
} elseif ($Family) {
    $StageArg = if ($Stage -eq "all") { "" } else { " --stage $Stage" }
    $FullCommand = "`"$Wrapper`" $TaskName -m dwlib.cli run --family $Family$StageArg"
} else {
    $StageArg = if ($Stage -eq "all") { "" } else { " --stage $Stage" }
    $FullCommand = "`"$Wrapper`" $TaskName -m dwlib.cli run $Dataset$StageArg"
}

if ($Weekly) {
    schtasks /create /tn $TaskName /tr $FullCommand /sc weekly /d $Weekly /st $Time /f
} else {
    schtasks /create /tn $TaskName /tr $FullCommand /sc daily /st $Time /f
}

Write-Host ""
Write-Host "已注册计划任务：$TaskName"
Write-Host "  命令：$FullCommand"
Write-Host "  日志：logs\$TaskName.log"
Write-Host "  时间：$(if ($Weekly) { "每周 $Weekly" } else { '每天' }) $Time"
Write-Host "  卸载：.\scripts\install_schedule.ps1 $(if ($Monitor) { '-Monitor' } elseif ($Family) { "-Family $Family" } else { "-Dataset $Dataset -Stage $Stage" }) -Remove"
