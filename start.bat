@echo off
title 白读 · ByRead
cd /d "%~dp0"

rem ---------- 0. 端口 / 监听地址（可用环境变量覆盖，app.py 读的是同一组变量）----------
rem 默认只监听本机 127.0.0.1:5000，局域网访问不到。想让别的设备访问：
rem     set BYREAD_HOST=0.0.0.0
rem     set BYREAD_PORT=8080
rem 再双击本文件。注意：开放到局域网时请同时设 BYREAD_TOKEN 访问口令。
if "%BYREAD_PORT%"=="" set BYREAD_PORT=5000
set BYREAD_URL_HOST=127.0.0.1
if not "%BYREAD_HOST%"=="" if not "%BYREAD_HOST%"=="0.0.0.0" set BYREAD_URL_HOST=%BYREAD_HOST%

echo ============================================
echo   白读 · ByRead   本地 RSS 阅读器
echo ============================================
echo.

rem ---------- 1. 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python。
  echo        请安装 Python 3.10 以上版本，安装时务必勾选 "Add python.exe to PATH"。
  echo        下载地址：https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

rem ---------- 2. 已经在运行？那就只把浏览器打开 ----------
rem 不处理的话，第二个实例会抛 [WinError 10048] 端口被占用，看着像程序坏了
netstat -ano | findstr /r /c:"TCP.*:%BYREAD_PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [提示] 白读已经在运行了，直接打开页面。
  echo        想重启它的话，先关掉那个正在运行的窗口（或按 Ctrl+C）。
  echo.
  start "" http://%BYREAD_URL_HOST%:%BYREAD_PORT%
  ping -n 4 127.0.0.1 >nul
  exit /b 0
)

rem ---------- 3. 检查依赖 ----------
rem 注意：truststore 也要检查。它负责让 Python 使用系统证书库；缺了它，
rem 被本机代理工具（如 SteamTools）做中间人的站点会证书验证失败，
rem 表现为"GitHub 每日趋势抓取失败"。
echo [1/2] 检查依赖...
python -c "import flask, feedparser, readability, lxml, requests, truststore, trafilatura" >nul 2>nul
if errorlevel 1 (
  echo       缺少依赖，正在安装（第一次运行需要联网，大约一两分钟）...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。可以先手动执行这一条看看报什么错：
    echo        python -m pip install -r requirements.txt
    echo        国内网络慢的话换镜像源：
    echo        python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
  )
)

rem ---------- 4. 启动 ----------
echo [2/2] 启动服务...
echo.
echo    浏览器访问： http://%BYREAD_URL_HOST%:%BYREAD_PORT%
echo    数据都在：   instance\bai_read.db   （复制这个文件就等于备份）
echo.
echo    这个窗口就是程序本体，别关它。要停止程序：关掉本窗口，或按 Ctrl+C。
echo.

start "" http://%BYREAD_URL_HOST%:%BYREAD_PORT%
python app.py

echo.
echo 白读已停止。
pause
