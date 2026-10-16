# Descubrimiento en fuentes sociales públicas

Las fuentes sociales pueden aportar **pistas y enlaces para revisar**. Una
publicación, su popularidad o el resultado de una búsqueda no prueban identidad,
conducta adversa ni veracidad. Los candidatos sociales se tratan como
`SocialUGC` de descubrimiento: sus extractos no se convierten en cuerpos de
noticias ni entran directamente al scoring.

La verificación del **16 de octubre de 2026** usó solicitudes públicas acotadas,
sin cuentas, claves de API ni tokens. Se conservaron el proxy del entorno y la
verificación TLS. Los resultados describen esos endpoints y ese momento; no
garantizan acceso futuro, autorización de reutilización ni cobertura completa.

## Qué funcionó realmente

| Fuente y operación | Resultado observado | Alcance y decisión |
| --- | --- | --- |
| Lemmy, búsqueda en `lemmy.ml`, API v3 | HTTP 200, cinco posts, 52.682 bytes, 1,057 s. | Candidato para descubrimiento opcional de metadatos. Busca en lo conocido por esa instancia, incluidas comunidades federadas; no en toda la web. |
| Mastodon, RSS de una cuenta pública conocida | HTTP 200, RSS válido, 20 entradas, 30.485 bytes, 1,201 s. | Candidato para leer cuentas seleccionadas y comparar un mismo feed contra todo el banco local. No es búsqueda global ni archivo histórico completo. |
| Mastodon, búsqueda anónima `type=statuses` | HTTP 200 con cero resultados. | No valida una búsqueda funcional de publicaciones. La documentación exige autenticación y soporte del motor de búsqueda para texto completo. |
| Mastodon, timeline público de la instancia probada | HTTP 422: requiere usuario autenticado. | No usar como alternativa anónima ni intentar eludir la restricción. |
| Bluesky, `public.api.bsky.app` / `searchPosts` | HTTP 403, respuesta HTML. | Mantener consulta manual. No cambiar de host o identidad para rodear ese rechazo. |
| YouTube, RSS de un canal conocido | CONNECT rechazado con HTTP 403. | Enlace manual en este entorno. No se verificó la descarga del feed. |
| Sepia Search, índice compartido de PeerTube | HTTP 200, cinco videos, 24.429 bytes, 0,689 s; total bruto informado de 89. | Su `robots.txt` devolvió HTML de aplicación: **no habilitar adquisición automática** mientras no se resuelva esa validación. El total no representa 89 coincidencias pertinentes. |

Estos tiempos son solicitudes individuales reales; no son una medición del
flujo completo de 300 entidades ni una promesa de rendimiento masivo.

## Lemmy: metadatos pertinentes con un límite de instancia

La única consulta probada fue:

```text
https://lemmy.ml/api/v3/search?q=Enrique%20Pe%C3%B1a%20Nieto&type_=Posts&sort=TopAll&limit=5&listing_type=All
```

Los cinco posts contienen el nombre completo tras normalizar acentos. Algunos
lo mencionan como contexto histórico; dos describen el mismo acontecimiento.
Esto valida el descubrimiento de menciones, no cinco hechos adversos
independientes. La respuesta contiene el enlace del post, el enlace editorial
cuando existe, la fecha y la comunidad de procedencia. La fecha del post puede
ser distinta de la publicación del artículo enlazado.

`listing_type=All` incluye contenido local y remoto **ya conocido por la
instancia consultada**. No ordena recorrer cada servidor Lemmy ni indexa la web.
La [documentación oficial](https://join-lemmy.org/docs/print.html) explica que
las comunidades conocidas dependen de la actividad y federación de la instancia;
resolver un objeto remoto por su URL es una operación distinta.

El [robots de lemmy.ml](https://lemmy.ml/robots.txt) devolvió texto válido con
`Crawl-delay: 60`. Deniega `/search/`, pero no `/api/v3/search`. Entre la lectura
de robots y la consulta API transcurrieron 85,455 segundos. La integración debe
respetar al menos esos **60 segundos entre peticiones al host**, además de cuotas,
backoff y presupuesto de sesión. Un intervalo general de cinco segundos no
sustituye una espera más larga de robots.

Con una consulta independiente para cada uno de 300 nombres, ese intervalo
impone casi cinco horas de espera en una sola instancia, antes de profundizar.
Por eso Lemmy sirve como complemento dirigido y con caché; no resuelve por sí
solo un barrido nominal de 300 personas en dos minutos. Los feeds de cuentas
seleccionadas sí permiten comparar una página contra todo el banco sin repetir
una petición por persona.

Hay una diferencia de versiones que debe conservarse en el adaptador: el
endpoint **v3 probado usa `q`**. El
[tipo `Search` oficial actual](https://raw.githubusercontent.com/LemmyNet/lemmy-js-client/main/src/types/Search.ts)
usa `search_term`. No aplicar el contrato de la versión actual del cliente a un
servidor v3 ni cambiar de versión automáticamente cuando falle una petición.

No se verificó una cuota API universal. Las reglas, disponibilidad, bloqueos de
federación y límites dependen de cada instancia. Que una petición anónima
funcione no autoriza extracción ilimitada.

## Mastodon: cuentas conocidas y RSS compartido

Se verificó `https://mastodon.social/@Mastodon.rss`: veinte entradas públicas
recientes, sin autenticación. Es una prueba del mecanismo RSS de esa cuenta;
no acredita que publique información pertinente sobre las entidades del banco.
Una cuenta de otra instancia requiere su propia comprobación y autorización.

La [documentación de búsqueda](https://docs.joinmastodon.org/methods/search/)
distingue cuentas, etiquetas y búsqueda de publicaciones. Un HTTP 200 con
`statuses: []` no demuestra acceso anónimo a texto completo. Los
[timelines públicos](https://docs.joinmastodon.org/methods/timelines/)
también pueden requerir autenticación por configuración del operador.

Los [límites REST documentados](https://docs.joinmastodon.org/api/rate-limits/)
incluyen valores generales de 300 peticiones por cinco minutos por IP y cuenta.
En la prueba se observaron `X-RateLimit-Limit: 300` y `Remaining: 299`.
**No se verificó una cuota independiente para RSS**: utilizar caché, consultas
condicionales cuando el servidor las soporte y una frecuencia conservadora;
no trasladar automáticamente una cuota REST al feed.

## PeerTube: plataforma, índice global y resultados imprecisos

La [documentación oficial de búsqueda](https://docs.joinpeertube.org/use/search)
separa tres operaciones: buscar en lo conocido por una plataforma, usar un
índice externo compartido y resolver un video o canal conocido por URL. La
búsqueda global necesita estar habilitada por el administrador y tampoco
equivale a un índice de toda la web.

Sepia informó 1.734 hosts indexados en la configuración pública observada. Su
[configuración de código abierto](https://framagit.org/framasoft/peertube/search-index/-/raw/master/config/default.yaml)
explica que parte de una lista de hosts y limita las descripciones indexadas a
250 caracteres. Esos extractos no son transcripciones del video.

Para “Enrique Peña Nieto”, tres de los cinco primeros resultados trataban de
Enrique Morente, Enrique Sobejano y Enrique Falcón. Otro sólo incluía el nombre
como etiqueta en un video de una cirugía durante un sismo. El `score` de
búsqueda es una medida de ordenación, no de riesgo ni de identidad. Además,
`publishedAt` y `originallyPublishedAt` pueden diferir por una republicación.

Aunque el endpoint respondió, su robots fue HTML. El recolector debe fallar
cerrado ante ese resultado; no añadir excepciones ni usar otro dominio para
evitarlo. El [aviso legal de Framasoft](https://framasoft.org/en/legals/)
tampoco valida los contenidos de terceros por el hecho de enlazarlos.

## Operación auditable de una fuente y su caché

El contrato de integración debe permitir revisar estos pasos antes de ampliar
el alcance:

1. **Seleccionar y autorizar la fuente.** Registrar una instancia Lemmy concreta
   o la URL RSS de una cuenta pública conocida. Verificar host permitido, TLS,
   robots y política del operador. La fuente empieza desactivada y su permiso
   no habilita las webs que enlaza.
2. **Definir un alcance pequeño.** Registrar consulta, cuenta, intervalo de
   fechas, límite de resultados y presupuesto. Distinguir consultas planeadas,
   ejecutadas y completadas. La espera de robots cuenta dentro del tiempo
   restante; si no cabe, dejar el trabajo reanudable.
3. **Consultar o reutilizar caché.** La clave debe distinguir fuente, endpoint,
   consulta y parámetros. Conservar hash, fecha de adquisición, vigencia,
   estado HTTP y procedencia de caché. Una lectura de caché no se contabiliza
   como HTTP nuevo ni como cobertura nueva de la red. Registrar también las
   respuestas vacías válidas; un 403 o robots inválido no es un resultado vacío.
4. **Filtrar y deduplicar localmente.** Comparar nombres y alias contra todos
   los metadatos de la página. Conservar por separado URL canónica del post,
   URL editorial enlazada, cuenta o comunidad, publicación y adquisición.
   Identificar republicaciones y mantener la procedencia aunque coincidan URLs.
   Un alias necesita contexto; una coincidencia no confirma identidad.
5. **Conservar sólo pistas revisables.** Guardar metadatos acotados y la razón
   de selección o descarte, con su recibo de consulta. No republicar cuerpos
   completos de posts ni convertir discusiones en evidencia periodística. No
   contar entradas leídas como personas efectivamente investigadas.
6. **Revisar y profundizar por separado.** El usuario puede abrir la publicación
   y evaluar su origen. Descargar un artículo enlazado exige la autorización
   editorial correspondiente, robots válido y presupuesto propio. Sólo el
   flujo de identidad y evidencia posterior decide si el documento es apto
   para scoring.
7. **Cerrar o reanudar con un estado explícito.** Distinguir agotamiento de
   presupuesto, espera por host, fallo de permisos, respuesta inválida y cero
   coincidencias. Respetar 429, `Retry-After` y cabeceras de cuota. La caché y los
   recibos deben permitir continuar sin repetir automáticamente consultas ya
   satisfechas ni ocultar fuentes pendientes.

Esta sección define los controles que debe cumplir el adaptador. No atribuye
una pantalla, una cuota, un TTL fijo ni una cobertura histórica que no se haya
validado en la implementación.

### Contrato del adaptador implementado

`peps/social_discovery.py` expone `build_lemmy_search_url`,
`parse_lemmy_results` y `external_social_search_links`.
La URL utiliza exclusivamente la API v3 comprobada, consulta nominal en `q`,
`type_=Posts`, `sort=TopAll` y `listing_type=All`. No añade operadores Boolean
ni filtros temporales que el servidor no haya garantizado. El límite normal
es diez resultados; el parser permite como máximo cincuenta. El helper admite
hasta diez páginas explícitas y no las recorre automáticamente.

El parser conserva sólo candidatos con enlace HTTPS público a un host externo.
Un post sin enlace editorial se omite de este adaptador. La discusión, el enlace
externo y su procedencia permanecen separados. Si varias discusiones enlazan
la misma URL, hay un candidato con la lista acotada `social_referrers`.

Como las cinco respuestas reales mencionaban a Peña en el texto de la discusión
y no en su título, se conserva un **extracto UGC de hasta 500 caracteres** cerca
del nombre. Permite filtrar pertinencia sin presentar ese texto como artículo
leído. Lleva `user_generated_content`, `discovery_only`,
`article_body_available=false`, `identity_verified=false` y `score_effect=none`.
El adaptador no devuelve el cuerpo completo ni perfiles del creador.

La fecha del post va sólo en `metadata.post_published_at`. `published_at` y
`discovered_at` de la noticia enlazada quedan desconocidos; la hora real de
adquisición procede del fetcher. Descargar posteriormente el original es una
operación independiente: requiere una fuente editorial habilitada y sus permisos.
Las pistas sociales por sí solas no habilitan scoring.

La atribución omite NLP de riesgo y no crea documentos ni evidencia para estos
metadatos sin cuerpo editorial. Una prueba de integración comprueba cero
llamadas a NLP y cero filas de evidencia; otra confirma que el original
editorial autorizado sí se analiza utilizando exclusivamente su propio texto,
sin incorporar el extracto del post.

Se comprobó además el adaptador mediante el `SafeFetcher` de producción y
`sources.parse_results`, sin sustituir transporte ni DNS: robots válido, espera
de 60 segundos y una respuesta API HTTP 200 con cinco candidatos. La petición
tardó 6,50 segundos incluyendo DNS/red; la repetición idéntica desde caché tardó
1,54 milisegundos con **cero solicitudes HTTP**. Se respetó el `max-age=60` del
servidor; no se extendió artificialmente a quince minutos.
El recibo, las fechas separadas y los enlaces están en
`runtime/public-discovery-research/lemmy-safe-production.json`, sin cuerpos
completos. Esta medición comprueba una consulta y su reutilización; no mide
cobertura de un padrón ni sustituye la espera por instancia.

## Redes que requieren revisión manual

Bluesky dispone de
[métodos públicos documentados](https://endpoints.bsky.app/), pero la búsqueda
probada devolvió 403. Sus
[límites por servicio](https://bsky.network/docs/rate-limits/)
no garantizan una cuota de búsqueda ni acceso desde este entorno.

Google documenta el feed por canal de YouTube en su
[guía de notificaciones](https://developers.google.com/youtube/data/guides/push_notifications).
Ese feed no es una API de búsqueda global, transcripción o descarga de videos.
El rechazo CONNECT observado impide validarlo aquí. YouTube Data API es un
servicio separado y no se presenta como alternativa sin claves.

Para estas fuentes y para **X o Reddit**, ofrecer enlaces de búsqueda que el
usuario abra y revise en su navegador. Puede requerirse inicio de sesión,
según plataforma, instancia o contenido. No se ha probado acceso anónimo ni
una API gratuita de X o Reddit. Consultar sus condiciones vigentes antes de
desarrollar otro conector:
[X API](https://docs.x.com/x-api) y
[Reddit Data API Terms](https://www.redditinc.com/policies/data-api-terms).
Abrir un enlace manual no habilita extracción automática ni valida lo publicado.

## Evidencia técnica local

Los reportes generados por esta investigación conservan URLs, parámetros,
estados, latencias, tamaños, hashes y limitaciones, sin cuerpos completos de
publicaciones:

- `runtime/public-discovery-research/federated-probe.json`: Lemmy y Sepia,
  robots, documentación oficial y resúmenes de las respuestas.
- `runtime/public-discovery-research/social-discovery-report.json`: Mastodon,
  Bluesky, YouTube, comparación de acceso y recomendaciones.

Son artefactos locales de investigación; no equivalen a un benchmark de
cobertura de 300 entidades ni a una medición del scoring final.
