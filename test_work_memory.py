"""Tests de la memoria de trabajo (Fase 2).

Lo importante aquí son los negativos: el módulo tiene que ser conservador.
Marcar una contradicción donde no la hay, o inventar un perfil a partir de una
frase suelta, degrada la conversación de forma difícil de detectar.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from memory.base import MemoryEntry
from memory.graph_backend import GraphBackend
from memory.work_memory import (
    CALLEJON,
    CORREGIDO,
    UTIL,
    MemoriaTrabajo,
    Traza,
    detectar_chocque,
    detectar_contradicciones,
    detectar_perfil,
    es_correccion,
    lecciones,
    nota_ampliada,
    render_lecciones,
    render_perfil,
)


class BaseTemporal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mv-wm-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def entrada(texto, role="user"):
    return MemoryEntry(id="x", text=texto, role=role)


# --------------------------------------------------------------------- trazas
class TestTrazas(BaseTemporal):
    def registro(self) -> MemoriaTrabajo:
        return MemoriaTrabajo(self.tmp)

    def test_registrar_y_persistir(self) -> None:
        r = self.registro()
        r.registrar("¿cómo se llama mi perro?", UTIL)
        otra = MemoriaTrabajo(self.tmp)
        self.assertEqual(len(otra.trazas()), 1)
        self.assertEqual(otra.trazas()[0].pregunta, "¿cómo se llama mi perro?")

    def test_pregunta_vacia_no_se_registra(self) -> None:
        self.assertIsNone(self.registro().registrar("   "))

    def test_resultado_desconocido_se_ignora(self) -> None:
        r = self.registro()
        self.assertIsNone(r.registrar("pregunta", "inventado"))
        self.assertEqual(r.trazas(), [])

    def test_no_crece_sin_limite(self) -> None:
        r = self.registro()
        for i in range(80):
            r.registrar(f"pregunta {i}", UTIL)
        self.assertLessEqual(len(r.trazas()), 40)

    def test_olvidar_por_pregunta(self) -> None:
        r = self.registro()
        r.registrar("¿qué hora es en Tokio?", UTIL)
        r.registrar("¿qué hora es en Madrid?", UTIL)
        quitadas = r.forget("Tokio")
        self.assertEqual(quitadas, 1)
        self.assertEqual(len(r.trazas()), 1)
        self.assertIn("Madrid", r.trazas()[0].pregunta)

    def test_archivo_corrupto_no_rompe(self) -> None:
        with open(os.path.join(self.tmp, "work-memory.json"), "w", encoding="utf-8") as fh:
            fh.write("no soy json")
        self.assertEqual(MemoriaTrabajo(self.tmp).trazas(), [])

    def test_el_peso_baja_con_el_tiempo(self) -> None:
        ahora = 1_000_000.0
        t = Traza("p", UTIL, cuando=ahora)
        self.assertAlmostEqual(t.peso(ahora), 1.0, places=3)
        self.assertLess(t.peso(ahora + 86400.0 * 30), 0.6)

    def test_sin_guardar_no_se_traga_la_excepcion(self) -> None:
        r = self.registro()
        r._ruta = os.path.join(self.tmp, "no-existe-dir", "sub", "w.json")
        r.registrar("pregunta", UTIL)  # no debe lanzar aunque no pueda guardar
        self.assertEqual(len(r.trazas()), 1)


# ------------------------------------------------------------------- lecciones
class TestLecciones(BaseTemporal):
    def registro(self) -> MemoriaTrabajo:
        return MemoriaTrabajo(self.tmp)

    def test_una_sola_senal_no_genera_leccion(self) -> None:
        r = self.registro()
        r.registrar("¿cómo va el proyecto?", UTIL)
        self.assertEqual(lecciones(r)["lecciones"], [])

    def test_dos_veces_si_genera_leccion(self) -> None:
        r = self.registro()
        r.registrar("proyecto de la tienda", UTIL)
        r.registrar("proyecto de la tienda", UTIL)
        resultado = lecciones(r)
        self.assertTrue(resultado["lecciones"])
        # El tema se agrupa por palabra, así que basta con que aparezca.
        self.assertTrue(any("proyecto" in l["leccion"] for l in resultado["lecciones"]))

    def test_un_callejon_sin_salida_es_leccion(self) -> None:
        r = self.registro()
        for _ in range(3):
            r.registrar("conseguir credenciales del servidor", CALLEJON)
        leccion = lecciones(r)["lecciones"][0]["leccion"]
        self.assertIn("callejon", leccion)

    def test_la_correccion_manda_en_el_veredicto(self) -> None:
        r = self.registro()
        r.registrar("el puerto del servidor", UTIL)
        r.registrar("el puerto del servidor", CORREGIDO, correccion="es el 8765")
        r.registrar("el puerto del servidor", CORREGIDO, correccion="es el 8765")
        datos = lecciones(r)["lecciones"][0]
        self.assertIn("correccion", datos["leccion"])
        self.assertTrue(any("8765" in c for c in datos["correciones"]))

    def test_es_determinista(self) -> None:
        r = self.registro()
        for i in range(4):
            r.registrar("tema estable de prueba", UTIL)
        self.assertEqual(lecciones(r), lecciones(r))

    def test_render_vacio_sin_lecciones(self) -> None:
        self.assertEqual(render_lecciones({"lecciones": []}), "")

    def test_render_incluye_la_leccion(self) -> None:
        r = self.registro()
        for _ in range(2):
            r.registrar("tema renderizable", UTIL)
        texto = render_lecciones(lecciones(r))
        self.assertTrue(texto.startswith("[Lecciones aprendidas]"))
        # El tema se agrupa por palabra, así que aparece una de sus piezas.
        self.assertTrue("renderizable" in texto or "tema" in texto)


# -------------------------------------------------------------- contradicciones
class TestContradicciones(BaseTemporal):
    def grafo(self) -> GraphBackend:
        return GraphBackend(self.tmp)

    def test_detecta_nombres_distintos(self) -> None:
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("Tengo un perro que se llama Bigotes")
        encontrados = detectar_contradicciones(g)
        self.assertEqual(len(encontrados), 1)
        a, b = encontrados[0]["a_texto"], encontrados[0]["b_texto"]
        self.assertNotEqual(a, b)
        recuerdos = [n for n in g._grafo["nodes"] if n.get("kind") in ("brief", "permanent")]
        self.assertTrue(all(n.get("contested") for n in recuerdos))

    def test_no_marca_redundancia_como_contradiccion(self) -> None:
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("Tengo un perro que se llama Nube y es muy tranquilo")
        self.assertEqual(detectar_contradicciones(g), [])

    def test_no_marca_recuerdos_distintos(self) -> None:
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("El proyecto de la tienda va con Python")
        self.assertEqual(detectar_contradicciones(g), [])

    def test_no_elige_ganador(self) -> None:
        """Marcar no es decidir: los dos siguen ahí y en disputa."""
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("Tengo un perro que se llama Bigotes")
        detectar_contradicciones(g)
        self.assertEqual(len([n for n in g._grafo["nodes"] if n.get("contested")]), 2)

    def test_ignora_etiquetas_al_buscar(self) -> None:
        g = self.grafo()
        g.remember("Ana tiene un perro llamado Nube", tags=["perro"])
        encontrados = detectar_contradicciones(g)
        self.assertTrue(all("perro" not in e["a_texto"] for e in encontrados))

    def test_no_explota_con_un_grafo_vacio(self) -> None:
        self.assertEqual(detectar_contradicciones(self.grafo()), [])

    def test_no_falla_si_no_es_un_grafo(self) -> None:
        self.assertEqual(detectar_contradicciones(None), [])

    def test_el_barrido_esta_acotado(self) -> None:
        """El barrido completo tiene que parar, aunque haya muchos temas.

        La versión anterior comparaba todos los pares de todos los nodos: con la
        memoria real del usuario (1418 nodos) eso es un millón de comparaciones
        y no cabe en el arranque. Aquí se comprueba que el tope se respeta.
        """
        g = self.grafo()
        for i in range(400):
            g.remember(f"Nota numero {i} sobre el tema {i} distinto", tags=[f"t{i}"])
        with mock.patch("memory.work_memory.PARES_MAX", 50):
            encontrados = detectar_contradicciones(g)
        self.assertEqual(encontrados, [])
        # Y aun con el tope durísimo, sigue sin colgarse:
        with mock.patch("memory.work_memory.PARES_MAX", 10_000_000):
            self.assertIsInstance(detectar_contradicciones(g), list)

    def test_solo_compara_con_los_vecinos(self) -> None:
        """Con ``solo`` solo se mira a los vecinos del nodo indicado."""
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("Tengo un perro que se llama Bigotes", tags=["perro"])
        nodo = [n for n in g._grafo["nodes"] if n.get("kind") == "brief"][-1]
        encontrados = detectar_contradicciones(g, solo=nodo["id"])
        self.assertEqual(len(encontrados), 1)


# ----------------------------------------------------------- choque en el turno
class TestChoqueTurno(BaseTemporal):
    """La vía que corre en el camino crítico del turno de texto."""

    def grafo(self) -> GraphBackend:
        return GraphBackend(self.tmp)

    def test_avisa_cuando_lo_nuevo_choca(self) -> None:
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        nota = detectar_chocque(g, "Tengo un perro que se llama Bigotes")
        self.assertIn("AVISO DE MEMORIA", nota)
        self.assertIn("pregunta", nota.lower())

    def test_marca_el_recuerdo_anterior_en_disputa(self) -> None:
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        detectar_chocque(g, "Tengo un perro que se llama Bigotes")
        self.assertEqual(len([n for n in g._grafo["nodes"] if n.get("contested")]), 1)

    def test_no_avisa_si_no_choca_nada(self) -> None:
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("El proyecto de la tienda va con Python")
        self.assertEqual(detectar_chocque(g, "Mañana tengo una reunión a las nueve"), "")

    def test_repetir_lo_mismo_no_es_un_choque(self) -> None:
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        self.assertEqual(detectar_chocque(g, "Tengo un perro que se llama Nube"), "")

    def test_sin_grafo_no_pasa_nada(self) -> None:
        self.assertEqual(detectar_chocque(None, "Tengo un perro que se llama X"), "")
        self.assertEqual(detectar_chocque(self.grafo(), ""), "")

    def test_el_choque_del_turno_no_rewrite_el_grafo(self) -> None:
        """Marcar una disputa no puede costar medio segundo en medio de la voz.

        El fallo era sutil y carísimo: ``marcar_disputa`` llamaba a ``_persistir``
        sin más, así que reescribía el JSON entero del grafo por poner una
        bandera booleana. Con la memoria real (1425 nodos, 3 MB) medido: 345 ms
        por turno. Con la escritura aplazada: 2 ms. Por eso ``marcar_disputa`` no
        escribe y quien marca muchas, ``marcar_disputas``, escribe una vez.
        """
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("el proyecto de la tienda va con Python")
        with mock.patch.object(
            g, "_persistir", wraps=g._persistir
        ) as persistir:
            detectar_chocque(g, "Tengo un perro que se llama Bigotes")
        aplazadas = [
            c for c in persistir.call_args_list if c.kwargs.get("urgente") is False
        ]
        self.assertTrue(
            aplazadas,
            "marcar una disputa escribió a disco: es lo que costaba 345 ms por turno",
        )
        self.assertFalse(
            [c for c in persistir.call_args_list if not c.kwargs],
            "hubo una escritura urgente por una sola bandera de disputa",
        )

    def test_el_barrido_marca_todas_de_una_vez(self) -> None:
        """Varias marcas, una escritura: por marca el grafo entero."""
        g = self.grafo()
        g.remember("Tengo un perro que se llama Nube")
        g.remember("Tengo un perro que se llama Bigotes")
        g.remember("Tengo un perro que se llama Chispa")
        with mock.patch.object(
            g, "marcar_disputas", wraps=g.marcar_disputas
        ) as batch:
            detectar_contradicciones(g)
        batch.assert_called_once()


# ----------------------------------------------------------------- correcciones
class TestCorreccion(unittest.TestCase):
    """Es la única señal donde el usuario dice que la app se equivocó."""

    def test_detecta_las_marcas_obvias(self) -> None:
        for frase in (
            "no, no es Bigotes, es Nube",
            "eso no es verdad",
            "te equivocas, es 2024",
            "incorrecto",
            "en realidad es 2025",
            "corrijo: son las tres",
        ):
            with self.subTest(frase=frase):
                self.assertTrue(es_correccion(frase))

    def test_no_detecta_conversacion_normal(self) -> None:
        for frase in (
            "¿qué hora es?",
            "pon un temporizador de diez minutos",
            "mi perro se llama Nube",
            "busca el tiempo en Madrid",
            "",
        ):
            with self.subTest(frase=frase):
                self.assertFalse(es_correccion(frase))

    def test_mensajes_largos_no_se_cuentan(self) -> None:
        """Un párrafo que Mentiona 'no, es' no es una rectificación."""
        largo = "no, " + ("y entonces le dije que " * 40)
        self.assertFalse(es_correccion(largo))


# --------------------------------------------------------------------- perfil
class TestPerfil(unittest.TestCase):
    def test_una_vez_no_genera_perfil(self) -> None:
        self.assertEqual(detectar_perfil([entrada("Responde en inglés, por favor")]), [])

    def test_dos_veces_si_genera_perfil(self) -> None:
        entradas = [
            entrada("Responde en inglés, por favor"),
            entrada("Otra vez: responde en inglés"),
        ]
        perfil = detectar_perfil(entradas)
        self.assertTrue(perfil)
        self.assertEqual(perfil[0]["tipo"], "idioma")

    def test_detecta_tono_corto(self) -> None:
        entradas = [entrada("Sé breve"), entrada("Mejor breve, por favor")]
        tipos = {p["tipo"] for p in detectar_perfil(entradas)}
        self.assertIn("tono", tipos)

    def test_no_inventa_perfil_de_una_frase_suelta(self) -> None:
        """Mencionar 'en inglés' sin pedirlo no es una preferencia."""
        entradas = [entrada("Leí un libro en inglés")]
        self.assertEqual(detectar_perfil(entradas), [])

    def test_render_vacio(self) -> None:
        self.assertEqual(render_perfil([]), "")

    def test_render_con_perfil(self) -> None:
        entradas = [entrada("Responde en inglés"), entrada("Responde en inglés otra vez")]
        texto = render_perfil(detectar_perfil(entradas))
        self.assertTrue(texto.startswith("[Cómo quieres que te hable]"))
        self.assertIn("idioma", texto)


# ------------------------------------------------------------------- integrado
class TestNotaAmpliada(BaseTemporal):
    def test_conserva_el_bloque_original(self) -> None:
        original = "[Breve]\n- usuario: hola"
        r = MemoriaTrabajo(self.tmp)
        for _ in range(2):
            r.registrar("tema ampliado", UTIL)
        salida = nota_ampliada(original, registro=r)
        self.assertTrue(salida.startswith(original), "el bloque previo no puede desaparecer")
        self.assertIn("Lecciones aprendidas", salida)

    def test_sin_nada_que_añadir_no_cambia_nada(self) -> None:
        original = "[Breve]\n- usuario: hola"
        r = MemoriaTrabajo(self.tmp)
        self.assertEqual(nota_ampliada(original, registro=r), original)

    def test_bloque_vacio_permite_solo_lo_nuevo(self) -> None:
        r = MemoriaTrabajo(self.tmp)
        for _ in range(2):
            r.registrar("solo lecciones", UTIL)
        salida = nota_ampliada("", registro=r)
        self.assertIn("Lecciones aprendidas", salida)


if __name__ == "__main__":
    unittest.main()