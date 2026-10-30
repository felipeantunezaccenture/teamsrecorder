# Reinicia el daemon de Noted de forma segura.
# Usar SIEMPRE este script en lugar de comandos WMI/CIM ad-hoc —
# los metodos Win32_Process.Create disparan alertas de seguridad en entornos corporativos.
#
# Mismo camino que restart_after_update.ps1 (el hook post-merge): la puerta,
# parar el daemon y arrancarlo con Start-TRDaemon. El watchdog es el UNICO que
# lanza main.py (ver tr_env.ps1). Antes este script lanzaba main.py por su
# cuenta, con el watchdog vivo relanzando otro a la vez, y mataba cualquier
# Python con una ventana titulada "Noted", que tambien es el panel.

$root = Split-Path $PSScriptRoot -Parent

# -- GUARDRAIL: no reiniciar con trabajo a medias ------------------------------
# Grabando, guardando el audio, procesando o en cola. La puerta es la misma que
# usa el hook post-merge, Get-TRActiveJobs en tr_env.ps1: antes este script tenia
# su propia copia, se arreglo solo aqui (377ee27) y la del hook siguio dejando
# reiniciar a mitad de un procesado.
. (Join-Path $root 'tr_env.ps1')

$jobs = @(Get-TRActiveJobs -Root $root)
if ($jobs.Count -gt 0) {
    Write-Host ""
    Write-Host "  *** NOTED ESTA TRABAJANDO - NO SE PUEDE REINICIAR ***" -ForegroundColor Red
    foreach ($j in $jobs) { Write-Host ("    - {0}: {1}" -f $j.stage, $j.label) -ForegroundColor Yellow }
    Write-Host "  Espera a que termine (icono de la bandeja) antes de reiniciar." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# Solo el daemon (main.py): el watchdog lo ve morir y lo relanza a los 5 s, y si
# no hay watchdog, Start-TRDaemon lo arranca. Python lo elige el watchdog
# (Get-TRPython: .venv, o el global sin el alias de WindowsApps).
Stop-TRDaemon -Root $root | Out-Null

if (Start-TRDaemon -Root $root) {
    Write-Host "Noted arrancado."
    exit 0
}
Write-Host "Noted: el daemon no ha arrancado. Revisa $root\noted.log" -ForegroundColor Red
exit 1
