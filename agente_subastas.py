#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agente de Subastas del Estado (Portal de Subastas del BOE)
==========================================================

Cada vez que se ejecuta:
  1. Lee el sumario diario del BOE por su API oficial y gratuita
     (https://www.boe.es/datosabiertos/), de los últimos días.
  2. Se queda solo con los ANUNCIOS DE SUBASTA (judiciales, notariales
     y administrativas de Hacienda / Seguridad Social).
  3. Los clasifica en INMUEBLES y VEHÍCULOS/MAQUINARIA (y "otros").
  4. Aplica los filtros opcionales que tú quieras (provincia, precio,
     palabras clave, tipo de bien).
  5. Te manda un email con la lista y el enlace directo a cada subasta.

No hace falta tocar el código para ajustarlo: todo se configura con
"secretos"/variables en GitHub (ver README.md).

Fuente: API de datos abiertos del BOE (sumario diario). Legal y gratuita.
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
# CONFIGURACIÓN (todo se puede cambiar con variables de entorno / secretos)
# ---------------------------------------------------------------------------

# Días hacia atrás que se revisan en cada ejecución.
# Ponlo un poco mayor que la frecuencia con la que se ejecuta el agente:
# si corre 1 vez por semana, 7-8 días está bien.
VENTANA_DIAS = int(os.environ.get("VENTANA_DIAS", "7"))

# Tipo de bien que te interesa: "inmuebles", "vehiculos" o "ambos".
TIPO_BIEN = os.environ.get("TIPO_BIEN", "ambos").strip().lower()

# Provincias / palabras de zona (separadas por comas). Vacío = toda España.
# Ej.: "Madrid,Toledo,Guadalajara"
PROVINCIAS = [p.strip() for p in os.environ.get("PROVINCIAS", "").split(",") if p.strip()]

# Palabras que SIEMPRE deben aparecer (separadas por comas). Vacío = sin filtro.
PALABRAS_CLAVE = [p.strip() for p in os.environ.get("PALABRAS_CLAVE", "").split(",") if p.strip()]

# Palabras que descartan un anuncio (separadas por comas). Vacío = sin filtro.
PALABRAS_EXCLUIR = [p.strip() for p in os.environ.get("PALABRAS_EXCLUIR", "").split(",") if p.strip()]

# Enriquecer: abrir el texto de cada anuncio para clasificar mejor y sacar
# el importe. Es más lento (una petición por anuncio). 1 = sí, 0 = no.
ENRIQUECER = os.environ.get("ENRIQUECER", "0") != "0"
MAX_ENRIQUECER = int(os.environ.get("MAX_ENRIQUECER", "150"))

# Filtros de precio (solo se aplican si ENRIQUECER=1 y el anuncio trae importe).
PRECIO_MIN = float(os.environ.get("PRECIO_MIN", "0") or 0)
PRECIO_MAX = float(os.environ.get("PRECIO_MAX", "0") or 0)  # 0 = sin tope

# Email
GMAIL_USER = os.environ.get("GMAIL_USER", "")            # tu_correo@gmail.com
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")  # contraseña de aplicación
DEST_EMAIL = os.environ.get("DEST_EMAIL", GMAIL_USER)    # a dónde enviar el aviso

API_SUMARIO = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
TIMEOUT = 60
UA = {"User-Agent": "agente-subastas/1.0 (uso personal)"}

# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------

def normaliza(texto):
    """minúsculas y sin acentos, para comparar sin liarnos con tildes."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


def as_list(x):
    """La API del BOE devuelve un objeto si hay 1 y una lista si hay varios.
    Esto lo deja siempre como lista para poder recorrerlo igual."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


# Palabras que identifican un ANUNCIO DE SUBASTA.
RE_SUBASTA = re.compile(r"subasta", re.IGNORECASE)

# Clasificación por tipo de bien (se busca en el título y, si se enriquece,
# también en el texto del anuncio).
KW_INMUEBLE = [
    "inmueble", "vivienda", "piso", "chalet", "apartamento", "finca",
    "local", "nave", "garaje", "plaza de garaje", "trastero", "solar",
    "terreno", "parcela", "rustica", "urbana", "edificio", "casa",
    "duplex", "atico", "vivienda unifamiliar",
]
KW_VEHICULO = [
    "vehiculo", "turismo", "automovil", "coche", "camion", "furgoneta",
    "furgon", "motocicleta", "moto", "ciclomotor", "remolque", "tractor",
    "matricula", "embarcacion", "maquinaria", "autobus", "autocar",
]


def clasifica_bien(texto_norm):
    inmueble = any(k in texto_norm for k in KW_INMUEBLE)
    vehiculo = any(k in texto_norm for k in KW_VEHICULO)
    if inmueble and not vehiculo:
        return "inmueble"
    if vehiculo and not inmueble:
        return "vehiculo"
    if inmueble and vehiculo:
        return "mixto"
    return "otros"


def tipo_procedimiento(texto_norm, seccion, departamento):
    dep = normaliza(departamento)
    if "notaria" in texto_norm or "notarial" in texto_norm or "notaria" in dep:
        return "Notarial"
    if ("agencia" in dep and "tributaria" in dep) or "hacienda" in dep \
       or "administrativa" in texto_norm or "seguridad social" in dep \
       or "tesoreria" in dep:
        return "Administrativa (Hacienda/SS)"
    if "justicia" in normaliza(seccion) or "juzgado" in texto_norm \
       or "judicial" in texto_norm or "ejecucion" in texto_norm:
        return "Judicial"
    return "Otra"


RE_IMPORTE = re.compile(
    r"(?:tipo|valor|importe|tasaci[oó]n|puja)[^0-9]{0,40}"
    r"([0-9]{1,3}(?:[.\s][0-9]{3})*(?:,[0-9]{2})?)\s*(?:euros|eur|€)",
    re.IGNORECASE,
)


def extrae_importe(texto):
    """Devuelve el primer importe razonable encontrado (float) o None."""
    if not texto:
        return None
    for m in RE_IMPORTE.finditer(texto):
        crudo = m.group(1)
        limpio = crudo.replace(".", "").replace(" ", "").replace(",", ".")
        try:
            valor = float(limpio)
            if valor >= 1:
                return valor
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# LECTURA DEL BOE
# ---------------------------------------------------------------------------

def descarga_sumario(fecha):
    """Devuelve el JSON del sumario del BOE para una fecha (datetime.date),
    o None si ese día no hay boletín (fines de semana / festivos)."""
    url = API_SUMARIO.format(fecha=fecha.strftime("%Y%m%d"))
    try:
        r = requests.get(url, headers={**UA, "Accept": "application/json"},
                         timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  [aviso] fallo de red en {fecha}: {e}", file=sys.stderr)
        return None
    if r.status_code == 404:
        return None  # no hay BOE ese día
    if r.status_code != 200:
        print(f"  [aviso] {fecha}: HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        return r.json()
    except ValueError:
        print(f"  [aviso] {fecha}: respuesta no es JSON", file=sys.stderr)
        return None


def items_del_sumario(data, fecha):
    """Recorre la estructura del sumario y va soltando cada anuncio/disposición
    como un diccionario simple."""
    try:
        sumario = data["data"]["sumario"]
    except (KeyError, TypeError):
        return
    for diario in as_list(sumario.get("diario")):
        for seccion in as_list(diario.get("seccion")):
            nombre_seccion = seccion.get("nombre", "")
            for depto in as_list(seccion.get("departamento")):
                nombre_depto = depto.get("nombre", "")
                # Items directos del departamento
                for it in as_list(depto.get("item")):
                    yield _pack(it, nombre_seccion, nombre_depto, fecha)
                # Items dentro de epígrafes
                for ep in as_list(depto.get("epigrafe")):
                    for it in as_list(ep.get("item")):
                        yield _pack(it, nombre_seccion, nombre_depto, fecha)


def _pack(item, seccion, departamento, fecha):
    url_pdf = ""
    if isinstance(item.get("url_pdf"), dict):
        url_pdf = item["url_pdf"].get("texto", "")
    return {
        "id": item.get("identificador", ""),
        "titulo": item.get("titulo", ""),
        "url_html": item.get("url_html", ""),
        "url_xml": item.get("url_xml", ""),
        "url_pdf": url_pdf,
        "seccion": seccion,
        "departamento": departamento,
        "fecha": fecha,
    }


def texto_anuncio(url_xml):
    """Descarga el texto plano de un anuncio (desde su XML) para clasificar
    mejor y sacar el importe. Devuelve '' si falla."""
    if not url_xml:
        return ""
    try:
        r = requests.get(url_xml, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            return ""
        # Quitamos etiquetas XML de forma sencilla.
        crudo = re.sub(r"<[^>]+>", " ", r.text)
        return html.unescape(crudo)
    except requests.RequestException:
        return ""


# ---------------------------------------------------------------------------
# FILTRADO
# ---------------------------------------------------------------------------

def es_subasta(item):
    return bool(RE_SUBASTA.search(item["titulo"] or ""))


def pasa_filtros(item):
    """Aplica tipo de bien, provincia y palabras clave/excluir sobre el texto
    disponible (título + texto enriquecido si lo hay)."""
    base = item["titulo"] or ""
    if item.get("texto"):
        base = base + " " + item["texto"]
    tnorm = normaliza(base)

    # Tipo de bien
    if TIPO_BIEN == "inmuebles" and item["tipo_bien"] not in ("inmueble", "mixto", "otros"):
        # "otros" se deja pasar solo si el usuario no pidió filtrar estricto;
        # como pidió inmuebles, descartamos vehículos claros.
        if item["tipo_bien"] == "vehiculo":
            return False
    if TIPO_BIEN == "vehiculos" and item["tipo_bien"] == "inmueble":
        return False

    # Provincias / zona
    if PROVINCIAS:
        if not any(normaliza(p) in tnorm for p in PROVINCIAS):
            return False

    # Palabras clave obligatorias
    if PALABRAS_CLAVE:
        if not all(normaliza(p) in tnorm for p in PALABRAS_CLAVE):
            return False

    # Palabras a excluir
    if PALABRAS_EXCLUIR:
        if any(normaliza(p) in tnorm for p in PALABRAS_EXCLUIR):
            return False

    # Precio (solo si hay importe)
    if item.get("importe") is not None:
        if PRECIO_MIN and item["importe"] < PRECIO_MIN:
            return False
        if PRECIO_MAX and item["importe"] > PRECIO_MAX:
            return False

    return True


# ---------------------------------------------------------------------------
# RECOLECCIÓN PRINCIPAL
# ---------------------------------------------------------------------------

def recolectar():
    hoy = dt.date.today()
    vistos = set()
    subastas = []

    print(f"Revisando BOE de los últimos {VENTANA_DIAS} días...")
    for i in range(VENTANA_DIAS):
        fecha = hoy - dt.timedelta(days=i)
        data = descarga_sumario(fecha)
        if not data:
            continue
        n_dia = 0
        for item in items_del_sumario(data, fecha):
            if not item["id"] or item["id"] in vistos:
                continue
            if not es_subasta(item):
                continue
            vistos.add(item["id"])
            item["tipo_bien"] = clasifica_bien(normaliza(item["titulo"]))
            item["procedimiento"] = tipo_procedimiento(
                normaliza(item["titulo"]), item["seccion"], item["departamento"])
            item["importe"] = None
            subastas.append(item)
            n_dia += 1
        print(f"  {fecha}: {n_dia} anuncios de subasta")
        time.sleep(0.2)

    # Enriquecer (opcional): abrir el texto para clasificar mejor / sacar precio
    if ENRIQUECER and subastas:
        print(f"Enriqueciendo hasta {MAX_ENRIQUECER} anuncios (esto tarda un poco)...")
        for n, item in enumerate(subastas):
            if n >= MAX_ENRIQUECER:
                break
            txt = texto_anuncio(item["url_xml"])
            if txt:
                item["texto"] = txt
                item["tipo_bien"] = clasifica_bien(normaliza(item["titulo"] + " " + txt))
                item["importe"] = extrae_importe(txt)
            time.sleep(0.25)

    # Filtrar
    filtradas = [it for it in subastas if pasa_filtros(it)]
    print(f"Total subastas encontradas: {len(subastas)} | tras filtros: {len(filtradas)}")
    return filtradas


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

ETIQUETA_BIEN = {
    "inmueble": "🏠 Inmuebles",
    "vehiculo": "🚗 Vehículos y maquinaria",
    "mixto": "📦 Lotes mixtos",
    "otros": "📁 Otros / sin clasificar",
}
ORDEN_BIEN = ["inmueble", "vehiculo", "mixto", "otros"]


def construir_html(subastas):
    hoy = dt.date.today().strftime("%d/%m/%Y")
    total = len(subastas)

    filtros = []
    if TIPO_BIEN != "ambos":
        filtros.append(f"tipo: {TIPO_BIEN}")
    if PROVINCIAS:
        filtros.append("zona: " + ", ".join(PROVINCIAS))
    if PALABRAS_CLAVE:
        filtros.append("incluye: " + ", ".join(PALABRAS_CLAVE))
    if PRECIO_MAX:
        filtros.append(f"hasta {int(PRECIO_MAX):,} €".replace(",", "."))
    texto_filtros = " · ".join(filtros) if filtros else "toda España, todos los bienes"

    partes = [f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:720px;margin:auto;color:#1a1a1a">
<h2 style="color:#0b5394;margin-bottom:4px">Subastas del Estado — aviso del {hoy}</h2>
<p style="color:#555;margin-top:0">{total} subasta(s) nueva(s) en el Portal de Subastas del BOE.<br>
<span style="font-size:13px">Filtros activos: {html.escape(texto_filtros)}</span></p>"""]

    if total == 0:
        partes.append('<p>No ha habido subastas nuevas que encajen con tus filtros esta vez.</p>')
    else:
        por_bien = {}
        for it in subastas:
            por_bien.setdefault(it["tipo_bien"], []).append(it)

        for clave in ORDEN_BIEN:
            grupo = por_bien.get(clave)
            if not grupo:
                continue
            partes.append(f'<h3 style="color:#0b5394;border-bottom:2px solid #e0e0e0;'
                          f'padding-bottom:4px;margin-top:24px">{ETIQUETA_BIEN[clave]} '
                          f'({len(grupo)})</h3>')
            for it in grupo:
                titulo = html.escape(it["titulo"])
                enlace = it["url_html"] or it["url_pdf"]
                imp = ""
                if it.get("importe"):
                    imp = (f'<span style="color:#0b5394;font-weight:bold"> · '
                           f'{int(it["importe"]):,} €</span>'.replace(",", "."))
                partes.append(f"""<div style="margin:10px 0;padding:10px 12px;background:#f7f9fc;
border-radius:8px;border-left:3px solid #0b5394">
  <div style="font-size:14px;line-height:1.4">{titulo}</div>
  <div style="font-size:12px;color:#666;margin-top:4px">
    {html.escape(it["procedimiento"])} · {it["fecha"].strftime("%d/%m/%Y")}{imp}
  </div>
  <div style="margin-top:6px">
    <a href="{html.escape(enlace)}" style="font-size:13px;color:#0b5394">Ver anuncio en el BOE →</a>
  </div>
</div>""")

    partes.append("""<hr style="margin-top:28px;border:none;border-top:1px solid #e0e0e0">
<p style="font-size:12px;color:#999">
Fuente: sumario diario del BOE (datos abiertos, gratuito y oficial). Cada anuncio
enlaza al detalle en subastas.boe.es, donde están el edicto, las cargas, el depósito
y las condiciones. Este aviso es informativo; revisa siempre el edicto oficial antes de pujar.
</p></div>""")
    return "\n".join(partes)


def enviar_email(cuerpo_html, n):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and DEST_EMAIL):
        print("[aviso] Faltan credenciales de email (GMAIL_USER / "
              "GMAIL_APP_PASSWORD / DEST_EMAIL). No se envía correo.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏛️ Subastas del Estado: {n} nuevas"
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


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    subastas = recolectar()
    cuerpo = construir_html(subastas)

    # Guardamos siempre una copia local (útil para revisar / depurar).
    with open("ultimo_aviso.html", "w", encoding="utf-8") as f:
        f.write(cuerpo)

    # Enviar solo si hay algo (o si se fuerza con ENVIAR_VACIO=1).
    enviar_vacio = os.environ.get("ENVIAR_VACIO", "0") != "0"
    if subastas or enviar_vacio:
        enviar_email(cuerpo, len(subastas))
    else:
        print("No hay subastas nuevas; no se envía email (pon ENVIAR_VACIO=1 para recibirlo igualmente).")


if __name__ == "__main__":
    main()
