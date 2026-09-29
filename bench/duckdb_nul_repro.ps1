# duckdb_nul_repro.ps1 — does DuckDB run slower on Windows when stdout is NUL?
#
# Standalone: runs the DuckDB CLI directly through cmd.exe (no Python, no
# harness), same query, only the stdout target changes:
#   nul        > NUL
#   file       > out.csv
#   pipe       | findstr (stdout is a pipe)
#   nul-nopb   > NUL with SET enable_progress_bar=false
#
# Usage: pwsh bench/duckdb_nul_repro.ps1 -Csv D:/taxi/trips.csv -Duck old=C:\d155\duckdb.exe,new=C:\d20\duckdb.exe [-Rounds 3]

param(
    [Parameter(Mandatory)] [string] $Csv,
    [Parameter(Mandatory)] [string[]] $Duck,
    [int] $Rounds = 3
)

$ErrorActionPreference = 'Stop'
$Csv = $Csv -replace '\\', '/'
$query = "SELECT cab_type, COUNT(*) FROM read_csv_auto('$Csv') GROUP BY cab_type"
$work = Join-Path ([IO.Path]::GetTempPath()) 'duckdb-nul-repro'
New-Item -ItemType Directory -Force -Path $work | Out-Null

$engines = [ordered]@{}
foreach ($d in $Duck) { $n, $p = $d -split '=', 2; $engines[$n] = $p }

# One .cmd per engine x mode, so cmd.exe does the redirection exactly as a user would.
$modes = [ordered]@{
    'nul'      = { param($exe) "`"$exe`" -csv -c `"$query`" > NUL" }
    'file'     = { param($exe) "`"$exe`" -csv -c `"$query`" > `"$work\out.csv`"" }
    'pipe'     = { param($exe) "`"$exe`" -csv -c `"$query`" | findstr /v /c:`"__none__`" > NUL" }
    'nul-nopb' = { param($exe) "`"$exe`" -csv -c `"SET enable_progress_bar=false; $query`" > NUL" }
}

foreach ($n in $engines.Keys) {
    Write-Host "$n : $(& $engines[$n] --version)"
}
Write-Host "query: $query"
Write-Host "file : $Csv ($([math]::Round((Get-Item $Csv).Length / 1GB, 2)) GB)"

function Run-Case($engine, $mode) {
    $bat = Join-Path $work "$engine-$mode.cmd"
    Set-Content -Path $bat -Encoding ascii -Value ("@echo off`r`n" + (& $modes[$mode] $engines[$engine]) + "`r`nexit /b %ERRORLEVEL%")
    $t = Measure-Command { cmd /c $bat }
    [pscustomobject]@{ Engine = $engine; Mode = $mode; Seconds = [math]::Round($t.TotalSeconds, 2); Exit = $LASTEXITCODE }
}

# Warm the OS file cache once, output to a file (the neutral target).
foreach ($n in $engines.Keys) { $null = Run-Case $n 'file' }

$results = @()
$cases = foreach ($n in $engines.Keys) { foreach ($m in $modes.Keys) { ,@($n, $m) } }
for ($r = 1; $r -le $Rounds; $r++) {
    # Rotate the order each round so no case always runs right after the same one.
    $k = ($r - 1) % $cases.Count
    $order = @($cases[$k..($cases.Count - 1)])
    if ($k -gt 0) { $order += @($cases[0..($k - 1)]) }
    foreach ($c in $order) {
        $res = Run-Case $c[0] $c[1]
        $res | Add-Member Round $r
        $results += $res
        Write-Host ("round {0}  {1,-8} {2,-9} {3,8:N2}s  exit={4}" -f $r, $res.Engine, $res.Mode, $res.Seconds, $res.Exit)
    }
}

Write-Host "`nMedian seconds per engine and stdout target:"
$results | Group-Object Engine, Mode | ForEach-Object {
    $s = $_.Group.Seconds | Sort-Object
    [pscustomobject]@{ Case = $_.Name; Median = $s[[int][math]::Floor($s.Count / 2)]; Runs = ($s -join ', ') }
} | Format-Table -AutoSize | Out-String | Write-Host
