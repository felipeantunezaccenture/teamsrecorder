@echo off
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

rem Eliminar VBS si todavia existe
del /Q "%STARTUP%\Noted.vbs" 2>nul

rem Eliminar tarea programada
powershell.exe -NonInteractive -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'Noted' -Confirm:$false -ErrorAction SilentlyContinue; Write-Host 'Tarea Noted eliminada.'"

echo Noted eliminado del inicio de Windows.
pause
