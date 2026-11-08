# Portable launcher; first run installs missing dependencies.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python scripts/pulse.py dev
exit $LASTEXITCODE
