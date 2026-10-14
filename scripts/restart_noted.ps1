# Reinicia el daemon de Noted de forma segura.
# Usar SIEMPRE este script en lugar de comandos WMI/CIM ad-hoc —
# los metodos Win32_Process.Create disparan alertas de seguridad en entornos corporativos.

$root = Split-Path $PSScriptRoot -Parent
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$venv = if (Test-Path $venvPython) { $venvPython } else { (Get-Command python -ErrorAction Stop).Source }
$main = Join-Path $root "main.py"

# ── GUARDRAIL: no reiniciar si hay una grabacion activa ──────────────────────
$sentinel = Join-Path $root ".recording_active"
if (Test-Path $sentinel) {
    $sentinelPid = (Get-Content $sentinel -ErrorAction SilentlyContinue).Trim()
    $isAlive = $false
    if ($sentinelPid) {
        try {
            $proc = Get-Process -Id ([int]$sentinelPid) -ErrorAction Stop
            $isAlive = $true
        } catch { }
    }
    if ($isAlive) {
        Write-Host ""
        Write-Host "  *** GRABACION ACTIVA — NO SE PUEDE REINICIAR ***" -ForegroundColor Red
        Write-Host "  Espera a que termine la reunion antes de reiniciar Noted." -ForegroundColor Yellow
        Write-Host ""
        exit 1
    } else {
        # Sentinel obsoleto (proceso murio sin limpiar) — ignorar y continuar
        Remove-Item $sentinel -Force -ErrorAction SilentlyContinue
    }
}

# -- GUARDRAIL: no reiniciar mientras se PROCESA una reunion -------------------
# El sentinel de arriba solo cubre la grabacion. El 2026-10-13 un reinicio con la
# grabacion ya terminada corto el pipeline 39 s despues de guardar las minutas, y
# se perdieron las notas de coaching y las acciones de esa reunion.
# .pipeline_status.json lista grabacion, procesado y cola; en reposo esta vacio.
# Se lee dos veces: entre sacar un trabajo de la cola y marcarlo como procesando
# hay un instante en que la lista sale vacia. Solo bloquea con el daemon vivo: si
# murio a mitad, el fichero se queda con trabajos y no debe impedir arrancarlo.
$lock = Join-Path $root ".lock"
$status = Join-Path $root ".pipeline_status.json"
$daemonAlive = $false
if (Test-Path $lock) {
    $lockPid = (Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($lockPid -and (Get-Process -Id ([int]$lockPid.Trim()) -ErrorAction SilentlyContinue)) { $daemonAlive = $true }
}
function Get-NotedJobs {
    try { return @((Get-Content $status -Raw -ErrorAction Stop | ConvertFrom-Json).jobs) } catch { return @() }
}
if ($daemonAlive) {
    $jobs = @(Get-NotedJobs)
    if ($jobs.Count -eq 0) { Start-Sleep -Seconds 3; $jobs = @(Get-NotedJobs) }
    if ($jobs.Count -gt 0) {
        Write-Host ""
        Write-Host "  *** NOTED ESTA PROCESANDO - NO SE PUEDE REINICIAR ***" -ForegroundColor Red
        foreach ($j in $jobs) { Write-Host ("    - {0}: {1}" -f $j.stage, $j.label) -ForegroundColor Yellow }
        Write-Host "  Espera a que termine (icono de la bandeja) antes de reiniciar." -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
}

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
