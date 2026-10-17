#requires -Version 7.0
<#
.SYNOPSIS
    Hermes 决策引擎 —— 机器可判定门禁（唯一"完成"裁定者）。

.DESCRIPTION
    本脚本的判定原则（针对本项目历史上"假绿"的失败模式）：
      1. 每一项都必须**真跑**产生证据；禁止用 grep 源码/常量字符串冒充通过。
      2. 无法验证的项显式标 BLOCKED 并给出原因，绝不静默算过。
      3. 退出码 = (FAIL + BLOCKED) 项数。0 表示门禁全清，"可交付"。

.PARAMETER Full
    额外跑全量模式性能门禁（G14，≤300s）。

.PARAMETER Quick
    跳过最慢的 3 项（基金契约 G04 / 幂等 G05 / 三窗口 G06），标 SKIP。
    仅用于迭代中的快速自检；**声明完成前必须跑不带 -Quick 的完整门禁**。

.EXAMPLE
    pwsh -File tools/check_gates.ps1
    pwsh -File tools/check_gates.ps1 -Full
#>
[CmdletBinding()]
param(
    [switch]$Full,
    [switch]$Quick
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
$env:PYTHONIOENCODING = 'utf-8'

$Root = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $Root 'source'
$CliPy = Join-Path $Src 'cli.py'
$Readme = Join-Path $Root 'README.md'

$results = [System.Collections.Generic.List[object]]::new()

function Add-Gate {
    param([string]$Id, [string]$Name, [string]$Status, [string]$Detail, $Evidence = $null)
    $results.Add([pscustomobject]@{
            Id       = $Id
            Name     = $Name
            Status   = $Status
            Detail   = ($Detail -replace '\s+', ' ').Trim()
            Evidence = $Evidence
        })
    $color = switch ($Status) {
        'PASS' { 'Green' } 'FAIL' { 'Red' } 'BLOCKED' { 'Magenta' } 'SKIP' { 'DarkGray' } default { 'Yellow' }
    }
    Write-Host ("  {0,-4} {1,-8} {2}" -f $Id, $Status, $Name) -ForegroundColor $color
    if ($Detail -and $Status -ne 'PASS') {
        Write-Host ("        └─ {0}" -f ($Detail -replace '\s+', ' ').Substring(0, [Math]::Min(240, $Detail.Length))) -ForegroundColor $color
    }
}

Write-Host "`n=== Hermes 门禁检查 ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) ===" -ForegroundColor Cyan
Write-Host "仓库: $Root`n"

# --------------------------------------------------------------------------
# 静态/廉价门禁
# --------------------------------------------------------------------------
$pyTestOut = ''
Push-Location $Src
try { $pyTestOut = (& python -m pytest tests/ -q --tb=line 2>&1 | Out-String) } finally { Pop-Location }
$passed = if ($pyTestOut -match '(\d+) passed') { [int]$Matches[1] } else { 0 }
$failed = if ($pyTestOut -match '(\d+) failed') { [int]$Matches[1] } else { 0 }
$errored = if ($pyTestOut -match '(\d+) error') { [int]$Matches[1] } else { 0 }
if ($failed -eq 0 -and $errored -eq 0 -and $passed -ge 25) {
    Add-Gate 'G01' '测试网离线全绿（≥25 passed）' 'PASS' "$passed passed / $failed failed" $pyTestOut.Trim()
} else {
    Add-Gate 'G01' '测试网离线全绿（≥25 passed）' 'FAIL' "$passed passed / $failed failed / $errored error" $pyTestOut.Trim()
}

# G09 无硬编码分支
$g9 = @(Select-String -Path $CliPy -Pattern 'if best_name\s*==' -AllMatches)
if ($g9.Count -eq 0) { Add-Gate 'G09' 'cli.py 无 if best_name == 硬编码分支' 'PASS' '0 处匹配' }
else { Add-Gate 'G09' 'cli.py 无 if best_name == 硬编码分支' 'FAIL' "$($g9.Count) 处匹配: $(($g9 | Select-Object -First 3 | ForEach-Object { $_.LineNumber }) -join ',')" }

# G10 cli.py 行数
$lines = (Get-Content $CliPy).Count
if ($lines -le 700) { Add-Gate 'G10' 'cli.py ≤ 700 行' 'PASS' "$lines 行" }
else { Add-Gate 'G10' 'cli.py ≤ 700 行' 'FAIL' "$lines 行 > 700" }

# G11 配置残留（仅 CLI，strategy_master 兼容垫片不算）
$g11 = @(Select-String -Path $CliPy -Pattern 'watchlist|save_config|load_config' -AllMatches)
if ($g11.Count -eq 0) { Add-Gate 'G11' 'CLI 无 watchlist/save_config/load_config 残留' 'PASS' '0 处' }
else { Add-Gate 'G11' 'CLI 无 watchlist/save_config/load_config 残留' 'FAIL' "$($g11.Count) 处" }

# G13 README 契约章节 + 示例 JSON 可解析
$readmeTxt = if (Test-Path $Readme) { Get-Content $Readme -Raw -Encoding UTF8 } else { '' }
$hasSection = $readmeTxt -match '给 Agent 的接口契约'
$jsonBlocks = [regex]::Matches($readmeTxt, '(?s)```json\s*(.*?)```')
$badBlocks = 0
foreach ($b in $jsonBlocks) {
    $tmp = Join-Path $env:TEMP ("readme_block_{0}.json" -f (Get-Random))
    Set-Content -Path $tmp -Value $b.Groups[1].Value -Encoding UTF8
    & python -c "import json,sys;json.load(open(sys.argv[1],encoding='utf-8-sig'))" $tmp 2>$null
    if ($LASTEXITCODE -ne 0) { $badBlocks++ }
    Remove-Item $tmp -ErrorAction SilentlyContinue
}
if ($hasSection -and $jsonBlocks.Count -gt 0 -and $badBlocks -eq 0) {
    Add-Gate 'G13' 'README 含 Agent 契约章节且示例 JSON 可解析' 'PASS' "章节存在，$($jsonBlocks.Count) 个 JSON 块全部可解析"
} else {
    Add-Gate 'G13' 'README 含 Agent 契约章节且示例 JSON 可解析' 'FAIL' "章节=$hasSection, json块=$($jsonBlocks.Count), 解析失败=$badBlocks"
}

# --------------------------------------------------------------------------
# 运行时门禁（探针真跑）
# --------------------------------------------------------------------------
$probeMode = if ($Quick) { 'all-quick' } else { 'all' }
$probeOut = & python (Join-Path $PSScriptRoot 'probe.py') $probeMode 2>&1 | Out-String
$probe = $null
try { $probe = $probeOut | ConvertFrom-Json } catch { }

function Get-ProbeStatus {
    param([string]$Key)
    if ($null -eq $probe -or $null -eq $probe.$Key) { return $null }
    return $probe.$Key
}

if ($null -eq $probe) {
    Add-Gate 'G02' 'CLI --json 契约（C1 纯 JSON + C3 字段齐备）' 'FAIL' '探针未产出 JSON，请先单独运行 tools/probe.py' $probeOut
    foreach ($g in 'G03', 'G04', 'G05', 'G06', 'G07', 'G08', 'G12') {
        Add-Gate $g '（探针缺失，无法判定）' 'BLOCKED' 'probe.py 未产出结果'
    }
}
else {
    $m = @{
        'json'       = @('G02', 'CLI --json 契约（C1 纯 JSON + C3 字段齐备）')
        'semantic'   = @('G03', '输出语义自洽（C4：无效候选不得给 buy / triggers 非空）')
        'fund'       = @('G04', '基金同契约（510300 --json）')
        'idempotent' = @('G05', '幂等确定性（C8：两次运行除时间戳外逐字节相同）')
        'windows'    = @('G06', '三窗口 action/共识族一致（B6）')
        'advise'     = @('G07', '单一真源 API（C6：advise() 键集合 == --json 键集合）')
        'a1'         = @('G08', 'A1 结构性不变式（自适应资金下 100% 可负担一手）')
    }
    foreach ($k in $m.Keys) {
        $r = Get-ProbeStatus $k
        $id = $m[$k][0]; $nm = $m[$k][1]
        if ($null -eq $r) { Add-Gate $id $nm 'BLOCKED' '探针无此键'; continue }
        if ($Quick -and $k -in @('fund', 'idempotent', 'windows')) {
            Add-Gate $id $nm 'SKIP' '-Quick 跳过了该项（完成声明前必须跑完整门禁）'; continue
        }
        $st = [string]$r.status
        $detail = [string]$r.reason
        if (-not $detail -and $r.observed) { $detail = ($r.observed | ConvertTo-Json -Compress -Depth 4) }
        Add-Gate $id $nm $st $detail $r
    }

    # G12 --fast 性能（用 G02 那次真实运行的耗时）
    $sec = $null
    if ($probe.json) { $sec = $probe.json.elapsed_sec }
    if ($null -ne $sec) {
        if ([double]$sec -le 60) { Add-Gate 'G12' '--fast 墙钟 ≤ 60s' 'PASS' "$sec s" }
        else { Add-Gate 'G12' '--fast 墙钟 ≤ 60s' 'FAIL' "$sec s > 60s" }
    }
    else { Add-Gate 'G12' '--fast 墙钟 ≤ 60s' 'BLOCKED' '未取得耗时' }
}

# G14 全量模式性能（可选）
if ($Full) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location $Src
    try { & python cli.py 600519 --json --jobs 4 > $null 2> $null } finally { Pop-Location }
    $sw.Stop()
    $fullSec = [Math]::Round($sw.Elapsed.TotalSeconds, 1)
    if ($fullSec -le 300) { Add-Gate 'G14' '全量模式墙钟 ≤ 300s' 'PASS' "$fullSec s" }
    else { Add-Gate 'G14' '全量模式墙钟 ≤ 300s' 'FAIL' "$fullSec s" }
}
else {
    Add-Gate 'G14' '全量模式墙钟 ≤ 300s' 'SKIP' '需 -Full 才执行' 
}

# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
$fail = @($results | Where-Object { $_.Status -eq 'FAIL' })
$blocked = @($results | Where-Object { $_.Status -eq 'BLOCKED' })
$skip = @($results | Where-Object { $_.Status -eq 'SKIP' })
$pass = @($results | Where-Object { $_.Status -eq 'PASS' })
$exit = $fail.Count + $blocked.Count

$report = [pscustomobject]@{
    generated_at = (Get-Date).ToString('o')
    repo         = $Root
    summary      = [pscustomobject]@{
        pass = $pass.Count; fail = $fail.Count; blocked = $blocked.Count; skip = $skip.Count; exit_code = $exit
    }
    gates        = $results
}
$reportPath = Join-Path $Root 'gates-report.json'
$report | ConvertTo-Json -Depth 8 | Set-Content -Path $reportPath -Encoding UTF8

Write-Host ""
Write-Host ("PASS {0} | FAIL {1} | BLOCKED {2} | SKIP {3} → 退出码 {4}" -f $pass.Count, $fail.Count, $blocked.Count, $skip.Count, $exit) `
    -ForegroundColor $(if ($exit -eq 0) { 'Green' } else { 'Red' })
Write-Host "报告: $reportPath"
if ($exit -ne 0) {
    Write-Host "`n未通过项（这些就是当前必须做的工作，不得跳过）：" -ForegroundColor Yellow
    foreach ($r in ($fail + $blocked)) { Write-Host ("  {0} {1} — {2}" -f $r.Id, $r.Name, $r.Detail) -ForegroundColor Yellow }
}
Write-Host ""
exit $exit