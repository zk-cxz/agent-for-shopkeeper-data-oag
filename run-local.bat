@echo off
setlocal
cd /d "%~dp0"
set API_PORT=8010
set WEB_PORT=5174

echo ============================================
echo   Shopkeeper Agent - OAG
echo   API %API_PORT%   WEB %WEB_PORT%
echo ============================================

echo [1/5] Check virtualenv ...
if not exist ".venv\Scripts\python.exe" goto novenv

echo [2/5] Check ontology ...
if exist "conf\ontology.json" goto hasonto
echo       ontology.json not found, building ...
".venv\Scripts\python.exe" scripts\build_ontology.py
if errorlevel 1 goto buildfail
:hasonto

echo [3/5] Check frontend deps ...
if exist "frontend\node_modules" goto hasfe
echo       node_modules not found, running pnpm install ...
pushd frontend
call pnpm install --registry=https://registry.npmmirror.com
if errorlevel 1 (
  popd
  goto fefail
)
popd
:hasfe

echo [4/5] Start backend on %API_PORT% ...
netstat -ano | findstr LISTENING | findstr ":%API_PORT% " >nul
if not errorlevel 1 goto apiup
start "oag-api" cmd /k ".venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port %API_PORT%"
goto web
:apiup
echo       port %API_PORT% already listening, skip start

:web
echo [5/5] Start frontend on %WEB_PORT% ...
netstat -ano | findstr LISTENING | findstr ":%WEB_PORT% " >nul
if not errorlevel 1 goto webup
start "oag-web" cmd /k "cd frontend && pnpm dev"
goto wait
:webup
echo       port %WEB_PORT% already listening, skip start

:wait
timeout /t 8 /nobreak >nul
start "" "http://127.0.0.1:%WEB_PORT%/"
echo.
echo   UI       : http://127.0.0.1:%WEB_PORT%/
echo   API docs : http://127.0.0.1:%API_PORT%/docs
echo   Ontology : http://127.0.0.1:%API_PORT%/api/ontology
echo   Stop     : stop-local.bat
echo.
echo   Note: MySQL must be running (meta/dw databases).
echo   No Elasticsearch / Qdrant / Embedding service needed.
echo   This UI is the same React app as the RAG version (port 5174 vs 5173).
goto end

:novenv
echo   [ERROR] .venv not found. Create it first:
echo       uv venv .venv -p 3.14
echo       uv pip install -r pyproject.toml
goto end

:buildfail
echo   [ERROR] build_ontology.py failed. Is MySQL running?
goto end

:fefail
echo   [ERROR] pnpm install failed. Is pnpm installed?
goto end

:end
echo.
pause
endlocal
