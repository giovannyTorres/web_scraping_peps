# Protocolo de comparación local de explicabilidad

## Orden y separación de datos

1. Reconstruir el scoring existente sin cambiar sus resultados.
2. Investigar y ejecutar candidatos tipo Jev disponibles legalmente; ejecutar también el clasificador discriminativo local equivalente.
3. Implementar y probar NLP clásico avanzado con su propia prueba de desarrollo.
4. Congelar modelos, pesos, reglas y umbrales. Sólo entonces ejecutar el conjunto challenge común y comparar. No modificar candidatos para mejorar este conjunto después de observarlo.
5. Seleccionar e integrar según los resultados, conservando el scoring como fuente de verdad.

`tests/data/context_challenge.jsonl` es un conjunto **sintético, escrito manualmente para esta prueba**, distinto del entrenamiento y holdout del clasificador. No contiene noticias reales ni datos internos. No es evidencia de rendimiento sobre el tráfico de producción. No utilizarlo para entrenamiento, selección de características, selección de umbral ni ajuste de reglas. Cualquier ajuste posterior requiere un nuevo conjunto independiente antes de afirmar una mejora generalizable.

Versión: `manual_synthetic_challenge_v1`. SHA-256 del archivo UTF-8, incluyendo saltos de línea:

```text
8425c256e6dcc97a1664577b6aa593b09a75123fc28adf6f07cc20f16e36816a
```

Son 72 notas, nueve por cada uno de ocho papeles, 104 menciones de personas/organizaciones y 63 anotaciones de papel de actor. La nota más extensa tiene 3.466 caracteres. Hay una pareja de notas del mismo expediente con acusación histórica y absolución posterior. Las demás entidades son distintas. El balance deliberado de clases sirve para comparar errores, pero no representa su prevalencia real.

Los nombres, empresas, expedientes y hechos son ficticios. La autoría de las anotaciones es manual; el pequeño script utilizado para serializarlas sólo calculó posiciones a partir de fragmentos elegidos y comprobó su consistencia. No utilizó IA ni NLP para generar etiquetas. No se entrena con este corpus.

## Qué se etiqueta

Cada línea incluye `id`, `text`, `entity`, `role`, `expected_relevant`, `tags`, `identity_reference`, `ent_spans`, `evidence`, `relations`, `ner_scope` y `annotation_origin`. `scenario_id` agrupa, cuando corresponde, notas de un mismo caso.

`entity` tiene `id`, `name`, `kind` (`person` u `organization`), `aliases` y `context`. El contexto admite `description` y `verified_anchors`, definidos como datos del escenario sintético. No deben suponerse comprobados en una evaluación real ni convertirse en confirmación de identidad por mera coincidencia de nombre. El caso de dos homónimos sin identificadores suficientes exige abstención, aunque se encuentre literalmente el nombre.

| Papel de referencia | Significado operativo |
| --- | --- |
| `accused` | La nota atribuye a la entidad una acusación, investigación, detención u otra presunta conducta. No significa culpabilidad demostrada. Una negativa de la propia persona no elimina una acusación expresamente reportada. |
| `victim` | La entidad recibe el daño; no se le atribuye la conducta. Si el centro de la atribución es una denuncia que interpuso, se utiliza `complainant`. |
| `complainant` | La entidad denuncia, promueve una querella o aporta el reporte del hecho contra otro actor. |
| `incidental` | Mención de experto, recinto, patrocinador, cita bibliográfica, otro artículo o identidad expresamente excluida; no existe vínculo de riesgo del objetivo en esa nota. |
| `negated` | Una aclaración o fuente explícita descarta la atribución relevante. No equivale a aceptar una autodefensa como hecho probado. |
| `resolved` | Existe una resolución jurídica o administrativa reportada. Puede ser absolución, anulación, archivo provisional, prescripción o reparación; no todos esos resultados prueban inocencia. |
| `insufficient` | No hay contenido suficiente, entidad identificable o identidad desambiguada para atribuir un papel concreto. |
| `indirect` | Hay una relación expresamente descrita con un actor o sociedad del caso, sin atribución directa al objetivo. Parentesco, defensa legal o vínculo societario no transfieren culpabilidad. |

**Relevancia** significa que la nota aporta contexto utilizable para la revisión de la entidad correcta, no que sea adversa ni que deba aumentar el score. Por tanto, los seis papeles `accused`, `victim`, `complainant`, `negated`, `resolved` e `indirect` tienen `expected_relevant=true`; `incidental` e `insufficient` tienen `false`. Un sistema que identifica correctamente a una víctima y no incrementa el riesgo puede acertar a la vez en relevancia y en la ausencia de autoría. La definición se congela antes de ejecutar candidatos.

`identity_reference` distingue el objetivo definido en el escenario de una identidad excluida, ambigua o no mencionada. En producción la identidad continúa necesitando verificación independiente. No medir identidad confirmada a partir de la exactitud de un clasificador de papeles.

## Referencias de texto y alcance de anotación

Todos los offsets son índices de caracteres Unicode de Python: inicio inclusivo, final exclusivo. Siempre debe cumplirse `text[start:end] == span.text`; no son offsets de bytes UTF-8. La referencia se aplica al texto original sin normalizar acentos, espacios ni saltos de línea.

`evidence` contiene al menos un fragmento exacto por nota. En casos insuficientes, ese fragmento explica la falta de datos y no constituye evidencia de un delito. En correferencias el actor puede aparecer en una oración anterior al fragmento elegido. No se exige que una explicación reproduzca exactamente la selección editorial del anotador: se mide la coincidencia textual por separado de la corrección del papel.

`ent_spans` contiene menciones **nombradas** de personas (`PER`) y organizaciones (`ORG`), incluyendo terceras partes expresamente anotadas. No se anotan referencias genéricas como «la fiscalía», «el tribunal», «su gerente» o «la compañía», ni pronombres, ni entidades anidadas dentro de un nombre más largo. Un alias que aparece sólo dentro de un nombre completo no añade otro span. El conjunto no es un benchmark general de entidades, lugares, eventos ni correferencias.

Los 104 spans se comparan de forma tipada (`start,end,label`) y sin tipo (`start,end`) bajo esta convención. Deben presentarse como resultados sobre **menciones PER/ORG de este corpus sintético**. Un detector literal del nombre objetivo puede evaluarse sin tipo, pero no debe describirse como NER completo ni compararse como si detectara entidades abiertas. Informar por separado capacidades no implementadas.

Las 63 `relations` son del tipo `actor_role`: un span de actor y su papel, enlazados a un fragmento. **No son un grafo completo de relaciones semánticas**. Su evaluación no mide extracción general de parentesco, dirección empresarial, propiedad o eventos. Para una comparación justa de papeles se utiliza el papel final de cada candidato junto con las menciones que realmente produjo; si no proporciona menciones o relaciones, se informa «no disponible», sin inventarlas a partir de la etiqueta de referencia.

## Métricas congeladas

| Dimensión | Medición y límite |
| --- | --- |
| Clasificación | Matriz de confusión, precisión, recall y F1 por papel, macro F1 de las ocho clases, accuracy, tasa de abstención. `insufficient` es una clase evaluada, no una predicción eliminada del denominador. |
| Relevancia | Accuracy/concordancia y precisión, recall, F1 binarios frente a `expected_relevant`, aplicando el mismo mapa de papeles a todos los candidatos. No es probabilidad de riesgo. |
| Falsas atribuciones | Número y fracción de notas no acusatorias etiquetadas `accused`; desglose específico de identidad ambigua/excluida y de víctimas/denunciantes. El denominador se publica. |
| Menciones/NER | Precision, recall y F1 exactos tipados y sin tipo en el alcance PER/ORG indicado; conteos TP/FP/FN. Si un método sólo entrega menciones literales, su tipado es «no disponible». |
| Papel de actor | Coincidencia exacta de `(start,end,role)` en las 63 referencias; se limita al papel contextual del actor, sin afirmar extracción de relaciones generales. |
| Evidencia literal | Fracción de fragmentos emitidos cuyo texto coincide exactamente con el intervalo original. Un fragmento literal **no demuestra** que la conclusión sea correcta. Publicar denominador, ausencia de fragmentos y, por separado, cobertura de los fragmentos gold cuando el papel es correcto. |
| Fidelidad al scoring | Los clasificadores son auxiliares y no aportan factores al score. Esta comparación no acredita fidelidad del cálculo. La reconstrucción aritmética y sus pruebas deben verificarse por separado en la instrumentación de scoring. |
| Robustez | Accuracy y macro F1 descriptivos por `tag`, con número de casos; grupos pequeños no permiten inferencia estadística. |
| Entidad | Agrupar notas por `entity.id`; media de accuracy por entidad y coincidencia de conjuntos de papeles por entidad. Publicar la pareja temporal como caso separado, sin inventar un único papel definitivo. |
| Rendimiento | Tiempo de pared y CPU por nota, p50/p95, tiempo total por entidad y tiempo de carga/primera nota en proceso nuevo. Misma máquina y datos, ejecución secuencial, GPU desactivada. |
| Recursos | Máximo RSS del proceso aislado, bytes de artefactos locales, versión del intérprete y SO. RSS incluye intérprete y dependencias; no es exclusivamente memoria de pesos. |

La abstención puede reducir atribuciones falsas y a la vez perder recall. Se publican ambas consecuencias; no se declara ganador por una métrica aislada. Los márgenes y funciones de decisión no se presentan como confianza calibrada.

No se afirma «proporción de afirmaciones semánticamente respaldadas» a partir de offsets válidos. Eso exige adjudicación humana independiente o un conjunto de afirmaciones semánticas exhaustivamente etiquetadas. Tampoco se usa latencia para inferir precisión.

## Ejecución reproducible

El evaluador `scripts/benchmark_explainability.py` carga modelos congelados y no entrena ni modifica configuración. Cada método se ejecuta en su propio subproceso nuevo. Se separan carga y primera nota, una pasada completa para calidad y repeticiones para tiempos calientes. El script registra la CPU utilizada por el proceso y el RSS máximo; no hace muestreo de una aplicación que pudiera contener otros datos.

Ejemplo, **sólo después de completar la prueba de FASE 3**:

```bash
python scripts/benchmark_explainability.py \
  --methods discriminative advanced hybrid \
  --repeats 3 \
  --laya docs/benchmarks/phase4_laya_challenge.json \
  --output docs/benchmarks/phase4_comparison.json
```

`--python` permite usar el intérprete del entorno local con spaCy instalado para todos los subprocesos comparables. No se descargan modelos en este script. Si un candidato no está disponible, el fallo se conserva en el reporte; no se reemplaza silenciosamente por otro.

Laya se ejecuta con su runner local separado y su artefacto de resultados se importa por ID. Se registra su hardware y condiciones; diferencias de runner, número de hilos o calentamiento deben declararse al comparar latencias. NER, relaciones y fragmentos se informan como no disponibles si su API sólo produce una elección tipada. No enviar textos a ninguna API ni usar Hugging Face.

El híbrido de referencia devuelve el papel sólo cuando los dos métodos coinciden; de lo contrario devuelve `insufficient`. La política está fijada antes del challenge. Conserva NER/fragmentos de sus componentes identificando su origen; esa política no entrena un modelo nuevo ni prueba que un acuerdo sea correcto.

El evaluador también admite `--methods baseline`, un adaptador del motor de reglas previo, sin cambiarlo: negación o etapa denial → `negated`; acquittal/dismissed → `resolved`; relación family/associate → `indirect`; relación direct → `accused`; relación context → `incidental`. Si hay papeles distintos se abstiene; sin hallazgos es incidental si hay mención e insuficiente si no hay mención. La representación anterior no separa víctima de denunciante; el adaptador no añade reglas nuevas para rellenar esas capacidades. Su tabla de ocho papeles es una comparación de representación adaptada, no una medida del scoring original.

Si el runner Laya conserva la opción más probable aunque marque `abstained`, se publican dos filas: elección bruta y política con esa abstención aplicada como `insufficient`. No se comparan silenciosamente elecciones brutas de un modelo con decisiones finales de otro. El umbral de abstención Laya continúa siendo el congelado por su runner, no uno elegido después de esta evaluación.

Los tamaños del directorio venv excluyen el intérprete base. Si ese venv hereda paquetes del sistema, se miden además los archivos instalados de las dependencias fijadas que estén fuera del venv; publicar únicamente el directorio local infravaloraría la instalación. Los bytes de pesos, entorno y RSS son magnitudes distintas. La carga fría medida comienza con las importaciones del worker, no con el arranque del intérprete.

Guardar en el reporte hash del corpus, hashes/versiones de modelos y configuración, fecha UTC, comandos/condiciones, predicciones por ID y errores. No exportar datos reales al reporte de ejemplo. No reescribir resultados adversos ni añadir mediciones estimadas como si fueran observadas.

## Criterio de selección y validación pendiente

La selección considera errores de atribución, pérdidas de recall, capacidad de abstenerse, auditabilidad, recursos y mantenibilidad. Si los resultados sintéticos son modestos, un candidato puede integrarse como apoyo opcional con advertencias y sin alterar el score. No se convierte una comparación sintética en aval para automatizar decisiones sobre personas.

Para validar uso cotidiano hacen falta notas del proyecto revisadas localmente por una persona autorizada, con identidad, papel y evidencia anotados; un segundo revisor en casos ambiguos; fechas y fuentes de procedencia; y un conjunto independiente de prueba. Esas notas no están disponibles en este corpus. La falta de datos reales y de evaluación en Windows debe declararse expresamente cuando no se hayan podido medir.
