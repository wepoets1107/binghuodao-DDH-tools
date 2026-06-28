$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$runScript = Join-Path $here "run_server.py"
$dataDir = Join-Path $here "data"
if (-not (Test-Path -LiteralPath $dataDir)) {
  New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
}
$vendor = Join-Path $here "vendor"
if (Test-Path -LiteralPath $vendor) {
  $env:PYTHONPATH = "$vendor;$env:PYTHONPATH"
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$pythonPath = if ($pythonCmd) { "python" } else { $null }
$pythonArgs = "run_server.py"

if (-not $pythonPath) {
  $preferredPython = @(
    "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"
  )
  $pythonPath = $preferredPython | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

if (-not $pythonPath) {
  $pythonCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($pythonCmd) { $pythonPath = "py" }
  $pythonArgs = "-3 run_server.py"
}

if (-not $pythonPath) {
  Write-Host "Python was not found. Please install Python 3.11+ and add it to PATH."
  exit 1
}

Start-Process -FilePath $pythonPath -ArgumentList $pythonArgs -WorkingDirectory $here -WindowStyle Hidden

Write-Host "DDH workbench started in background: http://127.0.0.1:8888"
