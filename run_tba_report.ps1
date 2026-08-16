$ErrorActionPreference = "Stop"
$scriptDir = "C:\Users\zhiyu\OneDrive\Documents\python"
$outDir    = "C:\Users\zhiyu\OneDrive\Documents\python\tba_reports"  # your folder
$logDir    = Join-Path $outDir "logs"
New-Item -ItemType Directory -Force -Path $outDir, $logDir | Out-Null

Set-Location $scriptDir
$env:YB_TBA_OUTPUT_DIR = $outDir   # once we add this to the script
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
python -u yieldbook_tba_metrics.py *>> (Join-Path $logDir "tba_$stamp.log")
