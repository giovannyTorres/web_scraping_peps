# Comparación retrospectiva después de dos correcciones

El recall documental observado pasó de **16/44 (36,4%) a 18/44 (40,9%)** al reprocesar los mismos originales con NLP 1.0.5. Es una mejora de desarrollo sobre errores ya observados, **no una evaluación fuera de muestra**. La recuperación permanece en **6/97 URLs conocidas (6,2%)** con redirecciones capturadas y verificadas, o 2/97 mediante coincidencia exacta.

Se utilizó una copia independiente del banco real: 300 personas, 401 artículos actuales globales —399 asociados a la lista y 152 personas con texto—, el mismo marco de 535 pares y las mismas 172 revisiones ya bloqueadas. La comparación probabilística conserva exactamente los mismos 143 pares, pesos y etiquetas. Las dos correcciones se eligieron después del desbloqueo a partir de falsos negativos de desarrollo; no se consultaron nuevas fuentes ni se descargaron artículos adicionales.

| Medición observada | NLP 1.0.4 | NLP 1.0.5 |
|---|---:|---:|
| Verdaderos positivos | 16 | 18 |
| Falsos positivos | 0 | 0 |
| Falsos negativos | 27 | 25 |
| Abstenciones positivas | 1 | 1 |
| Recall | 16/44 = 36,4% | 18/44 = 40,9% |
| Accuracy con abstenciones | 105/137 = 76,6% | 107/137 = 78,1% |
| Precisión entre positivos emitidos | 16/16 | 18/18 |

Permanecen seis papeles desconocidos y cuatro abstenciones sobre negativos conocidos. No se convierten en negativos para mejorar las cifras. La estimación ponderada del recall pasa de 36,4% a 40,3%; la accuracy, de 83,7% a 84,3%. Todavía hay **26 positivos conocidos omitidos o en abstención**. Los valores 16/16 y 18/18 no demuestran eficacia general de 100%.

El reprocesamiento productivo, incluida la atribución y el cálculo de 300 resultados, tardó 35,3 s sin nuevas peticiones. La verificación independiente confirmó igualdad exacta de entidades, documentos y metadatos, candidatos, asociaciones artículo/cuerpo, revisiones y resoluciones de identidad. La caché y el historial de red conservaron todas sus filas lógicas; una copia de SQLite puede tener bytes físicos distintos sin cambiar esas filas. Los originales y capturas siguen conservados y verificados por hash.

La identidad sigue requiriendo revisión: **0/44 documentos adversos conocidos de la muestra alcanzan un componente provisional positivo**, hay cero confirmaciones humanas y cero scores positivos validados. La accuracy de un score numérico continúa siendo N/D por falta de un objetivo externo independiente. Esta corrección no demuestra mayor cobertura de búsqueda, no resuelve los 97 documentos de referencia y no convierte un resultado pendiente en ausencia de incidentes.

Los tres suplementos originales permanecen intactos. `post-audit-comparison.json` contiene la matriz pareada, los denominadores, hashes y controles agregados, sin cuerpos, etiquetas individuales ni scores personales.
