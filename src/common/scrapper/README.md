# Orquestador de scraping

`Scrapper` recibe URI iniciales y visita cada una antes de decidir qué hacer con sus enlaces. Usa `ThreadPoolExecutor` para limitar el paralelismo. Los procesadores son inyectables; por defecto, Playwright produce `PageCapture` y Jev clasifica grupos del DOM como `WebPageEntities`.

```python
from src.common.scrapper import Scrapper, ScrapperConfig

scrapper = Scrapper(ScrapperConfig(
    max_threads=3,
    max_hops=2,
    max_sites=40,
    max_retries=1,
    min_page_confidence=0.7,
    min_entity_confidence=0.7,
))
scrapper.subscribe(lambda event: print(event.uri, event.entity.role, event.doubtful))
summary = scrapper.run(['https://ejemplo.com/catalogo'])
print(summary.run_id, scrapper.audit.list_doubtful(summary.run_id))
```

`max_hops` cuenta las URI iniciales como salto 0. `max_sites` reserva cupos para URI canónicas únicas; los reintentos no consumen otros cupos. Por defecto, `same_host_only=False`: también se pueden seguir enlaces a otros hosts si Jev los clasificó como navegación. Para limitar el recorrido a los hosts iniciales, usa `same_host_only=True`.

Cada página se captura y clasifica normalmente. Si la confianza del tipo de página queda bajo `min_page_confidence`, se archiva como dudosa y **ninguno de sus saltos se toma automáticamente**. Si una entidad de navegación queda bajo `min_entity_confidence`, solo se detienen los enlaces de esa entidad. Los enlaces omitidos permanecen en el historial con su motivo. Cada entidad de contenido emite un `ContentEvent`, incluso si la confianza es baja; `event.doubtful` permite tratarlo aparte.

La lista de tipos de entidad puede ampliarse. `WebPageEntity` guarda papel, propósitos (`content`, `navigation` o ambos), texto y enlaces exactos del DOM, confianza, evidencia y selector. Los grupos clasificados como `other` también quedan archivados para inspección.

## Procesadores personalizados

Pasa `uri_to_html` y `html_to_entities` a `ScrapperConfig`. El primero recibe una URI y devuelve `PageCapture`; el segundo recibe esa captura y devuelve `WebPageEntities`. Ambos deben tolerar llamadas desde varios threads cuando `max_threads > 1`.

## Historial

Por defecto, `AuditStore` escribe en `data/scrapper/`, excluido de Git. SQLite indexa recorridos, visitas, reintentos, enlaces descubiertos con su decisión, eventos y resultados. `captures/` guarda **cada HTML renderizado completo** comprimido y la captura JPEG de página completa. También se archiva el HTML si Jev falla después de la captura.

```python
visits = scrapper.audit.list_visits(summary.run_id)
visit = scrapper.audit.get_visit(visits[0]['id'])
html = scrapper.audit.read_html(visit['id'])
screenshot_bytes = scrapper.audit.read_screenshot(visit['id'])
edges = scrapper.audit.list_edges(summary.run_id)
events = scrapper.audit.list_events(summary.run_id)
```

`get_visit` reúne la decisión, cada intento, enlaces salientes y eventos. `read_html(visit_id, attempt=n)` recupera una captura anterior para estudiarla o reevaluarla más adelante. La clave de Jev no se guarda en el historial.
