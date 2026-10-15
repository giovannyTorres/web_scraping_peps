# Decisión de arquitectura: scoring trazable y contexto auxiliar local

Estado: seleccionada el 15 de octubre de 2026, después de ejecutar las fases 2, 3 y 4. Evidencia: [benchmark comparativo](BENCHMARK_EXPLICABILIDAD.md), [resultados completos](benchmarks/phase4_comparison.json) y [protocolo](BENCHMARK_PROTOCOL.md).

## Decisión

El motor de scoring sigue siendo la fuente del resultado numérico y de su explicación. Un clasificador discriminativo pequeño, entrenado desde cero sobre ejemplos sintéticos y ejecutable con la biblioteca estándar de Python, aporta **hipótesis contextuales auxiliares**. spaCy puede enriquecer opcionalmente la revisión con menciones, dependencias, relaciones y eventos. Sus predicciones no agregan puntos, cancelan factores ni cambian los umbrales del scorer.

La explicación «¿Por qué obtuvo este score?» se construye con los factores, decisiones de admisibilidad, agrupaciones, ponderaciones, redondeos y agregación registrados por el scoring. El texto de un modelo sólo puede describir su propio análisis, identificado como auxiliar. La coincidencia de una etiqueta contextual con un factor existente no demuestra que esa etiqueta haya intervenido en el cálculo.

El score representa prioridad heurística de revisión documental, de 0 a 95 sobre una escala nominal de 100. Un 85 es prioridad alta; un 40, media. No son probabilidades, culpabilidad ni pérdidas esperadas. Cero también puede deberse a evidencia excluida, identidad insuficiente o falta de documentos, y no acredita ausencia de riesgo. La definición completa está en [SCORING_ACTUAL.md](SCORING_ACTUAL.md).

## Componentes y responsabilidades

| Componente | Responsabilidad | Frontera |
| --- | --- | --- |
| `peps/scoring.py` | Cálculo canónico, traza de decisiones y desglose numérico | No consulta las etiquetas de los nuevos clasificadores para asignar puntos. |
| `peps/explainability.py` | Reconstrucción de factores, evidencias y advertencias del cálculo | No inventa razones para rellenar un score ya producido. |
| `peps/context_model.py` | Clasificación discriminativa local con margen fijo y abstención | No verifica identidad, veracidad, cronología ni probabilidades de riesgo. |
| `peps/advanced_nlp.py` | Extractor estadístico español y reglas sobre dependencias, opcional | Sus relaciones son hipótesis; NER y parser pueden equivocarse. No dispone de vectores semánticos preentrenados. |
| `peps/contextual.py` | Orquestación, límites de trabajo y restricción a la identidad/menciones admitidas | Una coincidencia literal del nombre no autoriza atribuir a la entidad todos los hechos de una nota. |
| Almacenamiento y UI existentes | Conservar versiones/referencias y navegar a texto y fragmentos exactos | Aplicar controles de acceso y conservación también a citas y exportaciones. |

El modo predeterminado es `discriminative`. El modo `enriched` conserva ese papel final y añade extracción opcional de spaCy, con origen de cada componente y desacuerdos visibles. Los modos experimentales `advanced` e `hybrid` sirven para inspección; no son la política predeterminada. Si un componente opcional falta, el sistema informa su ausencia y no descarga ni sustituye modelos silenciosamente.

La identidad se comprueba antes de atribuir contexto. Una revisión humana debe identificar las menciones admitidas; las menciones de otro homónimo no pueden prestar sus conductas a la entidad seleccionada. Un resultado contextual sin identidad corroborada conserva advertencias y requiere revisión. El análisis limitado a cláusulas seleccionadas no se presenta como un resolvedor general de pronombres o cronología.

## Motivos medidos

Se investigó Jev y sus proyectos homónimos, se verificaron las fuentes de distribución disponibles y se ejecutó Laya ONNX FP16 en CPU **antes** de desarrollar NLP clásico. Los impedimentos de NanoJev/OpenJev y la procedencia/licencias se documentan en [INVESTIGACION_MODELOS.md](INVESTIGACION_MODELOS.md). No se atribuye a proyectos no ejecutados un resultado medido.

En el challenge independiente de 72 notas:

- El discriminativo obtiene macro F1 **0.432**, con 30/72 papeles correctos, 42 abstenciones y cero falsas etiquetas de acusado entre 63 notas no acusatorias. Su artefacto ocupa 1,432,196 bytes y no necesita dependencias adicionales de inferencia.
- spaCy obtiene macro F1 de papel **0.259**, con 20/72 correctos y cinco falsas etiquetas de acusado entre 63 notas no acusatorias. Su extracción PER/ORG alcanza F1 tipado **0.558** y sin tipo **0.758**, sobre las 104 menciones nombradas anotadas; esas cifras no miden NER general en producción.
- Laya obtiene macro F1 **0.236** con opciones brutas y **0.181** aplicando su abstención experimental. Requiere 849,221,933 bytes de artefactos y alrededor de 1,831 MiB de RSS observado. No produce spans de evidencia, NER ni relaciones en el experimento.
- El híbrido por acuerdo obtiene macro F1 **0.144** y se abstiene en 63/72 casos. No se selecciona como política principal.

El discriminativo reduce coste y falsas atribuciones en esta muestra frente a las alternativas nuevas, pero **detecta sólo una de nueve acusaciones**. Por eso se selecciona como apoyo de revisión, no como filtro para descartar documentos ni como autoridad para asignar riesgo. Su precisión perfecta en esa clase se basa en una sola predicción y no se extrapola a otras notas.

La integración opcional de spaCy se justifica por sus referencias textuales y estructuras lingüísticas, con errores expresamente visibles; su papel no reemplaza al discriminativo en el modo enriquecido. Esta composición concreta no es el híbrido por acuerdo ensayado ni se le adjudica una mejora de F1 no medida.

## Auditabilidad y versiones

Cada evaluación debe permitir recorrer resultado → rama/evento/factor → documento → intervalo literal. Los datos registrados incluyen identificadores, hashes del contenido y de configuración, versión del método, momento de evaluación, reglas/ponderaciones/umbrales y causas de exclusión o advertencia. Las nuevas salidas auxiliares se identifican con `score_effect=auxiliary_only`.

Los puntos por evento no son sumandos lineales del resultado; la combinación usa un producto de complementos y un techo. La explicación debe conservar orden, pasos y rama ganadora, evitando sumar contribuciones como si fueran independientes. Las correcciones demostradas de integridad de caché y agrupación determinista se explican en [CORRECCIONES_SCORING.md](CORRECCIONES_SCORING.md); no son sustituciones del scorer por el modelo.

La cita literal acredita que el texto existe en la nota. No acredita que la acusación sea cierta, que la relación inferida sea correcta ni que ese fragmento haya aportado puntos. La UI debe mantener esas tres distinciones. Las notas rechazadas o no puntuables siguen siendo consultables con su causa, sin presentarlas como factores positivos.

Los resultados de modelos no son permanentes: cambiar contenido, entidad/alias/contexto, selección de menciones, modelo o configuración invalida sus referencias/versiones. Una evaluación histórica conserva su fecha y su versión; la evaluación actual no debe presentarse como idéntica a una previa cuando cambie alguno de esos elementos.

## Coste, privacidad y licencias

El modo predeterminado no requiere GPU, servidores de inferencia, nuevas bases de datos ni Docker. No usa Hugging Face ni servicios externos. La instalación de modelos opcionales es una operación explícita y separada; la inferencia no instala ni descarga artefactos.

Se midieron CPU y Linux, no Windows ni GPU. Las instrucciones para Windows describen instalación compatible con las distribuciones disponibles; no deben etiquetarse como un benchmark ejecutado en ese sistema. Los tiempos de carga fría y calientes, tamaños de pesos, tamaño total del entorno y RSS se publican por separado en el informe.

El código y los pesos originales del clasificador tienen avisos separados; el proyecto preexistente no se relicencia por añadir estos archivos. spaCy es MIT, pero el modelo español utilizado incluye GPL 3.0 y avisos de sus fuentes. Se admite uso comercial bajo sus condiciones, sin certificar la compatibilidad de una distribución combinada con un repositorio que aún no define licencia propia. El modelo opcional no se incorpora al repositorio y su instalación conserva sus avisos. Véase [NLP_LOCAL.md](NLP_LOCAL.md). La distribución Laya y sus atribuciones están en la investigación; no se asume que todo proyecto llamado Jev comparta licencia.

Las notas y citas pueden contener datos sensibles aun cuando procedan de una fuente pública. Se mantienen dentro del almacenamiento local y de los controles existentes; los corpus y reportes de ejemplo sólo contienen escenarios ficticios. El benchmark no autoriza publicar datos reales ni enviarlos a un servicio para evaluar un modelo.

## Condiciones para reconsiderar

No modificar umbrales, vocabulario o reglas usando el challenge como conjunto de desarrollo. Para mejorar cobertura se requiere otro corpus local con identidad y papel revisados, separación por entidad/evento/periodo, anotación independiente y una evaluación final nueva. Una versión futura sólo debe cambiar la selección tras comparar calidad, falsas atribuciones, abstención y recursos sobre datos no utilizados para ajustarla.

Hasta disponer de esa validación, conservar la traza aritmética como explicación del score y presentar las estructuras IA/NLP como hipótesis verificables. El sistema debe continuar permitiendo auditar el cálculo cuando un modelo esté ausente, falle o se abstenga.
