# Extractor semántico

La aplicación utiliza un único extractor: `SemanticMetadataStrategy`.
Las decisiones se apoyan en HTML, DOM, regiones de navegación, jerarquía,
repetición y vocabularios estándar. Las URL, nombres de clases, textos y rutas
específicos de sitios solo pueden aparecer en fixtures y pruebas de regresión.

## Captura y estados

La captura inicial es inmutable. Antes de finalizar se observan controles de
navegación declarados en cabeceras, menús y regiones de navegación. El hover no
hace clic ni sigue enlaces. Se bloquean navegaciones del documento durante la
observación. Los overlays visuales marcados por la captura se ocultan
provisionalmente; no se aceptan cookies ni se ejecutan botones de consentimiento.

Los estados con cambios conservan su HTML anterior y posterior y captura visual.
Cada intento registra control, resultado, enlaces agregados/retirados y error.
Los límites de tiempo y cantidad son explícitos. La ausencia de cambios o de
actividad de red no prueba cobertura completa de navegación ni paginación.
La cobertura distingue entrada estática, controles invisibles, fallos y límites.
Las expansiones de colecciones permanecen separadas de las observaciones de
menús. Descubrir destinos no autoriza una visita ni consume su cupo.

## Registros y navegación

Los tipos schema.org, microdatos y RDFa se interpretan según su vocabulario.
`ItemList` conserva sus miembros y sus referencias, incluso cuando no declaran
`Product`. Las URL de los miembros permiten asociar estructuralmente sus
encabezados, imágenes y textos al contenedor del DOM. Las tarjetas repetidas de
una colección pueden emitirse como elementos de listado sin afirmar una clase
comercial. Los valores sin marcado estándar viajan a Jev en muestras del DOM con
identificadores explícitos. Cada muestra lleva juntas todas sus preguntas sobre
tipo de bloque y campos; los valores de salida solo pueden referir a candidatos
de esa misma muestra. Cada decisión permite `other`, conserva confianza y selector,
y las omisiones y límites quedan registrados. No hay selectores, clases, textos
ni rutas específicos de tiendas en producción.

Las imágenes se leen del DOM original: `aria-hidden` no implica invisibilidad
visual. El precio actual, el anterior, el precio por unidad y el envío no se
confunden por reglas de posición. Jev asigna significado; el código conserva el
valor elegido y combina probabilidades solo para alias del mismo importe exacto.
La clase de página se deduce en una etapa posterior del resumen de bloques ya
reconocidos. Se registra cada consumo, también en errores; un consumo ausente
produce un gasto parcial. Una falla del modelo conserva el contenido estructural
y muestra que quedaron campos sin interpretar.

Los controles y listas de navegación permiten construir grupos con nombre,
ruta de ancestros y enlaces hijos. Un botón sin `href` puede ser un grupo; no se
inventa una URL para ese botón. El DOM completo sigue siendo evidencia aunque
algunos paneles estén cerrados. Visibilidad, estado observado y tipo declarado
se registran por separado.

## Presentación y auditoría

El portal presenta elementos de listados, contenido, categorías, enlaces y
metadatos. Cada fuente incluye JSON legible y su selector; los miembros asociados
al DOM conservan también el fragmento de HTML. Los límites y omisiones son visibles.
La exploración de menús permite consultar evidencia anterior y posterior.

`/api/extract` impone una visita y cero saltos en el servidor. La extracción es
independiente de la cola persistente y no incorpora destinos a esa cola.
La API solo admite el extractor semántico. Los extractores anteriores están fuera
de `src` y se conservan únicamente como fixtures de pruebas históricas.
