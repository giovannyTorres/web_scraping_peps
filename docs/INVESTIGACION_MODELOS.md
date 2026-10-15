# Modelos locales de decisiones: investigación y prueba de fase 2

Verificación: **15 de octubre de 2026 (UTC)**. Se consultaron fuentes públicas
primarias sin enviar notas, entidades ni datos del repositorio. No se abrió
Hugging Face, no se descargó desde ese servicio y no se llamó a inferencia externa.
Las referencias a ese proveedor que aparecen abajo describen impedimentos
encontrados en instrucciones públicas de los autores.

## Resultado comprobado

**Laya existe y se ejecutó realmente en CPU**, antes de iniciar la alternativa
de NLP clásico. Se encontró un distribuidor con pesos ONNX en GitHub Releases;
por ello no se descartó por el alojamiento habitual de su autor. La prueba local
está en [phase2_laya.json](benchmarks/phase2_laya.json) y se reproduce con
[poc_laya.py](../scripts/poc_laya.py).

El experimento pasa los cinco controles oficiales de protocolo (longitud de
secuencia y decisión), y obtiene **4/10 aciertos**, macro F1 **0.2440**, en un
piloto sintético bilingüe de papeles contextuales. Latencia p50 **436.8 ms**, p95
**718.1 ms**, RSS máximo observado **1745.0 MiB**. Estos resultados no son una
estimación de calidad en noticias reales: son una prueba funcional y un aviso de
errores relevantes, con textos y predicciones inspeccionables. No se ajustó ni
entrenó el modelo sobre esos ejemplos.

Una repetición posterior con telemetría nativa de ONNX Runtime deshabilitada y
operaciones socket de Python bloqueadas conserva 4/10 y macro F1 0.2440:
[phase2_laya_guarded.json](benchmarks/phase2_laya_guarded.json), p50 **748.3 ms**,
p95 **843.7 ms**, RSS observado **1790.4 MiB**. La variación de latencia bajo carga
compartida impide extrapolar esos tiempos a otra computadora. El guard bloquea
socket Python durante carga e inferencia y registra intentos bloqueados; no es
una captura de paquetes de bibliotecas nativas. No se presenta un contador de
tráfico nativo no medido como cero. La descarga ocurre fuera de ese guard y sólo
con `--prepare`; comprueba tamaño y hash antes de instalar el archivo temporal.

## Fase 4: challenge común después de probar NLP

Tras completar la primera prueba de NLP clásico, se ejecutó el mismo Laya sobre
los **72 casos sintéticos independientes** de `tests/data/context_challenge.jsonl`,
con una pregunta de ocho roles fijada antes de leer resultados. Se alinearon
las definiciones de roles entre candidatos; no se modificó el prompt tras
iniciar el challenge ni se usaron sus etiquetas para entrenar. Hash del corpus:
`8425c256e6dcc97a1664577b6aa593b09a75123fc28adf6f07cc20f16e36816a`.

[phase4_laya_challenge.json](benchmarks/phase4_laya_challenge.json) conserva ID,
hash del texto, respuesta por opción, versión de código, entorno y métricas:

| Medición Laya, CPU, dos hilos | Resultado |
| --- | ---: |
| Aciertos / total | 23 / 72 |
| Accuracy | 0.3194 |
| F1 macro sobre ocho roles | 0.2363 |
| Latencia p50 / p95 por nota | 780.2 / 1129.1 ms |
| RSS máximo observado al terminar cada nota, proceso nuevo | 1831.3 MiB |
| Notas truncadas al presupuesto de 512 tokens | 1 / 72 |
| Abstenciones experimentales | 56 / 72 |

La abstención se marca si la mayor probabilidad de opción es menor a **0.65** o
si hay truncamiento. Es un criterio fijo de experimento, sin validación de
calibración. Accuracy y F1 incluyen las **72 predicciones brutas**, también las
marcadas para abstenerse; no se eleva artificialmente la calidad excluyendo
errores. El checkpoint confunde especialmente menciones incidentales, relaciones
indirectas y evidencia insuficiente. No se midió NER ni extracción de relaciones
para Laya: devuelve una clasificación tipada, no spans. No se deben asignar
métricas ficticias a esas tareas ni tratar sus probabilidades como riesgo real.

Esta evaluación identifica una implementación ejecutable bajo las restricciones,
pero su calidad medida en español no justifica utilizarla como intérprete
autónomo de notas. La comparación global posterior debe considerar el baseline
discriminativo, NLP y la variante híbrida; este archivo no adjudica a Jev
comercial los resultados del checkpoint Laya.

## Candidatos, identidad y viabilidad

| Candidato verificado | Arquitectura y disponibilidad | Licencias verificadas y límites | Decisión de fase 2 |
| --- | --- | --- | --- |
| **Jev de TypeSafe** | El anuncio oficial describe decisiones tipadas y muestreo paralelo. La documentación ofrece acceso alojado; no se verificó distribución de pesos ni arquitectura reproducible completa. | No se halló permiso de redistribución de pesos locales en las fuentes oficiales revisadas. No debe inferirse por la existencia de SDKs abiertos. | No ejecutar: dependería del servicio externo prohibido. |
| **Laya de NandhaKishorM / Convai Innovations** | Inglés: ModernBERT-large + cabeza de dos capas, aproximadamente 421M parámetros. El autor también documenta una variante multilingüe de 322M; las rutas habituales de descarga usan el proveedor excluido. Se encontró exportación inglesa ONNX FP16 en GitHub por MstyAI. | Código del autor Apache-2.0. El README y NOTICE del distribuidor atribuyen código y pesos Laya a Apache-2.0. La licencia del checkpoint original no se verificó en su alojamiento excluido; se conserva la atribución del distribuidor y no se afirma una auditoría jurídica independiente. | **Descargado, hashes verificados, probado en CPU.** No confundir inglés probado con variante multilingüe no probada. |
| **NanoJev, TianyuCodings/NanoJev** | Qwen3-0.6B + cabezas Choice/Boolean/Score y entrenamiento propio. Se identificó este origen; hay forks con el mismo nombre. El README señala pesos y datos en un proveedor excluido; GitHub Releases estaba vacío y el árbol revisado no contenía checkpoint de inferencia. | Código MIT confirmado en GitHub. La licencia de los pesos ajustados no pudo verificarse sin acceder al alojamiento excluido. La licencia del backbone no autoriza por sí sola todos los artefactos derivados. | Impedimento concreto: no se verificó ruta alternativa de pesos con licencia y hashes. No se ejecutó ni se le atribuyeron métricas. |
| **nanojev, VishiATChoudhary/nanojev** | Proyecto diferente: lectura de logits de Qwen3-0.6B y alternativa NLI de DeBERTa. Usa PyTorch/Transformers; no es el checkpoint de Tianyu. | Código MIT. Pesos de terceros requieren comprobación separada; no se verificó una distribución sin el proveedor excluido para este proyecto. | No instalar dependencias excluidas ni presentar sus benchmarks publicados como mediciones locales. |
| **open-jev, daseinlabs** | Puntúa continuaciones con Gemma 3 4B; CPU/GPU en Windows/Linux y MLX en Apple. Sus instrucciones requieren login y descarga del modelo restringido por el usuario. Aproximadamente 8 GB de pesos a 16 bits; CPU usa float32. | Código MIT; los pesos Gemma tienen condiciones propias. No se verificó cadena completa de distribución autorizada sin el proveedor excluido. | No satisface instalación solicitada; además supera el tamaño preferido. |
| **openjev, razorback16** | La variante DiffusionGemma 26B-A4B requiere aproximadamente 18 GB de pesos y NVIDIA de al menos 24 GB, o MLX en Apple. Su distribución documentada incluye servidor y, para CUDA, Docker. Hay backends auxiliares Laya que no resuelven la descarga por sí solos. | Código Apache-2.0. El README afirma Apache-2.0 para esos pesos; no se verificó el checkpoint en el alojamiento excluido. | Descartar para este proyecto por recursos, infraestructura y dependencia de descarga. |
| **OpenJev / SemIf, openjev.com** | Experimento de navegador independiente; anuncia Qwen3 0.6B, MiniCPM5 2B y Qwen3.5 4B. La propia página dice que descarga pesos del proveedor excluido. | La web declara que no está afiliada a TypeSafe. No se verificaron licencias de cada peso ni distribución alternativa. | No integrar el playground ni usar su servicio como inferencia. |

Los nombres **OpenJev** y **NanoJev** no identifican un único proyecto oficial.
Se documentan repositorio y autor para evitar adjudicar licencias, tamaños o
resultados de una implementación a otra.

## Artefacto Laya que se probó

Distribución: [MstyAI/laya-onnx, release model-c5d78730](https://github.com/MstyAI/laya-onnx/releases/tag/model-c5d78730).
Revisión upstream declarada: `c5d78730f3493e4fe16d61507ef4b78eef7318cf`.
Se descargaron directamente de GitHub, verificando tamaño y SHA-256:

| Archivo | Bytes | SHA-256 |
| --- | ---: | --- |
| `model.onnx` | 3,564,788 | `55813eecb0318474f9ef109c145315d23ccf18f9952d05118dea4472c9eb9f35` |
| `model.onnx.data` | 842,072,064 | `3970321cdad29349fca999b6ab2f302472a3f26c54b14d8cc47aae31f03cb31d` |
| `tokenizer.json` | 3,583,228 | `6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30` |
| `rl_agent_config.json` | 745 | `ae287b56bbcf5f8c4f4541ae9dfd00c914c4c48b940b8398c3058af37ba92bbd` |
| `validation.json` | 1,108 | `5c3c008e5082be6255d50becb65642da5cc6008211ef30c001bd953b29b632ef` |

El runtime Go de ese distribuidor no soporta Windows. El PoC usa directamente
**ONNX Runtime CPU** con Python, NumPy y un tokenizer BPE ByteLevel implementado
desde el JSON local, sin `transformers`, `tokenizers` ni bibliotecas Hub. El
protocolo conserva los marcadores `[MASK]`, las cinco entradas ONNX y las
temperaturas del checkpoint. No se califican logits sin interpretar como prueba
exitosa: se validan cinco decisiones y conteos de tokens contra los controles
oficiales. Esto no prueba equivalencia de todo posible texto o de probabilidades
completas, para las que no se publicó un vector golden utilizable en esta prueba.

El presupuesto es de **512 tokens por pregunta, incluyendo instrucciones y
opciones**. El PoC registra truncamiento; los textos extensos requieren una
estrategia de ventanas validada aparte. No se verificó un GGUF de esta cabeza
discriminativa; un GGUF de ModernBERT o Qwen no equivale al checkpoint Laya.
GPU NVIDIA es opcional en ONNX Runtime, pero esta ejecución usó exclusivamente
`CPUExecutionProvider`. Compatibilidad Windows es una posibilidad del runtime,
no una prueba de hardware realizada en este entorno Linux.

## Reproducción

Crear un entorno separado evita añadir estas dependencias a la aplicación base:
el experimento se verificó con Python 3.12 y no requiere ejecutarse para usar la
aplicación. No se guardan textos de `--cases` en el reporte por defecto; se
conservan ID y hash para consultarlos en el archivo local. `--include-text` es
una opción explícita para ejemplos sintéticos compartibles. Los reportes de
notas privadas se deben escribir en una carpeta local ajena al repositorio.

```bash
python -m venv .venv-laya
.venv-laya/bin/python -m pip install -r requirements-poc-laya.lock.txt
.venv-laya/bin/python scripts/poc_laya.py --model-dir .local-models/laya --prepare --output docs/benchmarks/phase2_laya_reproduced.json
```

En Windows se utiliza `.venv-laya\Scripts\python.exe`. La descarga solo ocurre
con `--prepare` explícito. Después se ejecuta el mismo comando sin ese argumento
con la red desconectada. La carpeta contiene LICENSE/NOTICE y los pesos; no
requiere cuenta, clave ni servicio de inferencia. No añadir los ~849 MB de pesos
al historial Git. Los SHA-256 fijados permiten conservarlos en almacenamiento
local y reconstruir el experimento desde una fuente pública identificada.

Las versiones medidas exactas y el sistema operativo aparecen en el JSON.
Las distribuciones del modelo no constituyen probabilidades calibradas de riesgo
para este dominio. No participan en el score existente y no deben convertirse
en razones retrospectivas de un cálculo al que no contribuyeron.

## Fuentes primarias consultadas

- [Anuncio oficial de TypeSafe: Jev y System One](https://typesafe.ai/blog/introducing-system-one-models-and-jev), [modelos oficiales](https://docs.typesafe.ai/models).
- [Laya: código y documentación del autor](https://github.com/NandhaKishorM/laya), [LICENSE](https://github.com/NandhaKishorM/laya/blob/main/LICENSE).
- [MstyAI: README y límites de plataforma](https://github.com/MstyAI/laya-onnx/blob/v0.3.0/README.md), [NOTICE](https://github.com/MstyAI/laya-onnx/blob/v0.3.0/NOTICE), [manifest](https://github.com/MstyAI/laya-onnx/releases/download/model-c5d78730/manifest.json), [checksums](https://github.com/MstyAI/laya-onnx/releases/download/model-c5d78730/checksums.txt), [controles oficiales](https://github.com/MstyAI/laya-onnx/blob/v0.3.0/scripts/validate_model.py).
- [NanoJev: repositorio de origen](https://github.com/TianyuCodings/NanoJev), [LICENSE MIT](https://github.com/TianyuCodings/NanoJev/blob/main/LICENSE), [releases](https://github.com/TianyuCodings/NanoJev/releases).
- [VishiATChoudhary/nanojev](https://github.com/VishiATChoudhary/nanojev), [dependencias](https://github.com/VishiATChoudhary/nanojev/blob/main/pyproject.toml).
- [daseinlabs/open-jev](https://github.com/daseinlabs/open-jev), [instalación y requisitos del mantenedor](https://parseclab.ai/open-jev/getting-started/).
- [razorback16/openjev](https://github.com/razorback16/openjev), [dependencias y backends](https://github.com/razorback16/openjev/blob/main/pyproject.toml).
- [SemIf, antes OpenJev](https://openjev.com/).
- [ONNX Runtime: instalación oficial Python](https://onnxruntime.ai/docs/install/).

## Criterio legal y selección

El repositorio de aplicación no tenía LICENSE raíz durante la revisión. Esto no
impide investigar y ejecutar software permitido, pero **no permite certificar
compatibilidad comercial del conjunto** ni inventar una licencia para el código
existente. Las obligaciones de atribución de terceros y las licencias de pesos
son separadas. No se redistribuyeron pesos cuya licencia no se pudo verificar.
Se conservan [LICENSE](licenses/laya-onnx/LICENSE) y
[NOTICE](licenses/laya-onnx/NOTICE) del distribuidor del protocolo adaptado.

La prioridad Jev queda atendida mediante una alternativa real probada, no por
una simulación. La selección final debe usar el corpus comparativo posterior:
el piloto evidencia que este Laya inglés no basta por sí solo para contexto de
riesgo en español. Una alternativa discriminativa ligera entrenada localmente
y NLP clásico pueden contrastarse después, preservando el scoring como fuente
de verdad. Sus resultados no deben confundirse con los de Jev comercial.
