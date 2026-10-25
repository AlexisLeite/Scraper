# Scrapper

Portal de extracción semántica con Next.js, FastAPI y Playwright. El único
extractor disponible en la aplicación es `SemanticMetadataStrategy`: utiliza
HTML, DOM, metadatos y vocabularios estándar. Los campos comerciales sin marcado
estándar se interpretan con Jev dentro del mismo extractor; no se habilitan otros
extractores. Configurar `TYPESAFE_API_KEY` en el entorno o `.env` para esa etapa.
Sin credenciales se conserva la extracción estructural y se registra la omisión.
Cada llamada guarda tokens y gasto; la tarifa de entrada es US$0,042 por millón
y los tokens de salida cuestan cero.

## Instalación y publicación

Requiere Python 3.11 o posterior y Node.js 22 o posterior.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
npm ci --prefix src/web
npm run build --prefix src/web
.venv/bin/python -m src.server
```

La aplicación escucha exclusivamente en la IPv4 de Tailscale, puerto 8787.
FastAPI sirve `src/web/out` y `/api` en la misma dirección.

```bash
sudo install -m 0644 deploy/scrapper-api.service /etc/systemd/system/scrapper-api.service
sudo systemctl daemon-reload
sudo systemctl disable --now scrapper-web-dev.service
sudo systemctl enable --now scrapper-api.service scrapper-worker.service
sudo systemctl restart scrapper-api.service
```

Para desarrollo de Next.js, inicia una API en el puerto 8788 y utiliza
`BACKEND_URL=http://127.0.0.1:8788 npm run dev --prefix src/web`. El servicio
`scrapper-web-dev.service` queda deshabilitado en producción.

## Extraer una página

**Extraer** recibe una URL y presenta elementos del listado, contenido, categorías,
metadatos, enlaces y fuentes. `POST /api/extract` recibe
`{"url":"https://ejemplo.com/pagina"}`. El servidor fija una visita, cero saltos y
un thread: no sigue enlaces ni incorpora páginas a la cola persistente.

Los menús se observan mediante hover, sin clics. Las navegaciones de documento
que un hover intente iniciar se bloquean. Los paneles visuales superpuestos se
ocultan temporalmente durante esa observación, sin aceptar cookies ni pulsar
botones; se restauran al terminar. La captura inicial se conserva intacta.
Los estados que revelan contenido guardan HTML anterior y posterior y captura
visual. Estas observaciones de navegación dentro del mismo documento no son
expansiones de colecciones ni visitas a otros destinos.

Se inspeccionan controles de navegación por regiones y atributos estándar,
con límites explícitos de cantidad y tiempo. La cobertura informa controles
intentados, fallidos, invisibles y pendientes; el HTML importado o estático no
se presenta como navegación explorada. El resultado incluye los estados
observados y los límites de extracción, incluso cuando la cobertura es parcial.

Los miembros de `ItemList` se relacionan con sus enlaces y contenedores del DOM.
Se conservan nombre, descripción, imagen, destino y evidencia cuando existen.
Las tarjetas repetidas sin un tipo declarado permanecen como elementos de
listado; no se inventan precios ni una clasificación comercial. Los grupos de
navegación conservan su ruta jerárquica y los destinos de sus hijos.

El resultado se conserva en la auditoría. La URL con `extract_run` permite
recuperarlo después de recargar o reiniciar la API. En «Ver fuente» se muestra
JSON formateado, evidencia del DOM y selectores. Las evidencias de hover pueden
abrirse desde «Exploración de menús y evidencia».

## Motor persistente

Las secciones del portal son Análisis, Extraer, Actividad, Dominios y páginas,
Configuración, Historial, Fallidas y Dominios marcados. La API y el motor son
servicios independientes: `scrapper-api.service` y `scrapper-worker.service`.
Extraer funciona independientemente de la pausa de la cola persistente.

Cada origen permite definir dominios y una lista blanca o negra de URL.
Las reglas son genéricas y se evalúan sobre URL normalizadas, conservando los
parámetros de contenido. Las páginas compartidas reutilizan la extracción.
La vigencia predeterminada es de 24 horas; el re-scrap manual permite renovarla.
La expiración por sí sola no programa recapturas.

Cada URL inicial admite dos límites opcionales:

- `max_pages`: entero positivo o `null`. Limita las páginas distintas cuya
  extracción se inicia, incluida la inicial. Descubrir enlaces no consume cupo.
  Fallos sí consumen la página; reintentos y renovaciones reutilizan su lugar.
- `max_urls_per_minute`: entero entre 1 y 60000 o `null`. Incluye reintentos y
  distribuye los inicios a lo largo del minuto, evitando ráfagas.

Los cupos se comprueban bajo el bloqueo transaccional de la cola. Las páginas
compartidas respetan los límites de todos sus orígenes activos. Las nuevas
páginas permanecen en cola cuando no hay cupo; ampliar o quitar el límite permite
continuar sin interrumpir trabajo iniciado. El recuento persiste mientras se
conserve el historial.

El recorrido combina amplitud y profundidad. Las visitas se consumen antes de
capturar, no al descubrir una URL. El motor admite hasta 50 slots y reutiliza un
Chromium por grupo de ocho actividades con contextos privados. El transporte
compartido depende de Playwright Python **1.63.0**, fijado en `requirements.txt`.
Las regresiones de compatibilidad deben ejecutarse antes de actualizarlo.

La admisión respeta la RAM global observada; no interrumpe trabajo iniciado por
alcanzar ese umbral. El límite individual usa PSS y puede detener solo la
actividad que lo supera. La CPU se regula mediante systemd. Los controles
muestreados de RAM no garantizan evitar un OOM físico.

Para actualizar el motor sin interrumpir navegadores activos:

```bash
sudo systemctl kill --kill-whom=main --signal=SIGUSR1 scrapper-worker.service
# Esperar a que quede inactivo y después:
sudo systemctl start scrapper-worker.service
```

El drenaje sigue supervisando hasta que terminen las actividades; las pausadas
requieren reanudación o cancelación explícita. La configuración antigua que
seleccionaba otro extractor se normaliza a semántico para trabajo nuevo. Los
resultados históricos conservan el extractor que realmente los produjo.

## Persistencia

La instalación utiliza PostgreSQL 18 dedicado en `127.0.0.1:5433`, base y usuario
`scraper`, con datos en `/mnt/scraper-db/postgresql/18/scraper`. Los servicios
dependen del volumen externo. La conexión se lee de `/etc/scraper/database.env`
(modo 0600), variable `SCRAPER_DATABASE_URL`, y no se guarda en Git.

`deploy/postgres.sql` define `service` (cola/configuración), `audit` (visitas,
eventos, gastos y capturas BYTEA), `contents`, `subscribers` y
`subscriber_contents`. Las asociaciones de suscriptores se crean mediante
triggers transaccionales. Consultar contenidos no los marca como entregados.
La eliminación explícita de historial conserva los registros compartidos.

API de suscripciones: `POST/GET /api/service/subscribers`,
`GET /api/service/contents?page=1` y
`GET /api/service/subscribers/{id}/contents?page=1`.
Las listas están paginadas de a 20. Los directorios temporales de pruebas usan
SQLite aislado; nunca deben utilizarse pruebas destructivas contra producción.

## Pruebas y retiro de extractores anteriores

```bash
.venv/bin/python -m pytest -q
npm run build --prefix src/web
.venv/bin/python tests/live_semantic_menu.py --local
.venv/bin/python tests/live_semantic_prices.py --live
```

La regresión viva guarda productos, categorías, límites y evidencia de una sola
visita. Las expectativas específicas de páginas pertenecen únicamente a pruebas;
las reglas de producción no contienen dominios, rutas ni textos de esos sitios.

Los extractores anteriores y su interfaz se conservan exclusivamente en
`tests/legacy` como fixtures de regresiones históricas. La aplicación no los
importa ni los exporta. Jev se utiliza como clasificador de campos dentro del
extractor semántico, sin recuperar los extractores retirados.
Los endpoints anteriores de análisis, clasificación y generación de esquemas
responden HTTP 410; las solicitudes nuevas solo admiten `strategy="semantic"`.
El historial y su contabilidad anterior permanecen disponibles.

Las pruebas PostgreSQL requieren `SCRAPER_TEST_DSN` apuntando a una base
**desechable**: vacían sus tablas. Las pruebas de fixtures históricos usan una
credencial separada de pruebas cuando hace falta; no utilizan la del servidor.
