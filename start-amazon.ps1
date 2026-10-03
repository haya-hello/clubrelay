param([ValidateRange(1024,65535)][int]$Port = 8023)
$ErrorActionPreference = 'Stop'
# 独立虚构演示，不修改原工作库或STEMergent演示。 / Isolate the fictional demo from the working and STEMergent databases.
$env:QINGLIAN_DATA_DIR = Join-Path $PSScriptRoot 'var\clubrelay-amazon'
$env:QINGLIAN_AI_API_KEY = ''
$env:PYTHONIOENCODING = 'utf-8'
$runtimePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { Write-Host "Existing service retained: http://127.0.0.1:$Port/handoffs/e5abc715-cdf8-5514-983b-c88cd1a31672/briefing/"; exit 0 }
Set-Location -LiteralPath $PSScriptRoot
& $runtimePython manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Migration failed.' }
& $runtimePython manage.py seed_amazon_demo
if ($LASTEXITCODE -ne 0) { throw 'Fixture setup failed.' }
Write-Host "ClubRelay: http://127.0.0.1:$Port/handoffs/e5abc715-cdf8-5514-983b-c88cd1a31672/briefing/"
Write-Host 'Local credentials: var\clubrelay-amazon\demo-accounts.json. Configure your own model and permit only fictional files.'
& $runtimePython manage.py runserver "127.0.0.1:$Port" --noreload --insecure
