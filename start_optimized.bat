@echo off
setlocal
set "CMV_AUTO_MIGRATE=0"
rem Use the optimized source frontend and backend together.
call "%~dp0start_dev.bat" --no-hotfix --no-update-check %*
endlocal
