# Búsqueda nominal y claridad de la descarga · 2.9.3

## Problema reproducido

Una lista de 652 entidades podía consumir su sesión recorriendo índices generales sin ejecutar búsquedas por nombre. Las 235 referencias históricas y las 719 tareas históricas se presentaban junto al progreso de la sesión actual. Un coordinador esperando un reintento aparecía como descarga activa aunque no hubiera una petición ejecutándose.

Además, un resultado del editor se descartaba cuando el nombre no aparecía en su título o extracto, incluso cuando el cuerpo original sí lo contenía. Una búsqueda dirigida también podía agotar su presupuesto en proveedores públicos antes de consultar un editor habilitado.

## Cambios comprobables

- La búsqueda dirigida consulta frases nominales antes que términos adversos, alias y contexto. Los índices generales requieren elegir esa estrategia explícitamente.
- Los editores WordPress compatibles consultan el período solicitado mediante una petición acotada por nombre, con un top de diez resultados en modo rápido. No se subdivide automáticamente una respuesta limitada en cientos de ventanas.
- Un resultado de una búsqueda exacta del editor puede justificar la descarga limitada del original aunque sus metadatos no contengan el nombre. Se conserva la procedencia de la consulta; no se inventa relevancia, identidad ni puntuación.
- La cobertura se actualiza al contrastar el texto original conservado con la lista actual. Una coincidencia sólo nominal continúa requiriendo atribución de identidad durante el análisis.
- Los artículos originales ya encontrados tienen prioridad frente a nuevas consultas cuando están listos. La prueba de cola reproduce 652 consultas que antes podían adelantarse a un cuerpo disponible; la prioridad conserva reintentos, intentos y reservas vigentes.
- La pantalla principal muestra nombres consultados en la web, entidades con candidatos y entidades con texto. El contraste local de índices y el archivo histórico tienen alcances separados.
- La espera identifica fuente, error y próximo intento permitido. Pausar y analizar espera el cierre real del trabajador; sin textos pertinentes se ofrece buscar por nombre.
- Los cambios de permisos se publican mediante sustitución atómica del archivo JSON. Una regresión con HTTP real reproduce la lectura simultánea que antes devolvía un catálogo vacío o incompleto; ahora siempre se lee la versión anterior o la nueva, ambas completas.

## Comprobación de red real

El 16 de octubre de 2026 se consultaron seis notarios públicos de la lista reproducible de 300 personas. Esta prueba de descubrimiento utilizó transporte real, sin cuentas ni claves y respetando robots y las pausas del proveedor.

La frase exacta **«Roberto Núñez y Bandera»** en Contralínea devolvió el artículo [«Ceden Magdalena Mixhuca a Diablos Rojos por 30 años»](https://contralinea.com.mx/interno/featured/ceden-magdalena-mixhuca-a-diablos-rojos-por-30-anos/), fechado el **16 de febrero de 2016**. El título y extracto no contenían el nombre; el original contenía **dos menciones completas**. El filtro de metadatos producía relevancia cero y el texto original producía una coincidencia nominal recuperable. Esto demuestra un falso negativo del flujo anterior, no una identidad corroborada ni una conducta adversa.

La consulta aislada tardó **7,87 s** y la descarga aislada del original **6,54 s**. El texto extraído tenía 31.658 caracteres y SHA256 `61a431ee985b517bde1960a0ba77633a44ad0b3bb6c61ff8f6217c2f611e655e`. Estas mediciones no son el tiempo completo del pipeline ni un SLA para toda la lista.

El sondeo completo registró **31 peticiones HTTP reales**: 19 búsquedas (18 respuestas 200 y un timeout), cinco originales y siete peticiones de robots. Se intentaron 24 consultas lógicas; las demás no ejecutaron transporte por el circuito de espera. De los cinco originales, uno contenía el nombre completo de la lista, dos contenían una abreviatura revisable y dos fueron controles negativos. No se escribieron identidades confirmadas ni scores.

El buscador Mwmbl devolvió enlaces nominales poco pertinentes para estos casos. Bing, Yahoo, Brave y Mojeek prohibían las rutas de búsqueda inspeccionadas mediante robots; no se consultaron esas rutas. No hay evidencia para presentar estos proveedores como equivalentes automáticos a Google.

Las capturas originales permanecen en `runtime/search-clarity/notary-probe/`, fuera del repositorio. El informe `audit-summary.json` tiene SHA256 `e3b28d4539aa4d4355932826ea02b4ede5b3344cb65f43af79a4c0880cfe3544`.

## Reproducción y pruebas de interfaz

`scripts/validate_notary_search.py` ejecuta el pipeline de producción en un banco nuevo con un presupuesto explícito de red y una segunda ejecución local que debe prohibir transporte. No reutiliza ni modifica el banco anterior de 300. Registra consultas, originales, coincidencias en cuerpo, tiempos, hashes y límites sin calcular puntuaciones sobre las personas reales.

La ejecución real del pipeline completó **6 de 6 consultas nominales**, conservó **un original pertinente** para Roberto y actualizó su cobertura a partir del cuerpo, con **ocho peticiones HTTP**. Con la prioridad de originales, el primer texto quedó guardado a los **11,29 s** y la adquisición terminó a los **37,93 s**. Los otros cinco nombres no obtuvieron texto en esta muestra. El presupuesto permitía como máximo un cuerpo. No hubo atribuciones de identidad ni scores. En una ejecución previa a la mejora de prioridad, el primer original había esperado hasta los 31,50 s; las dos mediciones incluyen latencia real variable y no aíslan estadísticamente el efecto.

La copia local se ejecutó dos veces en **0,321 s**, con DNS, TCP y transporte HTTP prohibidos: no duplicó URLs, observaciones ni documentos y conservó las auditorías y los hashes. Pasaron **24 comprobaciones**. Los módulos del pipeline no cambiaron durante la medición. Informe: `runtime/search-clarity/pipeline-priority-result.json`, SHA256 `17087551aab8d52a5740fa31faf13ddd3abf8d310e462b2124c6517e91c84c61`. La medición previa se conserva en `runtime/search-clarity/pipeline-notaries-result.json`, SHA256 `3bafbe181677616180f3b073311429be9bc298c8d67fe4f6da5edd4709920603`.

`scripts/test_search_clarity_ui.py` utiliza el servidor local real y estados de presentación inyectados: 652 entidades, 235 referencias históricas, 527 páginas y 719 tareas históricas. Comprueba la espera coherente, los alcances de contadores, la recuperación ordenada y las transiciones de fase en escritorio y móvil. No es una medición de cobertura de internet.

Las regresiones del adaptador y del pipeline comprueban permiso separado del original, frase exacta, cambios de banco, límites de cuerpos, caché, cuerpos negativos y procedencia. Los benchmarks masivos usan fuentes sintéticas: miden trabajo local y conservación de las reglas, no velocidad ni resultados de buscadores reales.

La validación local final descubrió **897 pruebas unitarias**, con resultado correcto y siete omisiones correspondientes a capacidades opcionales. Ocho escenarios sintéticos con 652 entidades comprobaron filtrado, atribución, caché y separación entre cuerpos y metadatos. Las seis pruebas de navegador del flujo y recuperación sumaron 225 comprobaciones; importación y explicación se verificaron además por separado.

`scripts/benchmark_search_state.py` crea 652 entidades y 100.001 tareas en SQLite, sin red. Verificó 1.001 tareas de la lista actual frente al total histórico, el uso del índice de ámbito y ausencia de documentos o scores. La primera lectura tardó **88 ms** y la mediana posterior **21 ms**. Es una medición local de consulta de estado, no de adquisición web. Los benchmarks se repitieron con el código estable durante cada medición; sus informes están en `runtime/search-clarity/`.

## Límite de tiempo y cobertura

La sesión rápida limita el tiempo de trabajo; no garantiza terminar toda la lista. A cinco segundos de separación por host, 652 consultas individuales requieren al menos **54 minutos y 15 segundos de programación por editor**, antes de añadir latencia, robots, cuerpos o reintentos. Varias fuentes permiten distribuir trabajo, pero no eliminan sus restricciones ni garantizan cobertura.

El objetivo de esta corrección es dejar de gastar el presupuesto nominal en un recorrido general, producir textos verificables cuando el editor los devuelve y explicar con precisión lo que queda pendiente. Cero resultados sigue siendo cobertura insuficiente y no acredita ausencia de información o riesgo.
