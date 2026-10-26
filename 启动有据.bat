@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "URL=http://127.0.0.1:8848"

rem 已经起过一次的话，不要再起第二个 —— 第二个会占不到端口，
rem 报一个 WinError 10048，而窗口一闪就没了，看着像「双击没反应」。
netstat -ano | findstr /C:"127.0.0.1:8848" | findstr /C:"LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo 服务已经在跑，直接打开浏览器。
  start "" %URL%
  exit /b 0
)

rem 依次找：py 启动器 → python → 本机装过的绝对路径
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Users\17378\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\17378\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined PY (
  echo 没找到 Python。装一个 3.10 以上的，或者把下面这行改成你的 python.exe 路径。
  echo     set "PY=你的路径"
  pause
  exit /b 1
)

if not exist "data\kb.db" (
  echo 找不到 data\kb.db。数据包没解压，或者解到别的地方去了。
  echo 数据包要解到本目录，解完应该看到 data\kb.db、data\catalog.db、data\fulltext\
  pause
  exit /b 1
)

echo 有据 · 政策效力核验与可溯源引用
echo.
echo 正在启动，稍等 2 秒会自动打开浏览器...
start "" %URL%
%PY% app\server.py
if errorlevel 1 (
  echo.
  echo 启动失败。看上面最后几行报错；也可以先跑 python scripts\selftest.py
  pause
)
