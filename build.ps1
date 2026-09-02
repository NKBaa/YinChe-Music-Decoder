$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name "音澈-音乐解码" `
  --add-data "hook_qq_music.js;." `
  --add-data "assets;assets" `
  --collect-all frida `
  app.py

Write-Host "构建完成: $Root\dist\音澈-音乐解码.exe"
