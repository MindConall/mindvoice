# MindVoice

**Un asistente de escritorio que ve tu pantalla y te responde hablando.**

Escribes una orden o mantienes pulsado para hablar, y MindVoice envía una
captura de tu pantalla a la API multimodal en vivo de Google Gemini, te
responde con voz nativa y lo muestra en un panel flotante que se superpone a lo
que estés haciendo. Funciona sobre un juego o una ventana en pantalla completa.

Pensado como una capa de manos libres: preguntas sin soltar el ratón y te
responde sin apartar la vista.

---

## Instalación (Windows 10/11, x64)

1. Descarga `MindVoice-<versión>-setup.exe` desde la pestaña
   [Releases](https://github.com/MindConall/mindvoice/releases).
2. Doble clic.

Se instala **por usuario**: no pide permisos de administrador y no necesitas
tener Python instalado. Al abrirse por primera vez te pide la clave de la API
de Gemini; se guarda cifrada y de ahí en adelante es doble clic y listo.

> **La clave es obligatoria y gratuita.** Consíguela en
> [aistudio.google.com/apikey](https://aistudio.google.com/apikey) con tu
> cuenta de Google. Google no te cobra por el uso normal, pero sí aplica sus
> cuotas de uso: es un servicio con modalidad gratuita, no gratis sin límites.

### Si Windows te avisa

El instalador no va firmado, algo normal en proyectos de código abierto sin
certificado de pago: *Windows protegio tu PC* → **Más información** →
**Ejecutar de todas formas**.

---

## Qué sabe hacer

| | |
|---|---|
| **Ve tu pantalla** | Envía una captura fresca en cada orden y el modelo responde sobre lo que ve: transcribe el texto legible (ventana activa, mensajes, contenido), no solo el nombre de la aplicación. |
| **Te habla** | Reproduce el audio nativo que devuelve Gemini (voz *Puck*, PCM 24 kHz) con baja latencia. |
| **Escucha de verdad** | Mantén pulsado el botón de micrófono — o la tecla `Ctrl` derecho — y háblale. Te escucha aunque estés dentro de un juego. Puedes interrumpirlo hablándole. |
| **Busca en internet** | Si la orden pide información actual, busca y **te lee el resultado**. |
| **Privacidad en un clic** | Botón de ojo: pausa y reanuda el envío de tu pantalla en caliente. |
| **Recuerda** | Memoria breve y de largo plazo entre sesiones, con comando para olvidarla. |
| **Transcript** | Cada turno queda registrado con la hora en un fichero diario. |
| **Nunca se rompe** | Reconexión automática con retroceso exponencial, y auto-recuperación si el motor muere por un fallo. |

Atajos por defecto (configurables en Ajustes):

| Atajo | Acción |
|---|---|
| `Ctrl+Shift+Z` | Mostrar u ocultar el panel |
| `Ctrl` derecho (mantener) | Hablar |
| `Esc` | Ocultar el panel |
| `F9` | Silenciar la voz |

---

## Privacidad

MindVoice es una app que **mira tu pantalla y te escucha**. Conviene decirlo sin
 rodeos:

- Todo va directamente a la API de Google Gemini con **tu** clave. No hay
  servidores intermedios de MindVoice, ni analítica, ni telemetría: no hay
  código que envíe nada a ningún sitio que tú no veas.
- Solo se envía una imagen cuando tú ordenas algo (o mientras hablas en el modo
  de voz continua). El botón de ojo corta el envío en caliente.
- Tu clave se guarda cifrada con **DPAPI**: es ilegible fuera de tu cuenta de
  Windows, así que copiarla a otro equipo no sirve de nada.
- Memoria, transcripciones, ajustes y registros viven en
  `%LOCALAPPDATA%\MindVoice`. Para borrarlo todo, desinstala y elimina esa
  carpeta.
- Los registros incluyen fragmentos de lo que se muestra en pantalla. Míralos
  antes de compartirlos.

---

## Requisitos

- Windows 10 o 11, x64.
- Una clave de la API de Gemini (gratis, ver arriba).
- Micrófono y altavoces.

> Las funciones de overlay, atajo global y push-to-talk usan API de Windows
> (`RegisterHotKey`, hook de teclado a bajo nivel). **Por eso el soporte es
> Windows**: en Linux y macOS el núcleo puede arrancar, pero sin overlay ni
> atajo global.

---

## Uso desde el código

Para desarrollar o auditar el código (necesitas Python 3.10–3.13):

```bash
git clone https://github.com/MindConall/mindvoice
cd mindvoice
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Clave (si no, al arrancar te la pedirá en un diálogo)
$env:GEMINI_API_KEY = "TU_CLAVE"

python overlay.py     # el panel flotante (interfaz principal)
python launcher.py    # solo el lanzador del atajo global
python main.py        # modo consola, sin overlay
```

Opciones útiles:

```bash
python main.py --help
python main.py --no-screen          # sin enviar la pantalla
python main.py --fps 0.5 --max-size 1024 --quality 85
python overlay.py --smoke           # prueba de ventana (1,5 s)
python launcher.py --stop           # detiene el lanzador en segundo plano
```

### Compilar el instalador

```powershell
.\build_release.ps1              # staging + instalador en installer\output
.\build_release.ps1 -SkipInno    # solo staging (no requiere Inno Setup)
```

El script descarga el runtime oficial de Python embebido, instala las
dependencias dentro, copia el código y compila con [Inno Setup](https://jrsoftware.org/isinfo.php).
Antes de empaquetar hace un barrido que **aborta la construcción** si aparece
una clave, un `memory.json` o un registro dentro del paquete.

---

## Estructura

```
mindvoice/
├── MindVoice.py       # arranque de la instalación (ajusta sys.path)
├── overlay.py         # panel HUD flotante (PyQt6)
├── launcher.py        # atajo global en segundo plano (Win32)
├── start_mindvoice.py # acceso directo del escritorio
├── main.py            # modo consola
├── live_assistant.py  # sesión Live de Gemini: visión, voz, búsqueda, memoria
├── config.py          # toda la configuración en un sitio
├── rutas.py           # rutas de datos y del intérprete (multi-instalación)
├── credenciales.py    # almacén de la clave, cifrado con DPAPI
├── firstrun.py        # asistente de primera ejecución
├── prefs.py           # preferencias persistidas
├── hotkeys.py         # silenciar y push-to-talk
├── branding.py        # el logo, dibujado en código
├── media/             # micrófono, altavoz y captura de pantalla
└── installer/         # script de Inno Setup
```

---

## Configuración

Todo se ajusta desde el propio panel (**Ajustes**). Para cambios finos, variables
de entorno o `config.py`:

| Clave | Por defecto | Qué hace |
|---|---|---|
| `GEMINI_API_KEY` | — | Clave de la API. También se puede guardar cifrada desde la app. |
| modelo | `gemini-3.1-flash-live-preview` | Modelo de la sesión en vivo. |
| voz | `Puck` | Voz de la respuesta. |
| `mute_mode` | `toggle` | Silencio con `toggle` (una tecla) o `push_to_talk` (mantener). |
| `ptt_key` | `right ctrl` | Tecla de hablar, global. |
| `overlay_hotkey` | `Ctrl+Shift+Z` | Mostrar/ocultar el panel. |
| `overlay_opacity` | `0.7` | Opacidad del panel. |
| `save_transcripts` | `True` | Guardar las transcripciones diarias. |
| `response_modalities` | `audio` | `audio` (solo voz) o `audio_text` (voz + texto). |
| `screen_max_size` | `2048` | Lado máximo de la captura. |
| `screen_quality` | `95` | Calidad JPEG (1–95). |
| `web_search_enabled` | `True` | Buscar cuando la orden lo pide. |
| `web_smart_detect` | `True` | Detector por IA de "esto necesita datos actuales". |
| `web_search_provider` | `duckduckgo` | Motor de búsqueda. Alternativas: `serper`, `searxng`, `wikipedia`, `brave`, `google`, `tavily`, `serpapi`. Las que piden clave se configuran en Ajustes. |
| `session_resumption` | `False` | Reanudación de sesión (experimental; ver limitaciones). |

---

## Limitaciones conocidas

- Los modelos `*-live-preview` solo devuelven **audio**. El texto de la
  respuesta se obtiene activando la transcripción de salida, que tiene un
  pequeño retardo respecto al audio.
- La captura se envía **bajo demanda**, no en vídeo continuo. Los modelos
  preview agotan cuota con reenvíos sostenidos (~25 mensajes por sesión).
- El streaming de voz puede gastar cuota por minuto si hablas sin parar; el
  detector de silencio evita reenviar ruido de fondo.
- Subir la resolución del monitor por encima de 1080p/1440p no mejora nada:
  la imagen se reduce antes de enviarse.
- La búsqueda web con *grounding* de Google depende de la cuota del plan de tu
  clave. Con `429 RESOURCE_EXHAUSTED` el modelo responde desde su propio
  conocimiento avisando de que no pudo verificar el dato. Usar un motor externo
  (Serper, SearXNG, Brave…) evita esa dependencia.
- Sin firma digital: SmartScreen salta en la primera ejecución.

---

## English summary

MindVoice is a Windows desktop assistant that **sees your screen and answers
with voice**. It sends a fresh screenshot with each command to Google's Gemini
multimodal Live API, plays back the model's native audio, and shows the
conversation in a transparent overlay panel that floats above fullscreen apps.

- **Platform:** Windows 10/11 x64 (the overlay and global hotkey use Win32 APIs).
- **Install:** download the `.exe` from Releases and double-click. Per-user
  install, no admin rights, no Python needed.
- **Key:** a free Gemini API key is required; it's stored encrypted with DPAPI.
- **Privacy:** everything goes straight to Google with your key. No MindVoice
  servers, no telemetry. Your data lives in `%LOCALAPPDATA%\MindVoice`.
- **From source:** Python 3.10–3.13, `pip install -r requirements.txt`, then
  `python overlay.py`.
- **Build:** `./build_release.ps1` stages an embedded Python runtime and compiles
  the installer with Inno Setup.

---

## Licencia

**AGPL-3.0.** Ver [LICENSE](LICENSE).

Puedes leerlo, modificarlo y redistribuirlo, incluso comercialmente. Lo que no
puedes es presentarlo como obra tuya: los trabajos derivados tienen que seguir
con AGPL-3.0, conservar este aviso de copyright y dejar claro qué cambiaste. La
cláusula 13 obliga además a ofrecer el código fuente a quien use una versión
modificada a través de la red.

Si en algún momento notas una copia reenvendida con otro nombre y sin crédito,
es un incumplimiento y hay formas de reclamar.

---

## Autoría

**Idea, diseño, código y decisiones: [MindConall](https://github.com/MindConall).**

El trabajo de preparación para publicar el proyecto —rutas de datos,
almacenamiento cifrado de la clave, asistente de primera ejecución,
empaquetado con Python embebido, instalador, workflow de release y esta
documentación— se hizo con asistencia de **OpenCode**, un agente de
programación con IA. Los commits están firmados por MindConall porque es su
proyecto y su criterio, no porque la IA sea autora de las decisiones.

En los commits que despliegue código de la IA, el cuerpo incluye un rastro
`Co-Authored-By` para que se pueda ver qué líneas no las escribió una persona.

---

## Agradecimientos

- **Google Gemini** — API multimodal en vivo con audio nativo
  ([documentación](https://ai.google.dev/gemini-api/docs/live)).
- **[google-genai](https://github.com/googleapis/python-genai)** — SDK oficial.
- **[Inno Setup](https://jrsoftware.org/isinfo.php)** — compilador del
  instalador.
- Toda la gente que terció en [issues](https://github.com/MindConall/mindvoice/issues) y pull requests.
