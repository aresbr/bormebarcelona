#!/usr/bin/env python3
"""
borme_bcn.py — Vigilancia diaria del BORME, bloque de Barcelona.

Usa la API oficial de datos abiertos del BOE (no scraping):
    GET https://boe.es/datosabiertos/api/borme/sumario/AAAAMMDD

Del sumario extrae:
  - Sección A (Empresarios. Actos inscritos) -> bloque de la provincia elegida.
  - Sección C (Anuncios y avisos legales) -> convocatorias, fusiones, escisiones,
    insolvencias, disoluciones. Ojo: la sección C NO está dividida por provincias.

Filtra por nombres de empresa vigilados y/o por tipos de acto, y escribe un CSV.

Uso:
    python borme_bcn.py                          # BORME de hoy
    python borme_bcn.py --fecha 20260820
    python borme_bcn.py --desde 20260801 --hasta 20260821
    python borme_bcn.py --empresas empresas.txt --actos actos.txt --out alertas.csv
    python borme_bcn.py --volcado-completo       # todo el bloque, sin filtrar

Ficheros de filtro: un patrón por línea, se ignoran líneas vacías y las que
empiezan por '#'. Se comparan sin acentos y sin distinguir mayúsculas.

Dependencias:  pip install requests
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

API = "https://boe.es/datosabiertos/api/borme/sumario/{fecha}"
UA = {"User-Agent": "vigilancia-borme/1.0 (uso periodistico)",
      "Accept": "application/json"}

# Bloques de la Sección A: el sufijo del identificador es el codigo de provincia
# por orden de codigo postal. Barcelona = 08.
PROVINCIA_DEFECTO = "BARCELONA"

# Tipos de acto habituales en la Sección A, por si quieres partir de aquí.
ACTOS_SUGERIDOS = [
    "Disolucion", "Extincion", "Liquidacion", "Concurso", "Insolvencia",
    "Reduccion de capital", "Fusion", "Escision", "Cesion global",
    "Cambio de domicilio", "Traslado de domicilio", "Ceses", "Dimisiones",
    "Socio unico", "Declaracion de unipersonalidad",
]

# Una entrada de la Sección A empieza por el número de referencia registral.
RE_ENTRADA = re.compile(r"^\s*(\d{4,8})\s*[-–]\s*(.+?)\s*\.?\s*$")


# ---------------------------------------------------------------- utilidades

def normaliza(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar patrones."""
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def carga_patrones(ruta) -> list:
    if not ruta:
        return []
    lineas = Path(ruta).read_text(encoding="utf-8").splitlines()
    return [normaliza(l.strip()) for l in lineas
            if l.strip() and not l.strip().startswith("#")]


def limpia_xml(bruto: str) -> str:
    """Quita etiquetas y deja un texto plano línea a línea."""
    txt = re.sub(r"<[^>]+>", "\n", bruto)
    txt = (txt.replace("&aacute;", "á").replace("&eacute;", "é")
              .replace("&iacute;", "í").replace("&oacute;", "ó")
              .replace("&uacute;", "ú").replace("&ntilde;", "ñ")
              .replace("&Ntilde;", "Ñ").replace("&amp;", "&")
              .replace("&quot;", '"').replace("&nbsp;", " "))
    lineas = [l.strip() for l in txt.splitlines()]
    return "\n".join(l for l in lineas if l)


# ------------------------------------------------------------------- fetching

def get_sumario(fecha: str, reintentos: int = 3):
    """Devuelve el sumario del BORME de esa fecha, o None si no hay boletín."""
    for intento in range(reintentos):
        r = requests.get(API.format(fecha=fecha), headers=UA, timeout=30)
        if r.status_code == 404:
            return None                      # festivo, sábado o domingo
        if r.status_code == 200:
            return r.json()["data"]["sumario"]
        time.sleep(2 ** intento)
    r.raise_for_status()


def get_texto(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA["User-Agent"]}, timeout=60)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return limpia_xml(r.text)


def items_del_sumario(sumario, provincia: str):
    """Saca (item_provincia_seccionA, [items_seccionC])."""
    diarios = sumario["diario"]
    if isinstance(diarios, dict):
        diarios = [diarios]

    bloque_a, items_c = None, []
    objetivo = normaliza(provincia)

    for diario in diarios:
        secciones = diario["seccion"]
        if isinstance(secciones, dict):
            secciones = [secciones]
        for sec in secciones:
            codigo = sec.get("codigo")
            if codigo == "A":
                items = sec.get("item", [])
                if isinstance(items, dict):
                    items = [items]
                for it in items:
                    if objetivo in normaliza(it.get("titulo", "")):
                        bloque_a = it
            elif codigo == "C":
                # la sección C se agrupa en <apartado>
                for ap in _lista(sec.get("apartado", [])):
                    for it in _lista(ap.get("item", [])):
                        it = dict(it)
                        it["apartado"] = ap.get("nombre", "")
                        items_c.append(it)
                for it in _lista(sec.get("item", [])):
                    it = dict(it)
                    it.setdefault("apartado", "")
                    items_c.append(it)
    return bloque_a, items_c


def _lista(x):
    if not x:
        return []
    return x if isinstance(x, list) else [x]


def url_de(item) -> str:
    """Prefiere XML (disponible en Sección A desde la v2.0 de la API, 05/2026)."""
    for clave in ("url_xml", "url_html"):
        if item.get(clave):
            return item[clave]
    pdf = item.get("url_pdf")
    return pdf["texto"] if isinstance(pdf, dict) else pdf


# -------------------------------------------------------------------- parsing

def trocea_seccion_a(texto: str) -> list:
    """Convierte el bloque provincial en una lista de {ref, empresa, actos}."""
    entradas, actual = [], None
    for linea in texto.splitlines():
        m = RE_ENTRADA.match(linea)
        if m and len(m.group(2)) > 3:
            if actual:
                entradas.append(actual)
            actual = {"ref": m.group(1), "empresa": m.group(2).strip(" ."),
                      "actos": []}
        elif actual:
            actual["actos"].append(linea)
    if actual:
        entradas.append(actual)
    return entradas


def casa(entrada, pat_empresas, pat_actos) -> bool:
    """Sin patrones, pasa todo. Con patrones, basta con que case uno."""
    if not pat_empresas and not pat_actos:
        return True
    emp = normaliza(entrada["empresa"])
    cuerpo = normaliza(" ".join(entrada["actos"]))
    if any(p in emp for p in pat_empresas):
        return True
    return any(p in cuerpo for p in pat_actos)


# ----------------------------------------------------------------------- main

def procesa_dia(fecha, provincia, pat_empresas, pat_actos, volcado):
    sumario = get_sumario(fecha)
    if sumario is None:
        return []

    bloque_a, items_c = items_del_sumario(sumario, provincia)
    filas = []

    if bloque_a:
        texto = get_texto(url_de(bloque_a))
        for e in trocea_seccion_a(texto):
            if volcado or casa(e, pat_empresas, pat_actos):
                filas.append({
                    "fecha": fecha,
                    "seccion": "A",
                    "apartado": provincia,
                    "referencia": e["ref"],
                    "empresa": e["empresa"],
                    "texto": " ".join(e["actos"]),
                    "url": url_de(bloque_a),
                })
    else:
        print(f"  aviso: sin bloque de {provincia} el {fecha}", file=sys.stderr)

    for it in items_c:
        titulo = it.get("titulo", "")
        if not (volcado or any(p in normaliza(titulo) for p in pat_empresas)
                or any(p in normaliza(it.get("apartado", "")) for p in pat_actos)):
            continue
        filas.append({
            "fecha": fecha,
            "seccion": "C",
            "apartado": it.get("apartado", ""),
            "referencia": it.get("identificador", ""),
            "empresa": titulo,
            "texto": "",
            "url": url_de(it),
        })
    return filas


def rango(desde, hasta):
    d = dt.datetime.strptime(desde, "%Y%m%d").date()
    h = dt.datetime.strptime(hasta, "%Y%m%d").date()
    while d <= h:
        if d.weekday() < 5:                  # el BORME no sale sábados ni domingos
            yield d.strftime("%Y%m%d")
        d += dt.timedelta(days=1)


def main():
    p = argparse.ArgumentParser(description="Vigilancia del BORME por provincia")
    p.add_argument("--fecha", help="AAAAMMDD (por defecto, hoy)")
    p.add_argument("--desde", help="AAAAMMDD")
    p.add_argument("--hasta", help="AAAAMMDD")
    p.add_argument("--provincia", default=PROVINCIA_DEFECTO)
    p.add_argument("--empresas", help="fichero con nombres a vigilar")
    p.add_argument("--actos", help="fichero con tipos de acto a vigilar")
    p.add_argument("--volcado-completo", action="store_true",
                   help="no filtrar: saca todo el bloque provincial")
    p.add_argument("--out", default="borme_alertas.csv")
    p.add_argument("--estado", default=".borme_visto.json",
                   help="registro de referencias ya avisadas")
    args = p.parse_args()

    pat_empresas = carga_patrones(args.empresas)
    pat_actos = carga_patrones(args.actos)

    if args.desde:
        fechas = list(rango(args.desde, args.hasta or args.desde))
    else:
        fechas = [args.fecha or dt.date.today().strftime("%Y%m%d")]

    visto = set()
    estado = Path(args.estado)
    if estado.exists():
        visto = set(json.loads(estado.read_text()))

    filas = []
    for f in fechas:
        print(f"BORME {f}...", file=sys.stderr)
        for fila in procesa_dia(f, args.provincia, pat_empresas,
                                pat_actos, args.volcado_completo):
            clave = f"{fila['fecha']}|{fila['seccion']}|{fila['referencia']}"
            if clave in visto:
                continue
            visto.add(clave)
            filas.append(fila)
        time.sleep(1)                        # cortesía con el servidor

    if not filas:
        print("Sin novedades.", file=sys.stderr)
        return

    campos = ["fecha", "seccion", "apartado", "referencia", "empresa",
              "texto", "url"]
    nuevo = not Path(args.out).exists()
    with open(args.out, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        if nuevo:
            w.writeheader()
        w.writerows(filas)

    estado.write_text(json.dumps(sorted(visto)))

    print(f"\n{len(filas)} coincidencias -> {args.out}\n", file=sys.stderr)
    for fila in filas[:40]:
        print(f"[{fila['fecha']}] {fila['empresa']}")
        if fila["texto"]:
            print(f"    {fila['texto'][:220]}")


if __name__ == "__main__":
    main()
