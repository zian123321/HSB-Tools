$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Join-Path $Root 'config.json'
if (!(Test-Path $Config)) {
  $examplePath = Join-Path $Root 'config.example.json'
  if (!(Test-Path $examplePath)) { throw "Missing config.json and config.example.json under: $Root. Copy config.example.json next to run.ps1." }
  Copy-Item $examplePath $Config
}
# ASCII defaults (pure ASCII to avoid PowerShell 5.1 GBK parsing errors).
$defaults = [PSCustomObject]@{
  systemUrl = 'http://<WMS_HOST>:<PORT>/gwms_hsb/main.jsf'
  headless = $true
  downloadInventoryAutomatically = $true
  createOrdersAutomatically = $true
  auditOrders = $false
  maxPerOrder = 500
  barcodeType = '04'
  targetLocation = ''
  loginRetries = 2
}
$parsed = Get-Content -Raw -Encoding UTF8 $Config | ConvertFrom-Json
if ($null -eq $parsed -or $parsed -isnot [PSCustomObject]) {
  $badType = if ($null -eq $parsed) { 'null/empty file' } else { $parsed.GetType().Name }
  Write-Warning "config.json is not an object ($badType); backed it up; merged ASCII defaults."
  Copy-Item $Config ("$Config.bak_" + (Get-Date -Format 'yyyyMMdd_HHmmss')) -Force
  $cfg = $defaults.PSObject.Copy()
} else {
  $cfg = $defaults.PSObject.Copy()
  foreach ($prop in $parsed.PSObject.Properties) {
    $v = $prop.Value
    $hasValue = ($null -ne $v) -and -not ($v -is [string] -and [string]::IsNullOrWhiteSpace([string]$v))
    if (-not $hasValue) { continue }
    if ($cfg.PSObject.Properties.Name -contains $prop.Name) { $cfg.($prop.Name) = $v }
    else { $cfg | Add-Member -NotePropertyName $prop.Name -NotePropertyValue $v }
  }
}
$previousSource = [string]$cfg.sourceFile
Add-Type -AssemblyName System.Windows.Forms
# Always ask user to pick source Excel file each run
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Filter = 'Excel (*.xlsx)|*.xlsx'
$dlg.Title = 'Select source inventory Excel file'
$dlg.InitialDirectory = [Environment]::GetFolderPath('Desktop')
if ($dlg.ShowDialog() -ne 'OK') { throw 'No source file selected.' }
if ($cfg.PSObject.Properties.Name -contains 'sourceFile') { $cfg.sourceFile = $dlg.FileName }
else { $cfg | Add-Member -NotePropertyName sourceFile -NotePropertyValue $dlg.FileName }
if ($previousSource -and $previousSource -ne $cfg.sourceFile -and (Test-Path (Join-Path $Root '输出文件'))) {
  try {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Move-Item -LiteralPath (Join-Path $Root '输出文件') -Destination (Join-Path $Root "输出文件_backup_$stamp") -ErrorAction Stop
  } catch {
    Write-Warning 'Previous 输出文件 locked (file open in Word/Excel); kept old files, new run will overwrite them.'
  }
}
$account = if ($env:GWMS_ACCOUNT) { $env:GWMS_ACCOUNT } elseif ($cfg.account) { $cfg.account } else { '' }
$password = if ($env:GWMS_PASSWORD) { $env:GWMS_PASSWORD } elseif ($cfg.password) { $cfg.password } else { '' }
if (-not $account -or -not $password) { throw '缺少账号或密码：请在 config.json 填写 account/password，或设置环境变量 GWMS_ACCOUNT、GWMS_PASSWORD。' }
Write-Host ('[auto-login] account = ' + $account) -ForegroundColor DarkGray
$json = $cfg | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($Config, $json, (New-Object Text.UTF8Encoding($false)))
$env:GWMS_ACCOUNT = $account
$env:GWMS_PASSWORD = $password
Write-Host '[1/5] Configuration loaded. Starting...' -ForegroundColor Cyan
# Resolve Node/Python from PATH
$Node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (!$Node) { throw 'Node.js not found. Please install Node.js, then run npm install in the project directory.' }
Write-Host "Using system Node: $Node" -ForegroundColor DarkGray

$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (!$Python) { $Python = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (!$Python) { throw 'Python not found. Please install Python.' }
Write-Host "Using system Python: $Python" -ForegroundColor DarkGray

# Ensure local node_modules exists (run npm install once before first use)
if (!(Test-Path (Join-Path $Root 'node_modules'))) {
  Write-Warning 'node_modules 不存在；请先在项目目录执行 npm install，否则 Playwright 无法加载。'
}
try {
  # Kill leftover headless Chromium processes (Playwright) to free memory
  Get-CimInstance Win32Process -Filter "Name='msedge.exe' OR Name='chrome.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*--headless*' -or $_.CommandLine -like '*ms-playwright*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 1
  # Clean up old browser-session folders (older than 3 days)
  Get-ChildItem $Root -Directory -Filter 'browser-session-*' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-3) } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host '[1.5/5] Preflight environment check...' -ForegroundColor Cyan
  & $Node --max-old-space-size=8192 (Join-Path $Root 'check.mjs') $Root
  if ($LASTEXITCODE -ne 0) { throw 'Preflight check failed.' }
  if ($cfg.downloadInventoryAutomatically) {
    Write-Host '[2/5] Downloading inventory report...' -ForegroundColor Cyan
    & $Node --max-old-space-size=8192 (Join-Path $Root 'inventory-download.mjs') $Root
    if ($LASTEXITCODE -ne 0) { throw 'Inventory download failed.' }
  } else {
    Write-Host '[2/5] Using existing inventory report.' -ForegroundColor Cyan
  }
  Write-Host '[3/5] Processing barcodes and matching locations...' -ForegroundColor Cyan
  & $Python (Join-Path $Root 'prepare.py') $Root
  if ($LASTEXITCODE -ne 0) { throw 'Barcode processing failed.' }
  Write-Host '[4/5] Generating import .xls files (pure Python, no LibreOffice)...' -ForegroundColor Cyan
  & $Python (Join-Path $Root 'convert-xls.py') $Root
  if ($LASTEXITCODE -ne 0) { throw 'XLS generation failed.' }
  if ($cfg.createOrdersAutomatically) {
    Write-Host '[5/5] Creating transfer orders...' -ForegroundColor Yellow
    & $Node --max-old-space-size=8192 (Join-Path $Root 'system.mjs') $Root orders
    if ($LASTEXITCODE -ne 0) { throw 'Transfer order creation failed.' }
  } else {
    Write-Host '[5/5] Automatic order creation is disabled.' -ForegroundColor Cyan
  }
  & $Python (Join-Path $Root 'build-docs.py') $Root
  if ($LASTEXITCODE -ne 0) { throw 'Document generation failed.' }
  # Clean up temp files from previous run
  $outDir = Join-Path $Root '输出文件'
  $tempDirs = @('import_xls', 'import_xlsx', 'diagnosis', 'libreoffice_profile')
  foreach ($d in $tempDirs) {
    $p = Join-Path $outDir $d
    if (Test-Path $p) { Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue }
  }
  Get-ChildItem $outDir -Filter 'import_*.xls' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  Get-ChildItem $outDir -Filter 'import_*.xlsx' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  Get-ChildItem $outDir -Filter '*response*.html' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  Get-ChildItem $outDir -Filter 'fail_*.png' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  Write-Host 'Completed successfully.' -ForegroundColor Green
  Write-Host ("Output folder: " + $outDir) -ForegroundColor DarkGray
} catch {
  $msg = $_.Exception.Message
  Write-Host ('FAILED: ' + $msg) -ForegroundColor Red
  [System.Windows.Forms.MessageBox]::Show('Run failed: ' + $msg, 'Error', 'OK', 'Error') | Out-Null
  throw
}
