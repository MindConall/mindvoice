"""Tokens de diseño del HUD.

Una sola fuente para color, radio, espacio, tipografía y duración. Hasta ahora
todo eso vivía esparcido por el QSS de ``overlay.py``; esto lo saca a un sitio
comprobable y deja el sitio viejo funcionando igual (mismos valores), para que
aplicar el sistema no cambie un solo píxel hasta que se quiera.

Los colores no son inventados: son los que el HUD ya usaba, extraídos del QSS
existente. Los radios y el espaciado también. Lo nuevo (duraciones, tipografía,
jerarquía) viene con la Fase 3 y es lo que sí se puede medir.
"""

from __future__ import annotations

# --------------------------------------------------------------- color
# Paleta que el overlay ya llevaba. ``TINTA_*`` es la escala de texto gris-azulada
# que ya se veía en pantalla, de más claro a más apagado.
TINTA_ALT = "#f4f7fb"  # títulos y cifras destacadas
TINTA = "#e8ecf2"  # texto normal
TINTA_MEDIA = "#c7d1de"  # texto secundario
TINTA_SUAVE = "#9fb2c9"  # etiquetas
TINTA_TENUE = "#8fa3ba"  # texto casi decorativo
TINTA_BORDE = "#b9c8da"

FONDO = "#151a24"  # fondo del panel
FONDO_ALT = "#f4f7fb"

VERDE = "#59d98f"  # activo / escuchando
AMARILLO = "#ffb340"  # pensando / avisando
ROJO = "#ff5d5d"  # error
ROJO_TENUE = "#ffd7d7"
CIAN = "#7ee0ff"  # acento frío
AMBAR = "#f0c67a"

# ---------------------------------------------------------------- radio
# Los tres radios que ya se usaban en el QSS: 8 (chip), 11-12 (tarjeta), 18
# (panel). Los intermedios se redondean a la escala de abajo para no inventar
# medidas que luego suenen raras.
RADIO_CHIP = 8
RADIO_BLOQUE = 11
RADIO_TARJETA = 12
RADIO_PANEL = 18

# Alias en minúscula: se usan al escribir QSS, donde los identificadores en
# mayúsculas se leen como constantes y ensucian la plantilla.
tinta = TINTA
tinta_alta = TINTA_ALT
tinta_media = TINTA_MEDIA
tinta_suave = TINTA_SUAVE
tinta_tenue = TINTA_TENUE
tinta_borde = TINTA_BORDE
fondo = FONDO
verde = VERDE
amarillo = AMARILLO
rojo = ROJO
rojo_tenue = ROJO_TENUE
cian = CIAN
ambar = AMBAR
radio_chip = RADIO_CHIP
radio_bloque = RADIO_BLOQUE
radio_tarjeta = RADIO_TARJETA
radio_panel = RADIO_PANEL

# ----------------------------------------------------------------- space
# Escala de 4 px, la que ya siguen el padding del QSS (4/8/12) y los radios.
# Sube de dos en dos: 4, 8, 12, 16 y luego dobla, que es como se leen bien las
# separaciones grandes.
ESPACIO_XS = 4
ESPACIO_SM = 8
ESPACIO_MD = 12
ESPACIO_LG = 16
ESPACIO_XL = 24
ESPACIO_XXL = 32
# La escala entera, para poder comprobarla sin repetirla en cada test.
ESCALA_ESPACIO = (4, 8, 12, 16, 24, 32)

# ------------------------------------------------------------ tipografía
TAMANO_CIFRA = 15  # números grandes de tokens
TAMANO_TITULO = 13
TAMANO_CUERPO = 12
TAMANO_ETIQUETA = 10
TAMANO_MINI = 9  # la cota de "nunca por debajo de esto"

# Alias en minúscula para las medidas que se escriben dentro del QSS.
tamano_cuerpo = TAMANO_CUERPO
tamano_etiqueta = TAMANO_ETIQUETA
tamano_titulo = TAMANO_TITULO

# -------------------------------------------------------------- duración
# Todas por debajo del presupuesto de un frame a 60 Hz (16.67 ms) están; lo que
# importa es que ninguna supere el frame y que haya unas pocas, consistentes.
DURACION_TOGGLE_MS = 140
DURACION_APARECER_MS = 180
DURACION_DESVANECER_MS = 160
DURACION_PULSO_MS = 1200  # un ciclo entero del pulso del micrófono
DURACION_LISTA_MS = 200  # entrada/salida de un elemento de lista

# Curvas: solo dos. Suavizar de más en un HUD que se refresca a 60 Hz se nota
# como retraso; entre estas dos está el equilibrio. Los nombres son los de
# ``QEasingCurve.Type`` (PascalCase, tal cual los pone Qt).
CURVA_SALIDA = "OutCubic"
CURVA_ENTRADA = "InOutQuad"

# --------------------------------------------------------------- estado
# Colores por estado, para que el mismo significado no se pinte de dos formas.
ESTADO_COLOR = {
    "idle": TINTA_SUAVE,
    "listening": VERDE,
    "processing": AMARILLO,
    "speaking": CIAN,
    "error": ROJO,
}

# Alturas de los elementos que hay que medir en la Fase 3.
ALTO_BARRA = ESPACIO_MD
ALTO_CHIP = 26
ALTO_FILA_HISTORIAL = 22

# Reglas que la Fase 3 deja escritas para poder comprobarlas.
REGLAS = {
    "duracion_max_ms": 200,
    "duracion_min_ms": 80,
    "radio_max_px": RADIO_PANEL,
    "tamano_min_px": TAMANO_MINI,
    "animar_solo_transform_opacidad": True,
    "nunca_animar_layout": True,
}