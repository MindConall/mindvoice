<#
.SYNOPSIS
    Construye el instalador de MindVoice para Windows.

.DESCRIPTION
    Prepara un árbol de publicación autocontenido y lo compila con Inno Setup
    en un único .exe:

      1. Descarga el runtime oficial de Python *embebido* (x64), que no trae
         Tcl/Tk ni un instalador: unos 10 MB en vez de los ~60 MB de un Python
         completo. El runtime queda congelado dentro de la app.
      2. Arranca pip dentro de ese runtime e instala requirements.txt, así que
         el usuario final no necesita Python ni pip ni conexión.
      3. Copia el código de la app a staging\app y el runtime ya preparado a
         staging\runtime.
      4. Ajusta pythonXY._pth para que cargue site-packages y la carpeta de la
         app (sin esto, el runtime embebido no encuentra ni PyQt6 ni los
         módulos propios).
      5. Compila staging\ con Inno Setup.

    El resultado NO lleva la clave de Gemini ni ningún dato del usuario: el
    .gitignore impide que secrets.json, memory*.json o *.log entren en el
    paquete, y este script lo comprueba explícitamente antes de empaquetar.

.PARAMETER SkipInno
    Prepara staging\ pero no compila el .exe. Útil para inspeccionar el árbol o
    para cuando Inno Setup no está instalado.

.PARAMETER PythonVersion
    Versión del runtime embebido a empaquetar.

.EXAMPLE
    .\build_release.ps1
    .\build_release.ps1 -SkipInno
#>
[CmdletBinding()]
param(
    [switch]$SkipInno,
    [string]$PythonVersion = '3.13.7'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Staging   = Join-Path $Root 'staging'
$Runtime   = Join-Path $Staging 'runtime'
$AppOut    = Join-Path $Staging 'app'
$Downloads = Join-Path $env:TEMP 'mindvoice-build'
$PyTag     = ($PythonVersion -replace '(\d+\.\d+).*', '$1')   # 3.13.7 -> 3.13
$PyDll     = 'python' + ($PyTag -replace '\.', '')           # 3.13   -> python313

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-File($Url, $Destination) {
    Write-Host "    descargando $Url"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
}

# ---------------------------------------------------------------------------
# 1. Runtime de Python embebido
# ---------------------------------------------------------------------------
function Install-EmbeddedPython {
    $zip = Join-Path $Downloads "python-$PythonVersion-embed-amd64.zip"
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"

    if (-not (Test-Path $zip)) {
        New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
        Get-File $url $zip
    }

    Write-Step "Extrayendo el runtime embebido $PythonVersion"
    if (Test-Path $Runtime) { Remove-Item -Recurse -Force $Runtime }
    New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
    Expand-Archive -Path $zip -DestinationPath $Runtime -Force
}

# ---------------------------------------------------------------------------
# 2. pip dentro del runtime embebido
# ---------------------------------------------------------------------------
function Initialize-Pip {
    $getPip = Join-Path $Downloads 'get-pip.py'
    if (-not (Test-Path $getPip)) {
        Get-File 'https://bootstrap.pypa.io/get-pip.py' $getPip
    }

    # Un ._pth sin "import site" ignora site-packages: hay que habilitarlo
    # ANTES de que pip pueda instalarse en el sitio correcto.
    Enable-SiteImports

    Write-Step "Instalando pip en el runtime embebido"
    & (Join-Path $Runtime 'python.exe') $getPip --no-warn-script-location `
        --disable-pip-version-check | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "get-pip falló (código $LASTEXITCODE)" }
}

function Enable-SiteImports {
    $pth = Get-ChildItem -Path $Runtime -Filter 'python*._pth' | Select-Object -First 1
    if (-not $pth) { throw "No se encontró el ._pth del runtime embebido" }

    $lines = Get-Content $pth.FullName
    $wanted = @(
        "$PyDll.zip",
        '.',
        'Lib\site-packages',
        'DLLs',
        '..\app',   # el código de la app vive un nivel arriba
        'import site'
    )
    Set-Content -Path $pth.FullName -Value $wanted -Encoding ASCII
    Write-Host "    $($pth.Name) -> $($wanted -join ' | ')"
}

# ---------------------------------------------------------------------------
# 3. Dependencias
# ---------------------------------------------------------------------------
function Install-Requirements {
    Write-Step "Instalando dependencias de la app"
    Push-Location $Root
    try {
        & (Join-Path $Runtime 'python.exe') -m pip install --no-warn-script-location `
            --disable-pip-version-check --no-cache-dir -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "pip install falló (código $LASTEXITCODE)" }
    }
    finally { Pop-Location }
}

# ---------------------------------------------------------------------------
# 4. Código de la app
# ---------------------------------------------------------------------------
# 'gui.py' queda deliberadamente fuera del paquete: es la GUI antigua de
# tkinter y el runtime embebido de Python no incluye tkinter (no viene en el
# paquete oficial), así que lanzarla desde la instalación daba
# ModuleNotFoundError. La interfaz real de MindVoice es el overlay (PyQt6), que
# ya es dependencia obligatoria. En el repositorio sí se mantiene, para quien
# clone y trabaje con un Python completo.
$AppSourceFiles = @(
    'MindVoice.py', 'main.py', 'overlay.py', 'launcher.py',
    'start_mindvoice.py', 'live_assistant.py', 'config.py', 'prefs.py',
    'credenciales.py', 'firstrun.py', 'hotkeys.py', 'branding.py', 'rutas.py',
    # Fases 0-5: la instrumentación, el sistema de diseño/animaciones/acento y
    # la memoria viven en módulos y paquetes aparte. Si se olvidan aquí, el
    # paquete compila pero la app instalada muere con "No module named 'ui'".
    'perf_instr.py'
)

# Paquetes propios que hay que copiar ENTEROS y conservando su carpeta.
$AppPackages = @('media', 'ui', 'memory')

function Copy-AppSource {
    Write-Step "Copiando el código de la app"

    if (Test-Path $AppOut) { Remove-Item -Recurse -Force $AppOut }
    New-Item -ItemType Directory -Force -Path $AppOut | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $AppOut 'media') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $AppOut 'assets') | Out-Null

    foreach ($name in $AppSourceFiles) {
        $source = Join-Path $Root $name
        if (-not (Test-Path $source)) { throw "Falta el módulo $name" }
        Copy-Item $source -Destination (Join-Path $AppOut $name)
    }

    # Los paquetes propios ('media', 'ui', 'memory') NO son módulos sueltos: sus
    # archivos tienen que ir dentro de su carpeta, no aplanados en la raíz. Si
    # se copian en la raíz, Python encuentra la carpeta vacía como paquete de
    # espacio de nombres y falla ("cannot import name 'AudioPlayer' from
    # 'media'", "No module named 'ui'").
    foreach ($pkg in $AppPackages) {
        $pkgSource = Join-Path $Root $pkg
        if (-not (Test-Path $pkgSource)) { throw "Falta el paquete $pkg" }
        $pkgOut = Join-Path $AppOut $pkg
        New-Item -ItemType Directory -Force -Path $pkgOut | Out-Null
        Get-ChildItem -Path $pkgSource -Recurse -File -Filter '*.py' | ForEach-Object {
            $rel = $_.FullName.Substring($pkgSource.Length).TrimStart('\')
            $dest = Join-Path $pkgOut $rel
            New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
            Copy-Item $_.FullName -Destination $dest
        }
    }

    # Ojo: -Include solo filtra bien si la ruta termina en \* o si se usa
    # -Recurse. Con la ruta pelada devolvía una lista vacía y el paquete se
    # quedaba sin icono.
    $assetsOut = Join-Path $AppOut 'assets'
    Get-ChildItem -Path (Join-Path $Root 'assets') -File |
        Where-Object { $_.Extension -in '.png', '.ico' } |
        ForEach-Object { Copy-Item $_.FullName -Destination $assetsOut }

    foreach ($doc in 'LICENSE', 'README.md', 'requirements.txt') {
        if (Test-Path (Join-Path $Root $doc)) {
            Copy-Item (Join-Path $Root $doc) -Destination $AppOut
        }
    }

    # Bytecode compilado: no hace falta en el paquete, ocupa sitio y es código
    # duplicado. Puede haberlo generado una ejecución de prueba previa.
    Get-ChildItem -Path $AppOut -Recurse -Directory -Filter '__pycache__' |
        ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
}

<#
    Barrido de seguridad antes de empaquetar. El .gitignore protege el repo,
    pero un .exe se puede desempacar y leer: si un secrets.json, un memory.json
    o un registro<User> acabó en staging/, se convertiría en un archivo que
    cualquiera puede abrir. Mejor fallar aquí y loudly que publicar una clave.
#>
function Assert-NoSecrets {
    Write-Step "Comprobando que el paquete no lleva secretos ni datos personales"

    $prohibidos = @(
        'secrets.json', 'user_prefs.json', 'memory.json', 'memory-long.json',
        'timers.json', 'web_model_cache.json', 'mindvoice_codigo_completo.py'
    )
    $encontrados = @()

    foreach ($patron in $prohibidos) {
        $encontrados += Get-ChildItem -Path $Staging -Recurse -File -Filter $patron -EA SilentlyContinue
    }
    $encontrados += Get-ChildItem -Path $Staging -Recurse -File -Filter '*.log' -EA SilentlyContinue
    $encontrados += Get-ChildItem -Path $Staging -Recurse -File -Filter 'guion-conversacion-*.txt' -EA SilentlyContinue

    if ($encontrados.Count -gt 0) {
        $encontrados | ForEach-Object { Write-Host "    ¡ENCONTRADO! $($_.FullName)" -ForegroundColor Red }
        throw "El paquete contiene datos que no deben distribuirse. Corregido el origen y vuelve a construir."
    }

    # El bytecode compilado no debe viajar en el instalador.
    $pycache = Get-ChildItem -Path $AppOut -Recurse -Directory -Filter '__pycache__' -EA SilentlyContinue
    if ($pycache) {
        $pycache | ForEach-Object { Write-Host "    ¡BYTECODE! $($_.FullName)" -ForegroundColor Red }
        throw "Hay __pycache__ dentro del paquete."
    }

    # Segunda pasada: el patrón de una clave de Gemini en cualquier .py.
    $sospechosos = Get-ChildItem -Path $AppOut -Recurse -File -Include '*.py', '*.json', '*.md' -EA SilentlyContinue |
        Select-String -Pattern 'AIza[0-9A-Za-z_-]{35}','(?i)serper[_-]?api[_-]?key["'']?\s*[:=]\s*["'']?[0-9a-f]{32,}' -EA SilentlyContinue
    if ($sospechosos) {
        $sospechosos | Select-Object -First 5 | ForEach-Object {
            Write-Host "    ¡CLAVE EN CLARO! $($_.Filename):$($_.LineNumber)" -ForegroundColor Red
        }
        throw "Se ha detectado una clave con forma de API key dentro del paquete."
    }

    Write-Host "    limpio: sin claves, sin memoria, sin registros" -ForegroundColor Green
}

function Get-AppVersion {
    $file = Join-Path $Root 'installer\version.txt'
    if (Test-Path $file) { return (Get-Content $file -Raw).Trim() }
    throw "Falta installer\version.txt: la versión no se puede adivinar."
}

function Invoke-InnoSetup {
    Write-Step 'Compilando el instalador con Inno Setup'

    # El orden importa: primero lo que esté en el PATH, luego las rutas
    # estándar. Inno Setup se instala por usuario en %LOCALAPPDATA%\Programs
    # (con winget, sin admin) y a máquina en Program Files, y según quién lo
    # instale solo existe una de las dos.
    $candidates = @(
        (Get-Command 'ISCC.exe' -EA SilentlyContinue | Select-Object -Expand Source),
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

    $iscc = $candidates | Select-Object -First 1
    if (-not $iscc) {
        Write-Warning 'Inno Setup (ISCC.exe) no está instalado.'
        Write-Warning 'Instálalo con:  winget install --id JRSoftware.InnoSetup -e'
        Write-Warning 'O usa -SkipInno y compila en CI, que ya lo trae instalado.'
        return $false
    }

    & $iscc "/DAppVersion=$(Get-AppVersion)" "/DSourceRoot=$Staging" `
        (Join-Path $Root 'installer\MindVoice.iss')
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup falló (código $LASTEXITCODE)" }

    $out = Join-Path $Root 'installer\output'
    Get-ChildItem $out -Filter '*.exe' | ForEach-Object {
        Write-Host ("    {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) -ForegroundColor Green
    }
    return $true
}

# ---------------------------------------------------------------------------
# 5. Prueba de humo del árbol empaquetado
# ---------------------------------------------------------------------------
<#
    Importa TODOS los módulos con el runtime embebido, simulando cómo arranca
    de verdad la app instalada.

    Esto no es opcional ni decorativo: el runtime de Python embebido no añade
    la carpeta del script a sys.path (manda pythonXY._pth), así que un simple
    desajuste entre lo que copia Inno y lo que espera el ._pth rompe el arranque
    entero con un "ModuleNotFoundError: No module named 'config'" que en el
    usuario final solo se ve como "no pasa nada al hacer doble clic". Es un
    fallo silencioso, y la única defensa es probarlo antes de publicar.
#>
function Test-Package {
    Write-Step 'Probando el árbol empaquetado con el runtime embebido'

    $probe = Join-Path $env:TEMP 'mindvoice-smoke.py'
    @'
import sys, os
APP = os.environ["MV_APP"]
RUNTIME = os.environ["MV_RUNTIME"]
sys.path.insert(0, APP)
# Reproduce el arranque real: pythonw.exe <app>\MindVoice.py
import MindVoice, start_mindvoice, launcher, overlay, live_assistant
import config, prefs, credenciales, firstrun, rutas, hotkeys, branding
import perf_instr
from media import AudioPlayer, MicrophoneCapture, ScreenCapture
from ui import tokens, animations, accento
from memory import base, flat_backend, graph_backend, migrate, work_memory
import google.genai, mss, pyaudio, keyboard
from PyQt6 import QtWidgets

# Ojo: rutas.app_dir() y python_exe() devuelven pathlib.Path, y un Path nunca
# es igual a un str aunque apunten al mismo sitio. Hay que convertir con str().
assert str(MindVoice.APP_DIR) == APP, (MindVoice.APP_DIR, APP)
assert str(rutas.app_dir()) == APP, (rutas.app_dir(), APP)
# El intérprete que lanzará los hijos debe ser el runtime embebido, no un
# .venv de desarrollo: si no lo es, el lanzador arranca con una ruta ajena.
assert str(rutas.python_exe(True)) == os.path.join(RUNTIME, "pythonw.exe"), rutas.python_exe(True)
assert os.path.exists(branding.LOGO_ICO), "falta el icono"
assert credenciales.secrets_path().name == "secrets.json"
print("SMOKE OK")
'@ | Set-Content -Path $probe -Encoding UTF8

    try {
        $env:MV_APP = $AppOut
        $env:MV_RUNTIME = $Runtime
        # stderr va a fichero: con 2>&1 sobre un comando nativo, PowerShell lo
        # envuelve en NativeCommandError y el traceback de Python queda
        # ilegible justo cuando más falta hace.
        $errFile = "$probe.err"
        $salida = & (Join-Path $Runtime 'python.exe') $probe 2> $errFile
        $codigo = $LASTEXITCODE
        $err = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { '' }
        if ($codigo -ne 0 -or ($salida -notmatch 'SMOKE OK')) {
            Write-Host $err -ForegroundColor Red
            throw "El paquete no arranca: la prueba de humo ha fallado (codigo $codigo). NO publiques esto."
        }
        Write-Host '    SMOKE OK: todos los módulos importan y las rutas resuelven' -ForegroundColor Green
    }
    finally {
        Remove-Item $probe, "$probe.err" -EA SilentlyContinue
        Remove-Item Env:MV_APP, Env:MV_RUNTIME -EA SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------
Write-Host "Construyendo MindVoice $(Get-AppVersion)" -ForegroundColor Green

Install-EmbeddedPython
Initialize-Pip
Install-Requirements
Enable-SiteImports          # de nuevo: pip pudo reescribir el ._pth
Copy-AppSource
Assert-NoSecrets
Test-Package

Write-Host ''
Write-Host "    staging listo en $Staging" -ForegroundColor Green

if (-not $SkipInno) {
    if (Invoke-InnoSetup) {
        Write-Host ''
        Write-Host 'Instalador listo en installer\output' -ForegroundColor Green
    }
}
