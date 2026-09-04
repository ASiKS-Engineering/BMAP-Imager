$ErrorActionPreference = 'Stop'

Write-Host 'Checking MinGW-w64 compiler...' -ForegroundColor Cyan
$gxx = Get-Command x86_64-w64-mingw32-g++.exe -ErrorAction SilentlyContinue
if (-not $gxx) { $gxx = Get-Command g++.exe -ErrorAction SilentlyContinue }
if (-not $gxx) {
    Write-Error 'No MinGW-w64 g++ found in PATH. Install MSYS2 MinGW-w64 and open the UCRT64/MINGW64 shell, or add its bin directory to PATH.'
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root 'src\main.cpp'
$outDir = Join-Path $root 'bin'
$out = Join-Path $outDir 'bmapflash.exe'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

& $gxx.Source `
  -std=c++17 -O2 -Wall -Wextra -Wpedantic `
  -DUNICODE -D_UNICODE -DNOMINMAX -DWIN32_LEAN_AND_MEAN `
  $src -o $out `
  -static -static-libgcc -static-libstdc++ `
  -municode `
  -ladvapi32 -lsetupapi

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Built: $out" -ForegroundColor Green
