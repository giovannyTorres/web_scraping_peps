# Validación de integración, rendimiento y almacenamiento

La prueba integrada termina correctamente: **21 entidades, 285 notas, 60 replays exactos**, API local autenticada y recuperación de una nota fuera del límite visual de 200. El ensayo de carga separado también pasa con **1.000 entidades, 1.000 documentos, 1.000 snapshots y 1.000 tareas de cola**. Son datos ficticios y tiempos observados en una máquina Linux compartida; no son una garantía de latencia ni una medida de cobertura web.

Ejecución del pipeline: `2026-10-15T09:00:33.046595+00:00`. Artefactos: [phase7_pipeline.json](benchmarks/phase7_pipeline.json) y [phase7_load_1000.json](benchmarks/phase7_load_1000.json). Código reproducible: [benchmark_explanation_pipeline.py](../scripts/benchmark_explanation_pipeline.py), [benchmark.py](../scripts/benchmark.py) y [test_explanation_performance.py](../tests/test_explanation_performance.py).

## Alcance y condiciones

Se ejecutó el backend real del repositorio en modo contextual `discriminative`, sin GPU ni NLP opcional. Los fixtures son veinte entidades con cuatro notas cada una y otra con 205 notas, todas de fuente oficial sintética para medir cálculo, persistencia y consulta sin confundirlo con atribución de identidad en noticias reales. Las URLs `fixture.invalid` sólo sirven como procedencia ficticia y nunca se consultan.

Los workers usan Python 3.12.14, Linux x86-64 y SQLite local. La primera evaluación calcula y guarda el resultado. Las lecturas calientes validan integridad, decodifican JSON y comprueban la vigencia del método. Las solicitudes HTTP atraviesan el mismo servidor autenticado y los endpoints que utiliza la interfaz; se miden desde un cliente loopback hasta terminar de recibir el JSON. **No incluyen renderizado del navegador ni interacción humana.**

Cada una de las cuatro operaciones de lectura fría se ejecuta como primera operación en un proceso independiente. La columna fría incluye lo que esa operación carga perezosamente; la inicialización previa del Engine se registra por separado en el JSON. El arranque del intérprete del sistema operativo está fuera de ese cronómetro. El RSS se toma de `/proc/self/status` → `VmHWM`, para evitar el máximo heredado que `ru_maxrss` puede conservar a través de fork/exec.

El guard bloquea DNS y conexiones Python fuera de loopback durante todo el pipeline. Se permiten únicamente las 67 conexiones locales usadas por la API. Los procesos de lectura fría bloquean todas las operaciones socket de Python; no intentan ninguna. No es una captura de tráfico de bibliotecas nativas.

## Cálculo y lecturas medidas

| Operación | Muestras | Pared p50 (ms) | Pared p95 (ms) |
| --- | ---: | ---: | ---: |
| Explicación backend, entidad de cuatro notas | 60 | 6.34 | 6.55 |
| Snapshot histórico validado, entidad pequeña | 60 | 2.67 | 2.92 |
| Replay numérico/traza, entidad pequeña | 60 | 7.44 | 8.09 |
| Explicación backend, entidad de 205 notas | 3 | 51.61 | 52.56 |
| Explicación de run histórico, 205 notas | 3 | 41.17 | 41.19 |
| HTTP /api/explanation, entidad pequeña | 60 | 8.19 | 8.62 |
| HTTP /api/explanation, 205 notas | 3 | 66.96 | 68.32 |
| HTTP /api/run-evidence, nota fuera de página | 3 | 21.16 | 21.89 |

La primera evaluación de una entidad pequeña tarda **46.23 ms** y el recálculo del lote de veinte entidades, **148.42 ms**. La primera evaluación de las 205 notas tarda **363.98 ms**. Importar las 285 notas, persistirlas y ejecutar su análisis previo tarda 142.94 ms. El JSON conserva también CPU por etapa y CPU p50/p95 de las lecturas pequeñas.

Las tres muestras de la entidad extensa sólo describen ese caso. Un p95 de tres observaciones equivale al máximo y no estima una cola poblacional. Las sesenta muestras pequeñas corresponden a tres pasadas del lote de veinte entidades, no a sesenta entidades independientes.

| Primera operación tras reiniciar, proceso nuevo por fila | Pared operación (ms) | Importación + Engine + operación (ms) | RSS máximo (MiB) |
| --- | ---: | ---: | ---: |
| Explicación vigente pequeña | 39.03 | 43.77 | 28.50 |
| Lookup snapshot pequeño | 2.80 | 7.80 | 22.74 |
| Replay pequeño | 14.74 | 19.24 | 23.09 |
| Nota histórica fuera de las primeras 200 | 20.84 | 25.54 | 29.07 |

El pico del worker integrado que crea snapshots, verifica replays, atiende HTTP y procesa las 205 notas es **56.85 MiB**. Es memoria del proceso completo, no tamaño del modelo ni un requisito universal de RAM.

## Fidelidad y navegación a la evidencia

Se reconstruyeron **60 de 60** evaluaciones numéricas/trazas pequeñas sin diferencias y un replay frío adicional. En la prueba unitaria también se reproduce el snapshot de 205 notas. Se comprobaron dos citas originales y dos contrafactuales eliminando sus documentos fuente y ejecutando de nuevo el scorer: el resultado coincide con `score_without_event` y su delta. Este contraste evita validar el desglose únicamente repitiendo la fórmula de agregación en el test.

La cadena de auditoría y referencias de **330 eventos y 21 snapshots** pasa la verificación completa. El endpoint de entidad devuelve 200 de 205 notas; la más antigua queda fuera de esa página. `/api/run-evidence` recupera esa nota por run e ID y devuelve exactamente su cuerpo original. La respuesta dirigida ocupa 2,203 bytes; la explicación completa de 205 notas, 1,222,834 bytes. Una respuesta completa extensa sigue teniendo un coste de transferencia y renderizado que el lookup dirigido evita para una nota.

Las lecturas históricas comprueban la cadena completa de auditoría una vez por transacción y fallan si detectan alteraciones; el historial reutiliza esa comprobación en su transacción. El coste depende también del número total de eventos de auditoría. Las latencias anteriores se midieron con 330 eventos y no deben extrapolarse sin medir a un banco con muchos años de historial.

La cadena local no es una firma externa: una reescritura completa necesita contrastarse con un hash o respaldo conservado fuera de la base. El replay histórico verifica scoring y traza con sus entradas y fecha archivadas; no reinfiere los análisis auxiliares. Si cambia el fingerprint del scorer, responde `unsupported` y conserva la evaluación anterior.

## Almacenamiento real y redundancia

| Medición en el fixture integrado | Resultado |
| --- | ---: |
| SQLite tras checkpoint WAL | 5,046,272 bytes (4.81 MiB) |
| Cuerpos originales UTF-8 | 34,081 bytes |
| Payloads históricos estructurados | 2,280,045 bytes |
| Resultados actuales cacheados | 1,802,762 bytes |
| Documentos / enlaces / snapshots | 285 / 285 / 21 |
| Campos de cuerpo completo añadidos al manifiesto | 0 |

Los originales residen en `documents.text`; el manifiesto histórico conserva ID/hash del documento, entradas estructuradas y orden, sin añadir un segundo campo de cuerpo. Recalcular entradas idénticas mantiene 285 documentos y 21 snapshots: reutiliza el snapshot por contenido y no reinserta cuerpos. El JSON muestra estos conteos antes y después.

**Esto no significa cero redundancia textual.** Citas literales, menciones y trazas se conservan para auditoría, y el resultado actual cacheado comparte estructura con su versión histórica. En este fixture los 34 KB de originales producen 4,81 MiB de SQLite: índices, metadatos, análisis, explicaciones y resultados dominan el tamaño. El desglose por objeto SQLite está disponible en `storage.sqlite_page_bytes_by_object`. No se presenta el tamaño del modelo como si fuera el almacenamiento total del proyecto.

## Ensayo de carga de 1.000 entidades

Se reutilizó el script existente, sin modificarlo, con una carpeta nueva: `runtime/phase7-load/20261015-01`. Resultado `2026-10-15T08:54:01.098547+00:00`, `pass=true`.

| Etapa | Tiempo (s) |
| --- | ---: |
| Importar 1.000 entidades | 0.018 |
| Vinculación indexada exacta | 0.008 |
| Persistir/analizar 1.000 documentos | 0.455 |
| Calcular y guardar 1.000 scores/snapshots | 2.442 |
| Procesar 1.000 tareas con cuatro procesos | 2.604 |
| Verificar auditoría completa | 0.397 |

Rendimiento observado: **2195.61 documentos/s** de importación/análisis y **409.51 entidades/s** de scoring, cada una con una sola nota. Se obtienen exactamente 100 scores positivos de los 100 esperados, cero positivos validados sin revisión, ninguna discrepancia y 1.000 tareas finalizadas sin duplicación. La auditoría de 4,001 eventos y 1,000 snapshots es íntegra. SQLite ocupa 30.09 MiB en ese ensayo.

El ensayo de carga de 1.000 entidades coincidió con actividad concurrente sobre otra base en una máquina compartida; los tiempos incluyen esa condición. El pipeline final se repitió después de congelar backend y assets, y sus hashes coinciden con los archivos medidos. El ensayo de 1.000 entidades no ejecuta 1.000 solicitudes de explicación sobre una auditoría grande y no acredita su latencia de lectura. Su modelo de capacidad web incluido en el JSON es una estimación con supuestos, no una observación de scraping ni un SLA.

## Pruebas de regresión y reproducción

Las dos pruebas de rendimiento/integración pasan. El único umbral temporal en CI es deliberadamente amplio: **hasta cinco segundos** para recalcular veinte entidades con dos notas y abrir sus veinte explicaciones, sin ninguna operación socket. No se exige reproducir las medianas de esta máquina. El otro test comprueba recuperación histórica de la nota 205, ausencia de cuerpos completos en el manifiesto, no reinserción de documentos y replay exacto, sin imponer un límite temporal estrecho.

Una primera ejecución de estas pruebas detectó una discrepancia real de replay: una estructura `publisher_id_basis` usaba tuplas en memoria y listas al recuperarse de JSON. Se corrigió su salida JSON canónica antes de medir; el score numérico era idéntico y la traza ahora también se reproduce. Las pruebas no omiten diferencias de estructura aunque el número final coincida.

```bash
python -m unittest tests.test_explanation_performance -v
python scripts/benchmark_explanation_pipeline.py \
  --data-dir runtime/phase7-explanation-NUEVO \
  --entities 20 --notes 4 --heavy-notes 205 --repeats 3 \
  --output docs/benchmarks/phase7_pipeline.json
python scripts/benchmark.py --size 1000 \
  --data-dir runtime/phase7-load/NUEVO \
  --output docs/benchmarks/phase7_load_1000.json
```

Usar una carpeta nueva en cada ejecución; los scripts rechazan una base existente para evitar acumulaciones. Los reportes guardan versiones y hashes de los módulos exactos medidos. Cambios posteriores deben contrastarse con esos fingerprints antes de comparar o repetir un replay. Los datos temporales permanecen bajo `runtime/`, fuera de los entregables versionables.

La corrección de memoria de componentes aislados se conserva aparte en [phase4_resources_corrected.json](benchmarks/phase4_resources_corrected.json): no altera calidad ni tiempos del benchmark de modelos y no repite inferencia Laya. Véanse [BENCHMARK_EXPLICABILIDAD.md](BENCHMARK_EXPLICABILIDAD.md) y [DECISION_ARQUITECTURA.md](DECISION_ARQUITECTURA.md) para selección y limitaciones de los métodos.

## Validación consolidada de suite, navegador y paquete

La suite completa ejecutó 292 pruebas: en el entorno base terminaron 285 y se omitieron siete que requieren NLP opcional, en 9,839 s; en el entorno con NLP terminaron las 292, en 10,741 s. Estos tiempos describen esa ejecución y no añaden umbrales de CI al proyecto.

La [validación de navegador](benchmarks/ui_validation.json) registra **18 comprobaciones correctas** en Chromium 154 mediante Playwright 1.62.0, con tres entidades y 210 documentos sintéticos. Comprueba score/escala, factores y deltas no aditivos, cita Unicode después de 1.800 caracteres, nota fuera de las primeras 200, XSS como texto, separación de auxiliares, replay, revisión y retorno a la evaluación vigente, historial de sólo lectura, revisión obsoleta rechazada, cero sin falsa seguridad, offsets astrales, carrera al cambiar de entidad, móvil sin desbordamiento horizontal y ausencia de solicitudes externas/errores de navegador. La apertura de entidad observada fue 283,80 ms; es una observación, no una distribución de latencia de renderizado.

La [validación del paquete](benchmarks/package_validation.json) comprueba el wheel `peps_mx-2.2.0-py3-none-any.whl`, **389.610 bytes**, SHA-256:

```text
d3541be2e56a11bcf23a1019470cd36d61485a444b6bbcee9f94e415d13aced1
```

Se instaló desde archivos locales con `--no-index --no-deps --target` en un entorno limpio creado sin pip. No declara dependencias de ejecución; incluye el modelo JSON, sus avisos/licencias y los assets web. La prueba bloquea `socket.connect`, no importa bibliotecas científicas/ML y verifica score, traza, replay exacto y auditoría. Es una validación de la aplicación base local; los modelos NLP/Laya opcionales no se incluyen en ese wheel.

Quedan fuera de estas mediciones: precisión sobre noticias reales, tiempos de scraping/red, una distribución de renderizado de la interfaz, Windows, GPU y consumo eléctrico. Una cita literal y un replay correcto acreditan trazabilidad técnica; no demuestran veracidad de una noticia ni calidad contextual del clasificador.
