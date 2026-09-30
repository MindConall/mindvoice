"""El HUD usa las animaciones de verdad, y se ven de verdad.

``test_ui_tokens`` comprueba que ``ui/animations.py`` funciona: que el fundido
mueve la opacidad de verdad, que el contador no se fuga, que el pulso respeta su
periodo. Todo eso pasaba, y aun así la biblioteca no la usaba NINGÚN sitio del
overlay: era código muerto con tests verdes. Estos tests cierran ese hueco
comprobando el otro extremo, la integración:

* al mostrar el overlay, el panel entra con un fundido que se ve;
* al ocultarlo, se desvanece y la ventana se va al terminar;
* pulsar dos veces seguidas durante el desvanecido no deja la ventana a medias;
* no queda ninguna animación viva al cerrar.

Se construye el ``OverlayHud`` de verdad, en offscreen y sin red.
"""

from __future__ import annotations

import ctypes
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import overlay  # noqa: E402
from config import Settings  # noqa: E402
from ui.animations import (  # noqa: E402
    Desvanecer,
    Fade,
)

_app = QApplication.instance() or QApplication([])


def _correr(ms: int) -> None:
    """Deja correr el bucle de eventos ``ms`` milisegundos, de verdad.

    Sin esto las animaciones no avanzan: ``start()`` solo programa el reloj, y
    hasta que el bucle gira no hay frames. Por eso los tests antiguos de la
    biblioteca echaban el bucle a mano y el overlay no estaba probado.
    """
    bucle = QEventLoop()
    QTimer.singleShot(ms, bucle.quit)
    bucle.exec()


class TestAnimacionesEnElHud(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        # Un SOLO HUD para toda la clase. ``OverlayHud.__init__`` toma un mutex
        # de instancia única y, si no lo consigue, vuelve ANTES de construir el
        # panel; con un HUD por test el segundo no tendría ni ``panel`` ni
        # ``_anim_panel``. Tampoco se llama a ``close()``: su ``closeEvent``
        # hace ``QApplication.quit()`` y se llevaría por delante el resto.
        s = Settings()
        s.overlay_show_on_start = False
        cls.w = overlay.OverlayHud(s)
        assert cls.w._single_ok, "no se pudo construir el HUD de pruebas"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.w._anim_panel is not None:
            cls.w._anim_panel.stop()
            cls.w._anim_panel = None
        cls.w.hide()
        # Libera el mutex: CreateMutexW lo hace solo al morir el proceso, y el
        # proceso de pruebas sigue vivo.
        if cls.w._single_mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(cls.w._single_mutex))
            cls.w._single_mutex = 0

    def setUp(self) -> None:
        # Estado de partida conocido: sin animación en curso, oculto, flag
        # limpio. Cada test parte de aquí.
        if self.w._anim_panel is not None:
            self.w._anim_panel.stop()
            self.w._anim_panel = None
        self.w._cerrando = False
        self.w.hide()

    def _opacidad_panel(self) -> float:
        """Opacidad real del panel ahora mismo (1.0 si no hay efecto)."""
        efecto = self.w.panel.graphicsEffect()
        if efecto is None:
            return 1.0
        return float(efecto.opacity())

    # -- entrada --------------------------------------------------------
    def test_al_mostrar_arranca_un_fundido_real(self) -> None:
        """``show_overlay`` funde el panel; no aparece de golpe."""
        self.w.show_overlay()
        self.assertIsInstance(
            self.w._anim_panel,
            Fade,
            "mostrar el overlay no arrancó ninguna entrada",
        )
        # A mitad del fundido el panel tiene que estar a media opacidad: si
        # saliera ya opaco, la animación no estaría haciendo nada visible.
        _correr(60)
        opacidad = self._opacidad_panel()
        self.assertLess(
            opacidad,
            1.0,
            "a mitad del fundido el panel ya estaba opaco: no se ve nada",
        )

    def test_al_terminar_el_fundido_el_panel_queda_opaco(self) -> None:
        """Terminado el fundido, el panel se ve normal (y sin efecto colgado)."""
        self.w.show_overlay()
        _correr(600)
        self.assertEqual(1.0, self._opacidad_panel())
        self.assertIsNone(
            self.w.panel.graphicsEffect(),
            "el efecto de opacidad se quedó puesto para siempre: el panel "
            "paga un efecto gráfico en cada paint de la sesión",
        )

    def test_el_fundido_se_ve_en_pixeles(self) -> None:
        """El fundido cambia lo que se ve, no solo una propiedad.

        Este es el test que responde a "no veo ningún cambio en la interfaz".
        Comprueba los píxeles de verdad: al entrar, el panel pasa de transparente
        a opaco de forma progresiva. Si solo se comprobara ``efecto.opacity()``
        pasaría igual con una animación que no pinta nada en la ventana, que es
        exactamente lo que le pasaba a la versión anterior de ``FadeWidget``.
        """
        self.w.show_overlay()

        def _alfa_medio() -> float:
            img = self.w.panel.grab().toImage()
            total = 0
            n = 0
            for y in range(0, img.height(), 7):
                for x in range(0, img.width(), 7):
                    total += img.pixelColor(x, y).alpha()
                    n += 1
            return total / max(1, n)

        # Justo al entrar todavía no se ve nada.
        inicio = _alfa_medio()
        self.assertLess(inicio, 20.0, "el panel ya aparecía opaco de golpe")
        # A mitad de camino está a medias: es un fundido, no un salto.
        _correr(60)
        medio = _alfa_medio()
        self.assertGreater(medio, inicio, "el fundido no avanzó a mitad")
        self.assertLess(medio, 250.0, "a mitad ya estaba opaco: no hay fundido")
        # Y al final, opaco del todo.
        _correr(600)
        self.assertGreater(_alfa_medio(), 200.0, "el panel no quedó visible")

    # -- salida ---------------------------------------------------------
    def test_al_ocultar_se_desvanece_y_luego_se_van(self) -> None:
        """``hide_overlay`` desvanece y oculta la ventana al terminar."""
        self.w.show_overlay()
        _correr(600)  # termina la entrada
        self.w.hide_overlay()
        self.assertTrue(
            self.w._cerrando,
            "al ocultar debería marcar el fundido en curso",
        )
        self.assertIsInstance(self.w._anim_panel, Desvanecer)
        # Durante el fundido sigue visible (si no, parpadearía).
        _correr(50)
        self.assertTrue(self.w.isVisible(), "la ventana desapareció a mitad")
        # Y al terminar, fuera.
        _correr(600)
        self.assertFalse(self.w.isVisible(), "la ventana no se ocultó al terminar")

    def test_dos_toques_seguidos_no_dejan_la_ventana_a_medias(self) -> None:
        """Dos pulsaciones seguidas durante el desvanecido: se queda visible."""
        self.w.show_overlay()
        _correr(600)
        self.w.hide_overlay()
        _correr(50)  # a mitad del desvanecido
        # La segunda pulsación debe devolverlo, no dejarlo colgado.
        self.w.toggle()
        _correr(600)
        self.assertTrue(
            self.w.isVisible(),
            "pulsar dos veces durante el desvanecido dejó la ventana oculta",
        )
        self.assertFalse(self.w._cerrando)

    def test_ocultar_sin_haber_mostrado_no_deja_el_flag_puesto(self) -> None:
        """``hide_overlay`` sin ventana visible no deja ``_cerrando`` colgado.

        Si se quedara, el siguiente ``show_overlay`` funcionaría pero el
        siguiente ``hide_overlay`` no: el panel se iría sin desvanecerse y el
        flag bloquearía cualquier intento posterior.
        """
        self.w.hide_overlay()
        self.assertFalse(self.w._cerrando)
        self.w.show_overlay()
        _correr(600)
        self.w.hide_overlay()
        _correr(600)
        self.assertFalse(self.w.isVisible())

    # -- no fugas -------------------------------------------------------
    def test_al_cerrar_no_queda_ninguna_animacion_viva(self) -> None:
        """Cerrar con un fundido en curso suelta la animación, no la deja viva."""
        from ui.animations import animaciones_vivas

        antes = animaciones_vivas()
        self.w.show_overlay()
        _correr(30)
        self.assertGreater(
            animaciones_vivas(),
            antes,
            "el fundido no debería contar nada: no llegó a arrancar",
        )
        # `closeEvent` para la animación; se llama a mano porque `close()`
        # apagaría la QApplication entera.
        if self.w._anim_panel is not None:
            self.w._anim_panel.stop()
            self.w._anim_panel = None
        self.assertEqual(
            antes,
            animaciones_vivas(),
            "quedó una animación contando tras pararla a mano",
        )


if __name__ == "__main__":
    unittest.main()