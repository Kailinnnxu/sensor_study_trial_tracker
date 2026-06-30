$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
& "$Root\.venv\Scripts\Activate.ps1"
flask --app tracker.web.app run
