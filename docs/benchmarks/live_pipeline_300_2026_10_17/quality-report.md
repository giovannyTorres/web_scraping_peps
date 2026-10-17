# Evaluación real de 300 personas: volumen y calidad

La recuperación sigue siendo un límite material: el puente final recuperó **6 de 97 URLs documentales relevantes conocidas (6,2%)**, contando únicamente redirecciones previamente capturadas y verificadas. La coincidencia de URL exacta fue **2/97 (2,1%)**. Esta ejecución no demuestra una búsqueda equivalente a Google ni una solución con cobertura suficiente para descartar incidentes.

Se ejecutó el flujo productivo completo, con peticiones reales, conservación de originales, atribución y cálculo para 300 personas. Los resultados de abajo corresponden al modelo original `es-gazetteer-rules-1.0.4`, antes de nuevas correcciones posteriores a esta evaluación. No se cambiaron las etiquetas para mejorar las cifras.

## Volumen observado

| Corte independiente | Fase de adquisición | HTTP nuevos | Originales históricos conservados | Artículos actuales, todas las URLs | Artículos asociados a la lista | Personas con texto /300 |
|---|---:|---:|---:|---:|---:|---:|
| Frío, fuentes originales | 35 min 0 s | 3.451 | 368 | 368 | 366 | 143 |
| Primera reanudación | 3 min 3 s | 36 | 384 | 369 | N/D | N/D |
| Reanudación final corregida | 34 s | 7 | 391 | 376 | 374 | 141 |
| Puente editorial derivado | 32 s | 34 | 416 | 401 | 399 | 152 |

Los tiempos son de la fase de búsqueda/descarga; no representan el tiempo total del trabajo. La atribución fría tardó 14,5 s y el cálculo productivo de 300 resultados, 8,2 s. El puente parte de una copia del banco final, habilita nueve perfiles adicionales y solicita 25 originales entre 26 URLs ya descubiertas; una ya tenía cuerpo. Sus 32 s no son una nueva búsqueda fría de 300 personas. No hubo consultas nuevas ni semillas de URLs tomadas del conjunto de referencia.

Se preservaron 15 introducciones multimedia sin transcripción como referencias, separadas de los artículos disponibles para analizar. Por eso el número histórico de originales es mayor que el de artículos actuales. Los dos artículos sin asociación actual a la lista tampoco se cuentan como cobertura de personas.

La primera reanudación terminó sin una proyección vigente de candidatos: el auditor rechazó su marco de muestreo y conservó el diagnóstico, sin reconstruirlo silenciosamente. Por ello no se publica una estimación de calidad para ese corte. La ejecución siguiente publicó la proyección completa. Cada corte tiene su propio denominador; sus diferencias no constituyen un experimento controlado.

## Precisión, recall y accuracy documental

El muestreo probabilístico incluyó 143 pares persona/URL en cada corte válido: hasta 60 municipales, 60 notarios previos y el censo de 23 pares de notarios nuevos. Los marcos completos tenían 484, 496 y 535 pares, respectivamente. Se revisaron además muestras diagnósticas por persona, sin mezclarlas en la estimación probabilística. Las revisiones totales por corte fueron 171, 170 y 172; 277 pares distintos quedaron etiquetados y bloqueados durante todo el trabajo.

La verdad evaluada es el **papel reportado en la publicación**, no culpabilidad ni riesgo actual. Una víctima, testigo, autoridad receptora o notario que sólo certifica una escritura no hereda la conducta atribuida a otra persona.

| Corte | TP | FP | FN | Abstenciones positivas | Precisión observada | Recall observado | Accuracy, penalizando abstención |
|---|---:|---:|---:|---:|---:|---:|---:|
| Frío | 18 | 0 | 21 | 1 | 18/18 | 18/40 = 45,0% | 106/130 = 81,5% |
| Reanudación final | 18 | 0 | 21 | 1 | 18/18 | 18/40 = 45,0% | 112/136 = 82,4% |
| Puente derivado | 16 | 0 | 27 | 1 | 16/16 | 16/44 = 36,4% | 105/137 = 76,6% |

Hubo 13, 7 y 6 papeles desconocidos en esas muestras, respectivamente; no se convirtieron en negativos. También hubo 2, 2 y 4 abstenciones sobre papeles negativos conocidos. El recall conserva las abstenciones positivas en su denominador. La accuracy incluye todas las etiquetas conocidas, incluidas las abstenciones. El valor 18/18 no compensa los 22 positivos que el flujo no detectó: no se presenta como eficacia general de 100%.

Al ponderar por las probabilidades de inclusión de los tres estratos, el recall estimado es 49,6%, 49,8% y 36,4%; la accuracy, 89,4%, 90,4% y 83,7%. Son estimaciones del marco recuperado de cada corte, con dependencia entre noticias y personas; no son prevalencia nacional ni una validación externa.

En cada muestra probabilística hubo 141 pertinencias adjudicadas favorables y dos sin resolver: 141/141 entre las conocidas, o entre 141/143 y 143/143 si se conservan las desconocidas. Se preservó además un error de persona con nombre extendido detectado en una muestra preliminar, fuera de la selección final por hash; no se borra del diagnóstico ni se generaliza la precisión muestral a todo el banco. Los cuerpos utilizables fueron 137/143 en frío y 143/143 en ambos cortes corregidos.

## Recuperación frente a referencias conocidas

La referencia se congeló antes de esta ejecución: **115 pares persona/URL relevantes, 97 URLs y 38 personas** de los 60 notarios ya utilizados en desarrollo. No se descuentan documentos por robots, acceso fallido o editor no soportado. Otros 15 pares con pertinencia desconocida no son negativos.

| Corte | URLs exactas /97 | URLs con redirección capturada y verificada /97 | Pares persona/URL exactos /115 | Pares con redirección verificada /115 |
|---|---:|---:|---:|---:|
| Frío | 1 | 5 | 1 | 5 |
| Reanudación final | 1 | 5 | 1 | 5 |
| Puente derivado | 2 | 6 | 3 | 7 |

Las 17 equivalencias de redirección admitidas se verificaron contra capturas HTTP antiguas y sus hashes; no se infirieron eliminando barras finales. El recall de todas las noticias en internet y el de los 240 casos adicionales son **no identificables** con esta referencia incompleta. Tampoco se combina este recall con la precisión muestral para producir un F1 de recuperación.

## Identidad, scoring e integridad

Se calcularon 300 filas de resultados en cada corte válido, pero **0/40 documentos adversos conocidos de la muestra fría y final, y 0/44 del puente, alcanzaron un componente provisional positivo**. La atribución quedó como candidata o no atribuida y el scoring excluyó vínculos que requieren resolver identidad y seleccionar los fragmentos pertinentes. Hubo cero confirmaciones humanas y cero scores positivos validados. Un resultado pendiente de identidad o evidencia insuficiente no significa ausencia de riesgo.

La accuracy de un score numérico y la precisión de identidad legal son N/D: no existe un objetivo externo independiente para medirlas. Las comprobaciones de integridad sí verificaron los 300 registros congelados, originales y respuestas reales por hash, extracción según la versión correspondiente, preservación de evidencia y cadena de auditoría. Los cuerpos heredados se contrastaron con el archivo frío inmutable; los nuevos, con sus transportes reales. Las URLs canónicas son únicas; el censo frío y de primera reanudación tampoco encontró cuerpos exactamente duplicados. Eso no convierte historias relacionadas en evidencia independiente.

## Alcance y trazabilidad

La cohorte comprende 150 municipales de conveniencia geográfica —99 de Jalisco y 51 de Nuevo León—, 60 notarios previos usados en ajustes y 90 notarios adicionales de un directorio público de CDMX, muestreados con semilla 20261017. Existían etiquetas históricas de incidentes reportados para 30 personas; las otras 270 permanecían desconocidas. No hay personas certificadas como libres de incidentes.

Las etiquetas fueron revisiones documentales de agentes asistentes, ciegas a las predicciones individuales y bloqueadas antes de unir salidas. No son adjudicación humana externa ni ceguera completa a la selección: nombres, contexto e identificadores podían revelar notoriedad o selección histórica. Tras bloquear las etiquetas frías, el auditor recibió por mensaje conteos agregados de estado de la primera reanudación; esa exposición quedó registrada antes de las etiquetas futuras y no incluyó predicciones individuales. Todos los cortes válidos estaban cerrados al desbloqueo global del 17/10/2026 a las 02:07:18 UTC.

Los archivos `quality-metrics.json` y `reference-recall.json` contienen denominadores, matrices, ponderaciones, límites y hashes de los recibos. No incluyen cuerpos, etiquetas individuales ni scores personales. Cualquier corrección desarrollada después de este desbloqueo requiere una comparación retrospectiva separada y no puede presentarse como prueba fuera de muestra.
