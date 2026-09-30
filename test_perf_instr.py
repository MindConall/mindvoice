"""Tests de la instrumentación de rendimiento (Fase 0).

La instrumentación está apagada por defecto, así que lo que se comprueba aquí es
que apagada no moleste, encendida mida bien, y que el informe sea utilizable.
"""

import importlib
import json
import os
import pathlib
import re
import tempfile
import unittest


class TestPerfApagada(unittest.TestCase):
    """Con la instrumentación apagada no se debe recolectar nada."""

    def setUp(self) -> None:
        for var in ("MINDVOICE_PERF", "MINDVOICE_PERF_OUT"):
            os.environ.pop(var, None)
        import perf_instr

        self.mod = importlib.reload(perf_instr)

    def test_apagada_por_defecto(self) -> None:
        self.assertFalse(self.mod.PERF.enabled)

    def test_las_llamadas_no_revientan_sin_activar(self) -> None:
        # Aunque se llame a todo el API, sin MINDVOICE_PERF no debe pasar nada.
        p = self.mod.PERF
        p.mark_ui_built()
        p.mark_ui_visible()
        p.ui_cycle_start()
        p.ui_cycle_end()
        p.note_prompt(100, 200, 1)
        p.note_drop(3)
        snap = p.snapshot()
        self.assertIsInstance(snap, dict)

    def test_enabled_refleja_la_variable(self) -> None:
        os.environ["MINDVOICE_PERF"] = "1"
        try:
            mod = importlib.reload(self.mod)
            self.assertTrue(mod.PERF.enabled)
        finally:
            os.environ.pop("MINDVOICE_PERF", None)


class TestPerfEncendida(unittest.TestCase):
    """Con la instrumentación activa se miden las tres magnitudes de la fase."""

    def setUp(self) -> None:
        os.environ["MINDVOICE_PERF"] = "1"
        import perf_instr

        self.mod = importlib.reload(perf_instr)
        self.p = self.mod.Perf()
        self.p.enabled = True

    def tearDown(self) -> None:
        os.environ.pop("MINDVOICE_PERF", None)

    def test_marca_de_arranque(self) -> None:
        self.p.mark_ui_built()
        self.p.mark_ui_visible()
        snap = self.p.snapshot()
        self.assertIn("ui_construida_ms", snap)
        self.assertIn("ui_visible_ms", snap)
        # No puede ser negativo aunque el reloj venga raro.
        self.assertGreaterEqual(snap["ui_construida_ms"], 0.0)

    def test_ciclos_de_ui_se_cuentan(self) -> None:
        for _ in range(5):
            self.p.ui_cycle_start()
            self.p.ui_cycle_end()
        snap = self.p.snapshot()
        self.assertEqual(snap["ui_ciclos"], 5)

    def test_fps_real_no_es_absurdo(self) -> None:
        # Regresión: se midió sobre la ventana equivocada y salían millones de
        # ciclos por segundo. Con 20 ciclos en una ventana real debe salir un
        # número de menos de 1000.
        import time

        self.p.ui_cycle_start()
        self.p.ui_cycle_end()
        time.sleep(0.2)
        for _ in range(19):
            self.p.ui_cycle_start()
            self.p.ui_cycle_end()
            time.sleep(0.01)
        snap = self.p.snapshot()
        fps = snap.get("ui_ciclos_por_segundo_real")
        self.assertIsNotNone(fps)
        self.assertGreater(fps, 1.0)
        self.assertLess(fps, 1000.0)

    def test_tramos_largos_se_cuentan(self) -> None:
        # Un ciclo, una pausa larga, otro ciclo: el hueco tiene que verse.
        self.p.ui_cycle_start()
        self.p.ui_cycle_end()
        self.p._ui_gaps.append(250.0)  # hueco simulado de 250 ms
        self.p.ui_cycle_start()
        self.p.ui_cycle_end()
        snap = self.p.snapshot()
        self.assertGreaterEqual(snap["ui_tramos_sobre_100ms"], 1)

    def test_tamano_de_prompt(self) -> None:
        self.p.note_prompt(4043, 1200, 1)
        self.p.note_prompt(4000, 0, 0)
        snap = self.p.snapshot()
        self.assertEqual(snap["prompt_turnos"], 2)
        self.assertGreater(snap["memoria_chars_medio"], 0)
        self.assertEqual(snap["ultimo_turno"]["memoria_chars"], 4000)
        self.assertEqual(snap["ultimo_turno"]["pantalla_chars"], 0)
        # La estimación de tokens debe estar en un orden de magnitud razonable.
        tokens = snap["prompt_tokens_aprox_medio"]
        self.assertGreater(tokens, 0)
        self.assertLess(tokens, snap["prompt_total_chars_medio"])

    def test_mensajes_descartados(self) -> None:
        self.p.note_drop(7)
        self.assertEqual(self.p.snapshot()["ui_mensajes_descartados"], 7)

    def test_guardar_informe_json(self) -> None:
        self.p.mark_ui_built()
        self.p.note_prompt(1000, 500, 1)
        with tempfile.TemporaryDirectory() as tmp:
            destino = os.path.join(tmp, "perf.json")
            ruta = self.p.save(destino)
            self.assertEqual(ruta, destino)
            self.assertTrue(os.path.exists(destino))
            with open(destino, encoding="utf-8") as fh:
                datos = json.load(fh)
            self.assertTrue(datos["habilitado"])
            self.assertIn("ultimo_turno", datos)

    def test_guardar_en_ruta_invalida_no_revienta(self) -> None:
        # La instrumentación nunca debe tumbar la app por escribir el informe.
        ruta = self.p.save("Z:\\no\\existe\\dir\\perf.json")
        self.assertEqual(ruta, "")


class TestPerfSeleccion(unittest.TestCase):
    """``MINDVOICE_PERF`` admite elegir qué medidores se activan."""

    def _reload(self, valor):
        """Carga el módulo con ``MINDVOICE_PERF`` puesta.

        Devuelve el módulo y un ``finally`` que quita la variable: hay que
        limpiarla DESPUÉS de las aserciones, porque ``_wanted`` la lee del
        entorno en cada llamada.
        """
        os.environ["MINDVOICE_PERF"] = valor
        import perf_instr

        mod = importlib.reload(perf_instr)

        def limpiar():
            os.environ.pop("MINDVOICE_PERF", None)

        return mod, limpiar

    def test_solo_overlay(self) -> None:
        mod, fin = self._reload("overlay")
        try:
            self.assertTrue(mod._wanted("overlay"))
            self.assertFalse(mod._wanted("prompt"))
        finally:
            fin()

    def test_solo_prompt(self) -> None:
        mod, fin = self._reload("prompt")
        try:
            self.assertFalse(mod._wanted("overlay"))
            self.assertTrue(mod._wanted("prompt"))
        finally:
            fin()

    def test_todos(self) -> None:
        mod, fin = self._reload("1")
        try:
            self.assertTrue(mod._wanted("overlay"))
            self.assertTrue(mod._wanted("prompt"))
        finally:
            fin()

    def test_overlay_tiene_las_marcas_de_arranque(self) -> None:
        """El HUD tiene que llamar a las marcas, no solo a existir el modulo.

        Esto se comprobo por la mala: al borrar los cambios sin commitear de
        overlay.py, las 13 pruebas anteriores seguian en verde porque solo
        probaban perf_instr.py. Esta lee el codigo fuente del overlay y falla
        si le falta alguna llamada.
        """
        raiz = pathlib.Path(__file__).resolve().parent
        src = (raiz / "overlay.py").read_text(encoding="utf-8")
        # Se mira el codigo, no se importa el modulo: importarlo levanta Qt y en
        # Windows crea threads que no hacen falta para comprobar texto.
        for marca in (
            "mark_ui_built()",
            "mark_ui_visible()",
            "ui_cycle_start()",
            "ui_cycle_end()",
            "note_drop(",
        ):
            with self.subTest(marca=marca):
                self.assertIn(
                    marca,
                    src,
                    f"overlay.py no llama a {marca}: la metrica de la Fase 0 "
                    "no se esta tomando",
                )

    def test_el_ciclo_mide_el_cuerpo_y_no_solo_el_envoltorio(self) -> None:
        """``_drain_ui_queue`` debe medir el trabajo, no renombrarse a si mismo.

        Si alguien "optimiza" poniendo el bucle en una funcion que la de
        instrumentar no llama, las metricas de UI miden un ciclo vacio y dan
        unos numeros hermosos que no significan nada.
        """
        raiz = pathlib.Path(__file__).resolve().parent
        src = (raiz / "overlay.py").read_text(encoding="utf-8")
        m = re.search(
            r"def _drain_ui_queue\(self\).*?def (_drain_ui_queue_\w+)\(self\)",
            src,
            re.S,
        )
        self.assertIsNotNone(m, "no encuentro el envoltorio de _drain_ui_queue")
        cuerpo = m.group(1)
        self.assertIn(
            f"{cuerpo}()",
            src,
            "el envoltorio de _drain_ui_queue no llama a su propio cuerpo",
        )


if __name__ == "__main__":
    unittest.main()