"""Pone el informe de graphify en español.

graphify no trae i18n: las cabeceras de GRAPH_REPORT.md y las frases de las
preguntas sugeridas son literales en graphify/report.py y graphify/analyze.py.
Este script las traduce DESPUÉS de que graphify genere el informe, y reaplica
los nombres de comunidad en español sobre graph.json.

Uso (desde la raíz del repo, después de cualquier `graphify update` / rebuild):

    python tools/informe_es.py            # traduce el informe y las etiquetas
    python tools/informe_es.py --check    # solo dice qué comunidades faltan

Los ids de comunidad son estables porque el hook de graphify fija PYTHONHASHSEED=0.
Si algún día cambia el particionado, `--check` avisa de los ids que noizó.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "graphify-out"
GRAPH = OUT / "graph.json"
REPORT = OUT / "GRAPH_REPORT.md"
LABELS_JSON = OUT / ".graphify_labels.json"

# Nombre en español de cada comunidad. La clave es el id que asigna louvain.
LABELS_ES: dict[int, str] = {
    0: "Entrada CLI y Ajustes",
    1: "Captura de Micrófono",
    2: "Almacén de Clave Cifrado",
    3: "Deuda de Arquitectura y Hilos",
    4: "GUI de Control (Tkinter)",
    5: "API Pública de LiveAssistant",
    6: "Registro de Motores Web",
    7: "Mapa del Proyecto y Modelo de Privacidad",
    8: "Orquestación de Memoria y Web",
    9: "Bucle de Sesión Live",
    10: "CI de Release y Deuda de Empaquetado",
    11: "Ventana HUD del Overlay",
    12: "Puente de Preferencias en Runtime",
    13: "Temporizadores y Acciones Locales",
    14: "Reglas de Diseño de LiveAssistant",
    15: "Reproducción de Audio PCM",
    16: "Parsers de Resultados de DuckDuckGo",
    17: "Lanzador de Hotkey Global",
    18: "Captura de Pantalla y JPEG",
    19: "Streaming de Turno de Voz y VAD",
    20: "Extracción de Consultas Web",
    21: "Rutas de Datos y Migración",
    22: "Triaje de Errores y Caché de Modelo",
    23: "Máquina de Estados Push-To-Talk",
    24: "Dependencias y Stack Técnico",
    25: "Brandmark del Logo Robot",
    26: "Claves de Config y Límites Conocidos",
    27: "Sesión de Visión y Connect Config",
    28: "Chat del Overlay y Toggle de Privacidad",
    29: "Renderizado del Panel HUD",
    30: "Widgets del Panel de Ajustes",
    31: "Script de Build de Release",
    32: "Cola de UI y Sinc de Dispositivos",
    33: "Flujo del Panel de Ajustes del Overlay",
    34: "Shim de Arranque y Accesos Directos",
    35: "Asset Base del Logo",
    36: "Fetcher de DuckDuckGo",
    37: "Branding Pixel Art",
    38: "Archivo de Memoria a Largo Plazo",
    39: "Input del Overlay y Comandos Slash",
    40: "Punto de Entrada del Instalador",
    41: "Enum de Estados del Asistente",
    42: "Helpers de Mensajes Live",
    43: "Hilo del Worker de Hotkey",
    44: "Hook de Teclado de Bajo Nivel",
    45: "Ruta del Fichero de Preferencias",
}

# Cabeceras y frases fijas de report.py / analyze.py. El orden importa: las
# cabeceras se sustituyen antes que las frases para que no se pisen.
TRADUCCIONES: list[tuple[str, str]] = [
    ("# Graph Report - ", "# Informe de Grafo - "),
    ("## Work-memory lessons", "## Lecciones de memoria de trabajo"),
    ("## Corpus Check", "## Chequeo del corpus"),
    ("## Summary", "## Resumen"),
    ("## Graph Freshness", "## Frescura del grafo"),
    ("## Community Hubs (Navigation)", "## Centros de comunidad (navegación)"),
    (
        "## God Nodes (most connected - your core abstractions)",
        "## Dioses (nodos más conectados — tus abstracciones centrales)",
    ),
    (
        "## Surprising Connections (you probably didn't know these)",
        "## Conexiones inesperadas (probablemente no las conocías)",
    ),
    ("## Import Cycles", "## Ciclos de importación"),
    ("## Hyperedges (group relationships)", "## Hiperaristas (relaciones de grupo)"),
    ("## Ambiguous Edges - Review These", "## Aristas ambiguas — revisa estas"),
    ("## Knowledge Gaps", "## Huecos de conocimiento"),
    ("## Suggested Questions", "## Preguntas sugeridas"),
    ("_Questions this graph is uniquely positioned to answer:_",
     "_Preguntas que este grafo está en posición única de responder:_"),
    ("- Verdict: corpus is large enough that graph structure adds value.",
     "- Veredicto: corpus lo bastante grande como para que la estructura del grafo aporte valor."),
    ("- Token cost:", "- Coste de tokens:"),
    ("- Extraction:", "- Extracción:"),
    ("- Unclassified:", "- Sin clasificar:"),
    ("file(s) not represented in", "fichero(s) sin representar en"),
    ("None detected.", "Ninguno detectado."),
    ("relation: ", "relación: "),
    ("has ", "tiene "),
    ("Edge tagged ", "Arista etiquetada "),
    ("confidence is low.", "confianza baja."),
    ("High betweenness centrality", "Centralidad de intermediación alta"),
    ("this node is a cross-community bridge.", "este nodo es un puente entre comunidades."),
    ("INFERRED edges - model-reasoned connections that need verification.",
     "aristas INFERRED — conexiones razonadas por modelo que necesitan verificación."),
    (" isolated node(s):**", " nodo(s) aislado(s):**"),
    ("isolated node(s):**", "nodo(s) aislado(s):**"),
    ("These have ≤1 connection", "Tienen ≤1 conexión"),
    ("(Counts symbols only;", "(Cuenta solo símbolos;"),
    ("node(s) total have ≤1 connection", "nodo(s) en total tienen ≤1 conexión"),
    ("when file, concept and rationale nodes are included.)",
     "cuando se incluyen nodos de fichero, concepto y rationale.)"),
    ("thin communities", "comunidades finas"),
    ("omitted from report", "omitidas del informe"),
    ("run `graphify query` to explore isolated nodes.",
     "usa `graphify query` para explorar los nodos aislados."),
    ("total, ", "en total, "),
    ("thin omitted", "finas omitidas"),
    ("thin community", "comunidad fina"),
    ("thin_count_summary", "thin_count_summary"),
    ("possible missing edges or undocumented components",
     "posibles aristas que faltan o componentes sin documentar"),
    ("possible missing edges", "posibles aristas que faltan"),
    (" more)", " más)"),
    # Métricas del resumen y de cada comunidad
    (" files · ~", " ficheros · ~"),
    (" words", " palabras"),
    (" nodes · ", " nodos · "),
    (" edges · ", " aristas · "),
    (" communities", " comunidades"),
    (" shown, ", " mostradas, "),
    ("<3 nodes)", "<3 nodos)"),
    ("EXTRACTED ·", "EXTRAÍDO ·"),
    ("INFERRED ·", "INFERIDO ·"),
    ("AMBIGUOUS", "AMBIGUO"),
    ("INFERRED: ", "INFERIDAS: "),
    (" edges (avg confidence: ", " aristas (confianza media: "),
]

# Sustituciones con expresión regular: respetan el formato exacto del markdown
# y solo tocan las líneas que interestan (preguntas sugeridas, aristas ambiguas).
_PATRONES: list[tuple["re.Pattern[str]", str]] = [
    (re.compile(r"^(\d+\. `[^`]+` - \d+) edges$", re.MULTILINE), r"\1 aristas"),
    (re.compile(r"^Cohesion: ", re.MULTILINE), "Cohesión: "),
    (re.compile(r"^Nodes \((\d+)\):", re.MULTILINE), r"Nodos (\1):"),
    (re.compile(r"\[EXTRACTED\]"), "[EXTRAÍDO]"),
    (re.compile(r"\[INFERRED\]"), "[INFERIDO]"),
    (re.compile(r"^(- \*\*)What is the exact relationship between (`[^`]+`) and (`[^`]+`)\?\*\*$",
                re.MULTILINE),
     r"\1Cuál es la relación exacta entre \2 y \3?**"),
    (re.compile(r"^(- \*\*)Why does (`[^`]+`) connect (.+) to (.+)\?\*\*$", re.MULTILINE),
     r"\1Por qué conecta \2 con \4?**"),
    (re.compile(r"^(- \*\*)Are the (\d+) inferred relationships involving (`[^`]+`) "
                r"\(e\.g\. with (.+) and (.+)\) actually correct\?\*\*$", re.MULTILINE),
     r"\1¿Son correctas las \2 relaciones inferidas que involucran \3 (p. ej. con \4 y \5)?**"),
    (re.compile(r"^(- \*\*)What connects (.+) to the rest of the system\?\*\*$", re.MULTILINE),
     r"\1Qué conecta \2 con el resto del sistema?**"),
    (re.compile(r"^(\s+)_Arista etiquetada (AMBIGUO|AMBIGUOUS) \(relación: ([a-z_]+)\) - confianza baja\._$",
                re.MULTILINE),
     r"\1_Arista etiquetada \2 (relación: \3) — confianza baja._"),
]


def traducir_markdown(texto: str) -> str:
    for viejo, nuevo in TRADUCCIONES:
        texto = texto.replace(viejo, nuevo)
    for patron, nuevo in _PATRONES:
        texto = patron.sub(nuevo, texto)
    # `### Community N - "label"` -> `### Comunidad N - "label"`
    texto = re.sub(r"^### Community (\d+) - ", r"### Comunidad \1 - ", texto, flags=re.MULTILINE)
    # `## Communities (46 total, ...)` con el orden ya traducido
    texto = re.sub(r"^## Communities \((\d+) en total", r"## Comunidades (\1 en total", texto, flags=re.MULTILINE)
    return texto


def aplicar_etiquetas(dry_run: bool = False) -> tuple[list[int], list[int], int]:
    if not GRAPH.exists():
        print("No existe graph.json — ejecuta el pipeline de graphify primero.")
        sys.exit(1)
    grafo = json.loads(GRAPH.read_text(encoding="utf-8"))
    ids_presentes = {int(n["community"]) for n in grafo["nodes"] if n.get("community") is not None}
    faltan = sorted(i for i in LABELS_ES if i not in ids_presentes)
    sobran = sorted(i for i in ids_presentes if i not in LABELS_ES)

    cambiados = 0
    for nodo in grafo["nodes"]:
        cid = nodo.get("community")
        if cid is None:
            continue
        nuevo = LABELS_ES.get(int(cid))
        if nuevo and nodo.get("community_name") != nuevo:
            nodo["community_name"] = nuevo
            cambiados += 1

    if not dry_run and cambiados:
        GRAPH.write_text(json.dumps(grafo, ensure_ascii=False, indent=1), encoding="utf-8")
        LABELS_JSON.write_text(
            json.dumps({str(k): v for k, v in LABELS_ES.items()}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return faltan, sobran, cambiados


def main() -> None:
    dry = "--check" in sys.argv
    faltan, sobran, cambiados = aplicar_etiquetas(dry_run=dry)

    if not REPORT.exists():
        print("No existe GRAPH_REPORT.md — ejecuta el pipeline de graphify primero.")
        sys.exit(1)

    if dry:
        print(f"Comunidades en graph.json: {len(LABELS_ES)} definidas")
        if faltan:
            print(f"  AVISO: ids definidos que ya no existen en el grafo: {faltan}")
        if sobran:
            print(f"  AVISO: comunidades nuevas sin nombre en español: {sobran}")
            print("         Añádelas a LABELS_ES en este fichero.")
        print("Nada escrito (--check).")
        return

    original = REPORT.read_text(encoding="utf-8")
    traducido = traducir_markdown(original)
    aplicadas = len({v for v, _ in TRADUCCIONES if v in original})
    if traducido != original:
        REPORT.write_text(traducido, encoding="utf-8")
        print(f"GRAPH_REPORT.md traducido ({aplicadas} literales).")
    else:
        print("GRAPH_REPORT.md ya estaba en español; sin cambios.")
    print(f"Etiquetas de comunidad en español aplicadas: {cambiados} nodos.")
    if faltan or sobran:
        print(f"  AVISO: sin nombre en español -> definidos inexistentes {faltan}, nuevos {sobran}")


if __name__ == "__main__":
    main()
