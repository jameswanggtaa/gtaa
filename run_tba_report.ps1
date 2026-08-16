$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$outDir = Join-Path $scriptDir "tba_reports"
$logDir = Join-Path $outDir "logs"

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Set-Location $scriptDir
$env:YB_TBA_OUTPUT_DIR = $outDir

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $logDir "tba_report_$timestamp.log"

cmd.exe /c "python -u yieldbook_tba_metrics.py >> `"$logFile`" 2>&1"
exit $LASTEXITCODE
