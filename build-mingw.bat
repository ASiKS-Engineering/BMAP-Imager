@echo off
setlocal
where x86_64-w64-mingw32-g++.exe >nul 2>&1
if errorlevel 1 (
  where g++.exe >nul 2>&1
  if errorlevel 1 (
    echo ERROR: MinGW-w64 g++ not found in PATH.
    exit /b 1
  )
  set GXX=g++.exe
) else (
  set GXX=x86_64-w64-mingw32-g++.exe
)
if not exist bin mkdir bin
%GXX% -std=c++17 -O2 -Wall -Wextra -Wpedantic -DUNICODE -D_UNICODE -DNOMINMAX -DWIN32_LEAN_AND_MEAN src\main.cpp -o bin\bmapflash.exe -static -static-libgcc -static-libstdc++ -municode -ladvapi32 -lsetupapi
if errorlevel 1 exit /b %errorlevel%
echo Built: bin\bmapflash.exe
