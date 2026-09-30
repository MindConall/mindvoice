# Changelog

Todas las novedades de MindVoice están aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y las versiones
siguen [SemVer](https://semver.org/lang/es/).

Este fichero es la fuente de las notas de cada release: el workflow de
publicación lo lee y lo antepone a las instrucciones de instalación, así que
**lo que no esté aquí, la release no lo dirá**.

## [No publicado]

### Añadido

**Un halo que respira alrededor del panel mientras el motor trabaja.** Cuando MindVoice
está escuchando, procesando o hablando, el panel se rodea de un anillo del color de ese
estado que late despacio. Se dibuja con una isla QML que **no existe hasta la primera vez
que hace falta**: si abres el HUD y no usas el motor, el arranque no cambia ni un
milisegundo. Se eligió QML frente a un shader midiendo ambas: el shader no se puede pintar
en la plataforma sin pantalla con la que corren las pruebas y una llamada equivocada a su
API tumbaba el proceso entero.

### Corregido

**El instalador no llevaba tres de sus propias piezas.** `build_release.ps1` copia el código
por una lista explícita, y esa lista se quedó sin `perf_instr.py`, sin el paquete `ui/` y
sin el paquete `memory/` que el HUD y el motor importan desde hace varias versiones. El
paquete se construía igual, así que el fallo solo aparecía en la app instalada, al morir en
el arranque con `No module named 'ui'`. Ahora se copian los tres y la prueba de humo del
empaquetado importa cada módulo para que un olvido así no vuelva a pasar en silencio.

## [0.1.1] - 2026-09-30

Correcciones y mejoras sobre 0.1.0. Sin cambios de interfaz ni de formato de
configuración: se actualiza y sigue funcionando igual.

Cómo leerlo: **Corregido** son fallos que ya estaban, **Añadido** es lo nuevo de
esta versión y **Seguridad** son cambios de cómo se guardan o se muestran las
claves.

### Corregido

**La voz se quedaba muda a partir del primer turno fallido.** El contador de
intentos de reenvío de un turno (`_voice_replay_tries`) solo se rearmaba en dos
caminos internos del bucle de voz. En cuanto un turno se estancaba, el contador
se quedaba en `1` y el watchdog, que exige `< 1`, dejó de reenviar **todos los
turnos siguientes de la sesión**: la única recuperación existente quedaba
muerta hasta reiniciar la app. Se rearma ahora en las dos fronteras reales de
turno, `_end_turn` (turno completado) y `_clear_turn_state` (turno abandonado).
No se tocan timeouts ni se añaden reintentos.

**Los clones limpios respondían con el modelo más barato, en silencio.** La lista
de modelos de búsqueda web estaba ordenada por disponibilidad de una clave
concreta, no por calidad: como `gemini-3.5`–`3.8-flash` devolvían 429 en la
máquina del autor, `gemini-3.1-flash-lite` quedaba **primero**. Con
`_WEB_MODEL_MAX_PROBES = 6` era imposible llegar a los buenos, y
`web_model_cache.json` conservaba el resultado sin probarlo y sin avisar. Ahora
la lista va por calidad: `gemini-3.6-flash` primero y la red `-lite` al final.

**Nada de lo que ocurre fuera de tu vista.** Toda caída del modelo configurado
(caché vieja, cambio a un modelo de reserva, red caída o búsqueda desactivada)
se decía solo en el log. Ahora se dice también **en pantalla**, en el mismo sitio
donde se ven las respuestas.

**El motor de búsqueda se fijaba al de la máquina del autor.** `user_prefs.json`
venía con `duckduckgo` como valor por defecto, así que un clon limpio se
quedaba con DuckDuckGo aunque tuviera una clave de serper puesta. Ahora se decide
por lo que hay configurado: serper si existe `SERPER_API_KEY`, DuckDuckGo si no,
y el motor que hayas elegido en Ajustes no lo cambia nadie por la espalda.

**La caché del modelo web no sobrevivía a una instalación.** Se guardaba junto
al código, en la carpeta de la app. Instalada en `C:\Program Files` esa carpeta
no se puede escribir, el error lo tragaba un `except` y la resolución del modelo
se repetía en cada arranque. Ahora usa el directorio de datos del usuario, como
el resto del estado.

**`speech_vocabulary` no persistía.** No estaba en la lista de claves que
`save_prefs` deja pasar, así que el vocabulario personalizado se perdía al
cerrar la app. Añadido a los valores por defecto.

**El instalador y el tag podían no coincidir sin decir nada.** Si el tag y
`installer/version.txt` discrepaban, la publicación seguía adelante. Ahora es un
error claro, y si falta el fichero de versión el build falla en vez de suponer
un número.

**El control de secretos del build solo buscaba claves de Gemini.** Una clave de
otro proveedor (hexadecimal de 32+ caracteres) pasaba el filtro sin que nadie se
enterase. Ampliado el patrón.

**Ficheros de estado duplicados y divergentes.** Había copias viejas de
`user_prefs.json`, `memory.json` y `memory-long.json` en la raíz del proyecto, con
datos distintos de los reales. No llevaban nada especial y podían volver a
migrarse encima de los ajustes actuales si se borraba la carpeta de datos.

### Añadido

- **`.env.example`**: todas las variables de configuración en un solo fichero,
  comentadas y con valores vacíos. Antes había que leer el código para saber qué
  se podía cambiar por entorno.
- **Bloque `(Arranque)`**: al lanzar, la app dice con qué modelo de voz, qué API,
  si hay clave y con qué motor de búsqueda arranca. Solo presencia o ausencia de
  la clave, nunca su valor.
- **Aviso de calidad con DuckDuckGo**: sin clave de serper, la app avisa de que
  DuckDuckGo suele dar peores resultados y de que hay una alternativa gratuita.
- **Aviso si el modelo no responde**: si ningún modelo de la lista acepta la
  petición, se dice que la búsqueda web queda desactivada y por qué suele ser, en
  vez de fallar en silencio.
- **Sección «Por qué mi IA suena distinta a la del autor»** en el README, con las
  cinco causas reales y qué se puede hacer con cada una.
- **33 pruebas automáticas** nuevas (antes no había ninguna que se ejecutara en
  CI): 17 de clon limpio, 6 de ciclo de vida de voz y 13 de motores de búsqueda,
  estas últimas actualizadas al contrato nuevo.

### Corregido en la documentación

- El README prometía motores de búsqueda que el código no tenía (searxng,
  wikipedia, brave, tavily, serpapi). Ahora la lista coincide con el registro, y
  una prueba lo comprueba para que no vuelvan a divergir.
- Filas de configuración que describían variables que no se leían.
- Enlaces a releases e issues corregidos.

### Seguridad

- La clave de búsqueda web se guarda cifrada en `user_prefs.json`, fuera del
  repositorio, y ese fichero está en `.gitignore` desde siempre. Nunca se ha
  commiteado.
- El bloque de arranque no imprime el valor de ninguna clave.
- El control de secretos del build ahora detecta también claves hexadecimales de
  otros proveedores, no solo las de Gemini.

### Notas

- No hay cambios de formato ni de ubicación de la app: se actualiza encima.
- No hay migraciones de datos. Tus ajustes, memoria y transcripciones se
  mantienen tal cual.

[No publicado]: https://github.com/MindConall/mindvoice/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/MindConall/mindvoice/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/MindConall/mindvoice/releases/tag/v0.1.0