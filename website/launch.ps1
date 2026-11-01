# Watchtower for "__NAME__" -- from __SITE__/e/__EVENT__  (Windows PowerShell)
#   irm __SITE__/run/<token>.ps1 | iex                                   FMS + hub cameras (pick them in /control -> Setup)
#   $env:WATCHTOWER_ARGS='--mock'; irm __SITE__/run/<token>.ps1 | iex    FMS + fake fuel instead
# Installs into %USERPROFILE%\Watchtower. Uses uv (a Python installer) if there's no Python 3.10+.
$ErrorActionPreference = 'Stop'
$Site = '__SITE__'; $Event = '__EVENT__'; $Token = '__TOKEN__'
$WT = if ($env:WATCHTOWER_HOME) { $env:WATCHTOWER_HOME } else { Join-Path $HOME 'Watchtower' }
$App = Join-Path $WT 'app'; $Ev = Join-Path $WT "events\$Event"
$Args2 = if ($env:WATCHTOWER_ARGS) { $env:WATCHTOWER_ARGS -split ' ' } else { @() }
New-Item -ItemType Directory -Force -Path (Join-Path $Ev 'config') | Out-Null

$V = (Invoke-RestMethod "$Site/download/version").Trim()
$VerFile = Join-Path $App '.version'
if (-not (Test-Path $VerFile) -or (Get-Content $VerFile) -ne $V) {
  Write-Host "Downloading Watchtower ($V)"
  $Tgz = Join-Path $WT 'app.tgz'
  Invoke-WebRequest "$Site/download/watchtower.tar.gz" -OutFile $Tgz
  if (Test-Path $App) { Remove-Item -Recurse -Force $App }
  New-Item -ItemType Directory -Force -Path $App | Out-Null
  tar -xzf $Tgz -C $App; Remove-Item $Tgz
  Set-Content $VerFile $V
}

$Py = Join-Path $WT 'venv\Scripts\python.exe'
if (-not (Test-Path $Py)) {
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing uv (Python installer)'
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$HOME\.local\bin;$env:Path"
  }
  uv venv --seed --python 3.12 (Join-Path $WT 'venv')
}
& $Py -m pip install -q --disable-pip-version-check -r (Join-Path $App 'requirements-run.txt')
if ($Args2 -notcontains '--no-vision') {
  & $Py -m pip install -q --disable-pip-version-check -r (Join-Path $App 'vision\requirements-counter.txt')
  if ($LASTEXITCODE -ne 0) { Write-Host "OpenCV didn't install, so the hub cameras are off. Watchtower still runs." }
}

$Cfg = Join-Path $Ev 'config\event.yaml'
if (-not (Test-Path $Cfg)) {
  Invoke-WebRequest -Headers @{ 'X-Watchtower-Token' = $Token } "$Site/api/link/$Event/event.yaml" -OutFile $Cfg
}
& $Py (Join-Path $App 'launch.py') $Ev @Args2
