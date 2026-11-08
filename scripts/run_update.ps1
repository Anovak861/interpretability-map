# Runs one Interpretability Map update and appends its output to data\logs\scheduler.log.
# Task Scheduler calls this; it is also fine to run by hand.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "data\logs\scheduler.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

# Task Scheduler starts with a minimal environment: make sure headless Claude Code is findable
if (-not $env:CLAUDE_BIN) {
    $candidate = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
    if (Test-Path $candidate) { $env:CLAUDE_BIN = $candidate }
}
$env:PYTHONIOENCODING = "utf-8"

Set-Location $root
"`n===== $(Get-Date -Format s) update start" | Out-File -Append -Encoding utf8 $log
& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "cli.py") update *>&1 |
    Out-File -Append -Encoding utf8 $log
"===== $(Get-Date -Format s) update end (exit $LASTEXITCODE)" | Out-File -Append -Encoding utf8 $log
exit $LASTEXITCODE
