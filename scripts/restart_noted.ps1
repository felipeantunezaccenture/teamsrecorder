# Reinicia el daemon de Noted de forma segura.
# Usar SIEMPRE este script en lugar de comandos WMI/CIM ad-hoc —
# los metodos Win32_Process.Create disparan alertas de seguridad en entornos corporativos.

$root = Split-Path $PSScriptRoot -Parent
$main = Join-Path $root "main.py"

# -- GUARDRAIL: no reiniciar con trabajo a medias ------------------------------
# Grabando, guardando el audio, procesando o en cola. La puerta es la misma que
# usa el hook post-merge, Get-TRActiveJobs en tr_env.ps1: antes este script tenia
# su propia copia, se arreglo solo aqui (377ee27) y la del hook siguio dejando
# reiniciar a mitad de un procesado.
. (Join-Path $root 'tr_env.ps1')

# Sin venv, el Python global (9b4e15a), buscado con Get-TRPython: descarta el
# alias de WindowsApps, que abre la Store en vez de arrancar nada.
$py = Get-TRPython -Root $root
if (-not $py) {
    Write-Host "  No se encontro Python: ni .venv ni uno instalado." -ForegroundColor Red
    exit 1
}
$venv = $py.Python

$jobs = @(Get-TRActiveJobs -Root $root)
if ($jobs.Count -gt 0) {
    Write-Host ""
    Write-Host "  *** NOTED ESTA TRABAJANDO - NO SE PUEDE REINICIAR ***" -ForegroundColor Red
    foreach ($j in $jobs) { Write-Host ("    - {0}: {1}" -f $j.stage, $j.label) -ForegroundColor Yellow }
    Write-Host "  Espera a que termine (icono de la bandeja) antes de reiniciar." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

$lock = Join-Path $root ".lock"

# Detener instancia existente
if (Test-Path $lock) {
    $pid_in_lock = (Get-Content $lock -ErrorAction SilentlyContinue).Trim()
    if ($pid_in_lock) {
        Stop-Process -Id $pid_in_lock -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}

# Matar cualquier proceso python que tenga main.py en el titulo (por si acaso)
Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -like '*Noted*' } |
    Stop-Process -Force -ErrorAction SilentlyContinue

Start-Sleep -Seconds 1

# Arrancar con Start-Process (NO WMI / Invoke-CimMethod)
Start-Process -FilePath $venv -ArgumentList "`"$main`"" -WorkingDirectory $root -WindowStyle Hidden
Write-Host "Noted arrancado."
