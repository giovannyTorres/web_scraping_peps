# Obtención de noticias: fuentes, permisos y medición

Revisión técnica: 15 de octubre de 2026. El catálogo de ejemplo y el incluido en
la aplicación tienen las mismas fuentes. Todas comienzan desactivadas, con
`terms_approved=false`, `download_articles=false` y
`article_terms_approved=false`. Activar lectura de RSS no habilita automáticamente
descarga de artículos, almacenamiento de un archivo comercial ni redistribución.

## Dos trabajos separados

La adquisición descarga cada RSS autorizado una vez por actualización, conserva
URL, fuente, fecha de consulta, decisiones HTTP/robots y hash, y guarda candidatos
sin atribuirlos a una persona por mera coincidencia de nombre. Una fase local de
identidad relaciona esos candidatos con el padrón, incluyendo homónimos, variantes
corporativas y casos que requieren revisión. El tamaño de 40 mil identidades no
debe multiplicar las solicitudes de cada feed por 40 mil.

Los feeds son ventanas móviles de noticias recientes. Para seguimiento futuro se
construye el banco incrementalmente; para antecedentes se necesitan archivos,
buscadores o índices autorizados adicionales. Un RSS con 10, 40 o 100 entradas no
demuestra cobertura histórica del padrón. Títulos y resúmenes permiten descubrir
documentos; no bastan para establecer un hecho adverso ni una identidad.

## Catálogo inicial mexicano

| Fuente | Endpoint | Condición observada / pendiente | Prueba HTTP |
|---|---|---|---|
| La Jornada | `https://www.jornada.com.mx/rss/edicion.xml?v=1` | RSS gratuito no comercial; almacenamiento personal privado; comercial requiere permiso. | RSS 200; artículo 403, circuito conservado. |
| El Economista | `https://www.eleconomista.com.mx/rss/ultimas-noticias` | Directorio oficial de RSS; derechos del artículo reservados. | RSS 200. |
| Aristegui Noticias | `https://editorial.aristeguinoticias.com/feed/` | Feed del editor; no se estableció licencia de archivo comercial. | RSS 200. |
| Contralínea | `https://contralinea.com.mx/feed/` | Feed del editor; reutilización comercial pendiente. | RSS 200. |
| Forbes México | `https://forbes.com.mx/feed/` | Copia/explotación comercial requiere autorización expresa. | RSS 403; no se evadió. |
| ARTICLE 19 México y Centroamérica | `https://articulo19.org/feed/` | El pie del sitio anuncia CC BY-NC 2.5 México; revisar derechos particulares. | RSS y dos artículos 200, sólo evaluación privada no comercial. |
| Información Center | `https://informacion.center/rss` | RSS oficial gratuito con crédito y enlace; no implica licencia de artículos ajenos. | 301 al feed, RSS 200; reconsulta 200. |
| SinEmbargo | `https://www.sinembargo.mx/feed/` | Candidato de primera parte, permisos y disponibilidad pendientes. | No incluido en esta medición. |

Referencias primarias de permisos:

- [RSS de La Jornada](https://www.jornada.com.mx/rss/) y
  [aviso legal](https://www.jornada.com.mx/pagina/aviso-legal): la disponibilidad
  gratuita no elimina los límites de uso personal y no comercial.
- [Directorio oficial de El Economista](https://www.eleconomista.com.mx/rss.html):
  explica sindicación y lectura RSS; conserva la reserva de derechos.
- [Aviso de privacidad de Aristegui](https://aristeguinoticias.com/aviso-de-privacidad/)
  y [sitio de Contralínea](https://contralinea.com.mx/): se registran como puntos de
  revisión, sin confundir aviso de privacidad con licencia de reproducción.
- [Términos de Forbes México](https://forbes.com.mx/terminos-y-condiciones/):
  incluyen restricciones sobre copia y explotación comercial sin autorización.
- [ARTICLE 19](https://articulo19.org/) y
  [licencia anunciada](https://creativecommons.org/licenses/by-nc/2.5/mx/):
  atribución y uso no comercial, sujeto al alcance de cada obra.
- [Servicios RSS de Información Center](https://informacion.center/rss-services/):
  solicita atribución y enlace a la fuente original.

Los términos pueden cambiar. `robots.txt` regula el acceso técnico; un permiso de
robots no equivale a licencia. Cada configuración operativa debe guardar alcance,
fecha y responsable de revisión. El benchmark privado no aprueba estas fuentes
para un banco comercial. Los reportes exportan métricas, URLs de las solicitudes,
hashes y decisiones, sin publicar cuerpos de noticias.

## Antecedentes mediante GDELT

La [documentación primaria de DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
permite frases entre comillas y bloques OR sin anidación; ArtList admite hasta
250 resultados. `sourcecountry:mexico` selecciona el país del medio, no la
nacionalidad del sujeto; `sourcelang:spanish` selecciona el idioma original.
La ventana documentada es móvil de tres meses. `timespan` acepta intervalos desde
15 minutos; `STARTDATETIME` y `ENDDATETIME` usan `YYYYMMDDHHMMSS`.

El proyecto agrupa frases y deduplica consultas de homónimos, y puede subdividir
intervalos saturados. No hay paginación por cursor documentada: llegar al máximo
en el intervalo mínimo debe quedar como cobertura truncada pendiente.
`max_query_chars=250` es un límite local conservador configurable, no un máximo
oficial verificado. Los lotes se ajustan tanto por longitud como por cantidad de
nombres. El ensayo recibió 429 y conservó el backoff; no demostró cobertura de
antecedentes. El índice no concede permiso sobre los artículos que enlaza.

Con lotes efectivos de seis frases distintas, 40 mil nombres requerirían al
menos 6,667 consultas iniciales; con diez, 4,000. Son escenarios aritméticos:
alias, límites de longitud, ventanas saturadas y reintentos aumentan las llamadas.
El filtro de país deja fuera prensa extranjera sobre entidades mexicanas. La
configuración debe declarar este alcance y ampliarlo cuando exista autorización.

## Benchmark real reproducible

`scripts/benchmark_news.py` utiliza el mismo `SafeFetcher` de producción. Cuenta
HTTP reales, incluidos robots, redirecciones y denegaciones; impone límite global
atómico, tiempo de ejecución, bytes por respuesta y concurrencia. Sólo espera
aplazamientos por intervalo local del host. No insiste ante 429/503, CAPTCHA ni
403. Descarga como máximo dos artículos por fuente y exige los dos permisos
explícitos de cuerpo. Mantiene los circuitos entre ejecuciones.

```powershell
python scripts/benchmark_news.py --config work/fuentes_revisadas.json --state-dir work/benchmark_nuevo --output work/benchmark.json --max-requests 36 --seconds 180 --workers 4 --max-articles 4
```

La configuración de ejemplo desactivada no descarga nada. Para repetir con el
mismo estado, `--reuse-state` borra sólo la caché de representaciones y conserva
robots y restricciones. El reporte etiqueta ese alcance como
`response_cache_only; robots_and_origin_restrictions_retained`, para distinguirlo
de una caché completamente nueva. Nunca se borran bloqueos para mejorar cifras.

Los bytes medidos son cuerpos HTTP recibidos, antes y después de descomprimir;
excluyen cabeceras, TLS y TCP. La latencia de transporte y el tiempo total son
distintos: el total incluye las pausas de cortesía. Cero HTTP en caché caliente
no significa latencia de red cero; significa que no se utilizó la red.

Resultados observados en Windows/Python 3.10.18:

| Ensayo | Tiempo total | HTTP | Bytes recibidos / descomprimidos | Descubrimiento | Cuerpos obtenidos |
|---|---:|---:|---:|---:|---:|
| Lote principal, caché de respuestas fría | 16.813 s | 9 | 458,405 / 995,959 | 224 URLs únicas, cinco RSS | 2 de 4 intentados |
| Mismo lote, caché caliente | 0.266 s | 0 | 0 / 0 | 224 URLs desde caché | 2 desde caché |
| RSS adicional, fría | 6.422 s | 3 | 58,185 / 178,492 | 40 URLs | 0 intentados |
| RSS adicional, caliente | 3.078 s | 1 | 57,755 / 178,062 | 40 URLs | 0 intentados |

La fuente adicional requirió red al reconsultarse: se respetaron sus directivas de
caché. Las cifras de candidatos de los dos lotes son independientes; no constituyen
un conteo de personas ni una demostración de identidad. Se verificaron seis feeds
operativos en seis hosts. En conjunto hubo 27 HTTP reales durante toda la
investigación: 14 en la primera calibración, 9 en el lote corregido y 4 en el
suplemento. La primera calibración detectó un fallo del propio harness: la
consulta de robots contaba como intento y no esperaba después el intervalo del
host. Se corrigió y se agregó una prueba de regresión; esa calibración no se usa
como comparativa fría/caliente.

Los reportes de la entrega son `benchmark_descarga_real.json`,
`benchmark_rss_adicional.json` y `benchmark_descarga_manifest.json`. Incluyen
decisiones por fuente, las reglas de robots consultadas, hashes y marcas de
tiempo. Los dos cuerpos proceden de una ONG, no de una muestra representativa de
todos los periódicos. La medición de descarga es pequeña y honesta; las pruebas
sintéticas de 40 mil identidades evalúan otro componente.

## Condición verificable para una semana

Antes de estimar fecha de terminación hay que definir cuántas noticias únicas,
qué período histórico, qué fuentes autorizadas y qué evidencia de identidad
requiere cada entidad. Sin ello, la meta de 40 mil personas tiene una carga
indeterminada. Sin solapamiento, los siguientes volúmenes exigirían esta tasa
sostenida durante siete días:

| Notas por identidad, supuesto | Artículos distintos | Artículos por segundo |
|---:|---:|---:|
| 5 | 200,000 | 0.331 |
| 20 | 800,000 | 1.323 |
| 100 | 4,000,000 | 6.614 |

El solapamiento reduce descargas gracias a la deduplicación global; las pausas,
fuentes indisponibles y evidencia ambigua aumentan el trabajo. El banco debe
reportar entidades con noticias atribuidas, sin candidatos, ambiguas, pendientes
por bloqueo y con cobertura truncada. Un resultado vacío o una identidad sin
resolver no es evidencia de ausencia de riesgo. Este ensayo no establece SLA ni
cobertura histórica de 40 mil entidades en una semana.
