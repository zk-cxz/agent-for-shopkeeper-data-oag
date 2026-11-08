@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Stop Shopkeeper Agent - OAG
echo   (API 8010 + WEB 5174)
echo ============================================

for %%P in (8010 5174) do (
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr LISTENING ^| findstr ":%%P "') do (
    if not "%%p"=="0" (
      echo   kill port %%P pid %%p
      taskkill /F /PID %%p >nul 2>&1
    )
  )
)

echo.
echo   Stopped. Kept on disk: conf\ontology.json, logs\, eval\results\
echo   MySQL service is NOT touched.
echo.
pause
endlocal
