#!/usr/bin/env python3
"""
destacar.py — Lee el CSV del día y genera una cabecera con lo potencialmente
noticiable, para colocarla al principio del correo.

Dos pasos:
  1. Un prefiltro por reglas descarta el trámite puro (nombramientos rutinarios,
     cambios de denominación, depósito de cuentas). Barato y transparente.
  2. Lo que sobrevive se manda al modelo, que ordena y explica por qué.

El listado completo NO se toca: sigue yendo entero debajo y en el CSV.

Uso:
    python destacar.py --csv borme_alertas.csv --fecha 20260828 --out destacado.txt

Necesita GEMINI_API_KEY (por defecto) o, si PROVEEDOR_IA=anthropic,
ANTHROPIC_API_KEY.
"""

import argparse
import csv
import json
import os
import sys
import urllib.request

# Proveedor: "gemini" (capa gratuita) o "anthropic" (de pago).
PROVEEDOR = os.environ.get("PROVEEDOR_IA", "gemini")

MODELO_GEMINI = os.environ.get("MODELO_GEMINI", "gemini-2.5-flash")
MODELO_ANTHROPIC = "claude-sonnet-5"
MAX_ENTRADAS = 350          # tope de seguridad para el prefiltro

# Lo que casi nunca es noticia por sí solo.
RUIDO = [
    "nombramientos", "reeleccion", "cambio de denominacion social",
    "cambio de objeto social", "depos", "situacion concursal fin",
]

# Lo que casi siempre merece al menos una mirada.
SENAL = [
    "concurso", "insolvencia", "juzgado mercantil", "disolucion",
    "liquidacion", "extincion", "reduccion de capital", "fusion",
    "escision", "cesion global", "suspension de pagos",
    "quiebra", "convocatoria", "traslado de domicilio",
]

CONTEXTO = """Eres ayudante de una periodista de economía y trabajo de la
agencia EFE en Barcelona. Su ámbito es el tejido empresarial catalán:
reestructuraciones, concursos, empleo, sectores industriales del área
metropolitana y comarcas.

Recibes entradas del BORME de un día. Selecciona como mucho DIEZ que
merezcan que alguien las mire, y ordénalas de más a menos relevante.

Criterios de relevancia, en este orden:
1. Concursos, disoluciones y liquidaciones de sociedades que parezcan tener
   tamaño o actividad real, no sociedades patrimoniales ni vehículos vacíos.
2. Reducciones de capital importantes, que suelen indicar pérdidas.
3. Fusiones, escisiones y cesiones globales de activo y pasivo.
4. Empresas reconocibles, marcas conocidas o pertenecientes a grupos.
5. Constituciones con capital muy alto o con socios llamativos.

Reglas estrictas:
- Usa SOLO entradas de la lista. No añadas información que no esté ahí.
- No afirmes nada sobre la empresa que no diga el texto del BORME.
- Cita siempre el número de referencia para poder comprobarlo.
- Si algo te parece dudoso, inclúyelo igual pero dilo.
- Si el día no tiene nada destacable, dilo en una línea y no rellenes.

Formato de cada línea:
  NOMBRE DE LA EMPRESA (ref) — tipo de acto. Por qué puede interesar.

Sin encabezados, sin preámbulo, sin despedida. Solo la lista."""


def normaliza(t):
    import unicodedata
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def prefiltro(filas):
    """Se queda con lo que tiene alguna señal y no es puro trámite."""
    salida = []
    for f in filas:
        cuerpo = normaliza(f.get("texto", "") + " " + f.get("apartado", ""))
        if f.get("seccion") == "C":
            salida.append(f)                       # la sección C entra siempre
            continue
        if any(s in cuerpo for s in SENAL):
            salida.append(f)
        elif not any(r in cuerpo for r in RUIDO):
            salida.append(f)                       # actos que no sé clasificar
    return salida[:MAX_ENTRADAS]


def pregunta(entradas):
    lista = "\n".join(
        f"{e['referencia']} | {e['empresa']} | {e.get('texto', '')[:400]}"
        for e in entradas
    )
    if PROVEEDOR == "anthropic":
        return _anthropic(lista)
    return _gemini(lista)


def _pide(url, cuerpo, cabeceras):
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(),
                                 headers=cabeceras)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def _gemini(lista):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODELO_GEMINI}:generateContent")
    datos = _pide(url, {
        "system_instruction": {"parts": [{"text": CONTEXTO}]},
        "contents": [{"parts": [{"text": lista}]}],
        "generationConfig": {"maxOutputTokens": 4000, "temperature": 0.2},
    }, {
        "content-type": "application/json",
        "x-goog-api-key": os.environ["GEMINI_API_KEY"],
    })
    partes = datos["candidates"][0]["content"].get("parts", [])
    texto = "".join(p.get("text", "") for p in partes).strip()
    if not texto:
        raise RuntimeError(
            "respuesta vacia; sube maxOutputTokens o prueba otro modelo")
    return texto


def _anthropic(lista):
    datos = _pide("https://api.anthropic.com/v1/messages", {
        "model": MODELO_ANTHROPIC,
        "max_tokens": 1500,
        "system": CONTEXTO,
        "messages": [{"role": "user", "content": lista}],
    }, {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
    })
    return "".join(b["text"] for b in datos["content"] if b["type"] == "text")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="borme_alertas.csv")
    p.add_argument("--fecha", required=True)
    p.add_argument("--out", default="destacado.txt")
    args = p.parse_args()

    with open(args.csv, encoding="utf-8") as fh:
        filas = [f for f in csv.DictReader(fh) if f["fecha"] == args.fecha]

    if not filas:
        sys.exit(0)

    candidatas = prefiltro(filas)
    cabecera = [
        f"BORME Barcelona — {args.fecha}",
        f"{len(filas)} entradas en total, {len(candidatas)} pasan el prefiltro.",
        "",
        "POSIBLES INTERESES (selección automática, sin verificar)",
        "",
    ]

    try:
        cabecera.append(pregunta(candidatas))
    except Exception as e:
        cabecera.append(f"[No se pudo generar la selección: {e}]")
        cabecera.append("Revisa el listado completo de abajo.")

    cabecera += [
        "",
        "-" * 60,
        "Esto es una sugerencia, no un cribado. El listado completo va debajo",
        "y en el CSV adjunto. Nada de esto está verificado.",
        "-" * 60,
        "",
    ]

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(cabecera))


if __name__ == "__main__":
    main()
