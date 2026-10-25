# Cálculo del consumo de Jev

La tarifa indicada por el usuario es **US$0,042 por millón de tokens de entrada**.
Los tokens de salida son gratuitos: **US$0**. El coste total de Jev corresponde
exclusivamente a los tokens de entrada.
No es obligatorio reportar el gasto de Jev al final de cada turno.
Cuando el usuario solicite un informe de consumo, aplicar las siguientes reglas.

Para calcular el coste de entrada:

```text
coste_entrada = tokens_entrada / 1_000_000 × 0,042
```

- Usar los tokens de entrada informados por la API y guardados en el histórico.
- Sumar todas las llamadas del recorrido, incluidos reintentos e intentos fallidos
  que tengan consumo informado. Separar la exploración de las pruebas de desarrollo.
- Si falta el consumo de alguna llamada, presentar el importe como parcial.
- Informar los tokens de salida por separado para auditar el uso; su coste es cero.
  El coste de entrada también es el coste total, si el consumo informado está completo.
- Mantener precisión durante el cálculo y redondear únicamente al presentar el resultado.

Ejemplo: 291.421 tokens de entrada cuestan **0,012239682** USD de entrada.

Tarifa proporcionada por el usuario; actualizarla cuando comunique un cambio.

# Generalización del extractor

- Las heurísticas de producción deben ser genéricas y estructurales: DOM,
  repetición, regiones, visibilidad, identidad de colecciones y actividad de red.
- Jev asigna significado mediante la taxonomía jerárquica; cada clasificación
  permite `other`. El tipo de página se deduce de un resumen de bloques reconocidos.
- No introducir selectores, rutas, textos o condiciones ajustados a un dominio
  concreto en el extractor. Las URL y expectativas específicas pertenecen a
  pruebas de regresión, nunca a reglas de producción.
- Terminar una solicitud de red no prueba paginación: comprobar su efecto sobre
  la colección identificada y conservar evidencia de antes y después.

# Presupuesto y recorrido

- El recorrido predeterminado combina amplitud y profundidad, alternando páginas
  y expansiones; no agotar todos los enlaces de un nivel antes de descender.
- Descubrir una URL no consume una visita. Consumir el cupo antes de iniciar
  su captura o clic. Las expansiones también consumen una visita y un salto,
  incluso si fallan o no tienen efecto. No reintentar automáticamente un clic.
- Deduplicar URL con normalización genérica y conservar parámetros de contenido.
  Las expansiones son estados distintos, deduplicados por URL/control/función.

# Preguntas sobre contenido compartido

- Las preguntas disponibles sobre una misma muestra deben viajar juntas en una
  sola llamada. Enviar su contexto una sola vez y referenciarlo por un ID explícito.
- No dividir las preguntas de una muestra para completar un lote de otros bloques.
  Los límites de muestreo y las omisiones deben quedar registrados.
- Conservar etapas separadas cuando dependen de una decisión anterior de Jev.
- Las cadenas de contenedores con un único hijo y sin texto propio representan
  un solo bloque en su límite interior. Conservar atributos y selectores de los
  envoltorios como evidencia; no alterar la captura original.
