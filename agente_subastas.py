#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agente de Subastas del Estado (Portal de Subastas del BOE) - versión con
filtros finos por tipo de bien, zona y precio.

Cada ejecución:
  1. Lee el sumario diario del BOE (API oficial y gratuita de datos abiertos).
  2. Se queda con los ANUNCIOS DE SUBASTA.
  3. Abre cada anuncio para saber QUÉ es (casa, parking, moto, coche...),
     DÓNDE está y CUÁNTO cuesta.
  4. Aplica tus filtros: solo las categorías que quieres, en tus zonas y por
     debajo de tu tope de precio (un tope distinto por categoría).
  5. Te manda un email ordenado con el enlace a cada subasta.

Fuente: https://www.boe.es/datosabiertos/  (pública, oficial, gratuita).
"""

import os
import re
import ssl
import sys
import time
import html
import smtplib
import unicodedata
import datetime as dt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

# ---------------------------------------------------------------------------
# CONFIGURACIÓN (se cambia con variables/ secretos en GitHub, sin tocar código)
# ---------------------------------------------------------------------------

VENTANA_DIAS = int(os.environ.get("VENTANA_DIAS", "7"))

# Zonas de interés (provincia, isla o municipio). Vacío = toda España.
ZONAS_DEFECTO = ("Barcelona,Huesca,Tenerife,Santa Cruz de Tenerife,"
                 "Hospitalet,L'Hospitalet,Badalona,Terrassa,Sabadell,Mataró,"
                 "Jaca,Monzón,Barbastro,Fraga,Sabiñánigo,"
                 "La Laguna,Arona,Adeje,La Orotava,Granadilla,Puerto de la Cruz")
ZONAS = [z.strip() for z in os.environ.get("ZONAS", ZONAS_DEFECTO).split(",") if z.strip()]

# Categorías que quieres recibir (el resto se descarta).
CATEGORIAS_ACTIVAS = [c.strip().lower() for c in
                      os.environ.get("CATEGORIAS_ACTIVAS",
                                     "residencial,parking,moto,coche").split(",")
                      if c.strip()]

# Tope de precio por categoría (0 = sin tope).
PRECIO_MAX = {
    "residencial": float(os.environ.get("PRECIO_MAX_RESIDENCIAL", "100000") or 0),
    "parking":     float(os.environ.get("PRECIO_MAX_PARKING", "100000") or 0),
    "moto":        float(os.environ.get("PRECIO_MAX_MOTO", "1000") or 0),
    "coche":       float(os.environ.get("PRECIO_MAX_COCHE", "5000") or 0),
}

# Rendimiento del "abrir cada anuncio".
MAX_ENRIQUECER = int(os.environ.get("MAX_ENRIQUECER", "3000"))
PAUSA = float(os.environ.get("PAUSA", "0.15"))

# Email
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DEST_EMAIL = os.environ.get("DEST_EMAIL", GMAIL_USER)

API_SUMARIO = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
TIMEOUT = 60
UA = {"User-Agent": "agente-subastas/2.0 (uso personal)"}

# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------

def normaliza(texto):
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


def as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


RE_SUBASTA = re.compile(r"subasta", re.IGNORECASE)

# --- Detección de categoría --------------------------------------------------
# Orden de prioridad: primero vivienda, luego parking, luego moto, luego coche,
# y por último las categorías que se descartan.
KW_RESIDENCIAL = ["vivienda", "casa", "piso", "apartamento", "chalet", "atico",
                  "duplex", "unifamiliar", "adosad"]
KW_PARKING = ["parking", "garaje", "plaza de aparcamiento", "plaza de garaje",
              "aparcamiento"]
KW_MOTO = ["motocicleta", "ciclomotor", "moto ", "scooter"]
KW_COCHE = ["turismo", "automovil", "coche", "vehiculo"]  # "vehiculo" -> coche por defecto
KW_DESCARTE_INMUEBLE = ["local", "nave", "oficina", "terreno", "solar", "parcela",
                        "finca rustica", "rustica", "trastero", "almacen",
                        "industrial", "hotel", "suelo"]
KW_DESCARTE_VEHICULO = ["camion", "furgoneta", "furgon", "tractor", "autobus",
                        "autocar", "remolque", "embarcacion", "barco", "maquinaria",
                        "excavadora", "grua"]


def detecta_categoria(texto_norm):
    """Devuelve una de: residencial, parking, moto, coche, o None (descartar)."""
    # Vehículos que descartamos explícitamente
    if any(k in texto_norm for k in KW_DESCARTE_VEHICULO):
        # salvo que además sea claramente coche/moto (raro); descartamos
        if not (any(k in texto_norm for k in KW_MOTO) or "turismo" in texto_norm):
            return None
    # Inmuebles que descartamos (local, nave, terreno...) si no hay vivienda
    hay_residencial = any(k in texto_norm for k in KW_RESIDENCIAL)
    hay_parking = any(k in texto_norm for k in KW_PARKING)
    if any(k in texto_norm for k in KW_DESCARTE_INMUEBLE) and not (hay_residencial or hay_parking):
        return None

    if hay_residencial:
        return "residencial"
    if hay_parking:
        return "parking"
    if any(k in texto_norm for k in KW_MOTO):
        return "moto"
    if any(k in texto_norm for k in KW_COCHE):
        return "coche"
    return None  # no sabemos qué es -> fuera (evita ruido)


def tipo_procedimiento(texto_norm, departamento):
    dep = normaliza(departamento)
    if "notaria" in texto_norm or "notarial" in texto_norm or "notaria" in dep:
        return "Notarial"
    if "hacienda" in dep or ("agencia" in dep and "tributaria" in dep) \
       or "administrativa" in texto_norm or "tesoreria" in dep \
       or "seguridad social" in dep:
        return "Administrativa (Hacienda/SS)"
    if "juzgado" in texto_norm or "judicial" in texto_norm or "ejecucion" in texto_norm:
        return "Judicial"
    return "Otra"


# --- Extracción de importe ---------------------------------------------------
RE_PAIR = re.compile(
    r"(valor de subasta|tipo de la primera subasta|tipo de subasta|tipo de la subasta|"
    r"valor subasta|puja m[ií]nima|importe de la subasta|valor de tasaci[oó]n|"
    r"tasaci[oó]n|aval[uú]o|tipo)[^0-9]{0,40}"
    r"([0-9][0-9.\s]*(?:,[0-9]{1,2})?)\s*(?:euros|eur|€)",
    re.IGNORECASE,
)
RE_ANY_EUR = re.compile(r"([0-9][0-9.\s]*(?:,[0-9]{1,2})?)\s*(?:euros|eur|€)", re.IGNORECASE)


def _num(crudo):
    s = crudo.strip().replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def extrae_importe(texto):
    """Devuelve (importe_representativo, None) intentando coger el valor de
    subasta / tipo (lo más favorable) antes que la tasación."""
    if not texto:
        return None
    preferidos, otros = [], []
    for m in RE_PAIR.finditer(texto):
        etiqueta = normaliza(m.group(1))
        val = _num(m.group(2))
        if val is None or val < 1:
            continue
        if any(w in etiqueta for w in ["subasta", "tipo", "puja"]):
            preferidos.append(val)
        else:
            otros.append(val)
    if preferidos:
        return min(preferidos)
    if otros:
        return min(otros)
    # último recurso: cualquier cifra en euros (puede ser imprecisa)
    genericos = [_num(m.group(1)) for m in RE_ANY_EUR.finditer(texto)]
    genericos = [g for g in genericos if g and g >= 1]
    return min(genericos) if genericos else None


# ---------------------------------------------------------------------------
# LECTURA DEL BOE
# ---------------------------------------------------------------------------

def descarga_sumario(fecha):
    url = API_SUMARIO.format(fecha=fecha.strftime("%Y%m%d"))
    try:
        r = requests.get(url, headers={**UA, "Accept": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  [aviso] fallo de red en {fecha}: {e}", file=sys.stderr)
        return None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        print(f"  [aviso] {fecha}: HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        return r.json()
    except ValueError:
        return None


def items_del_sumario(data, fecha):
    try:
        sumario = data["data"]["sumario"]
    except (KeyError, TypeError):
        return
    for diario in as_list(sumario.get("diario")):
        for seccion in as_list(diario.get("seccion")):
            for depto in as_list(seccion.get("departamento")):
                nombre_depto = depto.get("nombre", "")
                for it in as_list(depto.get("item")):
                    yield _pack(it, nombre_depto, fecha)
                for ep in as_list(depto.get("epigrafe")):
                    for it in as_list(ep.get("item")):
                        yield _pack(it, nombre_depto, fecha)


def _pack(item, departamento, fecha):
    url_pdf = item["url_pdf"].get("texto", "") if isinstance(item.get("url_pdf"), dict) else ""
    return {
        "id": item.get("identificador", ""),
        "titulo": item.get("titulo", ""),
        "url_html": item.get("url_html", ""),
        "url_xml": item.get("url_xml", ""),
        "url_pdf": url_pdf,
        "departamento": departamento,
        "fecha": fecha,
    }


def texto_anuncio(url_xml):
    if not url_xml:
        return ""
    try:
        r = requests.get(url_xml, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            return ""
        crudo = re.sub(r"<[^>]+>", " ", r.text)
        return html.unescape(re.sub(r"\s+", " ", crudo))
    except requests.RequestException:
        return ""


# ---------------------------------------------------------------------------
# FILTRADO
# ---------------------------------------------------------------------------

def en_zona(texto_norm):
    if not ZONAS:
        return True, ""
    for z in ZONAS:
        if normaliza(z) in texto_norm:
            return True, z
    return False, ""


def descartable_por_titulo(titulo_norm):
    """Si el título ya deja claro que es algo que NO queremos, lo saltamos sin
    abrir el anuncio (ahorra tiempo)."""
    tiene_no = any(k in titulo_norm for k in (KW_DESCARTE_INMUEBLE + KW_DESCARTE_VEHICULO))
    tiene_si = (any(k in titulo_norm for k in KW_RESIDENCIAL) or
                any(k in titulo_norm for k in KW_PARKING) or
                any(k in titulo_norm for k in KW_MOTO) or
                any(k in titulo_norm for k in KW_COCHE))
    return tiene_no and not tiene_si


# ---------------------------------------------------------------------------
# RECOLECCIÓN PRINCIPAL
# ---------------------------------------------------------------------------

def recolectar():
    hoy = dt.date.today()
    vistos = set()
    candidatos = []

    print(f"Revisando BOE de los últimos {VENTANA_DIAS} días...")
    for i in range(VENTANA_DIAS):
        fecha = hoy - dt.timedelta(days=i)
        data = descarga_sumario(fecha)
        if not data:
            continue
        n = 0
        for item in items_del_sumario(data, fecha):
            if not item["id"] or item["id"] in vistos:
                continue
            if not RE_SUBASTA.search(item["titulo"] or ""):
                continue
            vistos.add(item["id"])
            if descartable_por_titulo(normaliza(item["titulo"])):
                continue
            candidatos.append(item)
            n += 1
        print(f"  {fecha}: {n} anuncios de subasta a revisar")
        time.sleep(0.2)

    print(f"Abriendo {min(len(candidatos), MAX_ENRIQUECER)} anuncios para leer "
          f"tipo, zona y precio...")
    resultados = []
    for n, item in enumerate(candidatos):
        if n >= MAX_ENRIQUECER:
            print(f"  [aviso] límite de {MAX_ENRIQUECER} alcanzado; "
                  f"quedan {len(candidatos)-n} sin revisar.", file=sys.stderr)
            break
        txt = texto_anuncio(item["url_xml"])
        base = (item["titulo"] or "") + " " + txt
        tnorm = normaliza(base)

        categoria = detecta_categoria(tnorm)
        if categoria is None or categoria not in CATEGORIAS_ACTIVAS:
            time.sleep(PAUSA)
            continue

        dentro, zona = en_zona(tnorm)
        if not dentro:
            time.sleep(PAUSA)
            continue

        importe = extrae_importe(txt)
        tope = PRECIO_MAX.get(categoria, 0)
        if importe is not None and tope and importe > tope:
            time.sleep(PAUSA)
            continue

        item.update({
            "categoria": categoria,
            "zona": zona,
            "importe": importe,
            "procedimiento": tipo_procedimiento(tnorm, item["departamento"]),
        })
        resultados.append(item)
        time.sleep(PAUSA)

    print(f"Coinciden con tus filtros: {len(resultados)}")
    return resultados


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

ETIQUETA = {
    "residencial": "🏠 Casas / viviendas / apartamentos",
    "parking": "🅿️ Parkings y garajes",
    "moto": "🏍️ Motos",
    "coche": "🚗 Coches",
}
ORDEN = ["residencial", "parking", "moto", "coche"]


def euros(v):
    return f"{int(round(v)):,} €".replace(",", ".")


def construir_html(subastas):
    hoy = dt.date.today().strftime("%d/%m/%Y")
    total = len(subastas)
    zonas_txt = ", ".join(ZONAS[:3]) + ("..." if len(ZONAS) > 3 else "")
    topes = (f"casas/parkings ≤ {euros(PRECIO_MAX['residencial'])}, "
             f"motos ≤ {euros(PRECIO_MAX['moto'])}, coches ≤ {euros(PRECIO_MAX['coche'])}")

    p = [f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:720px;margin:auto;color:#1a1a1a">
<h2 style="color:#0b5394;margin-bottom:4px">Subastas del Estado — aviso del {hoy}</h2>
<p style="color:#555;margin-top:0">{total} subasta(s) que encajan con tus filtros.<br>
<span style="font-size:13px">Zonas: {html.escape(zonas_txt)} · Topes: {topes}</span></p>"""]

    if total == 0:
        p.append("<p>Esta vez no ha salido ninguna que cumpla tus condiciones.</p>")
    else:
        por_cat = {}
        for it in subastas:
            por_cat.setdefault(it["categoria"], []).append(it)
        for clave in ORDEN:
            grupo = por_cat.get(clave)
            if not grupo:
                continue
            p.append(f'<h3 style="color:#0b5394;border-bottom:2px solid #e0e0e0;'
                     f'padding-bottom:4px;margin-top:24px">{ETIQUETA[clave]} ({len(grupo)})</h3>')
            for it in sorted(grupo, key=lambda x: (x["importe"] is None, x["importe"] or 0)):
                enlace = it["url_html"] or it["url_pdf"]
                if it["importe"] is not None:
                    precio = f'<span style="color:#0b5394;font-weight:bold">{euros(it["importe"])}</span>'
                else:
                    precio = '<span style="color:#c0392b">importe por confirmar (mira el edicto)</span>'
                p.append(f"""<div style="margin:10px 0;padding:10px 12px;background:#f7f9fc;
border-radius:8px;border-left:3px solid #0b5394">
  <div style="font-size:14px;line-height:1.4">{html.escape(it["titulo"])}</div>
  <div style="font-size:12px;color:#666;margin-top:4px">
    {precio} · 📍 {html.escape(it["zona"])} · {html.escape(it["procedimiento"])} · {it["fecha"].strftime("%d/%m/%Y")}
  </div>
  <div style="margin-top:6px">
    <a href="{html.escape(enlace)}" style="font-size:13px;color:#0b5394">Ver anuncio en el BOE →</a>
  </div>
</div>""")

    p.append("""<hr style="margin-top:28px;border:none;border-top:1px solid #e0e0e0">
<p style="font-size:12px;color:#999">
Fuente: sumario diario del BOE (datos abiertos, oficial y gratuito). El precio se
lee automáticamente del anuncio y es orientativo: confirma siempre el importe, las
cargas y el depósito en el edicto oficial (subastas.boe.es) antes de pujar.
</p></div>""")
    return "\n".join(p)


def enviar_email(cuerpo_html, n):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and DEST_EMAIL):
        print("[aviso] Faltan credenciales de email. No se envía correo.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏛️ Subastas del Estado: {n} coinciden con tus filtros"
    msg["From"] = GMAIL_USER
    msg["To"] = DEST_EMAIL
    msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [d.strip() for d in DEST_EMAIL.split(",")], msg.as_string())
        print(f"Email enviado a {DEST_EMAIL}.")
        return True
    except Exception as e:
        print(f"[error] No se pudo enviar el email: {e}", file=sys.stderr)
        return False


def main():
    subastas = recolectar()
    cuerpo = construir_html(subastas)
    with open("ultimo_aviso.html", "w", encoding="utf-8") as f:
        f.write(cuerpo)
    enviar_vacio = os.environ.get("ENVIAR_VACIO", "0") != "0"
    if subastas or enviar_vacio:
        enviar_email(cuerpo, len(subastas))
    else:
        print("Sin coincidencias; no se envía email (ENVIAR_VACIO=1 para recibirlo igual).")


if __name__ == "__main__":
    main()
