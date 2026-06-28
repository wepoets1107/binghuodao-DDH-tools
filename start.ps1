$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$vendor = Join-Path $here "vendor"
if (Test-Path -LiteralPath $vendor) {
  $env:PYTHONPATH = "$vendor;$env:PYTHONPATH"
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$pythonPath = if ($pythonCmd) { $pythonCmd.Source } else { $null }
$pythonArgs = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8888")

if (-not $pythonPath) {
  $preferredPython = @(
    "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"
  )
  $pythonPath = $preferredPython | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

if (-not $pythonPath) {
  $pythonCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($pythonCmd) { $pythonPath = $pythonCmd.Source }
  $pythonArgs = @("-3") + $pythonArgs
}

if (-not $pythonPath) {
  Write-Host "Python was not found. Please install Python 3.11+ and add it to PATH."
  exit 1
}

Write-Host "Starting Deribit DDH Workbench: http://127.0.0.1:8888"
& $pythonPath @pythonArgs
