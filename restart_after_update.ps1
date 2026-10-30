# Noted — reinicio tras una actualizacion
#
# Lo invoca hooks/post-merge despues de cada git pull. Tambien es seguro
# ejecutarlo a mano:
#   powershell -ExecutionPolicy Bypass -File .\restart_after_update.ps1
#
# La ruta se deduce de la ubicacion de este fichero, asi que funciona
# independientemente de donde este clonado el repo.

$root = $PSScriptRoot
. (Join-Path $root 'tr_env.ps1')

# Cualquier trabajo bloquea, no solo la grabacion: ver Get-TRActiveJobs.
$jobs = @(Get-TRActiveJobs -Root $root)
if ($jobs.Count -gt 0) {
    if (@($jobs | Where-Object { $_.stage -eq 'recording' }).Count -gt 0) {
        $aviso = 'Hay una grabacion en curso. Reinicia la app cuando termine la reunion.'
    } else {
        $aviso = 'Noted esta procesando una reunion. Reinicia la app cuando termine.'
    }
    Write-Host ''
    Write-Host "Noted: $aviso" -ForegroundColor Yellow
    foreach ($j in $jobs) { Write-Host ("   - {0}: {1}" -f $j.stage, $j.label) -ForegroundColor Yellow }
    Write-Host '   Los cambios ya se han descargado; la app sigue con el codigo anterior hasta reiniciarla:' -ForegroundColor Yellow
    Write-Host "   powershell -ExecutionPolicy Bypass -File `"$root\restart_after_update.ps1`"" -ForegroundColor Yellow
    Write-Host ''
    Show-TRNotification -Text $aviso
    exit 0
}

# Orden importante: parar el watchdog antes que el daemon. Al contrario, el
# watchdog relanza la app a los 5 segundos y pip actualiza ficheros en uso.
Stop-TRWatchdog -Root $root | Out-Null
Stop-TRDaemon   -Root $root | Out-Null

if (-not (Update-TRDependencies -Root $root)) {
    Write-Host 'Noted: no se pudieron actualizar las dependencias.' -ForegroundColor Yellow
    Write-Host '   Revisa que exista el entorno virtual (.venv) o que python este en el PATH.' -ForegroundColor Yellow
}

if (Start-TRDaemon -Root $root) {
    Write-Host 'Noted reiniciado con los ultimos cambios.' -ForegroundColor Green
    exit 0
}

Write-Host 'Noted: el daemon no ha arrancado.' -ForegroundColor Red
Write-Host "   Revisa $root\noted.log" -ForegroundColor Red
exit 1
