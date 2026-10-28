# Noted — funciones compartidas de entorno
#
# Una sola implementacion de "donde esta instalado", "que interprete usar" y
# "como parar/arrancar el daemon". Lo usan watchdog.ps1,
# restart_after_update.ps1 (llamado por hooks/post-merge),
# scripts/restart_noted.ps1 y el comando /update-noted. Ningun consumidor
# debe reinventar esta logica ni hardcodear rutas de instalacion.
#
# Uso:  . (Join-Path $dir "tr_env.ps1")

$script:TRConfigFile = Join-Path $env:LOCALAPPDATA 'Noted\install_path.txt'


function Test-TRRoot {
    # Un directorio es una instalacion valida si es un clon con el codigo dentro.
    # No se comprueba la URL del remote: los forks son legitimos.
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if (-not (Test-Path (Join-Path $Path '.git')))        { return $false }
    if (-not (Test-Path (Join-Path $Path 'main.py')))     { return $false }
    return (Test-Path (Join-Path $Path 'tray_app.py'))
}


function Save-TRRoot {
    # Persiste la ruta descubierta para que el siguiente arranque no vuelva a buscar.
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $dir = Split-Path -Parent $script:TRConfigFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Set-Content -Path $script:TRConfigFile -Value (Resolve-Path $Path).Path -Encoding utf8
    } catch {}
}


function Get-TRRoot {
    # Devuelve la ruta de instalacion, o $null si no la encuentra.
    # Orden: variable de entorno -> config guardada -> repo del directorio actual
    #        -> ubicaciones habituales.
    # Si devuelve $null, el llamador debe PREGUNTAR al usuario, nunca asumir
    # una ruta ni clonar de nuevo: un segundo clon deja dos apps compitiendo
    # por el mismo .lock.

    if (Test-TRRoot $env:NOTED_HOME) {
        return (Resolve-Path $env:NOTED_HOME).Path
    }

    if (Test-Path $script:TRConfigFile) {
        $saved = Get-Content $script:TRConfigFile -Raw -ErrorAction SilentlyContinue
        if ($saved) { $saved = $saved.Trim() }
        if (Test-TRRoot $saved) { return (Resolve-Path $saved).Path }
    }

    $top = & git rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -eq 0 -and $top) {
        $top = ($top -replace '/', '\').Trim()
        if (Test-TRRoot $top) {
            $top = (Resolve-Path $top).Path
            Save-TRRoot $top
            return $top
        }
    }

    foreach ($cand in @(
        (Join-Path $env:USERPROFILE 'Documents\Noted'),
        (Join-Path $env:USERPROFILE 'repos\noted'),
        (Join-Path $env:USERPROFILE 'source\repos\noted'),
        (Join-Path $env:USERPROFILE 'git\noted'),
        (Join-Path $env:USERPROFILE 'Noted')
    )) {
        if (Test-TRRoot $cand) {
            $cand = (Resolve-Path $cand).Path
            Save-TRRoot $cand
            return $cand
        }
    }

    return $null
}


function Get-TRPython {
    # Devuelve @{ Python; Pythonw; Source } o $null.
    # Prioridad: .venv del repo -> PATH (ignorando el alias de WindowsApps)
    #            -> instalaciones por usuario.
    # El .venv va primero: si el repo tiene uno, es el interprete con las
    # dependencias instaladas, y usar el del sistema instala en el sitio
    # equivocado y arranca la app sin sus paquetes.
    param([Parameter(Mandatory = $true)][string]$Root)

    $venv = Join-Path $Root '.venv\Scripts'
    $venvPy = Join-Path $venv 'python.exe'
    if (Test-Path $venvPy) {
        $venvPyw = Join-Path $venv 'pythonw.exe'
        if (-not (Test-Path $venvPyw)) { $venvPyw = $venvPy }
        return [pscustomobject]@{ Python = $venvPy; Pythonw = $venvPyw; Source = 'venv' }
    }

    $exe = $null
    foreach ($name in @('pythonw.exe', 'python.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { $exe = $cmd.Source; break }
    }

    if (-not $exe) {
        # Buscar en rutas conocidas (instalación clásica y pythoncore/Store)
        $candidates = @(
            "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe"
        )
        # pythoncore-* (Python Manager / Microsoft Store)
        if (Test-Path "$env:LOCALAPPDATA\Python") {
            $candidates += Get-ChildItem "$env:LOCALAPPDATA\Python" -Filter 'pythonw.exe' -Recurse -ErrorAction SilentlyContinue |
                           Select-Object -ExpandProperty FullName
        }
        foreach ($cand in $candidates) {
            if (Test-Path $cand) { $exe = $cand; break }
        }
    }

    if (-not $exe) { return $null }

    $dir = Split-Path -Parent $exe
    $py  = Join-Path $dir 'python.exe'
    $pyw = Join-Path $dir 'pythonw.exe'
    if (-not (Test-Path $py))  { $py  = $exe }
    if (-not (Test-Path $pyw)) { $pyw = $py }

    return [pscustomobject]@{ Python = $py; Pythonw = $pyw; Source = 'system' }
}


function Get-TRLivePid {
    # El PID que guarda un fichero (.lock, .recording_active) si es un Python
    # vivo, o $null. Es el criterio de main.py (_check_single_instance) para
    # dar por buena otra instancia: un PID muerto, o reciclado por otro
    # programa, no cuenta.
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    $id = 0
    if (-not $raw -or -not [int]::TryParse($raw.Trim(), [ref]$id)) { return $null }
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like '*python*') { return $id }
    return $null
}


function Read-TRPipelineJobs {
    # Una lectura de lo que hay en marcha. Un fichero a medio escribir (el
    # daemon lo trunca y lo reescribe) cuenta como vacio; lo tapa la segunda
    # lectura de Get-TRActiveJobs.
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$DaemonPid
    )

    $jobs = @()
    try {
        $status = Get-Content -LiteralPath (Join-Path $Root '.pipeline_status.json') -Raw -Encoding UTF8 -ErrorAction Stop |
            ConvertFrom-Json
        # Where-Object: con un fichero vacio .jobs es $null, y @($null) cuenta 1.
        $jobs = @($status.jobs | Where-Object { $_ })
    } catch {}

    # .recording_active vive desde que empieza la grabacion hasta que el WAV
    # esta escrito. El ultimo tramo, mezclar y guardar (segundos en una reunion
    # larga), no sale en .pipeline_status.json: la grabacion ya se marco como
    # terminada y el trabajo aun no ha entrado en la cola. Uno con otro PID es
    # de una instancia que murio grabando y no se limpio.
    $grabando = @($jobs | Where-Object { $_.stage -eq 'recording' }).Count -gt 0
    if (-not $grabando -and (Get-TRLivePid (Join-Path $Root '.recording_active')) -eq $DaemonPid) {
        $jobs += [pscustomobject]@{ stage = 'recording'; label = 'guardando el audio de la reunion' }
    }
    return $jobs
}


function Get-TRActiveJobs {
    # Lo que un reinicio mataria ahora mismo: grabacion, procesado
    # (transcribir, minutas, coaching, acciones) o cola. Vacio = se puede
    # reiniciar. TODO reinicio pasa por aqui: hooks/post-merge
    # (restart_after_update.ps1), scripts/restart_noted.ps1 y /update-noted.
    # Cada uno tenia su propia puerta y solo se arreglo una: el 2026-10-13 un
    # reinicio que solo miraba la grabacion corto el procesado de una reunion
    # de cliente, y se perdieron su coaching y sus acciones.
    #
    # Se lee dos veces, 3 s aparte: entre sacar un trabajo de la cola y
    # marcarlo como procesando la lista sale vacia un instante. Solo cuenta con
    # el daemon vivo: si murio a mitad, el fichero se queda con trabajos que ya
    # nadie va a terminar y no deben impedir arrancarlo.
    param([Parameter(Mandatory = $true)][string]$Root)

    $daemon = Get-TRLivePid (Join-Path $Root '.lock')
    if (-not $daemon) { return @() }

    $jobs = @(Read-TRPipelineJobs -Root $Root -DaemonPid $daemon)
    if ($jobs.Count -eq 0) {
        Start-Sleep -Seconds 3
        $jobs = @(Read-TRPipelineJobs -Root $Root -DaemonPid $daemon)
    }
    return $jobs
}


function Get-TRDaemonProcess {
    # Procesos del daemon de ESTA instalacion, filtrados por linea de comandos.
    # Filtrar por nombre de proceso ("Get-Process python") esta mal por dos
    # motivos: mata Python de otros proyectos del usuario, y no captura
    # pythonw.exe, que es precisamente como corre la app.
    param([Parameter(Mandatory = $true)][string]$Root)

    $mainPy = [System.Management.Automation.WildcardPattern]::Escape((Join-Path $Root 'main.py'))
    return @(
        Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*$mainPy*" }
    )
}


function Get-TRWatchdogProcess {
    param([Parameter(Mandatory = $true)][string]$Root)

    $wd = [System.Management.Automation.WildcardPattern]::Escape((Join-Path $Root 'watchdog.ps1'))
    return @(
        Get-CimInstance Win32_Process -Filter "Name like '%powershell%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*$wd*" }
    )
}


function Stop-TRDaemon {
    # Para el daemon (proceso padre + hijo de la UI). Devuelve cuantos paro.
    param([Parameter(Mandatory = $true)][string]$Root)

    # @() obligatorio: PowerShell desenvuelve los arrays de un elemento al
    # retornarlos, y entonces .Count vale $null en vez de 1.
    $procs = @(Get-TRDaemonProcess -Root $Root)
    if ($procs.Count -eq 0) { return 0 }

    # Los hijos primero: si muere el padre antes, deja huerfanos con el .lock tomado.
    $ids      = @($procs | ForEach-Object { $_.ProcessId })
    $children = @($procs | Where-Object { $ids -contains $_.ParentProcessId })
    $parents  = @($procs | Where-Object { $ids -notcontains $_.ParentProcessId })

    foreach ($p in ($children + $parents)) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    return $procs.Count
}


function Stop-TRWatchdog {
    # Se para antes de actualizar dependencias: si sigue vivo, relanza el daemon
    # a los 5 segundos y pip se pelea con los ficheros en uso.
    param([Parameter(Mandatory = $true)][string]$Root)

    $procs = @(Get-TRWatchdogProcess -Root $Root)
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    return $procs.Count
}


function Start-TRDaemon {
    # Arranca la app a traves del watchdog, que es el UNICO responsable de
    # lanzar main.py. Arrancar main.py aparte crea una segunda instancia que
    # pelea por el .lock con la que el watchdog acabara de levantar.
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [int]$TimeoutSeconds = 30
    )

    if (@(Get-TRWatchdogProcess -Root $Root).Count -eq 0) {
        $wd = Join-Path $Root 'watchdog.ps1'
        if (-not (Test-Path $wd)) { return $false }
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $wd) `
            -WorkingDirectory $Root | Out-Null
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-TRDaemonProcess -Root $Root).Count -gt 0) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}


function Update-TRDependencies {
    param([Parameter(Mandatory = $true)][string]$Root)

    $py = Get-TRPython -Root $Root
    if (-not $py) { return $false }

    $req = Join-Path $Root 'requirements.txt'
    if (-not (Test-Path $req)) { return $false }

    & $py.Python -m pip install -r $req --quiet
    return ($LASTEXITCODE -eq 0)
}


function Show-TRNotification {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$Title = 'Noted'
    )

    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $notify = New-Object System.Windows.Forms.NotifyIcon
        $notify.Icon = [System.Drawing.SystemIcons]::Information
        $notify.BalloonTipTitle = $Title
        $notify.BalloonTipText  = $Text
        $notify.Visible = $true
        $notify.ShowBalloonTip(8000)
        Start-Sleep -Seconds 2
        $notify.Dispose()
    } catch {}
}
