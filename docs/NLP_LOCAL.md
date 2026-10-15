# NLP español local opcional

La fase 3 se ejecutó después de las pruebas funcionales de Laya ONNX y del
clasificador discriminativo de la fase 2. El resultado verificable está en
`docs/benchmarks/phase3_nlp.json`. La comparación independiente corresponde a la
fase 4; este documento no confunde las nueve pruebas de desarrollo con precisión
en notas reales.

## Modelo investigado y ejecutado

`es_core_news_sm` 3.8.0 de Explosion es un pipeline español estadístico pequeño:
`tok2vec`, análisis morfológico, dependencias, reglas de atributos, lematizador y
NER. Está optimizado para CPU; no requiere GPU ni un LLM generativo. El wheel
oficial ocupa 12,884,212 bytes. No contiene vectores semánticos preentrenados.

Fuentes oficiales verificadas el 15 de octubre de 2026:

- [Metadata española oficial](https://raw.githubusercontent.com/explosion/spacy-models/master/meta/es_core_news_sm-3.8.0.json).
- [Release y pesos oficiales](https://github.com/explosion/spacy-models/releases/tag/es_core_news_sm-3.8.0).
- [spaCy: código y licencia MIT](https://github.com/explosion/spaCy).
- [Metadata multilingüe MIT, alternativa investigada](https://raw.githubusercontent.com/explosion/spacy-models/master/meta/xx_ent_wiki_sm-3.8.0.json).

El wheel español incluye `LICENSE` GNU GPL 3.0 y `LICENSES_SOURCES`: UD Spanish
AnCora v2.8 bajo GPL 3.0, WikiNER bajo CC BY 4.0 y lookups de spaCy bajo MIT.
La GPL permite uso comercial; impone condiciones al distribuir obras cubiertas,
incluidos los avisos y las obligaciones de código fuente que correspondan. No es
una licencia exclusivamente de investigación. Este repositorio no tenía una
licencia propia que permitiera certificar la compatibilidad de una distribución
combinada; por ello los pesos no se incorporan al repositorio. Antes de distribuir
una aplicación que los incluya, deben conservarse y satisfacerse sus condiciones
y definirse la licencia del proyecto. La instalación local explícita conserva
los archivos de licencia de la distribución oficial.

También se verificó `xx_ent_wiki_sm` 3.8.0, con licencia MIT y fuente WikiNER
CC BY 4.0. Incluye únicamente NER: carece del parser español necesario para
comprobar papeles por dependencias. Se investigó documentalmente; no se presenta
como alternativa funcional equivalente ni como modelo evaluado en el benchmark.

## Instalación reproducible

La aplicación base sigue usando solo la biblioteca estándar. El análisis opcional
requiere Python 3.12 y un entorno separado; las versiones ensayadas están en
`requirements-nlp.lock.txt`. Se ejecutó en Linux x86_64 CPU. spaCy ofrece wheels
para Windows, pero este entorno no permite afirmar que se probó en Windows o GPU.

Linux/macOS:

```sh
python3.12 -m venv .venv-nlp
.venv-nlp/bin/python -m pip install -r requirements-nlp.lock.txt
.venv-nlp/bin/python scripts/setup_local_nlp.py --download --install
.venv-nlp/bin/python -m unittest tests.test_advanced_nlp -v
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv-nlp
.venv-nlp\Scripts\python.exe -m pip install -r requirements-nlp.lock.txt
.venv-nlp\Scripts\python.exe scripts\setup_local_nlp.py --download --install
.venv-nlp\Scripts\python.exe -m unittest tests.test_advanced_nlp -v
```

Las dependencias vienen de PyPI. Los pesos se descargan únicamente del release
oficial de GitHub, sin Hugging Face, APIs de inferencia ni datos del usuario.
`--download` es una operación explícita de instalación; el backend nunca invoca
este script ni descarga un modelo. El SHA256 requerido del wheel es:

```text
e451a83d6df79b87e9eed0cb553f03e99e36a3bab18a7b79f0dcfd1fdf875e12
```

Para una computadora sin red, preparar un directorio `wheelhouse` de dependencias
en otra computadora con el mismo Python y plataforma mediante
`python -m pip download -r requirements-nlp.lock.txt --dest wheelhouse`, copiarlo
junto con el wheel del modelo y ejecutar:

```sh
python -m pip install --no-index --find-links wheelhouse -r requirements-nlp.lock.txt
python scripts/setup_local_nlp.py --wheel /ruta/es_core_news_sm-3.8.0-py3-none-any.whl --install
```

El instalador limita el cuerpo descargado a 12,884,212 bytes y cancela antes de
escribir un byte adicional; comprueba ese tamaño también en un wheel local.
Verifica el hash antes de usar un wheel existente y antes de
instalarlo con `pip --no-index --no-deps`. Una descarga incompleta no se conserva
como modelo válido. Nunca se usa `spacy download` ni una búsqueda implícita de
pesos. Si el modelo o las versiones exactas faltan, el método devuelve abstención
explícita y el motor base puede continuar funcionando.

## Qué interpreta y qué registra

`peps.advanced_nlp.analyze(text, entity, source_kind="news")` devuelve la misma
taxonomía contextual de ocho clases que el clasificador discriminativo:

| Papel | Interpretación auxiliar |
|---|---|
| `accused` | Acusación, investigación o actuación judicial reportada dirigida al objetivo. El estado adicional distingue alegación de resultado judicial reportado. |
| `complainant` | Objetivo como sujeto activo de una denuncia o comunicación. |
| `victim` | Objetivo como víctima o receptor del perjuicio. |
| `negated` | Negación de un evento o negativa reportada; no equivale a absolución. |
| `resolved` | Absolución, exoneración o archivo reportados; tampoco se certifica su veracidad. |
| `indirect` | Evento relacionado con un familiar, socio u otro tercero. |
| `incidental` | Mención sin vínculo sintáctico atribuible con un evento adverso detectado. |
| `insufficient` | Sin mención exacta, hipótesis, ambigüedad, papeles distintos o procesamiento parcial. |

La entidad se vincula por nombre o alias literal normalizado conservando
desplazamientos Unicode. Esta vinculación genera candidatos: no resuelve
homónimos ni sustituye la revisión de identidad existente.

NER se ejecuta estadísticamente sobre el texto original y sus etiquetas
`PER/ORG/LOC/MISC` se registran como `detector="statistical_ner"`. Las menciones
del objetivo se almacenan por separado. Nunca se completan las predicciones NER
con el nombre conocido para mejorar artificialmente sus métricas.

La interpretación contextual combina lemas y clases morfológicas con caminos de
dependencias, sujeto/objeto, voz pasiva, negadores unidos al predicado y prótasis
condicionales. Así `X denunció fraude` y `X fue denunciada por fraude` tienen
argumentos distintos; no se clasifican por la sola presencia de `fraude`.
Las relaciones son **objetivo → evento → papel sintáctico**, no un grafo general
de relaciones empresariales verificadas. Cada relación conserva el camino de
dependencias, el predicado y los desplazamientos exactos que permiten revisarla.

Cada hallazgo conserva la oración exacta, inicio y final, entidades NER, eventos,
relaciones y menciones. Los desplazamientos son índices de caracteres Python
(puntos de código), intervalo `[start, end)`. Para resaltar en JavaScript se debe
convertir a puntos de código, por ejemplo `Array.from(text).slice(start, end)`;
los índices UTF-16 de JavaScript pueden diferir con emojis.

El resultado conserva SHA256 del texto íntegro, firma de la entidad, hash de
archivos del modelo instalado, versión spaCy/modelo y hash de configuración y
reglas. El servicio que lo persista debe vincularlo al identificador y al texto
original ya almacenados, evitando copias de datos personales en logs. El módulo
no escribe textos en disco ni realiza peticiones de red.

`lexical_context_similarity` es coseno de frecuencias de lemas entre la oración
y los campos de contexto disponibles. No es similitud semántica neuronal,
probabilidad de identidad, precisión medida ni factor del scoring. Sin contexto
de entidad se devuelve `null`.

## Límites y mantenimiento

El presupuesto es 80,000 caracteres y 80 hallazgos por nota. El límite de texto
se aplica antes de buscar alias o ejecutar el parser. Si se supera cualquiera,
se registra advertencia y abstención; el hash sigue correspondiendo al texto
íntegro. No se resuelven referencias pronominales ni cronología automáticamente.

La prueba de desarrollo encontró etiquetas NER incorrectas (`Empresa Aurora
Ficticia` como MISC, `Fiscalía` como PER) y una coordinación con dependencias
incorrectas. Que una cita tenga offsets exactos solo demuestra respaldo textual;
no demuestra que la interpretación sea correcta. La clasificación, NER y las
relaciones deben evaluarse por separado con etiquetas de referencia. El informe
de fase 4 mide sus errores en el conjunto independiente.

La versión 1.0.1 corrige una limitación hallada por inspección independiente del
código, sin consultar los casos del conjunto de prueba: `X no fue absuelta` no
debe clasificarse como absolución y `X no negó las acusaciones` no constituye una
negativa ni una nueva acusación. Ambos casos, incluidos complementos como `No se
descartó que X cometiera fraude`, producen abstención y una advertencia explícita
de que no acreditan un nuevo hecho adverso. Ocho regresiones ficticias nuevas
verifican esta condición y que la salida conserve `score_effect="auxiliary_only"`.

La corrección se aplicó después de guardar la comparación inicial congelada en
`docs/benchmarks/phase4_initial_v1.json`. El archivo original de fase 3 se conserva
sin cambios; `phase3_nlp_v1_0_1_regression.json` registra el motivo, los hashes de
ambas implementaciones y las ocho regresiones. La reevaluación final identifica
la versión 1.0.1 por separado. El parche reversible
`docs/benchmarks/nlp_v1_0_0_to_v1_0_1.patch` permite reconstruir la implementación
inicial exacta y comprobar su SHA256; no se optimizaron reglas para los errores
del conjunto de prueba.

Las nueve notas ficticias de desarrollo de la versión 1.0.0 se resolvieron según sus expectativas.
En 45 ejecuciones CPU, la mediana cálida fue 3.205 ms; cargar el modelo tomó
782 ms y el pico RSS del proceso completo fue 157.4 MiB. Son mediciones de ese
entorno y notas cortas, no una promesa de rendimiento en otras computadoras. El
JSON conserva todos los tiempos individuales, versión, hash, plataforma y casos.

Los diccionarios y reglas se congelaron antes de evaluar el conjunto independiente.
Una modificación futura requiere versión nueva, ejemplos de desarrollo propios,
pruebas de regresión y un nuevo conjunto de evaluación o una separación explícita
entre resultados de desarrollo y prueba. Para cambiar el modelo, verificar la
licencia y pesos oficiales, fijar hash/versiones y repetir instalación, pruebas y
benchmark. No reemplazar pesos bajo el mismo identificador.

El valor `score_effect="auxiliary_only"` es deliberado: ninguna clasificación,
NER, similitud o relación se suma al score existente ni se presenta como causa
numérica de este. Las razones del score proceden de su instrumentación real;
este análisis añade contexto revisable y advertencias.
