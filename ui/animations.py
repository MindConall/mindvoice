"""Animaciones del HUD, con las reglas de rendimiento comprobables.

La Fase 0 midió que el HUD actual vuelca su estado a pantalla en 0,05 ms de un
presupuesto de 16,67 ms. Eso deja margen de sobra, pero solo si las animaciones
no lo desperdician. Aquí las reglas no son consejos: se aplican y se miden.

Las tres que importan:

1. **Solo se anima ``opacity`` y ``pos``/``scale``**, nunca ``geometry`` ni
   layout. Animar el layout obliga a Qt a recalcular la geometría en cada frame
   del HUD, que es justo lo que produce el tirón.
2. **Toda duración cabe en un frame largo** (≤ 200 ms). Una animación más larga
   que eso ya no se percibe como respuesta, sino como cola.
3. **Ninguna animación crea su propio hilo ni timer por elemento.** Un HUD que
   dibuja N elementos con N temporizadores se desacuerda consigo mismo; aquí hay
   un solo reloj por animación y todos los elementos leen de ahí.

Nada de esto toca el redibujado completo: si el HUD se dibuja entero cada
frame, el coste se multiplica por el área. Las animaciones cambian estado de
widgets, no llaman a ``update()`` global.

Dos decisiones que se tomaron corrigiendo fallos reales, no por gusto:

- **La opacidad se anima como Qt la honra de verdad.** ``windowOpacity`` solo
  funciona en ventanas de nivel superior; en un widget hijo Qt la ignora EN
  SILENCIO, sin error ni aviso. Animarla ahí producía animaciones que no se veían
  y tests que pasaban. Para un hijo hace falta un ``QGraphicsOpacityEffect``.
- **Un efecto de opacidad se retira al terminar.** Si se deja puesto, el widget
  paga un efecto gráfico en cada paint para siempre, que es justo lo que estas
  reglas intentan evitar. Al quedar opaco se devuelve el widget a su estado
  normal.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
)
from PyQt6.QtWidgets import QGraphicsOpacityEffect

from . import tokens

logger = logging.getLogger(__name__)

# Contador de animaciones vivas, para poder assertar en los tests que no se
# acumulan (una fuga aquí se ve como el HUD consumiendo CPU con la ventana
# cerrada).
_ANIMACIONES_VIVAS = 0

# Por debajo de esto la opacidad se considera "opaco" y el efecto se retira.
_OPACO = 0.999


def animaciones_vivas() -> int:
    return _ANIMACIONES_VIVAS


def _curve(nombre: str) -> QEasingCurve:
    return QEasingCurve(QEasingCurve.Type(getattr(QEasingCurve.Type, nombre)))


class _Opacidad:
    """Prepara cómo animar la opacidad de un widget, según lo que sea.

    Qt solo honra ``windowOpacity`` en ventanas. Para un hijo hace falta un
    ``QGraphicsOpacityEffect``, que es más caro de pintar.

    Y aquí está la trampa: **un ``QWidget`` solo admite un efecto, y al
    instalar otro Qt DESTRUYE el que tenía.** No hay forma de apartar el
    anterior y devolverlo después; guardarse la referencia no sirve, porque
    queda colgando. Por eso esta clase no intenta devolver nada:

    - si el widget no tiene efecto, se instala uno nuestro y se retira al
      terminar (así el widget se queda en el camino rápido de pintado);
    - si ya tiene un efecto de opacidad, se reaprovecha el suyo y no se toca;
    - si tiene otro tipo de efecto (una sombra, por ejemplo), no hay nada que
      hacer sin romperlo: se dice y no se anima. Fingir que sí, animando una
      propiedad que nadie lee, es justo el fallo que esto arregla.
    """

    def __init__(self, widget, desde: float) -> None:
        self._widget = widget
        self._efecto: QGraphicsOpacityEffect | None = None
        self._propio = False
        self._ultima = float(desde)
        self.animable = True
        previo = widget.graphicsEffect()

        if widget.isWindow():
            self._destino = widget
            self._propiedad = b"windowOpacity"
            widget.setWindowOpacity(self._ultima)
            return

        if previo is None:
            self._efecto = QGraphicsOpacityEffect(widget)
            self._propio = True
            # Hay que INSTALARLO: un efecto creado pero no puesto no pinta nada.
            widget.setGraphicsEffect(self._efecto)
            self._destino = self._efecto
            self._propiedad = b"opacity"
            self._efecto.setOpacity(self._ultima)
        elif isinstance(previo, QGraphicsOpacityEffect):
            # El suyo sirve: se anima el mismo y se deja intacto al terminar.
            self._efecto = previo
            self._destino = previo
            self._propiedad = b"opacity"
            self._efecto.setOpacity(self._ultima)
        else:
            # Tiene un efecto que no es de opacidad: al instalar otro, Qt lo
            # borraría. No se anima nada.
            self._destino = widget
            self._propiedad = b"opacity"
            self.animable = False
            logger.warning(
                "El widget ya tiene un efecto de tipo %s y no se puede fundir sin "
                "romperlo; se deja como está.",
                type(previo).__name__,
            )

    @property
    def destino(self):
        return self._destino

    @property
    def propiedad(self) -> bytes:
        return self._propiedad

    @property
    def opacidad(self) -> float:
        """Opacidad actual, o la última conocida si el efecto ya no existe.

        Al retirar el efecto, Qt destruye el objeto de C++. Sin este recuerdo,
        leer la opacidad después de terminar la animación revienta con
        "wrapped C/C++ object has been deleted".
        """
        try:
            self._ultima = float(
                self._destino.property(self._propiedad.decode()) or self._ultima
            )
        except RuntimeError:  # el efecto ya se retiró y fue destruido
            pass
        return self._ultima

    def soltar(self) -> None:
        """Retira el efecto si es nuestro y ya no se nota.

        Un efecto de opacidad permanente obliga al widget por un renderizado
        extra en cada paint. Si al terminar la animación no se nota, se quita:
        el HUD se queda en el camino rápido, que es de donde vino. Si el efecto
        era del widget, no se toca.
        """
        if self._efecto is None or not self._propio or self.opacidad < _OPACO:
            return
        try:
            self._widget.setGraphicsEffect(None)
        except RuntimeError as exc:  # el widget ya se fue
            logger.debug("No se pudo retirar el efecto de opacidad: %s", exc)
        self._efecto = None


def _descontar_al_destruirse(objeto) -> None:
    """Si el widget muere en mitad de la animación, la cuenta se devuelve igual.

    ``finished`` solo salta si la animación llega al final. Si el widget que se
    está fundiendo desaparece a mitad (el HUD cerrando un panel), nunca salta y
    la cuenta se filtra para siempre. ``destroyed`` es el único aviso que llega
    en ese caso.
    """
    contar = getattr(objeto, "_contando", False)
    if contar:
        objeto._descontar()


class _AnimacionOpacidad(QObject):
    """Base común: el contador, parar y soltar.

    El contador se lleva por instancia, no sumando a ciegas. Antes cada
    ``start()`` sumaba y solo ``finished`` restaba, pero Qt **no** emite
    ``finished`` cuando para una animación a mano: todo ``stop()`` fugaba una
    cuenta. Con un HUD que aparece y desaparece, eso es un contador que solo
    sube.
    """

    def __init__(self, widget, desde: float, duracion_ms: int, parent=None) -> None:
        super().__init__(parent or widget)
        self._widget = widget
        self._opacidad = _Opacidad(widget, desde)
        self._contando = False
        self._anim = QPropertyAnimation(
            self._opacidad.destino, self._opacidad.propiedad, self
        )
        self._anim.setDuration(int(duracion_ms))
        # KeepWhenStopped, no DeleteWhenStopped: con la política de borrado, un
        # `stop()` destruye la animación y volver a arrancarla revienta. Aquí la
        # animación vive lo que su contenedor, y se puede repetir.
        self._anim.finished.connect(self._al_terminar)
        self.destroyed.connect(_descontar_al_destruirse)

    def _contar(self) -> None:
        global _ANIMACIONES_VIVAS
        if not self._contando:
            self._contando = True
            _ANIMACIONES_VIVAS += 1

    def _descontar(self) -> None:
        global _ANIMACIONES_VIVAS
        if self._contando:
            self._contando = False
            _ANIMACIONES_VIVAS = max(0, _ANIMACIONES_VIVAS - 1)

    def _al_terminar(self) -> None:
        self._descontar()

    def start(self) -> None:
        if not self._opacidad.animable:
            return
        self._contar()
        self._anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def stop(self) -> None:
        self._anim.stop()
        # `finished` no salta al parar, así que hay que restar aquí también.
        self._descontar()

    @property
    def opacidad_actual(self) -> float:
        return self._opacidad.opacidad


class Fade(_AnimacionOpacidad):
    """Fundido de opacidad de 0 a un valor, al entrar algo en pantalla.

    Funciona con ventanas y con widgets hijos: quien decide cómo animar la
    opacidad es la clase base, no esta.
    """

    def __init__(self, widget, desde: float = 0.0, hasta: float = 1.0,
                 duracion_ms: int = tokens.DURACION_APARECER_MS, parent=None) -> None:
        super().__init__(widget, desde, duracion_ms, parent)
        self._anim.setStartValue(float(desde))
        self._anim.setEndValue(float(hasta))
        self._anim.setEasingCurve(_curve(tokens.CURVA_ENTRADA))

    def _al_terminar(self) -> None:
        super()._al_terminar()
        self._opacidad.soltar()


class Pulso(_AnimacionOpacidad):
    """Pulso continuo (el punto de micrófono, el indicador de escucha).

    Es la única animación que se repite sola, y por eso usa un solo reloj para
    todo el HUD en vez de uno por elemento.

    Ojo: al no terminar nunca, en un widget hijo el efecto de opacidad se queda
    instalado. Es el precio de que el pulso se vea; para el resto de transiciones
    está ``Fade``, que sí lo retira.
    """

    def __init__(self, widget, periodo_ms: int = tokens.DURACION_PULSO_MS, parent=None) -> None:
        super().__init__(widget, 0.55, periodo_ms, parent)
        self._periodo = periodo_ms
        self._anim.setStartValue(0.55)
        self._anim.setKeyValueAt(0.5, 1.0)
        self._anim.setEndValue(0.55)
        self._anim.setEasingCurve(_curve(tokens.CURVA_SALIDA))
        self._anim.setLoopCount(-1)

    @property
    def periodo_ms(self) -> int:
        return self._periodo


class Desvanecer(_AnimacionOpacidad):
    """Salida: baja la opacidad y, al terminar, oculta el widget.

    Ocultar al final (y no "visible" a mitad) evita el parpadeo: durante toda la
    animación el widget sigue ocupando su sitio y no lo suelta hasta que ya no se
    ve nada.
    """

    def __init__(self, widget, duracion_ms: int = tokens.DURACION_DESVANECER_MS, parent=None) -> None:
        desde = float(widget.windowOpacity() or 1.0) if widget.isWindow() else 1.0
        super().__init__(widget, desde, duracion_ms, parent)
        self._anim.setStartValue(desde)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(_curve(tokens.CURVA_SALIDA))
        self._anim.finished.connect(self._ocultar)

    def _al_terminar(self) -> None:
        super()._al_terminar()
        # Al terminar en 0 no hay nada que retirar: el widget está oculto.
        self._opacidad.soltar()

    def _ocultar(self) -> None:
        try:
            self._widget.hide()
            if self._widget.isWindow():
                # La opacidad se recupera para que la siguiente entrada no salga
                # desde invisible y no se vea el salto.
                self._widget.setWindowOpacity(1.0)
        except RuntimeError as exc:  # el widget ya se fue
            logger.debug("Widget fuera al terminar el desvanecido: %s", exc)


# Antes esta clase animaba una propiedad dinámica llamada "opacity" sobre su
# propio QObject: un valor que nadie lee, así que el fundido de un widget hijo no
# se veía. `Fade` ya cubre los dos casos (ventana e hijo) porque la elección la
# hace `_Opacidad`, así que se deja como alias para no romper a quien lo use.
FadeWidget = Fade


def validar_reglas() -> list[str]:
    """Comprueba las reglas de la Fase 3 y devuelve lo que se incumple.

    Existe para poder llamarla desde un test y desde el arranque: si alguien
    cambia una duración a 900 ms, esto lo dice en vez de dejar una animación
    que se siente lenta y nadie sabe por qué.
    """
    fallos = []
    for nombre, valor in tokens.REGLAS.items():
        if nombre == "duracion_max_ms":
            for dur in (
                tokens.DURACION_TOGGLE_MS,
                tokens.DURACION_APARECER_MS,
                tokens.DURACION_DESVANECER_MS,
                tokens.DURACION_LISTA_MS,
            ):
                if dur > valor:
                    fallos.append(f"duración {dur} ms supera el máximo {valor} ms")
                if dur < tokens.REGLAS["duracion_min_ms"]:
                    fallos.append(f"duración {dur} ms por debajo del mínimo")
        elif nombre == "radio_max_px":
            for radio in (tokens.RADIO_CHIP, tokens.RADIO_BLOQUE,
                          tokens.RADIO_TARJETA, tokens.RADIO_PANEL):
                if radio > valor:
                    fallos.append(f"radio {radio} px supera el máximo {valor} px")
        elif nombre == "tamano_min_px":
            for tam in (tokens.TAMANO_CIFRA, tokens.TAMANO_TITULO,
                        tokens.TAMANO_CUERPO, tokens.TAMANO_ETIQUETA, tokens.TAMANO_MINI):
                if tam < valor:
                    fallos.append(f"tipografía {tam} px por debajo del mínimo {valor} px")
    return fallos