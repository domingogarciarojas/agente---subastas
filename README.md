# Agente de Subastas del Estado 🏛️

Un pequeño programa que **cada lunes** lee las subastas nuevas del **Portal de
Subastas del BOE** (las del Estado: judiciales, notariales y de Hacienda),
las filtra según lo que te interese y te manda **un email** con la lista y el
enlace directo a cada una.

Funciona solo, en GitHub, y es **gratis**. Es el mismo estilo que tu agente de
licitaciones, pero apuntando a subastas.

---

## Qué hace exactamente

1. Lee el sumario diario del BOE por su **API oficial y gratuita** (datos abiertos).
2. Se queda solo con los **anuncios de subasta**.
3. Los separa en **🏠 inmuebles** y **🚗 vehículos/maquinaria**.
4. Te manda un correo ordenado, con el enlace a cada subasta en el BOE
   (que a su vez lleva a subastas.boe.es, con el edicto, las cargas y el depósito).

Ahora mismo viene configurado para **toda España** y **todos los bienes**
(inmuebles + vehículos), tal y como pediste.

---

## Puesta en marcha (una sola vez, ~10 minutos)

### 1. Crear el repositorio
- Crea un repositorio nuevo en GitHub (puede ser privado).
- Sube estos archivos tal cual (respetando la carpeta `.github/workflows/`).

### 2. Preparar el correo de Gmail
Para que el programa pueda enviarte emails necesita una **"contraseña de aplicación"**
(NO es tu contraseña normal de Gmail):
1. Activa la verificación en dos pasos en tu cuenta de Google.
2. Ve a **Cuenta de Google → Seguridad → Contraseñas de aplicaciones**.
3. Crea una nueva y copia los 16 caracteres que te da.

### 3. Guardar los "secretos" en GitHub
En tu repositorio: **Settings → Secrets and variables → Actions → New repository secret**.
Crea estos tres:

| Nombre | Valor |
|---|---|
| `GMAIL_USER` | tu correo, p. ej. `tucorreo@gmail.com` |
| `GMAIL_APP_PASSWORD` | la contraseña de aplicación de 16 caracteres |
| `DEST_EMAIL` | a dónde quieres recibir el aviso (puede ser el mismo correo) |

### 4. Encender el agente
- Ve a la pestaña **Actions** del repositorio y activa los workflows si te lo pide.
- Para probarlo ya mismo: **Actions → "Agente de Subastas del Estado" → Run workflow**.
- A partir de ahí se ejecuta solo cada lunes.

---

## Cómo ajustarlo (cuando quieras afinar)

Como pediste "toda España y ambos tipos", te llegarán **bastantes** subastas
(el BOE publica entre 50 y 200 al día). Cuando quieras reducir el ruido, edita el
archivo `.github/workflows/subastas.yml` y cambia estos valores (no hace falta
tocar el programa):

- **Solo tu zona:** `PROVINCIAS: "Madrid,Toledo,Guadalajara"`
- **Solo inmuebles:** `TIPO_BIEN: "inmuebles"`  (o `"vehiculos"`)
- **Filtrar por palabra:** `PALABRAS_CLAVE: "vivienda"` (solo lo que la contenga)
- **Descartar cosas:** `PALABRAS_EXCLUIR: "garaje,trastero"`
- **Precio máximo:** pon `ENRIQUECER: "1"` y `PRECIO_MAX: "150000"`
  (el modo enriquecido abre cada anuncio para leer el importe; tarda un poco más).

### Recibirlo a diario en vez de semanal
En el mismo archivo, cambia la línea del `cron` por:
```
- cron: "0 7 * * 1-5"
```
y pon `VENTANA_DIAS: "2"`.

---

## Notas

- **Fuente:** API de datos abiertos del BOE (sumario diario). Es pública, oficial
  y gratuita; solo se **lee**, no se rastrea nada prohibido.
- El agente guarda además una copia del último aviso en `ultimo_aviso.html`.
- Es un aviso **informativo**. Antes de pujar, revisa siempre el **edicto oficial**
  y las condiciones en subastas.boe.es.
