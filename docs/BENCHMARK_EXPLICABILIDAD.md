# Benchmark de explicabilidad y selección de arquitectura

El clasificador discriminativo local ofrece el mejor macro F1 de esta prueba y el menor coste entre los candidatos nuevos: **0.432**, con 30/72 papeles correctos y cero falsas etiquetas de acusado entre 63 notas no acusatorias. Se selecciona como apoyo contextual predeterminado, con abstención y sin aportar puntos al score. **Su recall de acusaciones es sólo 1/9 (11,1%)**: los resultados no justifican atribuciones automáticas ni utilizar la ausencia de una señal del clasificador para descartar una nota.

spaCy queda disponible como extractor opcional de menciones, dependencias, eventos y relaciones hipotéticas. Su papel predicho no vota ni sustituye al predeterminado en el modo enriquecido. El híbrido por acuerdo no mejora la calidad y se abstiene en 63/72 notas. Laya fue investigado y ejecutado antes de NLP clásico; no se descarta por intuición, sino por sus resultados medidos en este caso de uso.

## Evidencia y condiciones

Ejecución final: `2026-10-15T08:42:59.775039+00:00`. Referencia completa: [phase4_comparison.json](benchmarks/phase4_comparison.json). Protocolo: [BENCHMARK_PROTOCOL.md](BENCHMARK_PROTOCOL.md). Corpus: [context_challenge.jsonl](../tests/data/context_challenge.jsonl), 72 notas ficticias escritas manualmente, nueve por papel, 104 spans PER/ORG y 63 atribuciones actor→papel. SHA-256 `8425c256e6dcc97a1664577b6aa593b09a75123fc28adf6f07cc20f16e36816a`.

No hay noticias reales del proyecto en esta prueba. Las clases están balanceadas deliberadamente, las etiquetas proceden de un autor y no hubo adjudicación humana independiente. Los modelos/reglas se congelaron antes de abrir el challenge. El clasificador se entrenó en otro corpus; este conjunto no se utilizó para entrenamiento, selección de umbral ni ajuste de características.

Se respetaron las fases: investigación y piloto funcional Laya y discriminativo (fase 2), prueba de desarrollo NLP (fase 3), comparación común (fase 4). La primera comparación de spaCy 1.0.0 se conserva en [phase4_initial_v1.json](benchmarks/phase4_initial_v1.json). Una inspección independiente del challenge detectó negaciones de absolución y negaciones de negación; se corrigieron con ocho regresiones separadas en 1.0.1. La [regresión](benchmarks/phase3_nlp_v1_0_1_regression.json) y el [parche](benchmarks/nlp_v1_0_0_to_v1_0_1.patch) se conservan. La reevaluación 1.0.1 mantiene los mismos resultados de calidad en los 72 casos; no se presenta como una mejora del challenge.

Los componentes se midieron mediante sus API aisladas sobre el texto y la entidad del escenario; no se asigna a estas métricas el significado de una evaluación del flujo integrado de identidad, caché e interfaz. La resolución de identidad y la reconstrucción del score requieren sus pruebas propias.

## Clasificación de papeles

Todas las fracciones utilizan los 72 casos. `insufficient` cuenta como clase y como abstención: no se quitan sus errores del denominador. FP acusado = notas cuyo papel gold no es acusado y recibieron esa etiqueta; denominador 63.

| Candidato | Aciertos | Accuracy | Macro F1 | FP acusado / 63 | Salida insuficiente |
| --- | ---: | ---: | ---: | ---: | ---: |
| Reglas previas adaptadas | 17/72 | 0.236 | 0.216 | 13 | 4/72 |
| Laya: elección bruta | 23/72 | 0.319 | 0.236 | 4 | 0/72 |
| Laya: aplica abstención | 16/72 | 0.222 | 0.181 | 2 | 56/72 |
| Discriminativo local | 30/72 | 0.417 | 0.432 | 0 | 42/72 |
| spaCy 1.0.1 | 20/72 | 0.278 | 0.259 | 5 | 3/72 |
| Híbrido por acuerdo | 13/72 | 0.181 | 0.144 | 0 | 63/72 |

El runner Laya conserva una opción bruta y una bandera de abstención experimental (`maxProbs < 0.65` o truncamiento). Las dos filas corresponden a **las mismas inferencias**: una usa la opción bruta y la otra aplica esa bandera como `insufficient`. Hay 56 banderas y una nota truncada al presupuesto de 512 tokens, que también incluye instrucciones y opciones. El umbral no es una confianza calibrada. El prompt inglés se mantuvo fijo; su criterio de `negated` admite autonegativas, más ampliamente que la referencia, donde una autonegativa no elimina una acusación expresamente reportada. Esta diferencia limita la comparación y no se corrigió después de ver resultados.

El baseline es un adaptador documentado de las reglas existentes, sin añadir NLP ni cambiar el scoring. Su representación no diferencia víctima y denunciante. La selección de arquitectura nueva no modifica los resultados del motor anterior por medio de ese adaptador.

Cada celda siguiente es **precisión / recall / F1**; soporte nueve por papel. Cero indica ausencia de aciertos o de predicciones útiles para esa clase, no una capacidad medida como perfecta.

| Papel | Reglas previas | Laya bruto | Laya con abstención | Discriminativo | spaCy | Híbrido |
| --- | --- | --- | --- | --- | --- | --- |
| accused | 0.188 / 0.333 / 0.240 | 0.500 / 0.444 / 0.471 | 0.333 / 0.111 / 0.167 | 1.000 / 0.111 / 0.200 | 0.286 / 0.222 / 0.250 | 1.000 / 0.111 / 0.200 |
| victim | 0.000 / 0.000 / 0.000 | 0.500 / 0.222 / 0.308 | 1.000 / 0.111 / 0.200 | 1.000 / 0.222 / 0.364 | 1.000 / 0.222 / 0.364 | 1.000 / 0.111 / 0.200 |
| complainant | 0.000 / 0.000 / 0.000 | 1.000 / 0.111 / 0.200 | 0.000 / 0.000 / 0.000 | 0.833 / 0.556 / 0.667 | 1.000 / 0.111 / 0.200 | 1.000 / 0.111 / 0.200 |
| incidental | 0.128 / 0.667 / 0.214 | 0.000 / 0.000 / 0.000 | 0.000 / 0.000 / 0.000 | 0.667 / 0.222 / 0.333 | 0.173 / 1.000 / 0.295 | 0.667 / 0.222 / 0.333 |
| negated | 1.000 / 0.444 / 0.615 | 0.179 / 0.778 / 0.292 | 0.333 / 0.333 / 0.333 | 0.556 / 0.556 / 0.556 | 0.333 / 0.222 / 0.267 | 0.000 / 0.000 / 0.000 |
| resolved | 1.000 / 0.111 / 0.200 | 0.450 / 1.000 / 0.621 | 1.000 / 0.333 / 0.500 | 1.000 / 0.556 / 0.714 | 1.000 / 0.111 / 0.200 | 0.000 / 0.000 / 0.000 |
| insufficient | 0.750 / 0.333 / 0.462 | 0.000 / 0.000 / 0.000 | 0.143 / 0.889 / 0.246 | 0.190 / 0.889 / 0.314 | 1.000 / 0.333 / 0.500 | 0.127 / 0.889 / 0.222 |
| indirect | 0.000 / 0.000 / 0.000 | 0.000 / 0.000 / 0.000 | 0.000 / 0.000 / 0.000 | 0.500 / 0.222 / 0.308 | 0.000 / 0.000 / 0.000 | 0.000 / 0.000 / 0.000 |

La precisión 1.000 de `accused` en el discriminativo procede de **una** predicción acertada; otras ocho acusaciones no se detectan con ese papel. Su precisión de `insufficient` es 0.190: 34 de las 42 abstenciones se producen en casos que sí tenían otro papel de referencia. Es una pérdida de cobertura material que debe mostrarse junto con la prudencia observada.

## Relevancia y atribución incorrecta

Relevancia significa contexto útil para revisar a la entidad correcta, incluyendo ser víctima, denunciante, negación, resolución o relación indirecta. No significa culpabilidad ni incremento del score. Hay 54 notas relevantes y 18 no relevantes.

| Candidato | Accuracy relevancia | Macro F1 binario | Falsa acusación víctima/denunciante / 18 | Falsa acusación identidad ambigua/excluida/ausente / 9 |
| --- | ---: | ---: | ---: | ---: |
| Reglas previas adaptadas | 0.431 | 0.430 | 1 | 4 |
| Laya: elección bruta | 0.750 | 0.429 | 1 | 0 |
| Laya: aplica abstención | 0.389 | 0.388 | 1 | 0 |
| Discriminativo local | 0.625 | 0.619 | 0 | 0 |
| spaCy 1.0.1 | 0.486 | 0.486 | 0 | 0 |
| Híbrido por acuerdo | 0.333 | 0.314 | 0 | 0 |

Laya bruto marca las 72 notas como relevantes: su accuracy 0.750 equivale a contestar siempre «relevante» con esta prevalencia. La métrica no demuestra desambiguación. Cero falsas acusaciones en un subconjunto pequeño tampoco demuestra que un sistema haya resuelto la identidad o todos sus papeles.

## Entidades, atribución de papel y evidencia

NER se evalúa sólo sobre las 104 menciones nombradas PER/ORG, con límites exactos. No abarca entidades genéricas, lugares ni correferencias. El híbrido reutiliza el extractor spaCy: sus resultados NER no son una mejora independiente.

| Extractor | TP / FP / FN | Precisión | Recall | F1 |
| --- | ---: | ---: | ---: | ---: |
| spaCy PER/ORG tipado | 53 / 33 / 51 | 0.616 | 0.510 | 0.558 |
| spaCy límites sin tipo | 72 / 14 / 32 | 0.837 | 0.692 | 0.758 |

El discriminativo y el baseline sólo entregan un detector literal del objetivo: el JSON conserva su medición sin tipo por separado, pero no se le atribuye NER abierto. Laya no entrega spans y tiene NER/evidencia/relaciones como no disponibles, no como ceros inventados.

La atribución actor→papel usa la primera mención del objetivo que produjo el método y su papel final. Es un adaptador común frente a 63 referencias, **no una evaluación de extracción general de relaciones**. El resto del grafo de eventos, parentesco o propiedad no tiene etiquetas exhaustivas en este corpus.

| Método | TP / FP / FN actor→papel | Precisión | Recall | F1 | Fragmentos literales válidos | Cobertura caracteres gold con papel correcto |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Reglas previas adaptadas | 14 / 55 / 49 | 0.203 | 0.222 | 0.212 | 26/26 | 10.3% |
| Discriminativo local | 25 / 44 / 38 | 0.362 | 0.397 | 0.379 | 85/85 | 31.3% |
| spaCy 1.0.1 | 14 / 55 / 49 | 0.203 | 0.222 | 0.212 | 86/86 | 20.4% |
| Híbrido por acuerdo | 8 / 61 / 55 | 0.116 | 0.127 | 0.121 | 171/171 | 8.3% |

Todos esos fragmentos se verifican con `text[start:end] == quote`. Ese 100% acredita procedencia literal, **no** que el papel esté interpretado correctamente, que la afirmación sea verdadera o que haya contribuido al scoring. La cobertura gold incluye únicamente fragmentos emitidos con papel correcto en una nota cuyo papel final también fue correcto; mide coincidencia editorial, no apoyo semántico validado por una persona.

La fidelidad aritmética del score no se evalúa por comparar estos clasificadores: ninguno contribuye a sus puntos. Sus pruebas y correcciones demostradas se documentan en [SCORING_ACTUAL.md](SCORING_ACTUAL.md) y [CORRECCIONES_SCORING.md](CORRECCIONES_SCORING.md).

## Recursos y velocidad

Python 3.12.14, Linux x86-64, CPU y sin GPU. Cada método nuevo se ejecutó en un subproceso nuevo. Los 216 tiempos calientes por método corresponden a tres pasadas de 72 notas; la carga se mide por separado. Las máquinas compartidas no permiten considerar estos tiempos un límite contractual.

**Corrección de memoria:** la columna RSS usa [phase4_resources_corrected.json](benchmarks/phase4_resources_corrected.json), medido con `/proc/self/status` → `VmHWM` del espacio de memoria actual. Durante las pruebas finales se comprobó que `resource.ru_maxrss` puede heredar el máximo del padre a través de `fork/exec`, incluso con PID nuevo. Se reemplaza únicamente la interpretación del campo `methods.<método>.performance.peak_rss` del reporte original para baseline/discriminativo/spaCy/híbrido; el JSON original permanece íntegro. Calidad, predicciones y latencias no se recalcularon. La corrección ejecuta una sola pasada de los 72 textos, conserva las mismas salidas compactas y registra hashes de código/modelos y del evaluador [benchmark_context_resources.py](../scripts/benchmark_context_resources.py). No se repitió Laya: su RSS ya se medía mediante muestras psutil del proceso actual. La selección no cambia.

Laya se ejecutó con su runner ONNX CPU a dos hilos y una pasada de 72 notas; su RSS es una muestra al terminar cada nota, no un pico de asignación. Los demás workers fijan un hilo de bibliotecas matemáticas y ejecutan primero una nota de calentamiento. Estas diferencias impiden una comparación estrictamente idéntica de condiciones, aunque la diferencia de coste observada sea amplia.

| Método | Pared p50 / p95 nota (ms) | CPU p50 / p95 nota (ms) | Carga + primera nota (ms) | RSS (MiB) | Pesos/artefactos (MiB) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Reglas previas adaptadas | 0.42 / 0.62 | 0.42 / 0.62 | 3.1 | 12.93 | 0.00 |
| Discriminativo local | 0.64 / 3.74 | 0.64 / 3.74 | 53.2 | 18.15 | 1.37 |
| spaCy 1.0.1 | 4.53 / 10.12 | 4.53 / 10.12 | 1272.1 | 153.75 | 15.48 |
| Híbrido por acuerdo | 5.18 / 8.30 | 5.18 / 8.30 | 677.2 | 155.47 | 16.84 |
| Laya (ambas políticas) | 780.21 / 1129.10 | N/D por nota | 2839.8 sólo carga | 1831.29 observado | 809.88 |

El entorno spaCy aislado ocupa **302.62 MiB**, incluidos paquetes, modelo y herramientas del venv; excluye Python base. El modelo instalado representa 15.48 MiB. No sumar el modelo otra vez al total del venv. El discriminativo sólo requiere la biblioteca estándar y su artefacto de 1.37 MiB, aunque se midió en el mismo intérprete con paquetes opcionales presentes para mantener comparables los subprocesos.

El directorio local del venv Laya ocupa 67.86 MiB, pero hereda paquetes del sistema. Las dependencias fijadas situadas fuera de ese venv suman **138.42 MiB**; venv local más esas dependencias usadas son **206.27 MiB**, sin Python base ni los 809.88 MiB de pesos. La suma de archivos de todas las distribuciones fijadas, locales y heredadas, es 195.66 MiB; se superpone con el venv y no se suma otra vez. Estos son bytes de archivos instalados, no tamaño comprimido del instalador.

Los tiempos por entidad se guardan en `performance.warm_entity_wall_ms`, agrupando `entity.id`; la pareja `scenario_id=olmo-27` comparte ese ID. La suma corresponde a procesar sus notas, sin incluir una nueva predicción de riesgo de la entidad. CPU, pared, RSS y disco no son medidas intercambiables. No se midieron consumo eléctrico, hardware Windows ni GPU NVIDIA.

## Robustez y casos auditables

El JSON contiene todas las matrices de confusión y métricas por etiqueta de escenario. Esta selección muestra accuracy descriptiva; los grupos pequeños no estiman tasas poblacionales.

| Escenario/tag | n | Discriminativo | spaCy | Híbrido | Laya bruto |
| --- | ---: | ---: | ---: | ---: | ---: |
| identity_abstention | 4 | 0.750 | 0.250 | 0.750 | 0.000 |
| explicit_homonym_exclusion | 2 | 0.000 | 1.000 | 0.000 | 0.000 |
| entity_absent | 2 | 1.000 | 1.000 | 1.000 | 0.000 |
| multiple_negation | 2 | 0.500 | 0.000 | 0.000 | 1.000 |
| victim_complainant_accused | 1 | 0.000 | 0.000 | 0.000 | 0.000 |
| long_text | 1 | 0.000 | 0.000 | 0.000 | 0.000 |
| contradiction_pair | 2 | 0.000 | 1.000 | 0.000 | 1.000 |

Hay 71 entidades. La pareja temporal conserva una acusación histórica y una absolución posterior. spaCy y Laya conservan ambos papeles en sus salidas por nota; el discriminativo y el híbrido se abstienen en ambas. Esto no demuestra resolución automática de cronología: no se sintetiza un único papel final ni se elimina la nota histórica.

Ejemplos con referencia directa al corpus y a las predicciones por ID:

| ID | Referencia | Discriminativo | spaCy | Qué debe revisar una persona |
| --- | --- | --- | --- | --- |
| `challenge-v01` | victim | victim | incidental | La empresa sufrió el fraude; la palabra fraude no acredita autoría. |
| `challenge-c01` | complainant | complainant | complainant | La denunciante es distinta del administrador investigado. |
| `challenge-a03` | accused | complainant | negated | La acusada no es la asociación denunciante: el discriminativo invierte ese papel. |
| `challenge-u03` | insufficient | incidental | incidental | Dos homónimos sin identificadores: la referencia exige abstención; ambos métodos fallan al llamarlo incidental. |
| `challenge-r01` | resolved | insufficient | resolved | Absolución posterior con posible recurso; no borrar la acusación histórica. |

Estos ejemplos incluyen errores expresos; no se ocultan seleccionando únicamente explicaciones convincentes. Para cada uno se puede comprobar texto, spans, entidades, papel de referencia y salida del método sin consultar un servicio externo.

## Reproducción y límites de uso

```bash
python scripts/benchmark_explainability.py \
  --python /ruta/al/venv-nlp/bin/python \
  --methods discriminative advanced hybrid baseline \
  --repeats 3 \
  --laya docs/benchmarks/phase4_laya_challenge.json \
  --laya-environment /ruta/al/venv-laya \
  --output docs/benchmarks/phase4_comparison.json
```

La entrada Laya importa inferencias locales ya medidas por su runner; no las repite ni descarga pesos. [INVESTIGACION_MODELOS.md](INVESTIGACION_MODELOS.md) documenta fuentes, licencias, revisión de Jev/Laya/NanoJev/OpenJev y ejecución. [CLASIFICADOR_LOCAL.md](CLASIFICADOR_LOCAL.md) y [NLP_LOCAL.md](NLP_LOCAL.md) cubren instalación y mantenimiento.

Se comprobaron integridad del corpus, denominadores de precisión/recall/F1, coincidencias exactas tipadas, atribución de actor y rechazo de fragmentos inventados con verificaciones deterministas del evaluador. Los hashes de pesos, configuración, implementación y evaluador quedan en el JSON. No se hicieron cambios de umbral ni ajuste de modelos a raíz de estos resultados.

Antes de utilizarlo en decisiones reales deben medirse notas del proyecto revisadas localmente, identidad y correferencias, anotación independiente, desacuerdos entre revisores y representatividad temporal. Un margen alto, una etiqueta o una cita literal no sustituyen esas comprobaciones. La instrumentación del score permite revisar sus factores reales aun cuando el clasificador se equivoque o se abstenga.
