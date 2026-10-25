# Scrapper semántico

`Scrapper` administra cola, deduplicación, concurrencia y presupuesto.
`PageExecutor` abre y cierra el contexto de Playwright en el mismo trabajador.
El único extractor integrado es `SemanticMetadataStrategy`.

```python
from src.common.scrapper import Scrapper, ScrapperConfig, SemanticMetadataStrategy

scraper = Scrapper(ScrapperConfig(
    max_sites=1, max_hops=0, max_threads=1,
    strategy=SemanticMetadataStrategy(follow_navigation=False),
))
summary = scraper.run(['https://example.org/'])
```

La extracción usa HTML semántico, datos estructurados y colecciones repetidas.
Los campos sin marcado estándar se interpretan mediante Jev (`TYPESAFE_API_KEY`),
siempre dentro del extractor semántico y con valores referidos al DOM. El consumo
de entrada y salida y el coste de entrada se conservan en la auditoría. Las
pruebas locales simulan esta etapa, sin llamadas pagas por defecto.
Las observaciones de menús mediante hover permanecen dentro del documento.
No hacen clic, no expanden colecciones ni visitan destinos. Los estados nuevos
conservan evidencia anterior y posterior, y la cobertura explicita límites,
fallos y navegación no observable en entradas estáticas.

Las estrategias personalizadas de pruebas pueden implementar
`process(page, api)` y utilizar `enqueue`, `emit_content`, `consume_hop` y
`report_expense`. Cero debe ser explícito; importes desconocidos producen gasto
parcial. Las expansiones sí consumen visita y salto antes de interactuar y no se
reintentan automáticamente después de producir efectos.

`AuditStore` conserva visitas, estados, HTML, capturas, gastos y eventos. En
producción utiliza PostgreSQL; los directorios temporales explícitos usan SQLite.
Los identificadores del extractor en el historial no se reescriben al retirar una
implementación. La API y el ejecutor predeterminado solo crean trabajo semántico.
