# Consolidación de ramas

Revisión del 15 de octubre de 2026 sobre `f521850` y las ramas remotas de
`giovannyTorres/web_scraping_peps`. La versión soportada es la plataforma local
`peps`, su interfaz y su CLI; los notebooks, CSV, XML e informes originales se
conservan como material histórico. No se deben mezclar sus puntuaciones con las
evaluaciones actuales ni interpretar sus copias de listas como fuentes vigentes.

## Decisiones verificadas

| Rama remota | Resultado de la comparación | Decisión |
| --- | --- | --- |
| `main` (`8f57fc4`) | Es ancestro de la versión revisada. | Conservar la plataforma de `main` y las mejoras posteriores. |
| `feat/local-score-explainability` (`f521850`) | Base de la consolidación: historial de evaluaciones, explicaciones e importación guiada. | Integrar en la versión final después de validar. |
| `codex/peps-platform` (`9087e82`) | Es ancestro; no tiene archivos ni funcionalidad pendiente de recuperar. | Ya integrada. |
| `desarrollo-scraping` (`a7a289f`) | Es ancestro; sus cinco notebooks desarrollados están conservados byte a byte. | Conservarlos como referencia histórica. |
| `codex/rebuild-project-using-jupyter-notebooks` (`7130e86`) | Es ancestro. Los originales `02_mexico_fuentes_publicas`, `03_noticias_adverse_media` y `04_consolidacion_salida_final` permanecen idénticos; `00` y `01` evolucionaron. | No restaurar los scaffolds sobre los desarrollos. |
| `codex/rebuild-project-using-jupyter-notebooks-3q0k8a` (`52342cd`) | Todo el código notebook es idéntico al scaffold original. La única diferencia de archivos respecto a éste es un salto de línea final en `00`. | No aporta funcionalidad exclusiva. |
| `codex/rebuild-project-using-jupyter-notebooks-3y0k8z` (`285187d`) | Su historial no es ancestro directo, pero normalización, sanciones, archivos mexicanos, DDG/GDELT, consolidación y reportes existen desarrollados en los notebooks preservados y/o en la plataforma. | No hacer un merge que restaure implementaciones anteriores. |

Las PR [#2](https://github.com/giovannyTorres/web_scraping_peps/pull/2) y
[#3](https://github.com/giovannyTorres/web_scraping_peps/pull/3), abiertas al
revisar, corresponden respectivamente a las ramas `-3q0k8a` y `-3y0k8z`.
No contienen una funcionalidad exclusiva que justifique incorporarlas. Su cierre
queda para después de publicar y validar la consolidación; esta revisión no las
modifica.

## Rutas históricas y formatos

El flujo desarrollado preservado usa, en orden:

1. `notebooks/00_input_normalizacion_benchmark.ipynb`.
2. `notebooks/01_sanciones_oficiales_ofac_onu.ipynb`.
3. `notebooks/02_fuentes_mexico_sat_sancionados_contratos_declaranet.ipynb`.
4. `notebooks/03_noticias_adverse_media_duckduckgo_gdelt.ipynb`.
5. `notebooks/04_consolidacion_scoring_reporte_final.ipynb`.

La salida desarrollada de México es `02_fuentes_mexico_evidence.csv`; la rama
`-3y0k8z` usaba `02_mexico_fuentes_publicas_evidence.csv`. También cambiaron los
nombres de los benchmarks de México y noticias. No se deben intercambiar
notebooks de ambas generaciones dentro de una ejecución.

El esquema anterior tenía 53 columnas de evidencia y el desarrollado tiene 63:
conserva 52, agrega municipio, periodos, partido, RFC, CURP, empresa y alias,
incluidos identificadores cotejados; `notas_input` sólo existe en el esquema
anterior. Noticias y evidencia maestra tienen además siete columnas de medios
(70 en total). La consolidación desarrollada contiene 54 columnas de resumen,
un catálogo de URLs y cobertura por fuente reales, frente a los catálogos vacíos
y el benchmark de velocidad de relleno de la rama anterior.

Los CSV versionados tienen 10 entradas, 38 candidatos de sanciones y 38 registros
en la evidencia maestra. México y noticias contienen cero evidencias en esa copia;
esto no acredita cobertura de esas fuentes. La revisión comprobó estructura y
procedencia del código; no volvió a ejecutar notebooks ni consultar sus fuentes
externas.

## Uso soportado y migración

Para trabajar con datos actuales use los flujos de [OPERACION.md](OPERACION.md),
[NOTICIAS.md](NOTICIAS.md) e [IMPORTACION_GUIADA.md](IMPORTACION_GUIADA.md).
La plataforma tiene adaptadores explícitos para XML clásico OFAC/ONU y CSV SAT
69-B. El lector OFAC CSV y los umbrales nominales de la rama `-3y0k8z` son
implementaciones históricas; no amplían el alcance soportado actual. CompraNet,
DeclaraNet y sancionados conservan sus flujos notebook de archivos locales, sin
atribuir a la plataforma conectores automáticos que no implementa.

`import-legacy` es el puente para los CSV históricos. En 2.2.1, las altas conservan
`seed_id` como ID y el adaptador lo utiliza antes del nombre; rechaza conflictos y
sólo recurre al nombre cuando es único. Reporta filas omitidas, conserva la fila
original y sus fechas en la evidencia, y mantiene la marca de revalidación. Las
pruebas de regresión cubren homónimos, procedencia y exclusión del scoring. Los
valores históricos de riesgo o confianza no sustituyen la puntuación actual.

La comparación se realizó con ascendencia Git, árboles y hashes de archivos,
contenido de celdas, esquemas CSV y metadatos de las dos PR. No se cambiaron ramas
remotas ni se eliminaron artefactos históricos como parte de esta reconciliación.
