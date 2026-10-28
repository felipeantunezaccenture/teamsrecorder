@echo off
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

rem Eliminar VBS si todavia existe
del /Q "%STARTUP%\Noted.vbs" 2>nul

rem Eliminar las tareas programadas: la de inicio de sesion y la que lo relanza
rem cada 30 min (install_task.ps1). Sin la segunda, Noted volvia a arrancar solo.
powershell.exe -NonInteractive -ExecutionPolicy Bypass -Command "foreach ($t in 'Noted', 'Noted-Heartbeat') { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }; Write-Host 'Tareas Noted y Noted-Heartbeat eliminadas.'"

echo Noted eliminado del inicio de Windows.
pause
