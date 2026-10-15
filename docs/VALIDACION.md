# Validación del 15 de octubre de 2026

Ensayo local en Windows, Python 3.10.18 y CPU con 16 procesadores lógicos. Los resultados miden esta máquina y este conjunto; no son un SLA ni un estudio de precisión sobre prensa mexicana real.

## Carga persistida y scoring

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

## Auditoría lingüística y del scoring

La batería contiene 76 frases sintéticas etiquetadas y 180 variantes Unicode con verificación de offsets. Una revisión independiente añadió 30 desafíos sobre víctimas, testigos, fiscales, parentescos, condiciones, negaciones, archivo y absolución. La primera ronda detectó 13 fallos de 20; la siguiente detectó 3 de 10. Tras dos iteraciones pasan los 30. Estos casos ya conocidos son regresiones, no una estimación de precisión/recall externa.

Los tests también comprueban deduplicación, firmas de entidad y reglas, cambio de nombre, contenido alterado, fechas futuras, revisión humana, exclusión de consultas/índices y la separación de prioridad provisional y validada. Ejecutar:

```powershell
python -m unittest discover -s tests -v
```

## Muestra mexicana y red

Se seleccionaron 15 entidades de un pool de 32 mediante `random.Random(20261015).sample` y se añadió un control histórico explícito. La muestra incluye personas públicas, empresas y organismos. Se conservaron 16 extractos primarios verificados y el cuerpo de un comunicado histórico del DOJ descargado directamente por HTTPS. Una única entidad activó una señal en esos documentos; los demás ceros no acreditan ausencia de información adversa. La muestra y las puntuaciones se distribuyen localmente con el paquete, no como un ranking publicado en GitHub.

El caso histórico conserva su fecha y no acredita estado actual, firmeza ni situación penitenciaria. La muestra se interpreta como una comprobación de procedencia y funcionamiento, no como screening exhaustivo de cada entidad.

La prueba directa de red obtuvo HTTP 200 del DOJ y HTTP 429 de GDELT. El 429 produjo una pausa persistida; no se evadió. Por ello **no se ha demostrado cobertura web de 40 mil entidades en una semana**. El modelo de capacidad de 5.787 días supone 80 mil búsquedas a una por cinco segundos y 100 mil artículos únicos a una tasa agregada de uno por segundo; excluye bloqueos y revisión humana.

La interfaz se probó en Chromium con búsqueda, expediente, fuentes, auditoría y vista de 390 px, sin errores de JavaScript. Los ejecutables y el paquete portable se verifican aparte de los tests del código.

## Límites operativos

SQLite WAL requiere disco local. No hay cluster distribuido ni PostgreSQL. No hay OCR/PDF automático, bypass de CAPTCHA, scraping autenticado ni modelo NER estadístico universal. Las listas oficiales importadas son snapshots explícitos; no implementan sincronización automática de bajas. Los comentarios públicos son pistas de baja ponderación y requieren revisión. Los pesos son heurísticos documentados y necesitan validación externa antes de emplearse en un proceso real de cumplimiento.
