<#
.SYNOPSIS
    注册/卸载 Windows 计划任务：外部源监控，以及各 dataset 的定时刷新。

.DESCRIPTION
    任务命名规范： dw-<dataset>-<stage>   例如 dw-example__gdp-all
                   dw-monitor            外部源健康监控
    del-dataset skill 依赖这个命名规范来精确卸载，请勿自行改名。

.EXAMPLE
    # 每天 07:00 检查外部源健康
    .\scripts\install_schedule.ps1 -Monitor -Time 07:00

    # 每天 06:30 刷新一个 dataset（ingest+transform+test）
    .\scripts\install_schedule.ps1 -Dataset example__gdp -Time 06:30

    # 只刷新 transform 阶段，每周一
    .\scripts\install_schedule.ps1 -Dataset example__gdp_growth -Stage transform -Weekly Monday -Time 06:45

    # 卸载
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
    return "dw-$Dataset-$Stage"
}

if ($List) {
    schtasks /query /fo table /nh | Select-String "^\\?dw-"
    exit 0
}

if (-not $Monitor -and -not $Dataset) {
    Write-Error "需要 -Dataset <名字> 或 -Monitor（或 -List）"
}

$TaskName = Get-TaskName

if ($Remove) {
    schtasks /delete /tn $TaskName /f
    Write-Host "已卸载计划任务 $TaskName"
    exit 0
}

if ($Monitor) {
    $Script = Join-Path $RepoRoot "scripts\monitor_sources.py"
    $Action = "`"$Python`" `"$Script`""
} else {
    $StageArg = if ($Stage -eq "all") { "" } else { " --stage $Stage" }
    $Action = "`"$Python`" -m dwlib.cli run $Dataset$StageArg"
}

# 用 cmd /c 包一层，确保工作目录正确（dw 靠 warehouse.yaml 定位仓库根）
$FullCommand = "cmd /c cd /d `"$RepoRoot`" && $Action"

if ($Weekly) {
    schtasks /create /tn $TaskName /tr $FullCommand /sc weekly /d $Weekly /st $Time /f
} else {
    schtasks /create /tn $TaskName /tr $FullCommand /sc daily /st $Time /f
}

Write-Host ""
Write-Host "已注册计划任务：$TaskName"
Write-Host "  命令：$Action"
Write-Host "  时间：$(if ($Weekly) { "每周 $Weekly" } else { '每天' }) $Time"
Write-Host "  卸载：.\scripts\install_schedule.ps1 $(if ($Monitor) { '-Monitor' } else { "-Dataset $Dataset -Stage $Stage" }) -Remove"
