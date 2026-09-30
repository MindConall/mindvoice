"""Regenera GRAPH_REPORT.md desde graph.json con las etiquetas españolas.

No re-extrae nada: reconstruye el grafo desde graph.json, recalcula gods y
sorpresas, y llama a report.generate con LABELS_ES para que la lista de centros
y las cabeceras de comunidad salgan nativas en español. Lo que graphify tiene
hardcodeado en inglés (resumen, preguntas sugeridas) lo traduce después
tools/informe_es.py.
"""

import json
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from informe_es import LABELS_ES  # noqa: E402

from graphify.analyze import god_nodes, surprising_connections, suggest_questions  # noqa: E402
from graphify.report import generate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "graphify-out"
gpath = OUT / "graph.json"

data = json.loads(gpath.read_text(encoding="utf-8"))

G = nx.Graph()
for n in data["nodes"]:
    attrs = {k: v for k, v in n.items() if k != "id"}
    G.add_node(n["id"], **attrs)
for e in data["links"]:
    if e["source"] in G and e["target"] in G:
        attrs = {k: v for k, v in e.items() if k not in ("source", "target")}
        G.add_edge(e["source"], e["target"], **attrs)

# Comunidades ya calculadas: las lee de los nodos, no re-agrupa (mismo particionado).
communities: dict[int, list[str]] = {}
for n in data["nodes"]:
    cid = n.get("community")
    if cid is not None:
        communities.setdefault(int(cid), []).append(n["id"])

# Cohesión interna de cada comunidad (lo que usa el informe).
from graphify.cluster import score_all  # noqa: E402

try:
    cohesion = score_all(G, communities)
except Exception as exc:  # pragma: no cover
    print(f"score_all fallo ({exc}); uso cohesión 0.0", file=sys.stderr)
    cohesion = {cid: 0.0 for cid in communities}

# Reconstruye el bloque de detección para la sección "Chequeo del corpus".
por_tipo: dict[str, list[str]] = {}
for n in data["nodes"]:
    sf = n.get("source_file") or ""
    if not sf:
        continue
    ft = n.get("file_type") or "code"
    por_tipo.setdefault(ft, [])
    if sf not in por_tipo[ft]:
        por_tipo[ft].append(sf)
n_ficheros = len({n.get("source_file") for n in data["nodes"] if n.get("source_file")})
detection = {
    "files": {k: v for k, v in por_tipo.items()},
    "total_files": n_ficheros,
    # detect() no se conserva entre builds; este es el valor medido del último
    # detect completo sobre los 27 ficheros del corpus.
    "total_words": 57697,
    "unclassified": [],
}

gods = god_nodes(G)
surprises = surprising_connections(G, communities)
tokens = {"input": 19000, "output": 41000}  # último build, de cost.json

questions = suggest_questions(G, communities, LABELS_ES)
report = generate(G, communities, cohesion, LABELS_ES, gods, surprises, detection, tokens,
                  str(ROOT), suggested_questions=questions)
(OUT / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
print(f"GRAPH_REPORT.md regenerado con {len(LABELS_ES)} etiquetas en español.")
