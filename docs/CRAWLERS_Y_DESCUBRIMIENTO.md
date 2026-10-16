# Descubrimiento compartido para listas grandes

Para investigar una lista de personas, especialmente notarios, WebScope 2.9.3
recomienda empezar por **Búsqueda dirigida por entidad**: nombres canónicos antes
de contexto y variantes. **Índice de medios · complementario** permite revisar
archivos concretos, pero no reemplaza la búsqueda por nombre. Cientos de páginas
sin coincidencias no acreditan cobertura de la lista. Consulte la
[guía de adquisición masiva](ADQUISICION_MASIVA.md) y la
[recuperación de búsquedas anteriores](RECUPERACION_DESCARGA.md).

La estrategia adicional **Web abierta y fuentes públicas** incorpora Mwmbl,
DuckDuckGo HTML/Lite y enlaces sociales públicos sin claves de desarrollador.
Sus formatos, comprobaciones reales y límites se describen en
[búsqueda web abierta](DESCUBRIMIENTO_PUBLICO.md). Este documento explica el
recorrido compartido por los archivos de los editores.

El recorrido compartido permite **reutilizar un archivo entre todos los nombres**:
descargar una página pequeña de
metadatos del editor, cotejarla localmente contra el banco completo y solicitar
el texto únicamente de los candidatos pertinentes. Esto reduce las peticiones
sin convertir cada persona en una consulta de red independiente.

Un crawler organiza las visitas a sitios; un índice permite buscar sobre páginas
ya conocidas. Scrapy, Crawlee y Colly ayudan a construir el primero. No incluyen
el índice global, la infraestructura ni la cobertura histórica de un buscador.
Cambiar de Python a Go o aumentar los trabajadores tampoco elimina la latencia ni
los límites de una fuente. La elección de esta versión conserva el motor de
colas existente y añade adaptadores de metadatos e índices públicos.

## Comparación de alternativas

| Alternativa | Qué aporta sin una clave de API | Aplicación y límite práctico |
| --- | --- | --- |
| [WordPress REST](https://developer.wordpress.org/rest-api/reference/posts/) | Colección pública de entradas, filtros por fecha, paginación y búsqueda cuando el editor la expone. | Adaptador principal para los archivos verificados. Hasta 100 entradas por página; `_fields` evita solicitar cuerpos. El archivo de cada editor es independiente. |
| [Sitemaps](https://www.sitemaps.org/protocol.html) | URLs publicadas por el sitio y enlaces a otros mapas. | Complemento para editores sin REST. Un mapa normal puede carecer de título y fecha de publicación; `lastmod` indica modificación. No acredita contenido ni identidad. |
| [Scrapy](https://docs.scrapy.org/en/latest/topics/spiders.html#sitemapspider) | Framework Python de crawling, extractores y recorrido de sitemaps. | Adecuado para un servicio de ingestión continuo. [AutoThrottle](https://docs.scrapy.org/en/latest/topics/autothrottle.html) ajusta el ritmo; mantiene los límites configurados por dominio. No se incorpora como dependencia obligatoria al portable. |
| [Crawlee](https://crawlee.dev/python/docs/guides/request-throttling) | Colas y control de peticiones por dominio; permite combinar HTTP y navegación cuando haga falta. | Opción para un servicio separado con páginas dinámicas. Los navegadores añaden consumo; los reintentos deben respetar 429 y `crawl-delay`. |
| [Colly](https://go-colly.org/docs/examples/rate_limit/) | Crawler en Go con concurrencia y demoras configurables. | Útil si las mediciones justifican otro servicio de ingestión. Mantiene el coste de las mismas peticiones y exigiría integrar otro ejecutable. |
| [Trafilatura](https://trafilatura.readthedocs.io/en/latest/usage-python.html) | Extrae texto principal y metadatos; también tiene utilidades de descubrimiento. | Complemento para extraer artículos seleccionados. No sustituye una fuente de URLs ni justifica descargar indiscriminadamente todos los cuerpos. |
| [SearXNG propio](https://docs.searxng.org/dev/search_api.html) | API de metabusqueda JSON/RSS si está habilitada. | Proveedor opcional para una instalación operada por el usuario. Depende de motores externos; muchas instancias públicas desactivan JSON. Su [limitador](https://docs.searxng.org/admin/searx.limiter.html) documenta bloqueos y CAPTCHA del origen. |
| [DDGS](https://github.com/deedy5/ddgs) | Biblioteca que agrega resultados de varios motores. | Puede servir para experimentos, pero no aporta un índice propio ni una cuota garantizada. Hay [incidencias de 403](https://github.com/deedy5/ddgs/issues/480); no es el núcleo obligatorio de adquisición. |
| [GDELT DOC](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) | Descubrimiento de noticias con frases y lotes compatibles. | Devuelve metadatos, con hasta 250 resultados por petición en ArtList. Ese número no es una cuota de artículos descargables ni prueba de archivo completo. |
| [GDELT NGrams](https://blog.gdeltproject.org/using-the-new-web-ngrams-dataset-to-find-relevant-coverage/) | Archivos descargables de frases y una tabla de URLs para filtrar listas localmente. | Alternativa para un índice persistente de mayor escala. El anuncio de junio de 2026 recomienda esta vía ante la saturación de búsqueda. La ingestión histórica inicial puede requerir muchos datos. |
| [Common Crawl](https://www.commoncrawl.org/cdxj-index) | Capturas y un índice público por URL. | Útil para construir un corpus, no como búsqueda inmediata de nombres en texto completo. El [índice de URLs](https://commoncrawl.org/url-index) y los WARC requieren procesamiento adicional. Su [FAQ](https://www.commoncrawl.org/faq) pide espaciar las consultas al API. |

No se incorpora automatización de Google News RSS: el robots verificado el
16 de octubre de 2026 sigue denegando `/rss/search` para el agente de la prueba.
La disponibilidad técnica de una URL no implica autorización para automatizarla.

## Verificación pública de WordPress REST

Se hicieron **12 peticiones**, cuatro por editor, sin una lista de entidades del
usuario. Se verificó robots primero, se dejaron cinco segundos entre peticiones
al mismo host y se consultaron los tres hosts en paralelo. Son observaciones de
acceso y latencia desde un entorno concreto; no son una medición de precisión ni
del procesamiento completo de 300 personas.

Todas las consultas usaron `per_page=100` y
`_fields=id,link,date,title.rendered,excerpt.rendered`. No solicitaron `content`.
La [documentación oficial de paginación](https://developer.wordpress.org/rest-api/using-the-rest-api/pagination/)
admite entre 1 y 100 registros por página; [`_fields`](https://developer.wordpress.org/rest-api/using-the-rest-api/global-parameters/)
reduce la respuesta y el trabajo innecesario del servidor.

| Editor | Consulta | HTTP / entradas | Segundos | Bytes | Total / páginas informado |
| --- | --- | --- | ---: | ---: | ---: |
| Contralínea | Reciente, página 1 | 200 / 100 | 0,998 | 49.003 | 31.081 / 311 |
| Contralínea | Reciente, página 2 | 200 / 100 | 0,692 | 58.138 | 31.081 / 311 |
| Contralínea | Año 2020, página 1 | 200 / 100 | 0,595 | 61.206 | 2.633 / 27 |
| Contralínea | `corrupción`, relevancia | 200 / 100 | 2,159 | 68.510 | 4.852 / 49 |
| Aristegui | Reciente, página 1 | 200 / 100 | 1,486 | 52.778 | 298.966 / 2.990 |
| Aristegui | Reciente, página 2 | 200 / 100 | 1,221 | 52.326 | 298.966 / 2.990 |
| Aristegui | Año 2020, página 1 | 200 / 100 | 0,745 | 56.793 | 28.242 / 283 |
| Aristegui | `corrupción`, relevancia | 200 / 100 | 14,715 | 61.979 | 16.227 / 163 |
| Desinformémonos | Reciente, página 1 | 200 / 100 | 2,371 | 53.669 | 36.209 / 363 |
| Desinformémonos | Reciente, página 2 | 200 / 100 | 1,543 | 50.998 | 36.209 / 363 |
| Desinformémonos | Año 2020, página 1 | 200 / 100 | 1,252 | 50.522 | 2.893 / 29 |
| Desinformémonos | `corrupción`, relevancia | 200 / 100 | 7,705 | 56.077 | 2.650 / 27 |

Las respuestas sumaron 671.999 bytes. Los tres editores devolvieron
`X-WP-Total`, `X-WP-TotalPages` y un enlace a la página siguiente. En cada editor,
las dos páginas recientes tuvieron cero IDs repetidos. Las 300 fechas de las
consultas históricas pertenecieron a 2020, usando
`after=2020-01-01T00:00:00&before=2021-01-01T00:00:00`.

En las consultas compartidas de `corrupción`, los 100 títulos o extractos de cada
editor contenían el prefijo `corrup`. Esto valida una reducción temática, no su
pertinencia a una persona ni una conducta adversa acreditada. Además, buscar por
palabra resultó más lento que listar el archivo; no se asume que este mecanismo
sea siempre la opción de mayor rendimiento. WordPress no documenta agrupación
booleana OR de nombres para esta API; su
[implementación de búsqueda](https://developer.wordpress.org/reference/classes/wp_query/parse_search/)
combina términos de otra manera.

La primera página reciente cubrió del 2 al 15 de octubre en Contralínea, del
14 al 15 de octubre en Aristegui y del 26 de septiembre al 15 de octubre en
Desinformémonos. Un muestreo reciente por sí solo deja fuera la mayor parte de un
archivo histórico. La evidencia compacta conserva URLs, parámetros, fechas,
cabeceras, hashes y verificaciones en
[shared_discovery_research.json](benchmarks/shared_discovery_research.json).

## Usar el índice de medios como complemento

En **Preparar fuentes**, seleccione **Índice de medios · complementario**
(`strategy: shared`), revise el rango de fechas y el plan visible y guarde la
configuración antes de iniciar la descarga.

| Perfil | Tiempo de sesión | Tope de páginas compartidas por fuente |
| --- | --- | ---: |
| Rápido | 120 segundos | 24 |
| Ampliado | Presupuesto ampliado seleccionado | 240 |

El tiempo y las páginas son límites simultáneos: la sesión puede terminar antes
de agotar las páginas si hay latencia, reintentos o espera de la fuente. Una
página WordPress puede aportar hasta 100 metadatos; eso no significa 100 notas
pertinentes ni 100 cuerpos disponibles.

Habilite las nuevas fuentes de índice que quiera consultar tras revisar sus
condiciones. Las fuentes nuevas comienzan desactivadas y no heredan permisos de
otras fuentes. Para obtener texto, habilite además la fuente editorial y su
permiso explícito de descarga de cuerpos. Un permiso para consultar un índice
no habilita automáticamente los artículos que enlaza.

El recorrido sigue estos pasos:

1. Construye un índice local de nombres y alias del banco. La misma página se
   compara contra las 300 entidades; no dispara 300 llamadas adicionales.
2. Planifica páginas de metadatos WordPress o mapas del sitio dentro de las
   fuentes habilitadas, del rango y del presupuesto. En el archivo histórico
   muestrea una primera página por año antes de profundizar en la paginación.
   El plan permite inspeccionar sus ventanas y límites.
3. Reutiliza la caché persistente de respuestas mientras siga vigente y los
   candidatos ya conservados. Esto reduce solicitudes repetidas entre sesiones;
   no equivale a haber indexado permanentemente toda la web.
4. Interpreta metadatos acotados, deduplica URLs y filtra menciones pertinentes
   antes de conservar candidatos o programar cuerpos. En sitemaps, un título
   derivado de la URL se identifica como tal; `lastmod` no se transforma en fecha
   de publicación.
5. Solicita cuerpos sólo para candidatos relevantes con permisos editoriales y
   dentro de sus límites. Un extracto o un mapa del sitio permanece como
   referencia; no se usa como sustituto del cuerpo para puntuar.
6. Al llegar al presupuesto deja un archivo histórico **parcial** y puede dejar
   trabajos pendientes que se pueden reanudar. El avance registra páginas procesadas y
   cobertura observada; leer una página compartida no acredita una búsqueda
   exhaustiva de cada nombre.

El tope de páginas corresponde al recorrido guardado, no se reinicia al pulsar
Reanudar. Si se alcanza, seleccione Ampliado o acote el período para profundizar.
El plan incluye las semillas y sus descendientes dentro del mismo límite.

Los candidatos se contrastan después mediante identidad, contexto y evidencia.
Las notas neutras, las negaciones y las menciones de víctimas siguen siendo
material relevante; no se añaden puntos por encontrarlas. Si un nombre aparece
sólo en el cuerpo y no en título, extracto o URL, el primer filtro puede omitirlo.
Un expediente sin evidencia suficiente no debe interpretarse como ausencia de
riesgo ni convertirse artificialmente en un score positivo.

## Escalar hacia un índice propio

Para búsquedas repetidas, el siguiente nivel es una ingestión incremental de
fuentes autorizadas y un índice local persistente. Las consultas al corpus ya
ingestado pueden ser rápidas; construirlo por primera vez y mantenerlo vigente
es un trabajo separado de la consulta interactiva.

GDELT ofrece una vía pública para ello: sus
[Web NGrams 3.0](https://blog.gdeltproject.org/announcing-the-new-web-news-ngrams-3-0-dataset/)
tienen procedencia desde 2020 y el conjunto de quadgrams anunciado en junio de
2026 relaciona frases con URLs mediante un TOC. Una comprobación `HEAD` de la
muestra oficial de un minuto devolvió 8.192.280 bytes de ngrams y 284.249 bytes de
TOC; los archivos no se descargaron en esta investigación. Acumular días o años
globales puede requerir gigabytes y procesamiento adicional.

Por eso la versión usa primero los archivos compartidos de los editores y
sitemaps con presupuestos y caché. Scrapy o Crawlee tendrían sentido para operar
la ingestión continua como servicio separado cuando se necesite ese alcance.
No se promete cobertura completa de 300 personas durante diez años en dos
minutos, ni una equivalencia con la infraestructura de Google. La mejora
concreta es reducir peticiones repetidas y dedicar la descarga de texto a
evidencia con posibilidades reales de resultar pertinente.
