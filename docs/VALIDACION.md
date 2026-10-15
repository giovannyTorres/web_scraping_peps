# Validación 2.1 · 15 de octubre de 2026

Ensayo local en Windows, Python 3.10.18 y CPU con 16 procesadores lógicos. Los resultados miden esta máquina y este conjunto; no son un SLA ni un estudio de precisión sobre prensa mexicana real.

## Adquisición real de noticias 2.1

Se verificaron seis RSS operativos en seis hosts con el fetcher de producción. El lote principal descubrió 224 URLs y obtuvo dos cuerpos de ARTICLE 19; el suplemento descubrió 40 URLs. Son candidatos de dos lotes, no personas resueltas ni una muestra representativa de descarga editorial.

| Lote y caché | Segundos | HTTP reales | Bytes recibidos / descomprimidos |
|---|---:|---:|---:|
| Principal, respuestas frías | 16.813 | 9 | 458,405 / 995,959 |
| Principal, caliente | 0.266 | 0 | 0 / 0 |
| RSS adicional, frío | 6.422 | 3 | 58,185 / 178,492 |
| RSS adicional, caliente | 3.078 | 1 | 57,755 / 178,062 |

Se conservaron reglas de robots y circuitos anteriores al vaciar sólo la caché de respuestas. La reconsulta de la fuente adicional necesitó HTTP conforme a sus directivas. Los bytes excluyen cabeceras/TLS/TCP. La investigación completa utilizó 27 HTTP, incluida una calibración de 14 solicitudes que detectó y permitió corregir un fallo de espera en el harness; esa calibración no se usa para comparar frío/caliente.

GDELT devolvió 429; Forbes y un artículo de Jornada devolvieron 403. No se evadieron restricciones. Sólo se descargaron cuerpos autorizados para evaluación privada no comercial; el catálogo comercial sigue desactivado. [FUENTES_NOTICIAS.md](FUENTES_NOTICIAS.md) detalla términos, decisiones, fuentes primarias y reproducción.

## Carga local de adquisición con HTTP simulado

Cada caso usó un padrón de 40 mil entidades y dos editores sintéticos. La red, DNS/TLS y las pausas se simularon con reloj virtual; los segundos siguientes miden CPU, persistencia y cola locales, no Internet.

| URLs distintas | Workers | Frío, segundos | Caliente, segundos | HTTP simulados en frío | Documentos tras deduplicar |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 1 | 39.282 | 6.111 | 1,006 | 200 |
| 1,000 | 4 | 29.030 | 6.041 | 1,006 | 200 |
| 10,000 | 4 | 337.418 | 24.194 | 10,028 | 2,000 |

Todos los controles pasaron: conservación de URLs, cuerpos descargados, deduplicación dentro del editor, preservación entre editores, grupos de sindicación, métricas exactas, pausas virtuales, cero descarga caliente y auditoría. El caso de 10 mil URLs conservó mil grupos de contenido; sus 10,028 HTTP fueron 10 mil cuerpos, 26 feeds y dos robots. La adquisición no ejecutó atribución ni scoring en esos casos.

Se repitió la adquisición con el código final y la trazabilidad pública de la política de fuente: 1,000 URLs, cuatro workers y 40 mil entidades en 31.479 s frío y 6.955 s caliente; 1,006 HTTP simulados en frío y cero en caliente, 200 documentos, todos los controles aprobados. El reporte `benchmark_descarga_carga_version_final.json` conserva sus hashes exactos. La suite local final aprobó 194 pruebas; la construcción del ejecutable y CI se verifican por separado.

La referencia inicial de 1,000 URLs tardaba 94.474 s con un worker y 100.318 s con cuatro. Las cifras finales muestran mejora local; no acreditan una mejora equivalente en red. Los reportes guardan hashes de módulos al comienzo del proceso. Después del ensayo grande hubo cambios en atribución/exportación/planner; un smoke posterior, 20 URLs y cuatro workers, aprobó todos los controles (0.736 s frío, 0.194 s caliente). No se presenta el ensayo grande como una ejecución integral del último hash de todos los módulos.

## Identidad y scoring 2.1

La batería principal pasó 120 desafíos sintéticos. Una auditoría independiente añadió 20 casos nuevos y detectó ocho fallos iniciales, siete de atribución positiva indebida. Tras corregir la adjunción de anclas a su propio sujeto, los mismos 20 casos y ocho integraciones atribución→scoring pasan. La auditoría conserva el resultado inicial; los casos corregidos son regresiones, no un conjunto externo para estimar precisión.

Las integraciones comprueban homónimos en un mismo documento, selección humana de una ocurrencia, exclusión de descubrimiento incluso revisado, invalidación al cambiar el padrón y ausencia de contaminación de una mención corroborada por otra adversa sin identidad resuelta. `matched` puede producir prioridad provisional; la validada exige revisión humana vigente.

Una auditoría final de nueve regresiones también pasó: reatribuir después de cambiar el padrón no revive una revisión humana obsoleta; las noticias importadas localmente pasan por el resolver; el hash de reglas invalida resoluciones y revisiones antiguas. Los vínculos retirados conservan su historia para auditoría.

El índice local de 40 mil registros y 100 mil variantes se construyó en 11.773 s. Procesó 40 mil textos cortos en 24.285 s, aproximadamente 1,647 por segundo, sin errores del fixture. Mil textos de al menos 1,902 caracteres tardaron 4.344 s. Cinco mil homónimos devolvieron cinco mil candidatos en 0.776 s, con listas de competidores acotadas. No se midió memoria ni precisión sobre noticias reales.

```powershell
python scripts/benchmark_news_load.py --help
python tests/test_identity.py --benchmark work/benchmark_identity.json
python tests/test_identity_holdout.py --audit work/auditoria_identidad_independiente.json
python -m unittest discover -s tests -v
```

Los JSON detallados se distribuyen con la entrega de noticias. [NOTICIAS.md](NOTICIAS.md) documenta las fases separadas, la automatización y el planner. `source_country=mexico` filtra país del medio, por lo que puede omitir cobertura extranjera de entidades mexicanas; deje el filtro vacío cuando ese sea el alcance autorizado. **No se ha demostrado cobertura histórica de 40 mil entidades en menos de una semana.**

## Referencia 2.0: carga persistida y scoring

Se importaron **40,000 entidades sintéticas y 40,000 documentos distintos**, con 4,000 positivos etiquetados y 36,000 textos neutrales. No son datos reales de personas.

| Etapa | Segundos |
|---|---:|
| Importar entidades | 1.089 |
| Vincular por índice | 0.651 |
| Persistir y analizar documentos | 52.655 |
| Calcular y guardar scoring | 81.946 |
| 1,000 trabajos compartidos entre 4 procesos | 11.859 |
| Verificar 81,001 eventos y contenido/puntuaciones | 1.621 |

Resultados: 40,000 puntuaciones, 4,000 positivos esperados y observados, cero discrepancias y cero puntuaciones validadas sin revisión humana. Los cuatro procesos completaron 243, 267, 232 y 258 trabajos: 1,000 tareas únicas terminadas. La base ocupó aproximadamente 284 MB.

Reproducir en una carpeta nueva:

```powershell
python scripts/benchmark.py --data-dir .\runtime\benchmark-nuevo --size 40000 --output .\runtime\benchmark.json
```

El reporte registra hashes de los módulos NLP/scoring y sus parámetros. El ensayo utiliza frases cortas regulares; documentos largos o entidades con miles de evidencias pueden requerir mucho más tiempo.

## Referencia 2.0: auditoría lingüística y scoring

La batería contiene 76 frases sintéticas etiquetadas y 180 variantes Unicode con verificación de offsets. Una revisión independiente añadió 30 desafíos sobre víctimas, testigos, fiscales, parentescos, condiciones, negaciones, archivo y absolución. La primera ronda detectó 13 fallos de 20; la siguiente detectó 3 de 10. Tras dos iteraciones pasan los 30. Estos casos ya conocidos son regresiones, no una estimación de precisión/recall externa.

Los tests también comprueban deduplicación, firmas de entidad y reglas, cambio de nombre, contenido alterado, fechas futuras, revisión humana, exclusión de consultas/índices y la separación de prioridad provisional y validada. Ejecutar:

```powershell
python -m unittest discover -s tests -v
```

## Referencia 2.0: muestra mexicana y red

Se seleccionaron 15 entidades de un pool de 32 mediante `random.Random(20261015).sample` y se añadió un control histórico explícito. La muestra incluye personas públicas, empresas y organismos. Se conservaron 16 extractos primarios verificados y el cuerpo de un comunicado histórico del DOJ descargado directamente por HTTPS. Una única entidad activó una señal en esos documentos; los demás ceros no acreditan ausencia de información adversa. La muestra y las puntuaciones se distribuyen localmente con el paquete, no como un ranking publicado en GitHub.

El caso histórico conserva su fecha y no acredita estado actual, firmeza ni situación penitenciaria. La muestra se interpreta como una comprobación de procedencia y funcionamiento, no como screening exhaustivo de cada entidad.

La prueba directa de red obtuvo HTTP 200 del DOJ y HTTP 429 de GDELT. El 429 produjo una pausa persistida; no se evadió. El modelo anterior de capacidad también era una proyección con supuestos, sin demostrar cobertura ni tiempo real para investigar 40 mil entidades.

La interfaz se probó en Chromium con búsqueda, expediente, fuentes, auditoría y vista de 390 px, sin errores de JavaScript. Los ejecutables y el paquete portable se verifican aparte de los tests del código.

## Límites operativos

SQLite WAL requiere disco local. No hay cluster distribuido ni PostgreSQL. No hay OCR/PDF automático, bypass de CAPTCHA, scraping autenticado ni modelo NER estadístico universal. Las listas oficiales importadas son snapshots explícitos; no implementan sincronización automática de bajas. Los comentarios públicos son pistas de baja ponderación y requieren revisión. Los pesos son heurísticos documentados y necesitan validación externa antes de emplearse en un proceso real de cumplimiento.
