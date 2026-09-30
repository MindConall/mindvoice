# MindVoice — memoria del proyecto

> Lee esto **antes** de tocar código. Este fichero es el traspaso entre sesiones: describe
> qué es la app, cómo está montada, qué deuda es real y cómo se consulta el grafo.
> Última revisión: 2026-09-30 · commit `9e50684` · tag `v0.1.0`.

## Qué es

Asistente de voz y visión para Windows en español. Se habla al micrófono, Gemini Live
responde con audio en tiempo real, puede "ver" la pantalla, buscar en web, manejar
temporizadores, volumen y portapapeles, y recuerda conversaciones. Se distribuye como
instalador portable-ish (Python embebido + Inno Setup, sin instalación de Python).

- **Autor**: MindConall (Caracas, Venezuela). Usado a diario en producción.
- **Licencia**: ver `LICENSE`. Repo: `github.com/MindConall/mindvoice`.
- **Stack**: Python 3.13, PyQt6 (HUD), `google-genai` (Gemini Live), pyaudio, mss, Pillow,
  `keyboard`, httpx.
- **Modelo**: `gemini-3.1-flash-live-preview`, API `v1alpha` (`config.py:35-36`).
  *v1beta cortaba la sesión a los 20-30 s — no tocar sin medir.*

## Arranque: cuatro capas, no una

```
pythonw.exe MindVoice.py                  ← punto de entrada real (lo lanzan los accesos directos)
  └─ runpy.run_path("start_mindvoice.py") MindVoice.py:60     ← shim: sys.path + UTF-8, luego bootstrap
       ├─ firstrun.asegurar_clave()       start_mindvoice.py:98   ← sin clave Gemini, exit 2
       ├─ Popen(pythonw launcher.py)      start_mindvoice.py:106  ← solo si el mutex no existe
       └─ call(pythonw launcher.py --press) start_mindvoice.py:120 ← simula la primera pulsación
            └─ launcher.press_once()      launcher.py:250
                 └─ Popen(pythonw overlay.py) launcher.py:150   ← la app de verdad
                      └─ OverlayHud + _start_worker() overlay.py:2530
                           └─ LiveAssistant.run() live_assistant.py:1816
```

- `MindVoice.py` existe solo porque el runtime embebido **no** añade el script dir a
  `sys.path` (gana el `python313._pth`). No pongas lógica ahí.
- `launcher.py` es un demonio Win32 sin UI: posee `RegisterHotKey` (Ctrl+Shift+Z) y es el
  único que puede lanzar el overlay. **No supervisa**: descarta el handle de `Popen`
  (`launcher.py:150`), no hace `poll()`, no reinicia. Si el overlay crashea, solo se
  recupera con otra pulsación de la hotkey (`launcher.py:167`).
- Overlay = ventana HUD PyQt6 sin marco, siempre encima, transparente.
- `main.py` (REPL de consola) y `gui.py` (Tkinter, **muerto y no empaquetado**) son
  entrances de desarrollo. `gui.py` no lo importa nadie y `build_release.ps1:135-145`
  lo excluye del paquete a propósito.

## Mapa de módulos

| Módulo | Líneas | Rol |
|---|---|---|
| `live_assistant.py` | 5 528 | Motor Gemini Live. **Clase Dios**: 106 métodos, 4 474 líneas. |
| `overlay.py` | 2 558 | HUD PyQt6 + fontanería Win32 + hospeda el motor. **Clase Dios**: 76 métodos. |
| `config.py` | 361 | `Settings` (45 campos), registro `WEB_ENGINES`, prompt del sistema. |
| `rutas.py` | 202 | Rutas: data dir, logs, intérprete hijo, migración desde el app dir. |
| `credenciales.py` | 283 | Clave Gemini: DPAPI → Fernet → plain. |
| `prefs.py` | 158 | `user_prefs.json`: DEFAULTS, filtro de claves, `apply_prefs`. |
| `hotkeys.py` | 111 | `HotkeyController` sobre la librería `keyboard`. |
| `branding.py` | 112 | Pixel-art del robot, colores, generación de PNG/ICO. |
| `firstrun.py` | 141 | Diálogo PyQt6 de primera ejecución (pide la clave). |
| `launcher.py` | 289 | Demonio de hotkey global. |
| `media/microphone.py` | 302 | Captura 16 kHz mono, sondeo de dispositivos, gate adaptativo. |
| `media/playback.py` | 297 | Salida 24 kHz mono, ganancia PCM, `flush()` para barge-in. |
| `media/screen.py` | 165 | Captura de pantalla con mss + diff + JPEG. |

## Modelo de hilos (la parte que más importa)

Cuatro hilos del SO, un loop asyncio, cinco timers Qt, un named pipe.

| Hilo | Creado en | Dueño de | Cruza al otro lado por |
|---|---|---|---|
| Qt main | Qt | Widgets, 5 `QTimer`, drenado de `_ui_queue` | — (es el loop) |
| `mindvoice-overlay` | `overlay.py:2180` | El loop asyncio **entero**: `LiveAssistant`, WebSocket, `HotkeyController` | `loop.call_soon_threadsafe` |
| `mic-reader` | `media/microphone.py:252` | `stream.read()` bloqueante de pyaudio | `call_soon_threadsafe` → `chunk_queue` |
| `playback` | `media/playback.py:114` | `queue.Queue` → `stream.write` | `queue.Queue` (bloqueante) |
| `mindvoice-hotkey` | `overlay.py:1856` | `RegisterHotKey` + `GetMessageW` | `_ui_queue.put(("toggle", None))` |
| `mindvoice-ptt` | `overlay.py:1929` | Hook `WH_KEYBOARD_LL` | `_ui_queue.put(("ptt_on"/"ptt_off", …))` |
| `dev-enum` | `overlay.py:1125` | Enumeración de dispositivos PyAudio | `_uiput("devs", …)` |
| `mindvoice-transcript` | `overlay.py:463` | Escritura a `sessions/*.txt` | fire-and-forget |

**Único punto de cruce hacia la GUI: `_ui_queue`** (`overlay.py:462`). Se escribe con
`_uiput(kind, payload)` (`:2320`) y se drena con un `QTimer` de 60 ms → `_drain_ui_queue`
(`:2340`). 17 tipos de mensaje. Back-pressure: descarta por encima de 2 000 en cola,
salvo allow-list crítica (`ptt_on, ptt_off, toggle, dead, err, lvl, quit`).
Coalescing: `lvl` se descarta si ya hay uno pendiente.

**La superficie pública de `LiveAssistant`** es el único camino sancionado hacia el loop
(`live_assistant.py:1227-1372`): `submit_command`, `set_voice`, `set_output_device`,
`set_voice_name`, `set_transcript_lang`, `set_response_modalities`, `set_screen_enabled`,
`cancel`, `prefetch_description`. **No añadas atributos tocados desde el hilo Qt.**

Las 11 callbacks hacia la GUI (`on_text`, `on_user_text`, `on_meta`, `on_web`,
`on_turn_complete`, `on_interrupted`, `on_voice_level`, `on_tokens`, `on_state`,
`on_timers`) se envuelven en `_safe_call` (`live_assistant.py:5024`). No se usan
señales Qt en ninguna parte: la coupla GUI↔motor es enteramente por callbacks y colas.

## Pipeline de audio

- **Entrada**: pyaudio 16 kHz / `paInt16` / mono / chunks de 200 ms. `input_device_candidates`
  (`media/microphone.py:90`) ordena por **prioridad de host API: MME(0) → WASAPI(1) →
  DirectSound(2) → otro(3)**, porque el micro de la webcam de esta máquina suena roto por
  DirectSound. `_is_dead` (`:185`) mide ~0.5 s y descarta picos `< 4`.
- **VAD manual por defecto** (`voice_manual_vad=True`, `config.py:201`): el gate local
  (`_voice_loop:2649-2739`) es adaptativo sobre RMS, y el cierre de turno lo dispara el
  botón/la hotkey, no el servidor. El silencio de cola se inyecta a mano: 0.8 s
  (`_VOICE_TAIL_S`, `live_assistant.py:118`) — 200 ms deja turno muerto, 400-600 ms es lo
  normal (`:117`). **No tunes estos números a ojo.**
- **Salida**: pyaudio 24 kHz mono, ganancia PCM 0.0-1.5 (`media/playback.py:225`).
  Cola de 480 chunks (~96 s) a propósito; descarta **el más antiguo** al llenarse
  (opuesto al micro, que descarta el nuevo). `flush()` es el mecanismo de barge-in/cancel.
- **TTS es del servidor** (`speech_config`, `live_assistant.py:1754`). Voz por defecto
  `Puck`. Sin `voice`, los modelos Live responden solo texto.

## Reconexión y salud: cuatro capas

1. **Retry de `run()`** (`live_assistant.py:1879-1952`): backoff exponencial 1 s → 30 s.
   Si la sesión vivió ≥60 s, el intento se reinicia (la rotación de servidor no es un fallo).
   Cuota (429) → cooldown fijo de 90 s, sin presión exponencial.
2. **Watchdog de sesión** (`:4863`, poll 1 s): 4 veredictos — fin educado (2 s de
   silencio), micro abierto >300 s, turno de voz muerto (25 s → replay), y stall general
   (>30 s → `_INTERRUPT_TEXT` + `flush()`; a los 3 stalls en 120 s → `player.restart()`).
   Lanza `_SessionStalled`, que el retry de `run()` recoge.
3. **Watchdog de hilo** (`overlay.py:2201`, 5 s): reinicia el worker si murió. Frenos:
   8 s entre reinicios, y 4 muertes rápidas en <15 s → desactiva el auto-restart para no
   quemar cuota.
4. **Watchdog del hook PTT** (`overlay.py:2014`, 3 s): reinstala el hook hasta 5 veces.

## "Herramientas": no hay function-calling

**Cero declaraciones de herramientas de Gemini.** No busques `function_declarations`:
no existen. Todo es un **detector de intención del cliente** que corre *antes* del turno,
calcula la respuesta localmente y la inyecta como texto entre paréntesis; el modelo debe
leerla y hablarla.

- `_local_action` (`live_assistant.py:4230`, 162 líneas) — tabla regex: temporizadores
  (listar/cancelar/crear en lenguaje natural), volumen del sistema (ctypes/winmm),
  portapapeles leer/escribir.
- `_maybe_math_note` (`:5450`) — aritmética en español vía sustitución + evaluación con
  `ast`. El orden de sustitución cambia el significado: "multiplicado por" va antes que
  "resultado de", "dividido entre" antes que "dividido por" (`:186-190`).
- Búsqueda web (`:2809-3958`, ~900 líneas): caché 60 s → throttle 15 s (con mensaje al
  usuario, no silencioso) → budget total 9 s. Clima primero vía `wttr.in`. **Un solo
  proveedor, sin cadena de fallback** (`:3941-3958`): si es serper sin clave, lo dice y
  devuelve `""` — no sustituye por DuckDuckGo, porque que el motor anuncie un proveedor
  distinto del que buscó fue un bug real.
- Visión de pantalla (`:4616-4847`): captura → sesión Live **efímera y separada** que solo
  devuelve texto. Motivo documentado dos veces (`:8-14`, `:4619-4622`): una imagen inline
  dentro de `send_client_content` **deja muda para siempre** la sesión de audio.

## Estado y persistencia

Raíz: `%LOCALAPPDATA%\MindVoice` (Windows) o `~/.local/share/mindvoice` (POSIX), override
`MINDVOICE_DATA_DIR`. Resolución y migración en `rutas.py:84-131`.

| Fichero | Quién | Esquema |
|---|---|---|
| `user_prefs.json` | `prefs.save_prefs` | 20 claves, filtradas contra `DEFAULTS` |
| `memory.json` | `live_assistant._save_memory:1497` | `[{role: user\|assistant, text}]`, tope 24 × 400 chars |
| `memory-long.json` | `_do_archive:1543` | `[str]` resúmenes, tope 30 × 600 chars |
| `timers.json` | `_save_timers:4151` | `[{seconds,label,end,end_epoch,done}]`, solo pendientes; restaurar descarta `end_epoch` pasados |
| `sessions/YYYY-MM-DD.txt` | `overlay._log_transcript:1422` | `[HH:MM:SS] <tag>: <text>`, tags Tú/IA/Web/Sys |
| `web_model_cache.json` | `live_assistant._save_cached_web_model:981` | `{model, ts}`, TTL 7 días. Usa `rutas.data_file` (corregido en 0.1.1) |

La memoria se reinyecta en `system_instruction` en **cada** `_connect_config()` (`:1703`),
por eso sobrevive a reconexiones. Al desbordar 24 entradas se archiva en vez de borrarse.

## Clave de API

`credenciales.load_api_key:175` — orden: `GEMINI_API_KEY` → `MINDVOICE_API_KEY` →
`secrets.json` (DPAPI con entropía `b"MindVoice::secrets.json::v1"`, o Fernet, o **plain**).

**En esta máquina `GEMINI_API_KEY` está puesta, así que el almacén cifrado nunca se
consulta y no existe `secrets.json`.** Ojo: `clear_api_key()` no toca las vars de entorno,
así que "borrar la clave" aquí no hace nada. Y la clave de serper vive **en claro** en
`user_prefs.json` (gitignored, pero sin cifrar) — trátala como expuesta y rótala.

## Deuda real (verificada, con sitio exacto)

**P0**
- Clave de serper en claro en `user_prefs.json`; `Assert-NoSecrets` solo busca `AIza…`
  (`build_release.ps1:225`). Ampliar la regex y rotar la clave.
- Carrera en el cierre de generador asíncrono: `RuntimeError: aclose(): asynchronous
  generator is already running` — 9 veces en `overlay-launcher.log`.

**P1**
- `speech_vocabulary` **nunca persiste**: `overlay.py:1928` la escribe, pero no está en
  `prefs.DEFAULTS` (`prefs.py:27-48`) y `save_prefs` filtra por `k in DEFAULTS` (`:72`).
  Fix de una línea: añadir la clave a `DEFAULTS`.
- Bytecode en el instalador: `Test-Package` (`:350`) regenera los `.pyc` **después** del
  guard de `Assert-NoSecrets` (`:217`). 16 ficheros, 499 KB, en cada build.
- Sin lockfile: `requirements.txt` solo tiene `>=`. El runtime embebido resolvió
  `google_genai 2.25.0`, `PyQt6 6.11.0`, `websockets 16.1.1`. Fija versiones.
- Desajuste tag/`installer/version.txt` es **fatal-less** en `release.yml:43-49`: puedes
  publicar `v0.2.0` con un `.exe` llamado `0.1.0`.
- `httpx` se importa directamente (`live_assistant.py:54`) y **no está en
  `requirements.txt`**; solo funciona por transitive de `google-genai`. Cuatro rutas de
  red dependen de eso.

**P2**
- `overlay.py:2273` construye `HotkeyController(mode=mode, initially_muted=False)` sin
  pasar `ptt_key`, `toggle_key` ni `quit_keys` → en la app real, F9 y Ctrl+Shift+Esc
  están cableados a pelo mientras el overlay instala su propio hook `WH_KEYBOARD_LL` para
  la PTT. Dos mecanismos globales de teclado conviviendo.
- `main.py:43` sigue con `--quit-keys` default `"esc"`, que es exactamente la regresión que
  `hotkeys.py:35-41` documenta como ya arreglada (hook global de Esc → bucle de reinicios
  de 8 s). `config.py:192` también dice `"esc"`.
- Los volcados `debug_*` saltan `rutas.data_file` (`live_assistant.py:439`) → no
  funcionan bajo Program Files. **`web_model_cache.json` ya corregido en 0.1.1**
  (ahora usa `data_file`, con test de regresión que lo comprueba).
- God classes: `LiveAssistant` (4 474 líneas) y `OverlayHud` (2 049). God methods:
  `_voice_loop` (333), `_send_command_loop` (243), `_receive_loop` (173),
  `__init__` (168), `_build_panel` (**552**). Las costuras están anotadas en comentarios
  en español: **al extraer, muévelos con el código** (contienen mediciones empíricas:
  que 0.8 s de cola funciona, que `_INTERRUPT_TEXT` mata la sesión si se envía mal).
- Churn de watchdog: 172 reinicios en 8 días en los logs. Es síntoma, no causa — las
  causas son los 984× `1008 aborted` y los drops de DNS.
- `keyboard` (0.13.5, sin mantenimiento desde 2021) solo aporta F9/quit; la PTT y
  Ctrl+Shift+Z ya usan Win32 crudo. Eliminarlo quitaría una liability y arreglaría el
  hardcodeo de `ptt_key`/`toggle_key` de paso.

**Muerto / borrable**
- `gui.py` (320 líneas) — nadie lo importa, no se empaqueta.
- `mindvoice_codigo_completo.py` (373 KB) — volcado monolítico de todo, gitignored
  (`.gitignore:51`), desincronizado. **Es una trampa para búsquedas con grep**: matchea
  un tercio de las búsquedas de imports/hotkeys. Muévelo fuera del árbol.
- Constantes muertas: `config.DEFAULT_API_KEY:24`, `config.API_BASE_URL:42`,
  `config.Settings.input_channels:159`, `rutas.is_writable:164`, `branding.icon_canvas(scale=…)`.
- Logs en la raíz del repo: son de antes de `rutas.py`. Los vivos están en
  `%LOCALAPPDATA%\MindVoice\logs\`, sin rotación (`overlay-launcher.log` llegó a 1.6 MB).

## Reglas al tocar código

1. **Nunca** llames a Qt desde el hilo del loop, ni al loop desde Qt directamente: usa
   `_uiput` hacia dentro y la API pública de `LiveAssistant` hacia fuera.
2. **Nunca** añadas una imagen dentro del turno de voz. Visión va por sesión aparte.
3. **Nunca** envíes `_INTERRUPT_TEXT` en barge-in: mata la sesión (`:2682-2691`).
4. Si tocas el mic, respeta el orden de host API (MME > WASAPI > DirectSound) y el
   sondeo `_is_dead`.
5. Si tocas memoria o timers, respeta los topes: están puestos a propósito y cuesta
   memoria recrearlos.
6. Comentarios y prompts en **español**. Es la convención del repo.
7. Antes de un refactor grande, `git log --oneline` y este fichero. Después, actualiza
   este fichero.

## Instrumentación: `MINDVOICE_PERF` y el panel de diagnóstico (Fase 4)

`perf_instr.py` mide el HUD y **no lo cambia nunca**. Se enciende de dos maneras:

| Cómo | Cuándo |
|---|---|
| `MINDVOICE_PERF=1` (o `overlay`, `prompt`) | al arrancar; manda sobre todo lo demás |
| `Ctrl+Shift+D` dentro del HUD | en caliente, sin relanzar la app |

Cerrar el panel **no desactiva lo que arrancó el entorno**: `apagar()` solo devuelve el
mando a `MINDVOICE_PERF`. Si la app se lanzó medida, sigue midiendo.

Lo que enseña el panel (una línea por magnitud, todo leído de `Perf.snapshot()`):

- `HUD n/s · ciclo … ms · volcado … ms` — cadencia y coste del `_poll` de 60 ms.
- `tirones >100 ms / >500 ms` — tramos sin refrescar; es lo que el ojo ve como tirón.
- `arranque: construida / visible … ms` — marcas de la Fase 0.
- `prompt … car ~ … tok` — tamaño de lo inyectado al modelo por turno.
- `motor: reinicios / muertes rápidas / PTT / voz` — salud del proceso, que antes no se
  enseñaba en ningún sitio.

**Al tocar este panel, tres invariantes que cuestan cero pero que rompen todo si fallan**
(hay test para cada una, en `test_perf_overlay.py`):

1. **Cerrado no cuesta nada.** Sin timer corriendo, sin hueco en el layout, sin
   `PERF.enabled`. El timer se crea parado y solo `_toggle_perf` lo arranca. Y con el HUD
   oculto (`Ctrl+Shift+Z`) el tick también se para, sin cerrar el panel: al volver a
   mostrarlo sigue abierto. Un panel de diagnóstico que se nota es un panel que nadie
   abre.
2. **Nada de layout animado** y `setText` solo si el texto cambió: un `setText` con lo
   mismo de contenido igual repinta el panel entero.
3. **El atajo es un `QShortcut`**, no un `keyPressEvent`: al abrir el HUD el foco se va al
   `QLineEdit` de órdenes y la ventana nunca ve la tecla.

Tests: 30 en `test_perf_overlay.py` (contrato de `perf_instr` sin Qt, cableado del overlay
leyendo el fuente, y el panel sobre un HUD real). Comprobado que **fallan** contra 18
mutaciones del código, para que no sean decorativos.

## La capa de acento: QML, y por qué no shader (Fase 5)

El brillo del panel se decidió **midiendo**, no por gusto. Prototipo al tamaño real del
HUD (416×390, Radeon RX 580, GL 4.1 core):

| ruta | construir | repintado | offscreen | riesgo |
|---|---|---|---|---|
| QSS (fondo base) | 2–3 ms | 0,6–1,1 ms | sí | ninguno |
| isla QML (`QQuickWidget`) | 68 ms caliente / ~230 ms frío | 0,07–0,19 ms | **sí** | motor QML en el arranque |
| shader (`QOpenGLWidget`) | 2,5–12 ms | ~0,1 ms | **no pinta** | **API errónea revienta el proceso** |

El shader quedó **descartado con datos**, por tres motivos reproducidos: `QOpenGLFunctions`
no existe en PyQt6 (es `QOpenGLFunctions_4_1_Core`; usar la mala aborta con `0xC0000409` y sin
traza); en `offscreen` —la plataforma de TODA la suite— `initializeGL`/`paintGL` no llegan a
correr, así que el fondo sería intesteable; y en *core profile* sin VAO dibuja nada **en
silencio**. La isla QML rinde igual de barato en régimen y **sí** pinta offscreen.

Como el precio de QML es el arranque del motor, el diseño es **perezoso**: el fondo del panel
sigue siendo QSS y la isla solo se construye la primera vez que hay **algo que animar** (el
motor entra en escuchando/procesando/hablando/reconectando). Un HUD recién abierto que no usa
el motor **no paga QML**: el cold start queda como estaba.

Tres invariantes (una por test en `test_accento.py`):

1. **La isla no se construye en el constructor.** `HaloAcento.__init__` solo crea un widget
   vacío; el `QQuickWidget` nace en `asegurar()`, que solo llama `animar()`. `reposar()` sin
   isla no la construye.
2. **El anillo va DETRÁS del panel**, con `stackUnder`, no `raise_`. Es un halo alrededor, no
   una capa que tape el texto: se coloca a `panel.geometry()` ajustada ±6 px.
3. **El QML va embebido como cadena**, no como `.qml` del repo: el `QML_ACENTO` se escribe una
   vez (en caliente) en el directorio de datos. El instalador tiene una lista explícita de
   ficheros y un dato suelto sería un olvido silencioso.

Si QtQuick no está o el QML falla, se registra y se sigue: el HUD funciona sin acento, como
antes de la Fase 5. `closeEvent` suelta la isla (`apagar()`) para no dejar la animación QML
viva contra una ventana cerrándose.

**Ojo con el empaquetado (arreglado en la Fase 5).** `build_release.ps1` copia módulos por una
lista explícita y paquetes por `$AppPackages`. Hasta esta fase faltaban `perf_instr.py`, `ui/`
y `memory/` — los tres que `overlay.py`/`live_assistant.py` importan desde las Fases 0–4. El
paquete compilaba pero la app instalada moría con `No module named 'ui'`. Si se añade un módulo
o un paquete nuevo, hay que añadirlo a esa lista: la prueba de humo (`Test-Package`) importa
todos los módulos y lo caza.

Tests: 20 en `test_accento.py` (contrato del QML y del lazy leído del fuente, cableado del HUD,
y la isla real sobre offscreen). El bug del tamaño 0×0 (la vista nacía después de colocar el
halo) lo pilló el propio test que exige que el QML pinte tinta.

## El grafo (graphify) — la memoria entre sesiones

**886 nodos · 1 744 aristas · 46 comunidades** (reconstruido 2026-09-30, commit `2c8462c`).
Es la memoria estructural del proyecto: **consúltalo antes de leer 5 000 líneas**.

```bash
graphify query "¿cómo llega una pulsación de hotkey hasta Gemini?"   # BFS, contexto amplio
graphify query "..." --dfs --budget 4000                             # traza un camino, más Tokens
graphify path "LiveAssistant" "OverlayHud"                           # camino más corto
graphify explain "_voice_loop"                                       # explicación de un nodo
graphify affected "_voice_loop"                                      # qué se rompe si tocas esto  ← antes de editar
graphify god-nodes --top 12                                          # hubs arquitectónicos
graphify update .                                                    # incremental tras editar código
```

`query` trunca a ~2 000 tokens por defecto y avisa: sube `--budget` o estrecha la pregunta,
o usa `path` / `explain` para un símbolo concreto. Las 46 comunidades llevan nombre en
humano (ver *Community Hubs* en el informe), así que el informe sirve de índice de
navegación.

### Artefactos en `graphify-out/`

| Fichero | Qué es |
|---|---|
| `graph.json` | fuente de verdad estructural (886 nodos) |
| `graph.html` | visor interactivo, se abre en el navegador sin servidor |
| `mindvoice-callflow.html` | **18 secciones con diagramas Mermaid del flujo de llamadas** — el mapa de arquitectura |
| `GRAPH_TREE.html` | árbol D3 plegable por módulo |
| `GRAPH_REPORT.md` | informe legible: comunidades, god nodes, conexiones inesperadas, huecos |
| `manifest.json` + `cache/` | caché incremental: solo re-extrae lo que cambió |
| `cost.json` | coste acumulado de extracción |

### Cómo se extrae (y su coste)

- **Código (19 ficheros .py/.ps1/.yml)**: AST determinista, sin LLM, gratis.
  **Es el 729 de los 886 nodos.**
- **Documentos e imágenes (5 docs + 2 PNG)**: extracción semántica con subagentes
  (README, AGENTS.md, requirements.txt, release.yml, version.txt, los dos logos).
  Son los otros **157 nodos** y son los que aportan la *intención de diseño* (por qué
  existe cada costura) — esa parte **no** sale del AST. Último build: **19 k tokens in /
  41 k out**. El corpus está cacheado: rebuilds incrementales de código no gastan nada.

Avisos de integridad conocidos del build actual (no son bugs del código, son del grafo):
122 aristas "colgantes" apuntan a librerías externas sin nodo (`logging`, `os`, `httpx`,
`pyaudio`, `PyQt6`…) — pérdida cero; y 5 self-loops + 36 aristas colapsadas por
multi-relación (`calls`+`references`+`uses` al mismo destino), comportamiento normal del
extractor AST.

### Bucle de memoria entre sesiones

```bash
graphify save-result --question "..." --answer "..." --nodes LiveAssistant _voice_loop \
                     --outcome useful            # useful | dead_end | corrected
graphify reflect                                   # agrega todo en reflections/LESSONS.md
```

Marca el resultado de cada consulta al grafo para que la próxima sesión sepa qué rutas del
grafo sí sirven. `graphify hook install` añade un hook post-commit que reconstruye solo.

`mindvoice_codigo_completo.py` queda **fuera** del grafo a propósito (está gitignored):
duplicaría todos los componentes y ahogaría la señal.

### Informe en español (scripts propios)

Graphify no tiene i18n: solo genera el informe y el callflow en inglés y su `--lang` solo
acepta `auto|zh-CN|en`. Aquí se resuelve con dos scripts en `tools/`:

```bash
python tools/regenerar_informe.py   # regenera GRAPH_REPORT.md desde graph.json (886/1744/46)
python tools/informe_es.py          # aplica las 46 etiquetas ES a graph.json + traduce el informe
python tools/informe_es.py --check  # solo valida: 46 comunidades definidas, no escribe nada
```

Orden obligatorio: **siempre** los dos, en ese orden. `regenerar_informe.py` reconstruye el
informe entero (dioses, comunidades, aristas ambiguas, huecos y preguntas sugeridas) usando
las etiquetas que haya en `graph.json`; por eso hay que traducir antes de regenerar el
informe, no después de generarlo por primera vez.

- Las etiquetas manualizadas viven en `LABELS_ES`, en `tools/informe_es.py` (46, una por
  comunidad). Si un `cluster-only` reasigna ids, `--check` avisa de ids que faltan o sobran.
- `mindvoice-callflow.html` **no** está traducido y no se puede traducir: el exportador solo
  acepta `--lang auto|zh-CN|en`.
- Tras cambiar etiquetas hay que refrescar los visores: `graphify export html .` y
  `graphify tree .`, y volver a registrar el grafo global con
  `graphify global add graphify-out/graph.json --as mindvoice`.

## Dónde cortar `LiveAssistant` si hay que cortar (medido, no opinado)

114 métodos, **159 `self.<attr>` distintos**, 240 aristas hacia fuera en 20 comunidades.
Cruce por atributo (agrupado a mano, heurístico — ver caveat abajo):

| Subsistema | Atributos | LOC | ¿Toca estado de sesión/voz? |
|---|---|---|---|
| VOICE (turno, VAD, watchdog) | `_voice_*`, `_in_acc`, `_out_acc`, `_turn_*`, `_last_data_ts` | 2 489 | sí, siempre |
| WEB (búsqueda, DDG, proveedores) | `_web_*`, `_ddg_*`, `_provider_*` | 1 663 | 4 métodos sí |
| SCREEN (descripción de pantalla) | `_desc_*`, `_screen_ref` | 944 | 2 métodos sí |
| LOCAL (temporizadores, volumen, portapapeles, math) | `_timers`, `_tone`, `_clipboard*` | 876 | 2 métodos sí |
| MEMORY (breve + archivo largo) | `_memory`, `_permanent`, `_archive_*` | 685 | 1 método sí |

**Por qué es un puente**: no es que haga 114 cosas — es que **es el único dueño de la sesión
Live**, y todo subsistema termina en el mismo sumidero: producir un string y meterlo entre
paréntesis en el turno. Ese sumidero es `_send_command_loop(session, player, screen)`
(`:2114`, 243 LOC) — **toca los 5 subsistemas y el estado de voz, 30 atributos a la vez**.
Por eso cualquier otro cambio toca la clase entera.

**Corte seguro (25 métodos, ~600 LOC, riesgo cero)**: ninguno toca estado de sesión.
`_local_action` (162), `_web_search` (73), `prefetch_description` (40), `_maybe_math_note` (38),
`_schedule_voice_web` (37), `_web_search_impl` (35), `_timer_worker` (35), `_remember` (34),
`_log_script` (31), `_build_memory_block` (26), `_invalidate_web_model_on_quota` (26),
`_voice_screen_note` (26), `_publish_timers` (22), `_archive_to_permanent` (17),
`_web_note_for` (16), `_active_provider` (16), `_do_archive` (14), `_set_system_volume` (10),
`_cancel_timers` (10), `_load_timers` (9), `_save_timers` (8), `_save_memory` (7),
`engine_label`/`engine_spoken`/`_is_primary_provider` (3 c/u).
Extraerlos baja la clase de 4 474 a ~3 870 LOC sin tocar ni un `_voice_*`.

**Corte peligroso (14 métodos, ~1 700 LOC)**: `_voice_loop` (333, 34 attrs),
`_send_command_loop` (243), `_receive_loop` (173), `__init__` (168), `run` (154),
`_web_model_name` (150), `_launch_voice_web` (149), `_run_session` (139), `_describe_screen` (134),
`_connect_config` (121), `_end_turn` (104), `_cancel_now` (33), `_flush_input` (17),
`_flush_output` (7). Aquí el estado de voz (ventana de actividad del VAD, acumuladores
`_in_acc`/`_out_acc`, `_last_data_ts`) se lee en la misma pasada que la salida de un
subsistema hoja, y el watchdog lee lo que escribe `_end_turn`. Eso es un ciclo real, no
capas: **no lo extraigas sin tests**.

*Caveat*: el agrupado por atributo es heurístico (`_duckduckgo_search`, `_search_via_provider`,
`_web_query` y `_safe_math` salieron como "sin clúster" solo porque tocan atributos que el
patrón no cubre). Los LOC por subsistema solapan porque un método se cuenta en varios. Los
nombres y las líneas sí son exactos.

## Estado del repo

> Última revisión: 2026-09-30 — commit `4f1fc47` (panel de diagnóstico, Fase 4) + Fase 5
> (capa de acento QML perezosa y arreglo del empaquetado). Suite: **282/282** en 11 ficheros.

- Rama con 5 commits, `v0.1.0` tagueado. **`AGENTS.md` (este fichero) nunca se ha commiteado**
  — es memoria local. Si quieres que sobreviva a otra máquina, commitéalo.
- `graphify-out/` tampoco está commiteado. `graph.json` es lo que el merge driver usa en
  conflictos; los `.html` (790 KB + 315 KB) se regeneran con `graphify export html` /
  `export callflow-html`, así que no hacen falta en git.
- Hooks instalados: `post-commit`, `post-checkout` y un merge driver para `graph.json`
  (`.gitattributes:1`). Cada commit reconstruye el grafo incrementalmente solo.
- Grafo también registrado en el global: `~/.graphify/global-graph.json` (tag `mindvoice`).
- `installer/output/MindVoice-0.1.0-setup.exe` (77.9 MB) ya construido.
- `test_web_engines.py` es offline, rápido y hermético (`python test_web_engines.py`),
  pero **CI no lo ejecuta** y **no se empaqueta**. Ese hueco es lo que deja que el
  registro de motores de búsqueda pueda volver a romperse.
