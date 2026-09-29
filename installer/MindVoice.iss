; ---------------------------------------------------------------------------
;  MindVoice - instalador de Windows
;
;  Instalación POR USUARIO a propósito: sin UAC, sin permisos de administrador
;  y sin "este programa bloqueó un acceso a la carpeta de archivos de
;  programas" en el antivirus. El instalador completo pesa bastante y casi
;  ningún usuario corriente tiene cuenta de administrador, así que un
;  instalador por usuario es lo que de verdad hace que sea "doble clic y listo".
;
;  La app no necesita escribir en su carpeta de instalación: memoria,
;  preferencias, credenciales, registros y transcripciones viven en
;  {localappdata}\MindVoice (ver rutas.py). Eso permite instalar en un sitio
;  de solo lectura si algún día se cambia a Program Files.
;
;  Compilar:  iscc installer\MindVoice.iss  (o build_release.ps1 -SkipInno)
; ---------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceRoot
  #define SourceRoot "..\staging"
#endif

#define AppName        "MindVoice"
#define AppPublisher   "MindConall"
#define AppExeName     "pythonw.exe"

[Setup]
AppId={{8E3C7A41-5B29-4D6E-9F10-2C7B4A9D3E52}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppComments=Asistente de escritorio que ve tu pantalla y responde con voz (Gemini Live).
AppSupportURL=https://github.com/MindConall/mindvoice
AppUpdatesURL=https://github.com/MindConall/mindvoice/releases

; lowest = instalación por usuario, sin UAC.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\installer\output
OutputBaseFilename=MindVoice-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; Firma digital: si el proyecto publica un certificado, descomentar y definir
; SignTool por línea de comandos. Sin firmar, SmartScreen mostrará un aviso la
; primera vez; eso es normal en proyectos open source sin presupuesto de OV.
; SignedUninstaller=no
SetupIconFile=..\assets\mindvoice_logo.ico
UninstallDisplayIcon={app}\{#AppExeName}
; Solo x64: el runtime embebido y las dependencias son de 64 bits.
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "spanish"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Acceso directo en el escritorio.
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; \
    GroupDescription: "Accesos directos:"; Flags: checkedonce

; Autoinicio. Desmarcado a propósito: si arrancara en el inicio de sesión sin
; clave configurada, el diálogo de primera ejecución saltaría encima del
; escritorio antes de que el usuario haya abierto la app siquiera. Quien quiera
; el atajo global Ctrl+Shift+Z siempre activo lo marca aquí.
Name: "autostart"; Description: "Iniciar MindVoice al iniciar sesión (para que el atajo global esté siempre activo)"; \
    GroupDescription: "Inicio automático:"; Flags: unchecked

[Files]
; Runtime de Python con las dependencias ya instaladas (lo genera
; build_release.ps1). Esta es la parte heavyweight del instalador.
Source: "{#SourceRoot}\runtime\*"; DestDir: "{app}\runtime"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

; Código de la app, assets, licencia y README.
Source: "{#SourceRoot}\app\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; Se crea vacía para que quede a la vista desde el Explorador dónde viven los
; datos del usuario (memoria, clave cifrada, transcripciones).
Name: "{localappdata}\{#AppName}"

[Icons]
; Acceso directo del Menú Inicio: abre el lanzador y, con ello, el overlay.
Name: "{group}\{#AppName}"; Filename: "{app}\runtime\{#AppExeName}"; \
    Parameters: """{app}\MindVoice.py"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\assets\mindvoice_logo.ico"

Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"

Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\{#AppExeName}"; \
    Parameters: """{app}\MindVoice.py"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\assets\mindvoice_logo.ico"; Tasks: desktopicon

Name: "{userstartup}\{#AppName}"; Filename: "{app}\runtime\{#AppExeName}"; \
    Parameters: """{app}\MindVoice.py"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\assets\mindvoice_logo.ico"; Tasks: autostart

[Run]
; Se ejecuta siempre al terminar (salvo que el usuario cancele el reinicio):
; así la app aparece ya abierta y pide la clave de Gemini en ese momento, en
; lugar de esperar al próximo doble clic.
Filename: "{app}\runtime\{#AppExeName}"; Parameters: """{app}\MindVoice.py"""; \
    WorkingDir: "{app}"; Description: "Iniciar {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Registros de la instalación anterior: ahora viven en {localappdata}\MindVoice
; y se conservan para no perder la memoria ni la clave al desinstalar. El
; instalador ofrece borrarlos explícitamente (ver código en [Code]).
Type: filesandordirs; Name: "{app}\runtime"
