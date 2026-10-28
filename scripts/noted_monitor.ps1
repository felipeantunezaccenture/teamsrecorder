# Noted Monitor - panel visual de salud.
#
# Responde a una sola pregunta: "puedo entrar a una llamada ahora mismo?".
# Se lanza con Ctrl+Alt+M.
#
# Comprueba las cuatro cosas de las que depende que la reunion se grabe:
#   1. Daemon   - el que graba
#   2. Watchdog - el que lo levanta si se cae
#   3. Microfono    - tu voz
#   4. Sonido del PC - las voces de los demas (loopback WASAPI)
#
# No depende de Python ni del .venv a proposito: hace falta justo cuando la
# app no arranca. El audio se mira con WASAPI directamente.
#
#   .\scripts\noted_monitor.ps1           panel visual
#   .\scripts\noted_monitor.ps1 -Check    lo mismo en texto, sin ventana
#
# OJO al editar: todo texto entre comillas debe ser ASCII puro. El fichero se
# guarda en UTF-8 sin BOM y PowerShell 5.1 lo lee como ANSI; un guion largo
# acaba convertido en comilla tipografica y rompe el parseo.

param(
    [switch]$Check,
    # Fuerza el idioma (es / en / ca). Sin esto se usa el de settings.json.
    [string]$Lang
)

$ErrorActionPreference = 'Continue'

# ── Umbrales (ajusta aqui si tu maquina pide otra cosa) ──────────────────────
$UptimeWarnHours = 72     # daemon lleva demasiado levantado
$VolWarnPercent  = 20     # volumen tan bajo que casi no se oira

# ── Paleta: la misma que popup.py para que todo Noted se vea igual ───────────
$C = @{
    Bg     = '#1e1e2e'
    Card   = '#313244'
    Title  = '#cdd6f4'
    Sub    = '#a6adc8'
    Muted  = '#6c7086'
    Blue   = '#89b4fa'
    Btn    = '#45475a'
    Green  = '#a6e3a1'
    Red    = '#f38ba8'
    Amber  = '#f9e2af'
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function HC([string]$hex) { [System.Drawing.ColorTranslator]::FromHtml($hex) }

# Colores ya construidos. Se indexan por severidad (0 ok / 1 aviso / 2 fallo)
# en vez de resolverlos con un switch dentro del bucle de pintado.
$SevColor = @( (HC $C.Green), (HC $C.Amber), (HC $C.Red) )

# ── Resolucion de la instalacion ─────────────────────────────────────────────
$root = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $root 'tr_env.ps1'
if (-not (Test-Path $envFile)) {
    # Sin tr_env.ps1 no hay instalacion que mirar, y este aviso sale antes de
    # cargar las traducciones: va en ingles, el idioma de respaldo.
    [System.Windows.Forms.MessageBox]::Show("Cannot find tr_env.ps1 in $root", 'Noted Monitor') | Out-Null
    exit 1
}
. $envFile
if (-not (Test-TRRoot $root)) {
    $alt = Get-TRRoot
    if ($alt) { $root = $alt }
}

# ── Idioma ───────────────────────────────────────────────────────────────────
# El mismo que la UI de Noted: settings.json -> language (es / en / ca).
# Los textos viven en un JSON aparte y se leen como UTF-8 a proposito: dentro
# de este .ps1 los acentos se romperian, porque PowerShell 5.1 lee los .ps1
# sin BOM como ANSI.
# OJO: no llamar a estas variables $Lang, que es el parametro del script y se
# borraria a si mismo al inicializarlas.
$script:Txt         = $null
$script:TxtFallback = $null
try {
    $i18nFile = Join-Path $PSScriptRoot 'noted_monitor.i18n.json'
    if (Test-Path $i18nFile) {
        $all = Get-Content $i18nFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $script:TxtFallback = $all.en

        $code = 'en'
        $sf = Join-Path $root 'settings.json'
        if (Test-Path $sf) {
            try {
                $s = Get-Content $sf -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($s.language) { $code = [string]$s.language }
            } catch { }
        }
        if ($Lang) { $code = $Lang }
        if ($all.PSObject.Properties[$code]) { $script:Txt = $all.$code }
        if (-not $script:Txt) { $script:Txt = $all.en }
    }
} catch { }


function TX([string]$k) {
    # Texto traducido. Si falta la clave cae al ingles, y si tampoco esta
    # devuelve la propia clave: el panel nunca se queda en blanco.
    foreach ($d in @($script:Txt, $script:TxtFallback)) {
        if ($d -and $d.PSObject.Properties[$k]) {
            $v = $d.$k
            if ($v) { return [string]$v }
        }
    }
    return $k
}

$SevWord = @( (TX 'sev_ok'), (TX 'sev_warn'), (TX 'sev_bad') )


# ── WASAPI: dispositivo de audio por defecto, mute, volumen y nivel en vivo ──
# Interop minimo. Solo se declaran los metodos necesarios, pero respetando el
# ORDEN de la vtable: los huecos previos se rellenan con stubs sin usar.
if (-not ('NotedAudio' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumerator { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
                 [MarshalAs(UnmanagedType.IUnknown)] out object iface);
}

[ComImport, Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioMeterInformation {
    int GetPeakValue(out float peak);
}

[ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioEndpointVolume {
    int NotUsed1();                                  // RegisterControlChangeNotify
    int NotUsed2();                                  // UnregisterControlChangeNotify
    int NotUsed3();                                  // GetChannelCount
    int NotUsed4();                                  // SetMasterVolumeLevel
    int NotUsed5();                                  // SetMasterVolumeLevelScalar
    int NotUsed6();                                  // GetMasterVolumeLevel
    int GetMasterVolumeLevelScalar(out float level);
    int NotUsed7();                                  // SetChannelVolumeLevel
    int NotUsed8();                                  // SetChannelVolumeLevelScalar
    int NotUsed9();                                  // GetChannelVolumeLevel
    int NotUsed10();                                 // GetChannelVolumeLevelScalar
    int NotUsed11();                                 // SetMute
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
}

// Captura real del microfono con winmm (waveIn). Hace falta abrir un stream
// de verdad: el medidor WASAPI de un micro da 0 mientras nadie lo tenga
// abierto, asi que sin esto no se puede probar nada antes de una llamada.
// La WAVEHDR vive en memoria no gestionada a proposito: el driver sigue
// escribiendo en ella despues de volver de la llamada.
public class NotedMic {
    [StructLayout(LayoutKind.Sequential)]
    struct WAVEFORMATEX {
        public ushort wFormatTag, nChannels;
        public uint nSamplesPerSec, nAvgBytesPerSec;
        public ushort nBlockAlign, wBitsPerSample, cbSize;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct WAVEHDR {
        public IntPtr lpData;
        public uint dwBufferLength, dwBytesRecorded;
        public IntPtr dwUser;
        public uint dwFlags, dwLoops;
        public IntPtr lpNext, reserved;
    }

    [DllImport("winmm.dll")] static extern int waveInOpen(out IntPtr h, uint dev, ref WAVEFORMATEX f, IntPtr cb, IntPtr inst, uint flags);
    [DllImport("winmm.dll")] static extern int waveInPrepareHeader(IntPtr h, IntPtr hdr, uint cb);
    [DllImport("winmm.dll")] static extern int waveInAddBuffer(IntPtr h, IntPtr hdr, uint cb);
    [DllImport("winmm.dll")] static extern int waveInUnprepareHeader(IntPtr h, IntPtr hdr, uint cb);
    [DllImport("winmm.dll")] static extern int waveInStart(IntPtr h);
    [DllImport("winmm.dll")] static extern int waveInStop(IntPtr h);
    [DllImport("winmm.dll")] static extern int waveInReset(IntPtr h);
    [DllImport("winmm.dll")] static extern int waveInClose(IntPtr h);

    const uint WAVE_MAPPER = 0xFFFFFFFF;
    const uint WHDR_DONE   = 0x00000001;
    const int  RATE        = 16000;   // igual que SAMPLE_RATE del proyecto

    // Devuelve el pico captado (0..1), o -1 si no se pudo abrir el microfono.
    public static float Capture(int ms) {
        var f = new WAVEFORMATEX {
            wFormatTag = 1, nChannels = 1, nSamplesPerSec = RATE,
            nAvgBytesPerSec = RATE * 2, nBlockAlign = 2, wBitsPerSample = 16, cbSize = 0
        };
        IntPtr h;
        if (waveInOpen(out h, WAVE_MAPPER, ref f, IntPtr.Zero, IntPtr.Zero, 0) != 0) return -1f;

        int bytes = RATE * 2 * ms / 1000;
        uint hdrSize = (uint)Marshal.SizeOf(typeof(WAVEHDR));
        IntPtr buf  = Marshal.AllocHGlobal(bytes);
        IntPtr pHdr = Marshal.AllocHGlobal((int)hdrSize);
        try {
            var hdr = new WAVEHDR { lpData = buf, dwBufferLength = (uint)bytes };
            Marshal.StructureToPtr(hdr, pHdr, false);

            if (waveInPrepareHeader(h, pHdr, hdrSize) != 0) return -1f;
            if (waveInAddBuffer(h, pHdr, hdrSize) != 0)     return -1f;
            if (waveInStart(h) != 0)                        return -1f;

            int waited = 0;
            while (waited < ms + 1500) {
                var cur = (WAVEHDR)Marshal.PtrToStructure(pHdr, typeof(WAVEHDR));
                if ((cur.dwFlags & WHDR_DONE) != 0) break;
                System.Threading.Thread.Sleep(50);
                waited += 50;
            }
            waveInStop(h);
            waveInReset(h);

            var done = (WAVEHDR)Marshal.PtrToStructure(pHdr, typeof(WAVEHDR));
            int got = (int)done.dwBytesRecorded;
            if (got <= 0 || got > bytes) got = bytes;

            float peak = 0f;
            for (int i = 0; i + 1 < got; i += 2) {
                short s = (short)(Marshal.ReadByte(buf, i) | (Marshal.ReadByte(buf, i + 1) << 8));
                float a = Math.Abs((int)s) / 32768f;
                if (a > peak) peak = a;
            }
            waveInUnprepareHeader(h, pHdr, hdrSize);
            return peak;
        } catch {
            return -1f;
        } finally {
            waveInClose(h);
            Marshal.FreeHGlobal(buf);
            Marshal.FreeHGlobal(pHdr);
        }
    }
}

public class NotedAudio {
    // flow: 0 = salida (sonido del PC), 1 = entrada (microfono)
    public static bool Read(int flow, out bool muted, out float volume, out float peak) {
        muted = false; volume = 0f; peak = 0f;
        try {
            var en = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
            IMMDevice dev;
            if (en.GetDefaultAudioEndpoint(flow, 1 /* eMultimedia */, out dev) != 0 || dev == null)
                return false;

            object o;
            Guid gMeter = typeof(IAudioMeterInformation).GUID;
            if (dev.Activate(ref gMeter, 1, IntPtr.Zero, out o) == 0 && o != null)
                ((IAudioMeterInformation)o).GetPeakValue(out peak);

            Guid gVol = typeof(IAudioEndpointVolume).GUID;
            if (dev.Activate(ref gVol, 1, IntPtr.Zero, out o) == 0 && o != null) {
                var v = (IAudioEndpointVolume)o;
                v.GetMute(out muted);
                v.GetMasterVolumeLevelScalar(out volume);
            }
            return true;
        } catch { return false; }
    }
}
'@ -ErrorAction SilentlyContinue
}


function Get-AudioRow {
    # Devuelve una fila lista para pintar, con Level 0..1 para la barra.
    param(
        [Parameter(Mandatory = $true)][int]$Flow,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$MissingHint,
        [Parameter(Mandatory = $true)][string]$ZeroHint
    )

    $muted = $false; $vol = [single]0; $peak = [single]0
    $ok = $false
    try { $ok = [NotedAudio]::Read($Flow, [ref]$muted, [ref]$vol, [ref]$peak) } catch { $ok = $false }

    if (-not $ok) {
        return @{ Name=$Label; State=(TX 'st_none'); Detail=$MissingHint; Sev=2; Level=0; HasMeter=$true }
    }
    if ($muted) {
        return @{ Name=$Label; State=(TX 'st_muted'); Detail=(TX 'd_muted'); Sev=2; Level=0; HasMeter=$true }
    }

    $volPct  = [int][math]::Round($vol * 100)
    $peakPct = [int][math]::Round($peak * 100)

    if ($volPct -le $VolWarnPercent) {
        return @{ Name=$Label; State=((TX 'st_volume') -f $volPct); Detail=(TX 'd_vol_low'); Sev=1; Level=$peak; HasMeter=$true }
    }

    $d = (TX 'd_level') -f $volPct, $peakPct
    if ($peakPct -eq 0) { $d = $ZeroHint -f $volPct }
    return @{ Name=$Label; State=(TX 'st_ready'); Detail=$d; Sev=0; Level=$peak; HasMeter=$true }
}


function Format-Uptime([TimeSpan]$ts) {
    if ($ts.TotalMinutes -lt 1)  { return "{0}s" -f [int]$ts.TotalSeconds }
    if ($ts.TotalHours   -lt 1)  { return "{0}m" -f [int]$ts.TotalMinutes }
    if ($ts.TotalDays    -lt 1)  { return "{0}h {1}m" -f [int]$ts.TotalHours, $ts.Minutes }
    return "{0}d {1}h" -f [int]$ts.TotalDays, $ts.Hours
}


function Get-NotedStatus {
    $now  = Get-Date
    $rows = New-Object System.Collections.ArrayList
    $problems = 0   # 2 = no se grabara, 1 = aviso

    # ── Daemon ──
    # El .lock huerfano se cuenta aqui: no es un tema aparte, es la razon por la
    # que el daemon no puede arrancar.
    $daemon = @(Get-TRDaemonProcess -Root $root)
    if ($daemon.Count -eq 0) {
        $detail = TX 'd_daemon_down'
        $lock = Join-Path $root '.lock'
        if (Test-Path $lock) {
            $lockPid = (Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($lockPid) { $lockPid = "$lockPid".Trim() }
            $alive = $false
            if ($lockPid -match '^\d+$') {
                $alive = [bool](Get-Process -Id ([int]$lockPid) -ErrorAction SilentlyContinue)
            }
            if (-not $alive) { $detail = (TX 'd_daemon_locked') -f $lockPid }
        }
        [void]$rows.Add(@{ Name=(TX 'row_daemon'); State=(TX 'st_stopped'); Detail=$detail; Sev=2; HasMeter=$false })
        $problems = 2
    } elseif ($daemon.Count -gt 1) {
        $pids = ($daemon | ForEach-Object { $_.ProcessId }) -join ', '
        [void]$rows.Add(@{ Name=(TX 'row_daemon'); State=(TX 'st_dup'); Detail=((TX 'd_daemon_dup') -f $daemon.Count, $pids); Sev=2; HasMeter=$false })
        $problems = 2
    } else {
        $up = Format-Uptime ($now - $daemon[0].CreationDate)
        if (($now - $daemon[0].CreationDate).TotalHours -ge $UptimeWarnHours) {
            [void]$rows.Add(@{ Name=(TX 'row_daemon'); State=(TX 'st_old'); Detail=((TX 'd_uptime_old') -f $up); Sev=1; HasMeter=$false })
            if ($problems -lt 1) { $problems = 1 }
        } else {
            [void]$rows.Add(@{ Name=(TX 'row_daemon'); State=(TX 'st_rec_ok'); Detail=((TX 'd_uptime') -f $up); Sev=0; HasMeter=$false })
        }
    }

    # ── Watchdog ──
    $wd = @(Get-TRWatchdogProcess -Root $root)
    if ($wd.Count -eq 0) {
        [void]$rows.Add(@{ Name=(TX 'row_watchdog'); State=(TX 'st_stopped'); Detail=(TX 'd_wd_down'); Sev=1; HasMeter=$false })
        if ($problems -lt 1) { $problems = 1 }
    } else {
        $wdUp = Format-Uptime ($now - $wd[0].CreationDate)
        [void]$rows.Add(@{ Name=(TX 'row_watchdog'); State=(TX 'st_watching'); Detail=((TX 'd_wd_ok') -f $wdUp); Sev=0; HasMeter=$false })
    }

    # ── Audio ──
    # Nota: el medidor de un microfono solo da nivel cuando algo lo tiene
    # abierto (Teams, Noted). En reposo marca 0 aunque el micro este perfecto,
    # asi que el texto lo dice en vez de dar a entender que esta mudo.
    $mic = Get-AudioRow -Flow 1 -Label (TX 'row_mic') `
        -MissingHint (TX 'd_mic_missing') -ZeroHint (TX 'd_zero_mic')
    $spk = Get-AudioRow -Flow 0 -Label (TX 'row_spk') `
        -MissingHint (TX 'd_spk_missing') -ZeroHint (TX 'd_zero_spk')
    [void]$rows.Add($mic)
    [void]$rows.Add($spk)
    foreach ($a in @($mic, $spk)) {
        if ($a.Sev -eq 2) { $problems = 2 }
        elseif ($a.Sev -eq 1 -and $problems -lt 1) { $problems = 1 }
    }

    # ── Grabacion en curso ──
    $recording = $false
    $sentinel = Join-Path $root '.recording_active'
    if (Test-Path $sentinel) {
        $sPid = (Get-Content $sentinel -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($sPid) { $sPid = "$sPid".Trim() }
        if ($sPid -match '^\d+$' -and (Get-Process -Id ([int]$sPid) -ErrorAction SilentlyContinue)) {
            $recording = $true
        }
    }
    if (-not $recording) {
        try { $recording = Test-TRRecording -Root $root } catch { }
    }

    # El veredicto se dice con palabras: la frase basta para decidir sin
    # mirar ningun color.
    if ($recording) {
        $verdict = @{ Text=(TX 'verdict_rec_title');  Sev=3; Hint=(TX 'verdict_rec_hint') }
    } elseif ($problems -ge 2) {
        $verdict = @{ Text=(TX 'verdict_bad_title');  Sev=2; Hint=(TX 'verdict_bad_hint') }
    } elseif ($problems -eq 1) {
        $verdict = @{ Text=(TX 'verdict_warn_title'); Sev=1; Hint=(TX 'verdict_warn_hint') }
    } else {
        $verdict = @{ Text=(TX 'verdict_ok_title');   Sev=0; Hint=(TX 'verdict_ok_hint') }
    }

    return @{ Rows=$rows; Verdict=$verdict; Recording=$recording; Problems=$problems
              DaemonCount=$daemon.Count; WatchdogCount=$wd.Count }
}


# ── Modo texto ───────────────────────────────────────────────────────────────
if ($Check) {
    $st = Get-NotedStatus
    Write-Host ""
    Write-Host "  $($st.Verdict.Text) - $($st.Verdict.Hint)"
    Write-Host ""
    foreach ($r in $st.Rows) {
        "  {0,-7} {1,-15} {2,-14} {3}" -f "[$($SevWord[$r.Sev])]", $r.Name, $r.State, $r.Detail
    }
    Write-Host ""
    exit $st.Problems
}


# ── Ventana ──────────────────────────────────────────────────────────────────
$form                 = New-Object System.Windows.Forms.Form
$form.Text            = TX 'title'
$form.Size            = New-Object System.Drawing.Size(560, 540)
$form.StartPosition   = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox     = $false
$form.MinimizeBox     = $false
$form.TopMost         = $true
$form.BackColor       = HC $C.Bg
$form.Font            = New-Object System.Drawing.Font('Segoe UI', 9)

# Veredicto: bloque RELLENO con texto oscuro encima. Un pastel sobre fondo
# oscuro no se lee; relleno sobre texto oscuro si.
$banner           = New-Object System.Windows.Forms.Label
$banner.Location  = New-Object System.Drawing.Point(16, 16)
$banner.Size      = New-Object System.Drawing.Size(512, 52)
$banner.TextAlign = 'MiddleCenter'
$banner.Font      = New-Object System.Drawing.Font('Segoe UI', 15, [System.Drawing.FontStyle]::Bold)
$banner.BackColor = HC $C.Card
$banner.ForeColor = HC $C.Title
$form.Controls.Add($banner)

$hint           = New-Object System.Windows.Forms.Label
$hint.Location  = New-Object System.Drawing.Point(16, 74)
$hint.Size      = New-Object System.Drawing.Size(512, 20)
$hint.TextAlign = 'MiddleCenter'
$hint.Font      = New-Object System.Drawing.Font('Segoe UI', 10)
$hint.ForeColor = HC $C.Title
$form.Controls.Add($hint)

# Cada fila lleva una INSIGNIA con la palabra OK / AVISO / FALLO. Manda la
# palabra; el color solo refuerza.
# Todas las etiquetas nacen ya con su ForeColor: si el repintado fallara,
# el texto seguiria leyendose en vez de quedarse en negro sobre fondo oscuro.
$rowCtrls = @()
for ($i = 0; $i -lt 4; $i++) {
    $y = 106 + ($i * 56)

    $badge           = New-Object System.Windows.Forms.Label
    $badge.Location  = New-Object System.Drawing.Point(20, ($y + 1))
    $badge.Size      = New-Object System.Drawing.Size(74, 24)
    $badge.TextAlign = 'MiddleCenter'
    $badge.Font      = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold)
    $badge.ForeColor = HC $C.Bg
    $badge.BackColor = HC $C.Muted
    $form.Controls.Add($badge)

    $name           = New-Object System.Windows.Forms.Label
    $name.Location  = New-Object System.Drawing.Point(108, $y)
    $name.Size      = New-Object System.Drawing.Size(128, 24)
    $name.Font      = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
    $name.ForeColor = HC $C.Title
    $form.Controls.Add($name)

    $state           = New-Object System.Windows.Forms.Label
    $state.Location  = New-Object System.Drawing.Point(240, $y)
    $state.Size      = New-Object System.Drawing.Size(140, 24)
    $state.Font      = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
    $state.ForeColor = HC $C.Title
    $form.Controls.Add($state)

    # Barra de nivel en vivo (solo micro y sonido del PC)
    $meter           = New-Object System.Windows.Forms.Panel
    $meter.Location  = New-Object System.Drawing.Point(388, ($y + 7))
    $meter.Size      = New-Object System.Drawing.Size(140, 12)
    $meter.BackColor = HC $C.Card
    $form.Controls.Add($meter)

    $fill            = New-Object System.Windows.Forms.Panel
    $fill.Location   = New-Object System.Drawing.Point(0, 0)
    $fill.Size       = New-Object System.Drawing.Size(0, 12)
    $fill.BackColor  = HC $C.Green
    $meter.Controls.Add($fill)

    $detail           = New-Object System.Windows.Forms.Label
    $detail.Location  = New-Object System.Drawing.Point(108, ($y + 25))
    $detail.Size      = New-Object System.Drawing.Size(420, 18)
    $detail.ForeColor = HC $C.Sub
    $form.Controls.Add($detail)

    $rowCtrls += ,@{ Badge=$badge; Name=$name; State=$state; Detail=$detail; Meter=$meter; Fill=$fill }
}

# Prueba activa: suena un tono y se graba el micro a la vez.
$btnTest              = New-Object System.Windows.Forms.Button
$btnTest.Location     = New-Object System.Drawing.Point(16, 344)
$btnTest.Size         = New-Object System.Drawing.Size(512, 38)
$btnTest.FlatStyle    = 'Flat'
$btnTest.Font         = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$btnTest.Text         = TX 'btn_test'
$btnTest.BackColor    = HC $C.Green
$btnTest.ForeColor    = HC $C.Bg
$btnTest.FlatAppearance.BorderSize = 0
$btnTest.Cursor       = 'Hand'
$form.Controls.Add($btnTest)

$btnAction              = New-Object System.Windows.Forms.Button
$btnAction.Location     = New-Object System.Drawing.Point(16, 392)
$btnAction.Size         = New-Object System.Drawing.Size(252, 40)
$btnAction.FlatStyle    = 'Flat'
$btnAction.Font         = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$btnAction.BackColor    = HC $C.Blue
$btnAction.ForeColor    = HC $C.Bg
$btnAction.FlatAppearance.BorderSize = 0
$btnAction.Cursor       = 'Hand'
$form.Controls.Add($btnAction)

$btnClose           = New-Object System.Windows.Forms.Button
$btnClose.Location  = New-Object System.Drawing.Point(280, 392)
$btnClose.Size      = New-Object System.Drawing.Size(248, 40)
$btnClose.FlatStyle = 'Flat'
$btnClose.Text      = TX 'btn_close'
$btnClose.BackColor = HC $C.Btn
$btnClose.ForeColor = HC $C.Title
$btnClose.FlatAppearance.BorderSize = 0
$btnClose.Cursor    = 'Hand'
$btnClose.Add_Click({ $form.Close() })
$form.Controls.Add($btnClose)

$footer           = New-Object System.Windows.Forms.Label
$footer.Location  = New-Object System.Drawing.Point(16, 442)
$footer.Size      = New-Object System.Drawing.Size(512, 16)
$footer.ForeColor = HC $C.Muted
$footer.Font      = New-Object System.Drawing.Font('Segoe UI', 8)
$footer.TextAlign = 'MiddleCenter'
$footer.Text      = TX 'footer'
$form.Controls.Add($footer)

$script:PendingAction = 'restart'
$script:MeterRows     = @()   # indices de fila que llevan barra
$script:TestMic       = $null # resultado de la ultima prueba activa
$script:TestSpk       = $null


function Format-Pct([double]$v) {
    $p = $v * 100
    if ($p -ge 10) { return "{0:N0}%" -f $p }
    if ($p -ge 1)  { return "{0:N1}%" -f $p }
    return "{0:N2}%" -f $p
}


function Invoke-SoundTest {
    # Prueba activa en dos tiempos:
    #   1. suena un tono y se mira si la salida da senal
    #   2. se abre el micro de verdad y se graba
    # Se hace en dos tiempos y no a la vez para que un resultado no contamine
    # al otro (con altavoces, el micro oiria el tono y pareceria que hablas).
    $btnTest.Enabled = $false
    $btnAction.Enabled = $false
    $fast.Stop()
    $script:TestMic = $null
    $script:TestSpk = $null

    $player = $null
    try {
        $player = New-Object System.Media.SoundPlayer 'C:\Windows\Media\Alarm01.wav'
        $player.PlayLooping()
    } catch { $player = $null }

    $btnTest.Text = TX 'btn_test_playing'
    [System.Windows.Forms.Application]::DoEvents()

    $spkPeak = 0.0
    $muted = $false; $vol = [single]0; $peak = [single]0
    $spkCtrl = $rowCtrls[3]
    for ($i = 0; $i -lt 8; $i++) {
        Start-Sleep -Milliseconds 120
        try {
            if ([NotedAudio]::Read(0, [ref]$muted, [ref]$vol, [ref]$peak)) {
                if ($peak -gt $spkPeak) { $spkPeak = $peak }
                $spkCtrl.Fill.Width = [int]([math]::Min(1.0, [double]$peak) * $spkCtrl.Meter.Width)
            }
        } catch { }
        [System.Windows.Forms.Application]::DoEvents()
    }

    if ($player) { try { $player.Stop() } catch { } }

    # Micro: en trozos, para que la barra se mueva mientras grabas.
    $btnTest.Text = TX 'btn_test_speak'
    [System.Windows.Forms.Application]::DoEvents()

    $micPeak = -1.0
    $micCtrl = $rowCtrls[2]
    for ($i = 0; $i -lt 3; $i++) {
        $chunk = -1.0
        try { $chunk = [NotedMic]::Capture(700) } catch { $chunk = -1.0 }
        if ($chunk -ge 0) {
            if ($chunk -gt $micPeak) { $micPeak = $chunk }
            $micCtrl.Fill.Width = [int]([math]::Min(1.0, [double]$chunk) * $micCtrl.Meter.Width)
        }
        [System.Windows.Forms.Application]::DoEvents()
    }

    # ── Lectura de resultados ──
    if ($spkPeak -le 0.005) {
        $script:TestSpk = @{ Sev=2; Text=(TX 'test_spk_bad') }
    } else {
        $script:TestSpk = @{ Sev=0; Text=((TX 'test_spk_ok') -f (Format-Pct $spkPeak)) }
    }

    # Importante: el silencio NO prueba que el micro este roto. Un micro sano
    # en una sala callada da casi cero igual que uno averiado, asi que solo se
    # declara fallo cuando Windows ni siquiera deja abrir el dispositivo.
    if ($micPeak -lt 0) {
        $script:TestMic = @{ Sev=2; Text=(TX 'test_mic_cant') }
    } elseif ($micPeak -ge 0.02) {
        $script:TestMic = @{ Sev=0; Text=((TX 'test_mic_ok') -f (Format-Pct $micPeak)) }
    } else {
        $script:TestMic = @{ Sev=1; Text=((TX 'test_mic_low') -f (Format-Pct $micPeak)) }
    }

    $btnTest.Text    = TX 'btn_test_again'
    $btnTest.Enabled = $true
    $fast.Start()
    Update-Ui
}


function Update-Ui {
    $st = Get-NotedStatus

    $banner.Text      = $st.Verdict.Text
    if ($st.Verdict.Sev -eq 3) {
        $banner.BackColor = HC $C.Blue
    } else {
        $banner.BackColor = $SevColor[$st.Verdict.Sev]
    }
    $banner.ForeColor = HC $C.Bg
    $hint.Text        = $st.Verdict.Hint

    $meterRows = @()
    for ($i = 0; $i -lt $rowCtrls.Count; $i++) {
        $c = $rowCtrls[$i]
        if ($i -ge $st.Rows.Count) { continue }
        $r = $st.Rows[$i]

        $c.Badge.Text      = $SevWord[$r.Sev]
        $c.Badge.BackColor = $SevColor[$r.Sev]
        $c.State.ForeColor = $SevColor[$r.Sev]
        $c.Name.Text       = $r.Name
        $c.State.Text      = $r.State
        $c.Detail.Text     = $r.Detail

        # Si se hizo una prueba activa, su resultado manda sobre el estado
        # pasivo: es informacion mas fuerte que mirar la configuracion.
        $ov = $null
        if ($i -eq 2)     { $ov = $script:TestMic }
        elseif ($i -eq 3) { $ov = $script:TestSpk }
        if ($ov) {
            $c.Detail.Text = $ov.Text
            if ($ov.Sev -gt $r.Sev) {
                $c.Badge.Text      = $SevWord[$ov.Sev]
                $c.Badge.BackColor = $SevColor[$ov.Sev]
                $c.State.ForeColor = $SevColor[$ov.Sev]
            }
        }

        if ($r.HasMeter) {
            $c.Meter.Visible = $true
            $c.Fill.Width    = [int]([math]::Min(1.0, [double]$r.Level) * $c.Meter.Width)
            $c.Fill.BackColor = $SevColor[$r.Sev]
            $meterRows += $i
        } else {
            $c.Meter.Visible = $false
        }
    }
    $script:MeterRows = $meterRows

    if ($st.Recording) {
        $script:PendingAction = 'none'
        $btnAction.Text    = TX 'btn_recording'
        $btnAction.Enabled = $false
    } elseif ($st.DaemonCount -eq 0) {
        $script:PendingAction = 'start'
        $btnAction.Text    = TX 'btn_start'
        $btnAction.Enabled = $true
    } elseif ($st.WatchdogCount -eq 0) {
        $script:PendingAction = 'start'
        $btnAction.Text    = TX 'btn_start_wd'
        $btnAction.Enabled = $true
    } else {
        $script:PendingAction = 'restart'
        $btnAction.Text    = TX 'btn_restart'
        $btnAction.Enabled = $true
    }
}


function Update-Meters {
    # Solo el nivel de audio. Va aparte y mucho mas rapido que Update-Ui
    # porque consultar procesos con CIM es caro y no hace falta cada 300 ms.
    foreach ($i in $script:MeterRows) {
        $flow = if ($i -eq 2) { 1 } else { 0 }
        $muted = $false; $vol = [single]0; $peak = [single]0
        try {
            if ([NotedAudio]::Read($flow, [ref]$muted, [ref]$vol, [ref]$peak)) {
                $c = $rowCtrls[$i]
                $c.Fill.Width = [int]([math]::Min(1.0, [double]$peak) * $c.Meter.Width)
            }
        } catch { }
    }
}


$btnTest.Add_Click({ Invoke-SoundTest })

$btnAction.Add_Click({
    $btnAction.Enabled = $false
    try {
        if ($script:PendingAction -eq 'start') {
            # El watchdog es el unico que debe lanzar main.py (ver tr_env.ps1).
            Start-TRDaemon -Root $root -TimeoutSeconds 2 | Out-Null
        } elseif ($script:PendingAction -eq 'restart') {
            $rs = Join-Path $root 'scripts\restart_noted.ps1'
            Start-Process -FilePath 'powershell.exe' `
                -ArgumentList @('-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $rs) `
                -WorkingDirectory $root | Out-Null
        }
    } catch {
        [System.Windows.Forms.MessageBox]::Show(((TX 'err_action') -f $_), (TX 'title')) | Out-Null
    }
    Start-Sleep -Milliseconds 1200
    Update-Ui
})

$slow = New-Object System.Windows.Forms.Timer
$slow.Interval = 3000
$slow.Add_Tick({ Update-Ui })
$slow.Start()

$fast = New-Object System.Windows.Forms.Timer
$fast.Interval = 300
$fast.Add_Tick({ Update-Meters })
$fast.Start()

Update-Ui
[void]$form.ShowDialog()
$slow.Stop(); $slow.Dispose()
$fast.Stop(); $fast.Dispose()
