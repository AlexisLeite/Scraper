# Markup Studio

Aplicación local para analizar páginas comerciales con Jev. Playwright renderiza e interactúa con páginas, BeautifulSoup prepara el DOM y Codex CLI genera esquemas editables para la segunda pestaña. La clave de TypeSafe permanece en el servidor.

## Instalación

Requiere Python 3.11 o posterior. Crea un archivo `.env` en este directorio con `TYPESAFE_API_KEY=...`; está excluido de Git. Si la clave ya existe, no hace falta modificarla.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python server.py
```

Abre [http://127.0.0.1:8787](http://127.0.0.1:8787). En Windows usa `.venv\\Scripts\\python.exe` en los comandos. El servidor escucha solo en loopback. Para la varita de esquemas, instala Codex CLI por separado y asegúrate de que `codex` esté en `PATH`.

## Flujo

La primera pestaña acepta una URL o HTML renderizado importado. El HTML original y la copia limpia se pueden descargar durante la sesión. El navegador guarda los resultados resumidos y borradores en `localStorage`; para volver a descargar HTML hay que repetir el análisis. Los scripts, estilos y capas superpuestas se quitan de una copia del DOM, y los `<details>` se abren en ella. JSON-LD se conserva como evidencia. Los grupos candidatos se generan por estructura y Jev decide su función. Los controles propuestos se prueban con Playwright cuando se analiza una URL; el HTML importado no permite verificar interacciones. El objetivo de investigación se aplica a las rutas ya clasificadas.

La segunda pestaña clasifica texto con un esquema JSON editable. Todas las clasificaciones llaman a Jev. Codex CLI solo propone esquemas; el usuario puede editarlos antes de usar Jev.

```bash
.venv/bin/python -m pytest -q
```

Los resultados incluyen evidencia, confianza y estado de verificación. La confianza de Jev no equivale a una interacción comprobada. Los sitios pueden bloquear navegadores automatizados o cambiar sus páginas; usa la importación manual cuando sea necesario.

Para repetir las cinco comprobaciones públicas comparables de la estrategia (requiere red y llamadas a Jev):

```bash
.venv/bin/python tests/live_regression.py
```
