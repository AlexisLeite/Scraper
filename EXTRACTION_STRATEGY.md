# Extracción guiada por modelos: pruebas del 29 de septiembre de 2026

## Muestra y resultados

Se capturaron con Chrome renderizado las fichas y listados de Vichy y NIVEA indicados por el usuario. Sibari Calming Cream se usó como quinto sitio de control. Los HTML y resultados completos quedaron en capturas temporales de la investigación original; no se incorporan capturas grandes al repositorio. Las etiquetas esperadas se revisaron manualmente contra el DOM y las acciones del navegador.

| Decisión | Casos | Jev | Modelo anterior, 8192 tokens |
|---|---:|---:|---:|
| Papel de grupos de navegación y controles | 8 | 8 | 2 |
| Tipo principal de página | 4 | 4 | 3 |
| Estado y recorrido de reseñas | 4 | 4 | 1 |
| Papel de grupos repetidos en listados | 7 | 7 | 1 |
| Papel de bloques de atributos de producto | 8 | 8 | 5 |

Estos son ensayos pequeños y los esquemas se ajustaron durante el estudio; las cifras no estiman precisión en sitios nuevos. En Sibari, Jev identificó la ficha y dos menús: tipos de producto y problemas de piel. Un grupo de líneas de marca quedó ambiguo (confianza 0,56), por lo que el sistema debe admitir abstención.

La clasificación forzada «elige el carrusel secundario» produjo un falso positivo de Jev en el listado Vichy. Clasificar cada grupo como una función concreta u «otro» corrigió el problema en los siete grupos ensayados. Las preguntas de relevancia `noul` del modelo anterior dieron 0,994–0,999 a todas las categorías; Jev priorizó PIGMENTACIÓN (0,79) y SIGNOS DE ENVEJECIMIENTO (0,83) para manchas y arrugas, y Desmaquillantes (0,87) para agua micelar.

## Cobertura y acciones verificadas

- El extractor anterior separó el texto principal, pero no preservó todas las secciones de reseñas ni toda la navegación. No debe ser la única representación para descubrir rutas o metadatos.
- En la ficha Vichy, ocho `<details>` cerrados dejaron solo 281 caracteres de texto útil tras el extractor anterior. Al abrirlos en una copia del DOM antes de procesar, quedaron 2549 caracteres, incluidos beneficios, ingredientes y aplicación.
- Vichy muestra un widget de reseñas con cero registros; NIVEA tiene 266 valoraciones y tres reseñas cargadas inicialmente. El modelo debe separar «existe widget» de «existen reseñas publicadas».
- Las acciones se verificaron en Chrome: «Más productos» Vichy aumentó las tarjetas de 12 a 16; «Cargar más» NIVEA aumentó reseñas de 3 a 13; «Mostrar más reseñas» Sibari aumentó de 6 a 11. Las tres acciones actualizan el DOM sin cambiar de URL. Un control no debe declararse navegable hasta comprobar su efecto.
- El listado NIVEA tiene diez tarjetas principales y carruseles secundarios con productos repetidos. La elección del grupo principal evita indexar duplicados como si fueran nuevos resultados.

## Estrategia propuesta

1. **Capturar y conservar evidencia.** Renderizar la página, retirar scripts, estilos y capas que tapan el contenido, y guardar DOM semántico, URL final, título, encabezados, destinos de enlaces, atributos útiles y JSON-LD. Los atributos y rutas solo generan candidatos; no les asignan significado final.
2. **Revelar contenido acotadamente.** Abrir `<details>` en una copia. Detectar controles colapsados en la región principal, activarlos uno por uno con un límite, y guardar el cambio de DOM. Evitar recorrer menús globales o disparar compras durante esta etapa.
3. **Construir un mapa de bloques.** Enumerar grupos de hermanos repetidos, paneles, barras de enlaces y controles con IDs estables; incluir encabezado, región, texto de todos los elementos, URLs y contexto cercano. El DOM limpio es la representación base; ningún resumen de texto elimina bloques del mapa.
4. **Decidir jerárquicamente con Jev.** Primero `product/listing/other`; luego clasificar **cada** grupo completo (`primary_products`, `secondary_products`, `categories`, `breadcrumb`, `pagination`, `site_menu`, `reviews`, `other`). En fichas, clasificar bloques (`benefits`, `ingredients`, `usage`, `reviews`, etc.). Extraer los valores exactos del DOM original una vez elegidos los bloques. JSON-LD aporta valores alternativos y evidencia con procedencia, nunca una sustitución silenciosa de lo visible.
5. **Descubrir y comprobar el recorrido.** Tras elegir una grilla, derivar y validar un selector contra sus elementos hermanos. Tras elegir una acción de cargar más, ejecutarla y exigir nuevos IDs/URLs o más registros. Si no cambia el DOM, registrar fallo o incertidumbre. Los carruseles se distinguen de paginación y no entran automáticamente a la cola del listado.
6. **Aplicar el objetivo después.** Puntuar solo categorías confirmadas y productos extraídos para decidir qué recorrer o indexar. Mantener una cola de URL o acciones, canonización conservadora, huellas de estados visitados, deduplicación de productos y límites de profundidad/acciones.

Cada resultado debería incluir `valor`, `evidence_ids`, `model`, `confidence` y `verification`. La confianza no sustituye la verificación del DOM. En la investigación original Jev fue más útil que el modelo anterior; el conector usa exclusivamente el `.env` de este repositorio.

## Límite de la copia

Esta copia no incluye las URL exactas de los cinco casos ni sus HTML de referencia. Las observaciones anteriores son una línea de base histórica, no expectativas verificables contra capturas que no están en el repositorio.

## Comprobación actual en páginas públicas comparables

Las URL exactas de la muestra original no estaban en esta copia. `tests/live_regression.py` contiene cinco URL públicas comparables usadas para comprobar el flujo actual. En la ficha de agua micelar de Vichy, Jev distinguió un widget vacío; en la ficha de NIVEA identificó reseñas publicadas y Playwright verificó «Cargar más» 3→13 sin cambio de URL. En el listado general de Vichy, Playwright verificó «Más productos» 12→36 en la página actual; la observación anterior 12→16 queda como resultado histórico de otra muestra. En Sibari, «Mostrar más reseñas» pasó de 6 a 11 nodos de reseña (5→10 en la copia limpia, que excluye un nodo oculto). En el listado actual de cuidado facial de NIVEA, el grupo principal tuvo diez tarjetas en el navegador y nueve URL únicas extraíbles; Jev separó los grupos secundarios. Estas cifras son observaciones de páginas vivas, no supuestos para el código.
