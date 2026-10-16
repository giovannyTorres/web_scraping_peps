# Evaluación retrospectiva documental de notarios · 16 de octubre de 2026

Abra [el informe interactivo offline](index.html). Resume volumen, cobertura,
pertinencia y detección de papel adverso con denominadores visibles. No es una
evaluación de culpabilidad, una confirmación de identidad ni un scoring.

## Qué se comparó

La muestra contiene 60 personas: 30 casos históricos elegidos por publicaciones
conocidas y 30 comparadores sorteados de un directorio de CDMX. La selección no
es representativa de todos los notarios ni de la prevalencia de incidentes, y los
grupos no se emparejaron por notoriedad o estado. Los dos comparadores con reportes
adversos hallados se conservaron en el grupo: son 2 etiquetas históricas positivas
y 28 desconocidas. El grupo dirigido contiene 28 positivas y 2 desconocidas por
fuentes ambiguas o inaccesibles. En total: **30 positivas, 30 desconocidas y cero
negativos personales certificados**. Son etiquetas históricas de publicaciones,
no determinaciones de culpabilidad, identidad o situación actual.

La referencia finita consta de 130 pares persona-URL y 111 URLs. Una URL puede
referirse a varias personas; varias URLs o hashes distintos pueden corresponder
a una misma historia, edición regional o sindicación. Para recall se utilizan
115 pares pertinentes y 97 URLs: 111 pares de noticias en 93 URLs y 4 pares
institucionales en 4 URLs. Los restantes 15 pares / 14 URLs mantienen etiqueta
desconocida; no se excluyen por un fallo de acceso ni se convierten en negativos.
No se afirma deduplicación
semántica completa. La cohorte y las URLs se congelaron antes de las búsquedas
del pipeline. La investigación externa que las reunió no cuenta como hallazgo
de WebScope.

Se mantienen tres brazos iniciales separados y un cuarto retest adaptado:

| Brazo | Entrada | Qué mide |
|---|---|---|
| Fuentes iniciales | Nombres de la cohorte | Descubrimiento por nombre y conservación de originales |
| Fuentes ampliadas | Los mismos nombres y más editores | Descubrimiento con una selección distinta de fuentes |
| Corpus de referencias | URLs ya conocidas | Adquisición desde un conjunto cerrado y clasificación documental |
| Retest Notisistema | Los mismos nombres y un editor adaptado | Prueba posterior de soporte en subdirectorio; no A/B de idénticas fuentes |

Sus volúmenes no se suman para presentar un único total de descubrimiento. La
captura del corpus de URLs conocidas no mide búsquedas por nombre ni recall de
internet. Los índices se consultaron en la fecha del estudio: no es un backtest
prospectivo ni una reconstrucción de un buscador en una fecha histórica.

## Etiquetas y revisión

La pertinencia (`relevance`) exige que el cuerpo conservado corresponda a la
persona objetivo según la revisión independiente. `incident` describe un papel
adverso reportado en ese documento al publicarse; no determina culpabilidad ni
vigencia actual. Las resoluciones, exoneraciones, papeles de experto, testigo,
denunciante o víctima se distinguen del papel adverso. `unknown` se conserva.

El juicio histórico por persona y el papel en una nota son objetivos distintos.
Un documento sin señal adversa no certifica ausencia de otros incidentes. Las
referencias ambiguas no se transforman en negativos ni positivos automáticos.
No se calculó scoring ni se confirmó identidad mediante el modelo.

Los anotadores fueron agentes asistentes con predicciones del modelo ocultas;
los nombres y grupos de selección sí eran visibles. La revisión independiente
se refiere a esa separación respecto de las predicciones, no a un panel de
expertos jurídicos humanos ni a un doble ciego completo.

El sello de congelación a las **22:10:14 UTC** corresponde al último insumo
constituyente. La compilación derivada del gold ocurrió a las **22:11:15 UTC**,
después del inicio de la corrida nativa a las **22:10:26 UTC**; sus metadatos
declaran que no se consultaron resultados recuperados y que nombres y URLs no
cambiaron. El hash y el orden temporal de constituyentes permiten auditar esa
afirmación; no prueban por sí solos independencia de selección ni que el archivo
compilado ya existiera antes de empezar. `reference_articles.json` conserva el
resumen de esa procedencia sin rutas internas.

## Métricas y denominadores

| Medida | Definición y límite |
|---|---|
| URLs con texto | URLs canónicas distintas con cuerpo usable; no historias independientes |
| Pares persona-URL | Cada asociación entre persona objetivo y URL; una URL puede aportar varios pares |
| Precisión de pertinencia | Pertinentes entre pares revisados; el conjunto sin juicio se conserva aparte y se muestran cotas |
| Recall de referencia | Pares conocidos pertinentes recuperados / pares conocidos pertinentes; no recall de toda la web |
| Precisión del detector | VP / (VP + FP) en papeles documentales con verdad conocida |
| Recall del detector | VP / positivos conocidos, incluidas las abstenciones positivas en el denominador |
| F1 del detector | Media armónica de precisión y recall del mismo objetivo documental |
| Accuracy adjudicada | (VP + VN) / etiquetas documentales conocidas; penaliza las abstenciones |
| Accuracy de decisiones contestadas | (VP + VN) / decisiones contestadas con etiqueta conocida; sólo diagnóstico de selección |
| Accuracy global | N/D cuando hay verdad desconocida; no se infiere de un subconjunto |
| Cobertura de decisión | Fracción de pares revisados en los que el modelo emitió sí o no |

Las matrices muestran VP, FP, FN y VN; las abstenciones se muestran por separado
y no desaparecen de recall y accuracy adjudicada. Los intervalos Wilson son
proporciones marginales, no intervalos de F1, y no corrigen selección dirigida,
dependencia entre personas/URLs o sindicación.

Los JSON conservan el campo
`f1_reviewed_precision_and_frozen_reference_recall`: combina la precisión de
pertinencia del subconjunto revisado con recall de otra referencia finita. Esa
media armónica híbrida **no** es el F1 convencional del clasificador y no se usa
como indicador principal en el HTML.

Las métricas exactas usan la URL normalizada por `canonical_url` y no equiparan
de forma general las rutas con y sin barra final. Un suplemento conserva por
separado 17 equivalencias de redirección verificadas en capturas originales
usables con sus hashes; no modifica el gold ni las métricas exactas. Para texto
original, el recall de URLs de la corrida ampliada pasa de 1/97 exacto a 4/97
con equivalencias verificadas; Notisistema pasa de 0/97 a 2/97. La corrida inicial
permanece en 0/97 y el corpus en 62/97. No se equiparan rutas por una eliminación
heurística de barras, por parecido de título ni por inferencia del modelo.

## Correcciones y temporalidad de la evaluación

Se conservan las mediciones originales de red y sus colas incompletas, errores,
coincidencias ajenas y latencias. Las correcciones posteriores no convierten
esas corridas en una repetición validada del pipeline final ni acreditan dos
minutos para listas masivas. No se afirma equivalencia con Google.

Primero se corrigió un error del adaptador de evaluación que omitía la categoría
`reputational` emitida legítimamente por el NLP. Después se corrigieron fallos de
adquisición y reglas de medidas administrativas notariales, variantes nominales
y extracción de fechas originales. Esas adaptaciones surgieron al examinar estos
casos. Las métricas finales son una reevaluación retrospectiva sobre los cuerpos
congelados: no hay un conjunto independiente reservado ni una afirmación de
mejora fuera de muestra. Las fechas de publicación proceden del HTML conservado
y sus metadatos; no se imputan desde el gold o desde la URL.

El clasificador auxiliar conserva el artefacto publicado y sus pesos. Su fallo
de recall en el corpus se presenta explícitamente; los otros brazos mantienen
sus propios resultados. No se entrenó con estas etiquetas. Los
márgenes del auxiliar no son probabilidades calibradas ni factores de scoring.

La proyección local corregida conserva el archivo original de 71 URLs, pero
retira de la lista actual 10 asociaciones ajenas: pasa de 74 a 64 pares con
texto y de 71 a 61 URLs de texto en la proyección. Quedan **63 pertinentes
revisados, 0 ajenos conocidos y 1 desconocido**: precisión revisada 63/63;
cotas sobre todos los 64 pares 63/64 a 1. El recall de referencia sigue siendo
1/97 exacto y 4/97 con redirects verificados. Es un filtrado retrospectivo,
sin nueva descarga, sin cambio en la latencia fría observada y sin validación
fuera de muestra.

Se conserva el replay local v1 fallido: aunque no hizo red y retiró los pares
ajenos, produjo cambios no idempotentes de observaciones, artículos y trabajos
de cuerpo. Tras corregir ese problema, el replay v2 pasó **19/19** comprobaciones
con cero DNS/TCP/HTTP. Las pruebas generales registraron **1044 tests en 65,368 s**,
con 7 opcionales omitidos; la UI dirigida pasó 21 comprobaciones y la adquisición
compartida 29. Son validaciones de software, separadas de las métricas de cobertura
y calidad documental, que aún presentan limitaciones importantes.

La optimización de admisión por anfitrión tiene además una comparación A/B
**local y simulada** de 300 tareas: claims 602 → 303; aplazamientos por anfitrión
300 → 1; CPU 4,365 → 3,099 s; pared 10,786 → 9,083 s. Conserva 302 solicitudes
simuladas idénticas, un mismo cuerpo, auditoría válida y 2.100 s de tiempo
virtual. No son 300 personas reales ni un SLA de internet.

## Archivos auditables

- `index.html`: informe offline con filtros por entidad y brazo; fuentes locales incrustadas.
- `report-bundle.json`: datos de presentación y límites explícitos.
- `summary.json` y `metrics-*.json`: resultados numéricos por brazo.
- `predictions-*.json`: predicciones documentales, motivos, spans y hashes, sin cuerpos.
- `evaluation-input-*.json`: entradas públicas saneadas para recalcular conteos y métricas sin cuerpos.
- `entities.csv`, `sources.csv`, `document-errors.csv`: tablas completas por entidad, fuente y error/abstención.
- `document-errors.html`: exploración filtrable de errores y abstenciones, con revisión y detector separados.
- `retrieved-pairs.csv`: todos los pares recuperados con pertinencia y papel revisados, incluidos ajenos y desconocidos.
- `cohort.json` / `cohort.csv`: selección y etiquetas por persona con sus límites.
- `reference_articles.json` / `.csv`: URLs, títulos, etiquetas y hashes públicos.
- `evaluation-receipt.json`: versión, configuración y procedencia de la reevaluación.
- `reference-denominators.json` / `.csv` y `evaluation-receipt-supplement.json`: desglose de denominadores y recibo enlazado.
- `redirect-aware-reference-recall.json` / `.csv` y su recibo: equivalencias verificadas separadas del recall exacto.
- `performance-local.json`: A/B del planificador con 300 tareas y transporte simulado.
- `expanded-current-projection-*.json`: proyección corregida retrospectiva, sin sobrescribir la corrida de adquisición original.
- `offline-replay-history.json`: comprobaciones y hashes tanto del replay v1 fallido como del v2 corregido.
- `software-validation.json`: pruebas de regresión y UI, separadas de la calidad de búsqueda y detección.
- `manifest.json`: SHA-256 de entradas originales y artefactos públicos saneados, por separado.
- `*.svg`: gráficas estándar exportables, también incrustadas en el HTML.
- `font-licenses.txt`: licencias y procedencia de las tipografías incrustadas.

Los cuerpos y capturas no se publican. No se incluyen citas extensas ni rutas
privadas. Un hash del archivo original no es intercambiable con el de su versión
pública saneada. Las filas CSV conservan valores desconocidos como celdas vacías;
el HTML los presenta como N/D, nunca como cero.

`public_file_sha256` del manifiesto identifica los bytes exactos de cada archivo
público, incluido JSON con indentación y salto final. Por separado,
`renderer_bundle_semantic_sha256` identifica el objeto de presentación como JSON
canónico UTF-8 (`ensure_ascii=False`, claves ordenadas, separadores `,` y `:`,
sin indentación ni salto final). Son hashes de representaciones diferentes y
no deben coincidir necesariamente.

## Reproducción y límites de lo publicado

Desde la raíz del repositorio, con Python 3.10 o posterior, se pueden recalcular
conteos, denominadores, matrices y cocientes sin red ni cuerpos originales:

```bash
python scripts/notary_backtest_metrics.py docs/benchmarks/notary_backtest_2026_10_16/evaluation-input-native.json --output runtime/verificacion-notarios/metrics-native.json
python scripts/notary_backtest_metrics.py docs/benchmarks/notary_backtest_2026_10_16/evaluation-input-expanded.json --output runtime/verificacion-notarios/metrics-expanded.json
python scripts/notary_backtest_metrics.py docs/benchmarks/notary_backtest_2026_10_16/evaluation-input-corpus.json --output runtime/verificacion-notarios/metrics-corpus.json
```

Las entradas públicas contienen los mismos juicios y predicciones registrados,
no los cuerpos. Se comprobó que todos sus campos métricos coinciden con la
evaluación original salvo la procedencia del `gold_snapshot`: el saneamiento
elimina campos no numéricos y genera otro hash semántico, que se identifica como
tal y conserva el hash original por separado. No se espera igualdad de bytes
entre JSON originales, saneados y recalculados.

El informe se puede regenerar con sus datos de presentación publicados; las
gráficas SVG requieren matplotlib y el HTML queda consultable sin conexión:

```bash
python scripts/render_notary_backtest.py --bundle docs/benchmarks/notary_backtest_2026_10_16/report-bundle.json --output docs/benchmarks/notary_backtest_2026_10_16/index.html
```

El helper de ensamblado de esta sesión no forma parte de la reproducibilidad
pública. Los JSON/CSV exportados permiten verificar los números y el renderer
permite reconstruir su presentación. Repetir extracción de fechas, clasificación
NLP o revisión del contenido requiere además los cuerpos originales cuyos
hashes se publican; esos textos y capturas no se redistribuyen aquí.

Para una nueva adquisición real, prepare su propio catálogo de fuentes públicas
con permisos y condiciones revisados. Use destinos nuevos para no mezclar
mediciones. Esta orden utiliza sólo nombres y no siembra URLs del gold:

```bash
python scripts/backtest_notary_news.py --online --cohort docs/benchmarks/notary_backtest_2026_10_16/cohort.json --sources-config MIS_FUENTES_APROBADAS.json --data-dir runtime/nueva-prueba-notarios/cold --local-dir runtime/nueva-prueba-notarios/local --output runtime/nueva-prueba-notarios/report.json --seconds 900 --max-http 650 --body-budget 120 --workers 4
```

El catálogo es una entrada propia, no un archivo de ejemplo ya autorizado.
Repetir la consulta en otra fecha no garantiza las mismas respuestas, cobertura,
condiciones de acceso ni latencias de la web. Un nuevo resultado necesita su
propia revisión documental antes de calcular precisión o recall.

## Retest adaptado de un editor

Notisistema se consultó después de adaptar el soporte del editor en subdirectorio.
Se mantienen por separado su resumen, recibo, métricas y predicciones. No se
suman sus URLs a las corridas anteriores como descubrimiento independiente ni
se atribuye la diferencia de tiempo a una comparación A/B de idénticas fuentes.
El denominador de personas sigue siendo 60, aunque sólo cuatro recibieron texto.

```bash
python scripts/notary_backtest_metrics.py docs/benchmarks/notary_backtest_2026_10_16/evaluation-input-notisistema.json --output runtime/verificacion-notarios/metrics-notisistema.json
```
