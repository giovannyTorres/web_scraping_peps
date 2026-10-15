# Clasificador contextual local: prueba discriminativa de fase 2

`peps/context_model.py` implementa `es-context-discriminative-1.0.0`, una alternativa pequeña de decisiones estructuradas. Complementa la investigación y la prueba de Laya documentadas en [INVESTIGACION_MODELOS.md](INVESTIGACION_MODELOS.md). Sus predicciones son hipótesis auxiliares: no modifican el scoring y no se presentan como causas de sus puntos.

La arquitectura usa TF-IDF de palabras de uno a tres tokens y caracteres de tres a cinco posiciones, con regresión logística multiclase. Reemplaza el nombre o alias exacto de la entidad por `ENTITY_TARGET` para clasificar su papel contextual. El entrenamiento usa scikit-learn; la inferencia reproduce sus operaciones con Python estándar, vocabulario, IDF y coeficientes numéricos en JSON. No hay pickle, chatbot, modelo generativo, Hugging Face, API de inferencia, descarga automática de pesos ni GPU obligatoria.

## Uso e instalación

La inferencia funciona con el Python del proyecto, versión 3.10 o posterior, y no añade dependencias. El archivo `peps/models/context-role-es-v1.json` está incorporado al repositorio y ocupa 1,432,196 bytes. No se ejecutan instalaciones ni peticiones de red al importar o utilizar el módulo. La prueba automatizada bloquea sockets e importaciones de scikit-learn, NumPy, SciPy, spaCy, PyTorch y Transformers durante una inferencia en un proceso nuevo.

Ejemplo sintético, ejecutable desde la raíz del repositorio:

```python
from peps.context_model import analyze, get_model_info

entity = {
    "id": "ejemplo-sintetico",
    "name": "José Peña Villaluz",
    "kind": "person",
    "aliases": [],
    "context": {},
}
text = "José Peña Villaluz denunció a los responsables de fraude ante las autoridades."
result = analyze(text, entity)
print(result["role"], result["relevance"], result["abstained"])
for finding in result["findings"]:
    assert text[finding["start"]:finding["end"]] == finding["quote"]
    print(finding["quote"])
print(get_model_info()["artifact_sha256"])
```

Este ejemplo debe devolver `complainant`, `reporter_context`, `False`. El fragmento identifica al denunciante en el texto inventado; no demuestra la verdad de una denuncia ni la identidad de una persona real.

## Contrato de resultados

| Campo | Interpretación |
| --- | --- |
| `role` | Papel contextual final; `insufficient` cuando el motor se abstiene. |
| `predicted_role` | Categoría con mayor función de decisión antes de la abstención. |
| `relevance` | Descripción contextual del papel; no es puntuación ni probabilidad de riesgo. |
| `decision_margin` | Diferencia entre las dos mayores funciones de decisión; no está calibrada como probabilidad. |
| `abstained` | Señala evidencia ausente, margen insuficiente, papeles incompatibles o presupuestos excedidos. |
| `findings` | Clasificación por cláusula, cita exacta, offsets y menciones nominales de esa cita. |
| `feature_evidence` dentro de cada fragmento | N-gramas que contribuyeron al margen del clasificador auxiliar. No son factores del scoring ni citas probatorias independientes. |
| `content_hash`, `entity_signature` | Identifican el texto original completo y los campos de entidad suministrados. |
| `model_hash`, `config_hash`, `runtime_config_hash` | Identifican pesos, configuración de entrenamiento/decisión y límites operativos. |
| `processed_chars`, `total_chars`, `truncated` | Hacen visible el alcance del procesamiento. |
| `score_effect` | Siempre `auxiliary_only`: no altera el resultado del scoring. |

Las ocho categorías son `accused`, `victim`, `complainant`, `incidental`, `negated`, `resolved`, `insufficient` e `indirect`. `accused` agrupa distintas formas de atribución adversa reportada; no distingue por sí sola una acusación de una condena. `negated` incluye desmentidos y negaciones reportadas; una negación no equivale a absolución. `resolved` representa una resolución reportada, cuya fuente, fecha y alcance deben revisarse. `indirect` identifica contexto relativo a terceros, sin transferir responsabilidad a la entidad.

El umbral de margen es 0.7, fijado antes de evaluar el holdout. Si varias cláusulas tienen papeles distintos, el resultado global se abstiene y conserva los fragmentos y `competing_roles`. No intenta resolver cronologías o contradicciones automáticamente.

La cita garantiza correspondencia con el texto de entrada: `text[start:end] == quote`. Esta garantía de procedencia no valida la corrección semántica de la etiqueta, la calidad de la fuente o la veracidad del hecho. Las menciones son coincidencias literales del nombre o alias con normalización de acentos y offsets originales; no constituyen NER estadístico ni entity linking.

## Reproducir el entrenamiento y el benchmark

El entrenamiento es opcional y requiere Python 3.11 o posterior para las versiones fijadas en [requirements-context-training.lock.txt](../requirements-context-training.lock.txt). Utilice un entorno virtual separado. En Windows:

```powershell
py -3.12 -m venv .venv-context
.\.venv-context\Scripts\python -m pip install -r requirements-context-training.lock.txt
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
.\.venv-context\Scripts\python scripts/train_context_model.py
```

En Linux/macOS:

```sh
python3.12 -m venv .venv-context
.venv-context/bin/python -m pip install -r requirements-context-training.lock.txt
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv-context/bin/python scripts/train_context_model.py
```

La instalación de bibliotecas utiliza el índice configurado de pip; no descarga modelos ni transmite notas. Para instalar también esas bibliotecas sin conexión, prepare sus wheels previamente y use `python -m pip install --no-index --find-links RUTA_WHEELS -r requirements-context-training.lock.txt`. No necesita ese entorno para usar la aplicación ni el clasificador distribuido.

El script entrena exclusivamente con `tests/data/context_train.jsonl`, exporta el JSON y verifica sus funciones de decisión contra scikit-learn. El máximo error absoluto observado fue `4.999191760557409e-7`, considerando que la salida auditable redondea a seis decimales. Después evalúa `tests/data/context_holdout.jsonl` y escribe [phase2_discriminative.json](benchmarks/phase2_discriminative.json).

`--make-fixtures` reconstruye ambos archivos sintéticos desde las familias de plantillas explícitas del script. `--benchmark-only` vuelve a medir los pesos distribuidos sin entrenar. El entrenamiento y el benchmark se ejecutan localmente; no invocan Hugging Face ni servicios externos de inferencia.

Las versiones y la semilla están fijadas. Diferencias de Python, biblioteca matemática, plataforma o implementación de álgebra pueden cambiar los últimos decimales de un reentrenamiento. El artefacto distribuido tiene SHA-256 fijado en el código y no se acepta otro silenciosamente. Para publicar pesos distintos se requiere una versión revisada, nuevas métricas y actualizar deliberadamente el SHA esperado; el script no modifica ese pin automáticamente.

## Resultados medidos y alcance

Medición del 15 de octubre de 2026 en Linux x86_64, Python 3.12.14, CPU y sin GPU. El informe JSON conserva etiquetas esperadas, predicciones, márgenes, matriz de confusión, parámetros, hashes y tiempos.

| Medida | Resultado |
| --- | ---: |
| Ejemplos de entrenamiento | 384, procedentes de 96 familias |
| Ejemplos holdout | 128, procedentes de 32 familias distintas |
| Macro F1 de ocho papeles con abstención | 0.352930 |
| Macro F1 de argmax antes de abstención | 0.536021 |
| Abstenciones | 74 de 128 |
| Falsos positivos `accused` | 4 entre 112 ejemplos de otras clases |
| Relevancia para revisión: precisión / recall / F1 | 1.000 / 0.458333 / 0.628571 |
| Correspondencia exacta de citas con la entrada | 100% de los fragmentos devueltos |
| Latencia por nota y una entidad: mediana / p95 | 0.500 / 0.613 ms |
| Carga fría del artefacto | 31.4 ms |
| Tiempo de CPU mediano por nota | 0.500 ms |
| Pico de RSS del proceso frío | 16,502,784 bytes |
| Pico de asignaciones Python con tracemalloc | 6,921,311 bytes |
| Archivo del modelo | 1,432,196 bytes |

La relevancia para revisión considera relevantes los papeles distintos de `incidental` e `insufficient`; incluye víctimas y denunciantes y no equivale a relevancia adversa ni culpabilidad. Las cifras miden viabilidad sobre textos sintéticos inventados. Las variantes de una plantilla están correlacionadas: separar familias evita reutilizar la misma plantilla entre entrenamiento y holdout, pero no convierte estas métricas en validación con noticias reales. No se ajustaron pesos o umbral sobre los resultados del holdout.

El pico de RSS se tomó de `/proc/self/status` (`VmHWM`) en un proceso nuevo sin tracemalloc, incluyendo el intérprete. Las asignaciones Python se midieron en otro proceso con tracemalloc. No se usa `ru_maxrss` heredado del proceso de entrenamiento para atribuirle su consumo al clasificador. La latencia no incluye agregación de varias notas por entidad, acceso a disco de notas o interfaz. Windows no fue medido; no se infiere su rendimiento a partir de Linux.

No se reporta F1 de NER o relaciones porque este candidato no realiza esas tareas generales. El 100% de citas exactas valida la procedencia, no la interpretación. La fidelidad al scoring se consigue manteniendo el motor auxiliar fuera del cálculo: las contribuciones reales deben reconstruirse desde el scoring instrumentado, no desde sus etiquetas.

La calidad observada, especialmente las abstenciones y los falsos positivos adversos, no justifica usar este modelo como decisión autónoma. Su selección como componente de producción exige comparación con NLP clásico, arquitectura híbrida y validación independiente con notas revisadas. Este documento no declara un ganador de esa comparación.

## Límites, integridad y mantenimiento

El análisis procesa como máximo un prefijo de 60,000 caracteres antes de buscar menciones. Limita candidatos nominales a 128, alias a 32, longitud de nombre/alias a 512 caracteres, cláusulas a 48 y cada cita analizada a 1,800 caracteres. Rebasar estos presupuestos provoca abstención y advertencias. `content_hash` sigue correspondiendo al texto original completo. La búsqueda de menciones está precedida de un conteo acotado para evitar trabajo cuadrático con nombres repetidos; la segmentación se consume de forma incremental.

La carga rechaza archivos mayores de 3 MB, SHA distinto del distribuido, versión desconocida, categorías inesperadas, dimensiones incompatibles, vocabularios duplicados y valores no finitos o fuera de rango. `get_model_info()` devuelve copias para que un consumidor no pueda cambiar la configuración cacheada conservando su hash publicado. Las pruebas cubren estas condiciones, offsets Unicode, alias, entidad ausente, contradicciones, límites, independencia del scoring e inferencia sin red.

```sh
python -m unittest tests.test_context_model -v
```

El clasificador no verifica identidad, homónimos, fuente, vigencia temporal o veracidad; tampoco resuelve pronombres generales ni lenguaje jurídico abierto. El enmascarado y los n-gramas permiten aprender asociaciones contextuales, pero no garantizan comprensión semántica. No añada ejemplos de evaluación al entrenamiento ni ajuste el umbral usando sus errores. Para mejorar el modelo, cree un conjunto de desarrollo independiente y reserve otra evaluación final antes de reabrir decisiones de arquitectura.

Mantenga las notas y los resultados de producción fuera de estos corpus sintéticos. El módulo no registra textos en archivos ni realiza llamadas de red. Su consumidor debe aplicar la política de acceso y conservación del repositorio a las explicaciones y a las citas, que pueden contener datos personales.

Artefacto distribuido:

```text
SHA-256 modelo: ba227bc782936a1e19901f8a1464618e160a5e525593c71affd1705f45a8f563
SHA-256 corpus entrenamiento: f24e47520ef4900d76005969b82e5278f9c2204dfa1296aea30748a4f31e6531
SHA-256 configuración: cfc5174ed6b5d764d58fad885ff3f979b33e402b0ac8d7f10f36e28eeee80544
SHA-256 límites operativos: 8806813034f55e34f35cc1846dfb089f46ada08c05d02dc1e3fc1363b624f37f
```

## Licencias y fuentes

[LICENSE-CODE](licenses/context-model/LICENSE-CODE) aplica MIT solamente a los archivos nuevos `peps/context_model.py`, `scripts/train_context_model.py` y `tests/test_context_model.py`. [LICENSE-DATA](licenses/context-model/LICENSE-DATA) dedica a CC0-1.0 únicamente los pesos/vocabulario originales y los dos corpus sintéticos de esta prueba. Estas licencias no alteran el código preexistente, otras notas, otros corpus, modelos ajenos o las licencias de las bibliotecas.

scikit-learn, NumPy, SciPy, joblib y threadpoolctl declaran BSD-3-Clause; sus distribuciones pueden incluir avisos adicionales de componentes empaquetados, que deben conservarse si se redistribuyen las bibliotecas. El modelo numérico distribuido se entrenó desde cero sobre los ejemplos originales; no incorpora pesos de esos proyectos ni de modelos de terceros.

Textos legales consultados el 15 de octubre de 2026:

- MIT: [identificador y texto SPDX](https://spdx.org/licenses/MIT.html), con [texto oficial del catálogo SPDX](https://raw.githubusercontent.com/spdx/license-list-data/main/text/MIT.txt).
- CC0-1.0: [referencia canónica de Creative Commons](https://creativecommons.org/publicdomain/zero/1.0/legalcode) y [texto oficial del catálogo SPDX](https://raw.githubusercontent.com/spdx/license-list-data/main/text/CC0-1.0.txt). El acceso directo a `creativecommons.org/publicdomain/zero/1.0/legalcode.txt` devolvió HTTP 403 en este entorno; se incorporó el texto completo del catálogo SPDX, identificando la referencia canónica sin afirmar que se descargó desde ella.

No se consultó ni utilizó Hugging Face para estos textos, los pesos o el entrenamiento.
