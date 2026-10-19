Antes de hacer nada, comprueba si Noted tiene trabajo en curso ejecutando este bloque PowerShell:

```powershell
$_tr_dir = Split-Path (Resolve-Path 'tr_env.ps1') -Parent
. "$_tr_dir\tr_env.ps1"
$_jobs = @(Get-TRActiveJobs -Root $_tr_dir)
if ($_jobs.Count -gt 0) { Write-Output "NOTED_BUSY"; $_jobs | ForEach-Object { Write-Output "  - $($_.stage): $($_.label)" } }
```

Si el output contiene "NOTED_BUSY", detente aquí y avisa al usuario de lo que está en marcha (grabando, procesando o en cola): "Noted está trabajando. Espera a que termine antes de actualizar: el pull reinicia la app y cortaría lo que está en marcha."

Esta comprobación va ANTES del pull a propósito. El hook post-merge también la hace y no reinicia si hay trabajo, pero para entonces el pull ya ha cambiado el código en disco bajo un daemon que sigue cargando módulos a mitad del procesado.

Si no hay nada en curso, haz git pull en este proyecto (Noted) y muestra un resumen de los cambios descargados. Si ya estaba al día, indícalo claramente.
