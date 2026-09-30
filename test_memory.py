"""Tests del paquete de memoria.

La garantía más importante de la Fase 1 es negativa: sin grafo, el bloque de
memoria tiene que ser EXACTAMENTE el de antes. Ese test compara contra una copia
del renderizado original, no contra una expectation escrita a mano.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from memory.base import (
    CONTESTED,
    ETIQUETAS_MAX,
    EXTRACTED,
    INFERRED,
    autoetiquetas,
    normalizar,
    palabras,
    recortar,
)
from memory.flat_backend import MAX_BRIEF, MAX_PERMANENT, FlatBackend
from memory.graph_backend import GraphBackend
from memory.migrate import get_backend, grafo_disponible, migrar


class BaseTemporal(unittest.TestCase):
    """Cada test con sus propios archivos: nada toca la memoria real."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mv-mem-")
        self.brief = os.path.join(self.tmp, "memory.json")
        self.perman = os.path.join(self.tmp, "memory-long.json")
        os.environ.pop("MINDVOICE_MEMORY", None)
        os.environ.pop("MINDVOICE_MEMORY_GRAPH", None)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MINDVOICE_MEMORY", None)
        os.environ.pop("MINDVOICE_MEMORY_GRAPH", None)

    def _escribir(self, ruta, datos):
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False)


# --------------------------------------------------------------- utilidades
class TestUtilidades(unittest.TestCase):
    def test_normalizar_quita_acentos_y_mayusculas(self) -> None:
        self.assertEqual(normalizar("Acción Rápida"), "accion rapida")

    def test_palabras_tira_vacias_y_cortas(self) -> None:
        resultado = palabras("El perro de Juan come y habla conerton")
        self.assertIn("perro", resultado)
        self.assertIn("juan", resultado)
        self.assertNotIn("el", resultado)
        self.assertNotIn("y", resultado)

    def test_recortar_mete_elpisis(self) -> None:
        self.assertEqual(recortar("hola", 10), "hola")
        largo = "x" * 50
        self.assertTrue(recortar(largo, 10).endswith("…"))
        self.assertEqual(len(recortar(largo, 10)), 11)

    def test_autoetiquetas_sacan_los_terminos(self) -> None:
        etiquetas = autoetiquetas("Tengo un perro que se llama Nube en casa")
        self.assertIn("perro", etiquetas)
        self.assertIn("nube", etiquetas)

    def test_autoetiquetas_ignoran_vacias_cortas_y_numeros(self) -> None:
        etiquetas = autoetiquetas("el de la con 2026 sobre una mesa")
        self.assertNotIn("2026", etiquetas)
        for e in etiquetas:
            self.assertGreaterEqual(len(e), 5)

    def test_autoetiquetas_no_repiten(self) -> None:
        etiquetas = autoetiquetas("perro perro perro gato perro")
        self.assertEqual(len(etiquetas), len(set(etiquetas)))

    def test_autoetiquetas_tienen_tope(self) -> None:
        texto = " ".join(f"palabra{i}x" for i in range(50))
        self.assertLessEqual(len(autoetiquetas(texto)), ETIQUETAS_MAX)

    def test_autoetiquetas_cogen_nombres_propios_cortos(self) -> None:
        # "Nube" no llega al mínimo de longitud, pero es justo el tipo de
        # término que hace falta para recuperar el recuerdo.
        etiquetas = autoetiquetas("Tengo un perro que se llama Nube")
        self.assertIn("nube", etiquetas)
        etiquetas2 = autoetiquetas("Ana programa en Python todos los dias")
        self.assertIn("ana", etiquetas2)
        self.assertIn("python", etiquetas2)


# ------------------------------------------------------------------- plano
class TestPlano(BaseTemporal):
    def test_lectura_de_los_dos_archivos(self) -> None:
        self._escribir(self.brief, [{"role": "user", "text": "me llamo Ana"}])
        self._escribir(self.perman, ["Ana trabaja de noche"])
        plano = FlatBackend(self.brief, self.perman)
        self.assertEqual(len(plano._brief), 1)
        self.assertEqual(len(plano._permanent), 1)
        self.assertEqual(plano._brief[0].text, "me llamo Ana")

    def test_filtra_entradas_invalidas(self) -> None:
        self._escribir(
            self.brief,
            [
                {"role": "user", "text": "buena"},
                {"role": "pirata", "text": "rol inválido"},
                {"role": "user", "text": "   "},
                {"role": "user", "text": 42},
                "no soy un dict",
            ],
        )
        plano = FlatBackend(self.brief, self.perman)
        self.assertEqual([e.text for e in plano._brief], ["buena"])

    def test_archivo_corrupto_no_rompe(self) -> None:
        with open(self.brief, "w", encoding="utf-8") as fh:
            fh.write("{ esto no es json")
        plano = FlatBackend(self.brief, self.perman)  # no debe lanzar
        self.assertEqual(plano._brief, [])

    def test_texto_vacio_no_se_guarda(self) -> None:
        plano = FlatBackend(self.brief, self.perman)
        self.assertIsNone(plano.remember("   "))
        self.assertEqual(plano._brief, [])

    def test_desborde_saca_los_viejos(self) -> None:
        plano = FlatBackend(self.brief, self.perman)
        for i in range(MAX_BRIEF + 5):
            plano.remember(f"recuerdo {i}")
        self.assertEqual(len(plano._brief), MAX_BRIEF)
        self.assertEqual(plano._brief[0].text, "recuerdo 5")
        # Lo sacado de más tiene que haber quedado en el archivo.
        with open(self.brief, encoding="utf-8") as fh:
            guardado = json.load(fh)
        self.assertEqual(len(guardado), MAX_BRIEF)

    def test_el_bloque_reproduce_el_formato_original(self) -> None:
        """El contrato 'sin grafo, igual que antes'."""
        plano = FlatBackend(self.brief, self.perman)
        plano.remember("me llamo Ana", role="user")
        plano.remember("hola Ana", role="assistant")
        plano.archivar([type("E", (), {"text": "Ana trabaja de noche"})()])
        bloque = plano.block()
        self.assertIn("[A largo plazo (hechos persistentes resumidos)]", bloque)
        self.assertIn("[Breve (conversación reciente)]", bloque)
        self.assertIn("- usuario: me llamo Ana", bloque)
        self.assertIn("- MindVoice: hola Ana", bloque)
        self.assertIn("- Ana trabaja de noche", bloque)
        # El orden es el del original: larga plazo primero.
        self.assertLess(
            bloque.index("[A largo plazo"), bloque.index("[Breve")
        )

    def test_el_bloque_respeta_el_presupuesto(self) -> None:
        plano = FlatBackend(self.brief, self.perman)
        for i in range(20):
            plano.remember("z" * 400 + f" {i}")
        bloque = plano.block(presupuesto=1500)
        seccion = bloque.split("[Breve")[1]
        self.assertLessEqual(len(seccion), 1500 + 200)

    def test_bloque_vacio_si_no_hay_nada(self) -> None:
        self.assertEqual(FlatBackend(self.brief, self.perman).block(), "")

    def test_olvidar_y_reset(self) -> None:
        plano = FlatBackend(self.brief, self.perman)
        e = plano.remember("secreto de Ana")
        self.assertTrue(plano.forget(e.id))
        self.assertFalse(plano.forget("no-existe"))
        plano.remember("otro")
        plano.reset()
        self.assertEqual(plano._brief, [])


# ------------------------------------------------------------------- grafo
class TestGrafo(BaseTemporal):
    def backend(self) -> GraphBackend:
        return GraphBackend(self.tmp)

    def test_arranca_vacio_y_sobrevive_al_reinicio(self) -> None:
        g = self.backend()
        self.assertEqual(len(g._grafo["nodes"]), 0)
        g.remember("Ana tiene un perro llamado Nube", role="user")
        otro = GraphBackend(self.tmp)  # relee del disco
        textos = [n.get("description") for n in otro._grafo["nodes"]]
        self.assertIn("Ana tiene un perro llamado Nube", textos)

    def test_el_esquema_es_el_de_graphify(self) -> None:
        """Las claves del nodo tienen que ser las que Graphify ya usa."""
        g = self.backend()
        g.remember("Ana trabaja de noche")
        nodo = g._grafo["nodes"][0]
        for clave in ("id", "label", "norm_label", "file_type", "_origin", "source_file"):
            self.assertIn(clave, nodo)
        self.assertEqual(nodo["file_type"], "document")
        self.assertIn("description", nodo)

    def test_las_aristas_usan_el_formato_de_graphify(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro", tags=["perro"])
        self.assertTrue(g._grafo["links"])
        arista = g._grafo["links"][0]
        for clave in ("source", "target", "relation", "confidence", "confidence_score", "weight"):
            self.assertIn(clave, arista)
        self.assertIn(arista["confidence"], (EXTRACTED, INFERRED, CONTESTED))

    def test_una_etiqueta_crea_nodo_y_conecta(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro", tags=["perro"])
        g.remember("el perro se llama Nube", tags=["perro"])
        etiquetas = [n for n in g._grafo["nodes"] if n.get("kind") == "tag"]
        self.assertEqual(len(etiquetas), 1, "la segunda vez debe reutilizar la etiqueta")
        # Los dos recuerdos tienen que quedar unidos a través de la etiqueta.
        self.assertEqual(len(g._grafo["links"]), 2)
        self.assertTrue(any(len(vecinos) >= 2 for vecinos in g._adj.values()))

    def test_la_recuperacion_filtra_por_relevancia(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro llamado Nube", tags=["perro"])
        g.remember("Ana swims in the pool every morning at six", tags=["deporte"])
        g.remember("el coche de Ana es un Renault Clio", tags=["coche"])
        resultado = g.retrieve("¿qué perro tiene Ana?", limite=2)
        self.assertTrue(resultado)
        self.assertIn("perro", resultado[0].text)

    def test_sin_coincidencia_no_inventa(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro llamado Nube")
        # No debe devolver recuerdos que no existen ni inventar ninguno...
        for e in g.retrieve("¿cuánto cuesta el oro en Tokio?", limite=5):
            self.assertIn("perro", e.text)
        # ...pero con un solo recuerdo guardado solo puede devolver ese: el
        # suelo de seguridad rellena con lo que hay, no con aire.
        self.assertEqual(len(g.retrieve("¿cuánto cuesta el oro?", limite=5)), 1)

    def test_nunca_devuelve_menos_del_minimo(self) -> None:
        g = self.backend()
        for i in range(10):
            g.remember(f"dato distinto número {i} sin nada que ver", importance=0.1)
        for pregunta in ("¿dónde está Cairo?", "", "xyz", "¿qué hora es?"):
            with self.subTest(pregunta=pregunta):
                self.assertGreaterEqual(len(g.retrieve(pregunta, limite=8)), 3)

    def test_sin_etiquetas_se_sacan_del_texto(self) -> None:
        """Sin esto el grafo sería una bolsa de nodos: no habría relaciones."""
        g = self.backend()
        g.remember("Tengo un perro que se llama Nube")
        self.assertTrue(
            g._grafo["links"], "recordar sin etiquetas debe crear aristas igualmente"
        )
        self.assertTrue(any(n.get("kind") == "tag" for n in g._grafo["nodes"]))

    def test_recuerdos_que_comparten_tema_quedan_unidos(self) -> None:
        g = self.backend()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("El perro come bien y no muerde")
        # Los dos recuerdos comparten la etiqueta 'perro': hay un camino entre
        # ellos aunque en ninguno se mencione el nombre del otro.
        Perro = "tag_perro"
        self.assertIn(Perro, g._idx)
        vecinos = {v for v, _ in g._adj.get(Perro, [])}
        self.assertEqual(len(vecinos), 2)

    def test_la_recuperacion_por_tema_alcanza_a_todos(self) -> None:
        g = self.backend()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("El perro come bien y no muerde")
        g.remember("El coche es un Renault Clio")
        encontrados = [e.text for e in g.retrieve("¿y el perro?", limite=8)]
        self.assertTrue(any("Nube" in t for t in encontrados))
        self.assertTrue(any("no muerde" in t for t in encontrados))

    def test_el_bloque_no_queda_vacio_con_pregunta(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro llamado Nube")
        for pregunta in ("¿cuánto cuesta el oro?", "¿qué hora es?", "xyz"):
            with self.subTest(pregunta=pregunta):
                self.assertTrue(g.block(pregunta).strip())

    def test_una_sola_entrada_no_inventa_el_minimo(self) -> None:
        """Si no hay tanto, se manda lo que hay; no se rellena con aire."""
        g = self.backend()
        g.remember("Ana tiene un perro")
        self.assertEqual(len(g.retrieve("¿qué hora es?", limite=8)), 1)

    def test_lo_usado_sube_de_puntuacion(self) -> None:
        g = self.backend()
        g.remember("el perro de Ana se llama Nube")
        g.remember("Ana vive en Zaragoza")
        primero = g.retrieve("perro")[0]
        self.assertIn("perro", primero.text)
        # Un uso cuenta como refuerzo, y `hits` queda registrado.
        nodo = next(n for n in g._grafo["nodes"] if "perro" in n["description"])
        self.assertGreaterEqual(nodo["hits"], 1)

    def test_contested_se_hunde(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro llamado Nube", importance=0.5)
        g.remember("Ana tiene un gato llamado Bigotes", importance=0.5)
        g.retrieve("mascota")
        objetivo = [n for n in g._grafo["nodes"] if "perro" in n["description"]][0]
        g.marcar_disputa(objetivo["id"], True)
        self.assertTrue(
            next(n for n in g._grafo["nodes"] if "perro" in n["description"])["contested"]
        )
        # El recuerdo en disputa sigue en el grafo: se marca, no se borra.
        self.assertTrue(any("perro" in n["description"] for n in g._grafo["nodes"]))

    def test_olvidar_borra_el_recuerdo_pero_no_la_etiqueta(self) -> None:
        """Olvidar un recuerdo no puede raspar el hilo compartido con otros."""
        g = self.backend()
        e = g.remember("secreto de Ana", tags=["secreto"])
        self.assertTrue(g.forget(e.id))
        self.assertFalse(g.forget(e.id))
        # El recuerdo y sus aristas fuera...
        self.assertEqual([n for n in g._grafo["nodes"] if n.get("kind") != "tag"], [])
        self.assertEqual(g._grafo["links"], [])
        # ...pero la etiqueta se queda, porque otros recuerdos la comparten.
        self.assertEqual([n["id"] for n in g._grafo["nodes"]], ["tag_secreto"])

    def test_una_etiqueta_no_se_puede_olvidar_como_recuerdo(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro", tags=["perro"])
        etiqueta = next(n["id"] for n in g._grafo["nodes"] if n.get("kind") == "tag")
        self.assertFalse(g.forget(etiqueta))

    def test_reset_conserva_las_etiquetas(self) -> None:
        g = self.backend()
        g.remember("Ana tiene un perro", tags=["perro"])
        g.reset()
        self.assertEqual([n for n in g._grafo["nodes"] if n.get("kind") != "tag"], [])

    def test_grafo_corrupto_no_rompe_ni_borra(self) -> None:
        with open(os.path.join(self.tmp, "knowledge_graph.json"), "w", encoding="utf-8") as fh:
            fh.write("{{{ roto")
        g = self.backend()
        self.assertEqual(g._grafo["nodes"], [])
        self.assertTrue(g._degradado)
        # El archivo roto sigue ahí: no se toca lo del usuario.
        with open(os.path.join(self.tmp, "knowledge_graph.json"), encoding="utf-8") as fh:
            self.assertIn("roto", fh.read())

    def test_escritura_atomica_no_deja_temporales(self) -> None:
        g = self.backend()
        g.remember("Ana")
        self.assertFalse([f for f in os.listdir(self.tmp) if f.endswith(".tmp")])

    def test_el_bloque_del_grafo_respeta_el_formato(self) -> None:
        g = self.backend()
        g.remember("me llamo Ana", role="user")
        g.remember("hola", role="assistant", kind="permanent")
        bloque = g.block()
        self.assertIn("[Breve (conversación reciente)]", bloque)
        self.assertIn("- usuario: me llamo Ana", bloque)

    def test_el_bloque_es_mas_corto_que_el_volcado_completo(self) -> None:
        """La promesa de la fase: menos tokens, no más."""
        g = self.backend()
        for i in range(24):
            g.remember(f"recuerdo {i}: Ana habló del tema número {i} en detalle", importance=0.5)
        pregunta = "¿qué hizo Ana con el tema 7?"
        especifico = g.block(pregunta, presupuesto=1500)
        completo = "\n".join(n["description"] for n in g._grafo["nodes"])
        self.assertLess(len(especifico), len(completo))


# --------------------------------------------------------- elección y migración
class TestEleccion(BaseTemporal):
    def test_sin_fuerzar_se_elige_el_grafo_si_cabe(self) -> None:
        b = get_backend(self.tmp, self.brief, self.perman)
        self.assertEqual(b.nombre, "grafo")

    def test_fuerzar_plano_manda(self) -> None:
        os.environ["MINDVOICE_MEMORY"] = "flat"
        b = get_backend(self.tmp, self.brief, self.perman)
        self.assertEqual(b.nombre, "plano")

    def test_fuerzar_grafo_manda(self) -> None:
        os.environ["MINDVOICE_MEMORY"] = "graph"
        b = get_backend(self.tmp, self.brief, self.perman)
        self.assertEqual(b.nombre, "grafo")

    def test_valor_invalido_no_rompe(self) -> None:
        os.environ["MINDVOICE_MEMORY"] = "inventado"
        b = get_backend(self.tmp, self.brief, self.perman)
        self.assertIn(b.nombre, ("plano", "grafo"))

    def test_directorio_no_escribible_cae_a_plano(self) -> None:
        # Un archivo donde debería ir el directorio: no se puede crear, y sin
        # grafo la memoria tiene que seguir funcionando en plano.
        ocupado = os.path.join(self.tmp, "ocupado")
        with open(ocupado, "w", encoding="utf-8") as fh:
            fh.write("soy un archivo, no un directorio")
        os.environ["MINDVOICE_MEMORY_GRAPH"] = os.path.join(ocupado, "sub")
        b = get_backend(self.tmp, self.brief, self.perman)
        self.assertEqual(b.nombre, "plano")

    def test_migracion_copia_y_no_borra_los_json(self) -> None:
        self._escribir(self.brief, [{"role": "user", "text": "me llamo Ana"}])
        self._escribir(self.perman, ["Ana trabaja de noche"])
        g = GraphBackend(self.tmp)
        resultado = migrar(g, self.brief, self.perman)
        self.assertEqual(resultado["estado"], "migrado")
        self.assertEqual(resultado["insertados"], 2)
        self.assertTrue(os.path.exists(self.brief), "el JSON original debe seguir ahí")
        self.assertTrue(os.path.exists(self.perman))
        textos = [n.get("description") for n in g._grafo["nodes"]]
        self.assertIn("me llamo Ana", textos)
        self.assertIn("Ana trabaja de noche", textos)

    def test_migrar_dos_veces_no_duplica(self) -> None:
        self._escribir(self.brief, [{"role": "user", "text": "me llamo Ana"}])
        g = GraphBackend(self.tmp)
        migrar(g, self.brief, self.perman)
        antes = len(g._grafo["nodes"])
        resultado = migrar(g, self.brief, self.perman)
        self.assertEqual(resultado["estado"], "sin cambios")
        self.assertEqual(len(g._grafo["nodes"]), antes)

    def test_migrar_en_plano_no_hace_nada(self) -> None:
        plano = FlatBackend(self.brief, self.perman)
        resultado = migrar(plano, self.brief, self.perman)
        self.assertEqual(resultado["estado"], "plano")


class TestGrafoVsPlano(BaseTemporal):
    def test_el_grafo_recupera_y_el_plano_no_puede(self) -> None:
        """La diferencia funcional que justifica la fase."""
        datos = [{"role": "user", "text": t} for t in (
            "Ana tiene un perro llamado Nube",
            "Ana swims every morning at six",
            "el coche de Ana es un Renault Clio",
        )]
        self._escribir(self.brief, datos)
        pregunta = "¿cómo se llama el perro de Ana?"

        plano = FlatBackend(self.brief, self.perman)
        grafo = GraphBackend(self.tmp)
        migrar(grafo, self.brief, self.perman)

        del_grafo = grafo.retrieve(pregunta, limite=1)
        self.assertTrue(del_grafo)
        self.assertIn("perro", del_grafo[0].text)
        # El plano no busca: devuelve la ventana fija.
        self.assertEqual(len(plano.retrieve(pregunta, limite=1)), 1)


class TestAuditoria(BaseTemporal):
    """``buscar`` / ``forget_matching`` / ``en_disputa`` en los dos backends.

    La misma pregunta tiene que dar la misma respuesta con y sin grafo. No con
    la misma precisión —el grafo busca por etiquetas y el plano por palabras—
    pero sí con el mismo contrato, que es lo que permite que el motor no sepa
    cuál tiene delante.
    """

    RECUERDOS = (
        "Ana tiene un perro llamado Nube",
        "el proyecto de la tienda va con Python",
        "Ana swims every morning at six",
        "el coche de Ana es un Renault Clio",
    )

    def _plano(self) -> FlatBackend:
        self._escribir(self.brief, [{"role": "user", "text": t} for t in self.RECUERDOS])
        return FlatBackend(self.brief, self.perman)

    def _grafo(self) -> GraphBackend:
        g = GraphBackend(self.tmp)
        for t in self.RECUERDOS:
            g.remember(t)
        return g

    def test_buscar_encuentra_lo_relevante_en_ambos(self) -> None:
        for backend in (self._plano(), self._grafo()):
            with self.subTest(backend=backend.nombre):
                encontrados = backend.buscar("perro de Ana", limite=3)
                self.assertTrue(encontrados)
                self.assertIn("perro", encontrados[0].text)

    def test_buscar_sin_coincidencia_devuelve_vacio(self) -> None:
        for backend in (self._plano(), self._grafo()):
            with self.subTest(backend=backend.nombre):
                self.assertEqual(backend.buscar("zafiro watch", limite=3), [])

    def test_buscar_texto_vacio_no_rompe(self) -> None:
        for backend in (self._plano(), self._grafo()):
            with self.subTest(backend=backend.nombre):
                self.assertEqual(backend.buscar("", limite=3), [])
                self.assertEqual(backend.buscar("   ", limite=3), [])

    def test_olvidar_un_tema_solo_borra_ese_tema(self) -> None:
        """El "olvida" fino: se va el perro y se queda el proyecto."""
        for backend in (self._plano(), self._grafo()):
            with self.subTest(backend=backend.nombre):
                borrados = backend.forget_matching("el perro", limite=12)
                self.assertTrue(borrados)
                self.assertTrue(any("perro" in t for t in borrados))
                lo_que_queda = " ".join(e.text for e in backend.buscar("proyecto Python", limite=5))
                self.assertIn("Python", lo_que_queda)

    def test_olvidar_lo_que_no_hay_no_hace_nada(self) -> None:
        for backend in (self._plano(), self._grafo()):
            with self.subTest(backend=backend.nombre):
                self.assertEqual(backend.forget_matching("avión depapado", limite=5), [])

    def test_olvidar_deja_la_memoria_usable(self) -> None:
        """No basta con que borre: tiene que seguir contestando."""
        for backend in (self._plano(), self._grafo()):
            with self.subTest(backend=backend.nombre):
                backend.forget_matching("el perro", limite=12)
                self.assertTrue(backend.block("hola"))

    def test_el_grafo_marca_y_lista_las_disputas(self) -> None:
        g = self._grafo()
        self.assertEqual(g.en_disputa(), [])
        nodos = [n for n in g._grafo["nodes"] if n.get("kind") == "brief"]
        g.marcar_disputa(nodos[0]["id"], True)
        self.assertEqual(len(g.en_disputa()), 1)
        self.assertEqual(g.en_disputa()[0].contested, True)

    def test_el_plano_no_tiene_disputas_nunca(self) -> None:
        """El plano no sabe de disputas: contesta vacío en vez de inventar estado."""
        self.assertEqual(self._plano().en_disputa(), [])

    def test_el_bloque_del_grafo_dice_de_donde_sale_cada_dato(self) -> None:
        g = self._grafo()
        bloque = g.block("perro de Ana")
        self.assertIn("tú lo dijiste", bloque)

    def test_el_bloque_saca_los_disputos_a_su_seccion(self) -> None:
        g = self._grafo()
        nodos = [n for n in g._grafo["nodes"] if n.get("kind") == "brief"]
        g.marcar_disputa(nodos[0]["id"], True)
        bloque = g.block("perro de Ana")
        self.assertIn("En disputa", bloque)
        self.assertIn("pregunta al usuario", bloque)

    def test_un_disputado_no_aparece_dos_veces(self) -> None:
        """Repetir la misma línea con dos instrucciones hace que gane la primera.

        La sección de disputa lleva "no elijas una", y la lista normal lleva el
        dato seco. Si el recuerdo sale en las dos, el modelo lee dos veces lo
        mismo y decide con la instrucción que se encuentre primero.
        """
        g = self._grafo()
        nodos = [n for n in g._grafo["nodes"] if n.get("kind") == "brief"]
        g.marcar_disputa(nodos[0]["id"], True)
        bloque = g.block("perro de Ana")
        self.assertEqual(bloque.count("perro llamado Nube"), 1)
        # Y lo que no está en disputa sigue apareciendo con su procedencia.
        self.assertIn("Python", bloque)
        self.assertIn("tú lo dijiste", bloque)


class TestRendimientoDeRecuperacion(unittest.TestCase):
    """Recuperar por pregunta tiene que ser barato, no solo correcto.

    Dos fallos reales que solo se ven midiendo:

    1. ``retrieve`` escribía TODO el grafo a disco en cada llamada, solo para
       subir un contador de refuerzo (con 400 recuerdos: ~36 ms).
    2. ``palabras()`` se recalculaba para cada nodo en cada recuperación
       (80.000 normalizaciones por pregunta).

    Juntas daban 98 ms por recuperación. Con memoria real, eso es casi 100 ms de
    parada antes de poder enviar la orden.
    """

    def _grafo(self, tmp: str, n: int = 400):
        from memory.graph_backend import GraphBackend

        g = GraphBackend(tmp)
        for i in range(n):
            g.remember(
                f"el usuario hablo del tema numero {i} con su colega "
                f"en la reunion {i % 20}",
                role="user",
            )
        return g

    def test_recuperar_no_escribe_a_disco(self) -> None:
        """Una lectura no puede reescribir el archivo entero."""
        with tempfile.TemporaryDirectory() as tmp:
            g = self._grafo(tmp, n=50)
            antes = os.path.getmtime(g._ruta)

            g.retrieve("¿qué tema?")

            self.assertTrue(
                g._pendiente,
                "recuperar debería aplazar la escritura, no hacerla",
            )
            self.assertEqual(
                os.path.getmtime(g._ruta),
                antes,
                "el archivo se reescribió durante una recuperación",
            )

    def test_los_contadores_se_vuelcan_cuando_se_piden(self) -> None:
        """Lo aplazado tiene que llegar al disco cuando toca."""
        with tempfile.TemporaryDirectory() as tmp:
            g = self._grafo(tmp, n=20)
            g.retrieve("¿qué tema?")
            self.assertTrue(g._pendiente)

            hubo = g._vaciar_pendientes()

            self.assertTrue(hubo, "no había nada pendiente")
            self.assertFalse(g._pendiente, "sigue pendiente tras volcar")
            with open(g._ruta, encoding="utf-8") as fh:
                self.assertIsInstance(json.load(fh), dict)

    def test_recuperar_es_rapido_tras_calentar(self) -> None:
        """Con memoria grande, recuperar no puede comerse un frame."""
        with tempfile.TemporaryDirectory() as tmp:
            g = self._grafo(tmp, n=400)
            g.block("tema")  # calentar cachés

            t0 = time.perf_counter()
            for _ in range(20):
                g.block("¿qué tema?")
            ms = (time.perf_counter() - t0) / 20 * 1000

            self.assertLess(
                ms, 16.67,
                f"recuperar cuesta {ms:.2f} ms: se pasa del presupuesto de un frame",
            )

    def test_la_cache_de_palabras_no_altera_el_resultado(self) -> None:
        """La caché es una optimización, no un cambio de comportamiento."""
        from memory.base import palabras

        texto = "¿Qué necesita mi perro Luna para el parque?"
        primero = palabras(texto)
        segundo = palabras(texto)
        self.assertEqual(primero, segundo)
        # Y sigue filtrando igual que antes: sin palabras vacías ni cortas.
        self.assertIn("perro", primero)
        self.assertIn("luna", primero)
        self.assertNotIn("para", primero)

    def test_la_cache_se_vacia_al_llenarse(self) -> None:
        """Una sesión larga no puede hacer crecer la memoria sin límite."""
        from memory import base

        base._CACHE_PALABRAS.clear()
        for i in range(base._CACHE_PALABRAS_MAX + 50):
            base.palabras(f"texto de prueba numero {i} con palabras")
        self.assertLessEqual(
            len(base._CACHE_PALABRAS),
            base._CACHE_PALABRAS_MAX,
            "la caché creció más de lo permitido",
        )


class TestPodaDeLaMemoria(unittest.TestCase):
    """La memoria tiene que dejar de crecer sola.

    El grafo solo sumanba: cada recuerdo reescribe el fichero entero y nada
    olvidaba nada. Medido en la app real: 4 MB y 1418 nodos, con el coste de
    reescritura dentro del camino crítico de cada turno. ``podar`` es lo que
    hace bounded eso, así que se prueba con las dos reglas: por volumen y por
    antigüedad, y sobre todo que lo que se protege NO se toca.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="mv-poda-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.b = GraphBackend(self.dir)

    def _llenar(self, n: int) -> None:
        for i in range(n):
            self.b.remember(f"recuerdo {i} sobre Python y listas de nombres", role="user")

    def test_por_volumen_se_queda_en_el_tope(self) -> None:
        self._llenar(60)
        self.assertGreater(self.b.estadisticas()["nodos"], 60)
        self.b.podar(max_nodos=30)
        self.assertLessEqual(self.b.estadisticas()["nodos"], 34)

    def test_lo_nuevo_no_es_lo_que_se_corta(self) -> None:
        """Al pasarse del tope cae lo viejo, no lo recién dicho.

        Si se cortara lo nuevo, la memoria empezaría a perder justo lo que el
        usuario acaba de contar, que es lo único que quiere que recuerde. Se
        comprueba contra el grafo guardado y no contra ``retrieve``: la
        búsqueda devuelve solo unos pocos y su orden es por relevancia, así que
        que el 59 no salga primero no significa que se haya perdido.
        """
        self._llenar(60)
        self.b.podar(max_nodos=30)
        numeros = sorted(
            int(m.group(1))
            for n in self.b._grafo["nodes"]
            if (m := re.search(r"recuerdo (\d+)", n.get("description") or ""))
        )
        self.assertIn(59, numeros, "la poda se llevó el recuerdo más nuevo")
        self.assertNotIn(0, numeros, "la poda NO se llevó el más viejo")
        self.assertEqual(
            list(range(34, 60)),
            numeros,
            "la poda debería haberse quedado con los 26 últimos",
        )

    def test_las_etiquetas_no_se_cortan_nunca(self) -> None:
        """Las etiquetas son el hilo del tema y sobreviven a todo.

        Aunque no queden recuerdos, "esto se habló de Python" tiene que seguir
        ahí: es lo que hace que la siguiente pregunta sobre Python encuentre el
        tema aunque los mensajes concretos se hayan olvidado.
        """
        self._llenar(60)
        self.b.podar(max_nodos=10, max_dias=0)
        self.b.podar(max_nodos=0, max_dias=1)
        tags = self.b.estadisticas()["por_tipo"].get("tag", 0)
        self.assertGreater(tags, 0, "la poda se llevó las etiquetas del tema")

    def test_podar_por_antiguedad(self) -> None:
        """Con ``max_dias`` caen los recuerdos anteriores al corte."""
        self._llenar(30)
        # Envejecer los recuerdos: si no, se acaban de crear y un tope de 1 día
        # no tocaría ninguno, que es lo correcto pero no lo que se prueba aquí.
        self._envejecer(400)
        fuera = self.b.podar(max_dias=30)
        self.assertEqual(30, fuera)
        self.assertEqual(0, len(self.b.retrieve("python")))

    def test_lo_reciente_no_cae_por_antiguedad(self) -> None:
        """El tope de antigüedad respects lo dicho hace poco."""
        self._llenar(10)
        self.b.podar(max_dias=30)
        self.assertGreater(
            len(self.b.retrieve("python")),
            0,
            "la poda por antigüedad se llevó recuerdos de hace un momento",
        )

    def _envejecer(self, dias: int) -> None:
        """Pone ``created_at`` de ``dias`` días atrás a todos los recuerdos."""
        viejo = (
            datetime.now(timezone.utc) - timedelta(days=dias)
        ).isoformat()
        for n in self.b._grafo["nodes"]:
            n["created_at"] = viejo
        self.b._reindexar()
        self.b._persistir()

    def test_un_tope_de_cero_no_poda(self) -> None:
        """``0`` significa "sin tope", no "no dejes nada"."""
        self._llenar(20)
        antes = self.b.estadisticas()["nodos"]
        self.b.podar(max_nodos=0, max_dias=0)
        self.assertEqual(antes, self.b.estadisticas()["nodos"])

    def test_la_poda_agrega_al_mismo_grafo(self) -> None:
        """La poda tiene que respectar el grafo: sin aristas colgando."""
        self._llenar(60)
        self.b.podar(max_nodos=25)
        ids = {n["id"] for n in self.b._grafo["nodes"]}
        for l in self.b._grafo["links"]:
            self.assertIn(l["source"], ids, "arista con origen que ya no existe")
            self.assertIn(l["target"], ids, "arista con destino que ya no existe")

    def test_se_puede_recordar_despues_de_podar(self) -> None:
        """Podar no deja el índice inservible."""
        self._llenar(60)
        self.b.podar(max_nodos=20)
        self.b.remember("un recuerdo nuevo y muy distinto", role="user")
        self.assertTrue(self.b.retrieve("distinto"))
        # Y sobrevive a una recarga desde disco.
        otro = GraphBackend(self.dir)
        self.assertGreaterEqual(otro.estadisticas()["nodos"], 1)


if __name__ == "__main__":
    unittest.main()