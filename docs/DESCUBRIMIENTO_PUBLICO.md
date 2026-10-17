# Búsqueda web abierta sin claves de desarrollador

WebScope puede obtener candidatos de buscadores generales sin limitar el
descubrimiento a los editores del catálogo. **Los resultados de búsqueda son
enlaces, títulos y extractos; no son artículos leídos ni evidencia puntuable.**
La descarga editorial sigue exigiendo una fuente habilitada y permisos para su
host. Un buscador no concede permiso para descargar todos los sitios que enlaza.

## Recorrido en la aplicación

1. Cargue el banco y revise los nombres y alias. Los nombres completos y las
   marcas corporativas distintivas reducen las coincidencias irrelevantes.
2. En **Preparar fuentes**, elija **Web abierta y fuentes públicas**
   (`strategy: public`). Habilite los proveedores que quiera consultar y registre
   la revisión de sus condiciones. Las fuentes nuevas comienzan desactivadas;
   los permisos existentes se conservan al actualizar.
3. Revise el plan y su presupuesto. Cada entidad recibe primero una consulta
   nominal. Las búsquedas públicas no se multiplican por año: estos servicios
   no garantizan filtros históricos. Los alias siguen al primer barrido.
4. Inicie la búsqueda. El colector mantiene las pausas de cada host, robots,
   caché, límites de respuesta y el tiempo de sesión. Al pausarse quedan tareas
   reanudables; no se alternan motores o formatos para eludir restricciones.
5. Revise los candidatos. Para obtener un cuerpo editorial, habilite su editor
   y el permiso correspondiente. **Analizar y calcular** distingue la evidencia
   disponible, la identidad pendiente y la ausencia de texto original.

La estrategia **Búsqueda dirigida** también admite estos proveedores junto a
los archivos de los editores. **Índice compartido** continúa cotejando páginas
de metadatos contra todo el banco y sirve para listas grandes.

El perfil **Identidad** añade sólo la búsqueda nominal y los alias configurados.
**Señales adversas** añade después del primer barrido dos consultas de texto
libre: nombre + `corrupción` y nombre + `sanción`. **Actividad pública** usa
nombre + `contrato` y nombre + `nombramiento`. Los alias van después de esas
consultas; todas consumen el mismo presupuesto global. No se introducen OR ni
fechas que el proveedor no admita. Las palabras orientan la recuperación: no
acreditan una conducta, no garantizan resultados pertinentes y no generan puntos.

## Proveedores y límites

| Proveedor | Acceso implementado | Límite práctico |
| --- | --- | --- |
| **Mwmbl** | `GET https://mwmbl.org/api/v2/search/?q=...`, JSON anónimo. No requiere cuenta ni clave para la consulta verificada. | Índice de alcance variable. No garantiza actualidad, exhaustividad, idiomas, archivo ni una cuota ilimitada. El límite local predeterminado es de 50 resultados. |
| **DuckDuckGo HTML / Lite** | Interfaces oficiales sin JavaScript; extracción de tarjetas orgánicas y enlaces del resultado. | Integración experimental, sin contrato de API ni capacidad masiva garantizada. Se selecciona un solo formato por plan. Ante CAPTCHA, bloqueo o HTML desconocido se informa un error; no se cambia de endpoint para sortearlo. |
| **Lemmy** | Consulta nominal a una instancia pública, sin cuenta de desarrollador; candidatos con enlaces externos. | Sólo conoce los posts de esa instancia/federación. Un extracto de usuario es una pista. La fecha del post no se convierte en fecha editorial. La instancia verificada exige 60 s entre solicitudes. Véase [fuentes sociales](FUENTES_SOCIALES_PUBLICAS.md). |
| **SearXNG propio** | Adaptador JSON opcional a un endpoint HTTPS operado por el usuario. | Requiere habilitar JSON y mantener el servidor. Depende de sus motores; tener software libre no garantiza acceso ilimitado a ellos. No se seleccionan ni rotan instancias públicas automáticamente. |
| **Arquivo.pt** | API pública de búsqueda textual histórica, sin cuenta ni claves; conserva sólo metadatos y URL originales. | Complemento desactivado por defecto. La ventana de red ocupa como máximo el 10 % de la sesión y sigue al primer barrido de los otros proveedores. Respeta la pausa de 5 s; las consultas pendientes se guardan. La fecha de captura no acredita la publicación. |

Arquivo.pt busca en su propio archivo con la [API documentada](https://github.com/arquivo/pwa-technologies/wiki/Arquivo.pt-API).
La consulta real de viabilidad del 17-10-2026 devolvió 83 capturas y 54 URL
originales para un nombre; 19 URL tenían el nombre completo en título o
extracto. Incluía feeds e índices: no son 19 noticias verificadas ni una prueba
de cobertura masiva. El adaptador conserva y deduplica las capturas por URL,
descarta destinos inseguros y mantiene las fechas de captura separadas de la
fecha editorial. No sigue enlaces de replay, cuerpos archivados ni extracción
de texto. Cada original sigue sujeto a sus permisos y restricciones.

La consulta inicial no se multiplica por años ni supone soporte de OR o lotes.
Los metadatos locales pueden reutilizarse después de agotar la ventana de red.
El siguiente inicio de sesión libera sólo ese límite local; los bloqueos,
robots y pausas de las fuentes permanecen vigentes. Este complemento amplía
el descubrimiento histórico disponible y no garantiza recuperar noticias para
cada nombre.

Mwmbl documenta la búsqueda anónima en su
[implementación oficial de la API](https://github.com/mwmbl/mwmbl/blob/main/mwmbl/tinysearchengine/search.py).
DuckDuckGo documenta sus
[interfaces sin JavaScript](https://duckduckgo.com/duckduckgo-help-pages/features/non-javascript).
Sus [condiciones](https://duckduckgo.com/terms) y
[política de uso](https://duckduckgo.com/acceptable-use) siguen aplicándose;
una respuesta HTTP 200 no equivale a una autorización para reproducir la
interfaz, revender el servicio o imponerle carga ilimitada. La app no inserta
la interfaz del buscador, no ejecuta scripts ni solicita sus recursos auxiliares.

El presupuesto de sesión es un límite de trabajo, no un plazo garantizado para
completar 300 búsquedas. Con una pausa de cinco segundos, iniciar 300 consultas
independientes al mismo host ya requiere aproximadamente 25 minutos, antes de
latencia, DNS, robots y descarga de artículos. Las consultas repetidas se
benefician de la caché; un índice compartido evita una solicitud por nombre.
No se reduce la pausa de un proveedor para simular una aceleración.

## Búsqueda manual en la web y redes

Los enlaces de búsqueda externa se generan localmente para el nombre elegido.
Se abren sólo al pulsarlos en el navegador. Esto permite consultar buscadores
y plataformas que requieren una sesión personal sin pedir credenciales de
desarrollador a WebScope. Generar el enlace no llama al proveedor, modifica
permisos, descarga publicaciones ni produce puntos. Algunas plataformas pueden
pedir iniciar sesión en su propia página.

## SearXNG operado por el usuario

La [API oficial de SearXNG](https://docs.searxng.org/dev/search_api.html) admite
JSON cuando el administrador lo habilita. El
[limitador](https://docs.searxng.org/admin/searx.limiter.html) y las restricciones
de los motores siguen aplicándose. En la configuración de una instalación propia:

```yaml
search:
  formats:
    - html
    - json
```

Una fuente opcional en `news_sources.json` puede usar esta forma; sustituya el
dominio por el servidor que opere y revise las condiciones antes de habilitarla:

```json
{
  "id": "mi_searxng",
  "source_name": "Mi buscador SearXNG",
  "kind": "searxng",
  "url": "https://buscador.example.org/search",
  "allowed_hosts": ["buscador.example.org"],
  "enabled": false,
  "terms_approved": false,
  "robots_required": true,
  "discovery": true,
  "discovery_only": true,
  "document_kind": "discovery",
  "download_articles": false,
  "article_terms_approved": false,
  "body_terms_approved": false,
  "min_interval_seconds": 5,
  "max_records": 50,
  "max_bytes": 2000000,
  "timeout_seconds": 15
}
```

El colector normal sólo admite HTTPS público en el puerto 443 y no accede a
`localhost` ni a redes privadas. Esta configuración no instala SearXNG ni
incluye un servidor dentro del portable; mantiene las dependencias del cliente
en la biblioteca estándar. No incorpora contraseñas ni tokens en las URLs.

## Comprobaciones públicas del 16 de octubre de 2026, UTC

Se verificaron documentación y respuestas reales mediante el proxy heredado,
TLS validado, un agente identificado, robots y pausas por host. No se usaron
datos privados, cuentas, claves de API, rotación, CAPTCHA ni cuerpos editoriales.
La [evidencia compacta](benchmarks/public_discovery_2_9_0.json) conserva URLs de
consulta, estados, fechas UTC, hashes, recuentos y tiempos; omite cuerpos y
cabeceras sensibles. Incluye además la prueba pública de Lemmy: cinco enlaces
externos, cuatro pertinentes según el filtro local, con fechas editoriales
desconocidas y sin convertir extractos de usuarios en artículos.

| Consulta pública | Respuesta | Entradas observadas | URLs HTTPS aceptadas por el adaptador |
| --- | --- | ---: | ---: |
| Mwmbl: `Enrique Peña Nieto` | 200 JSON | 48 | 31 |
| Mwmbl: `Pemex` | 200 JSON | 74 | 55 |
| DuckDuckGo HTML: `"Enrique Peña Nieto"` | 200 HTML | 11 | 10 |
| DuckDuckGo Lite: `"Enrique Peña Nieto"` | 200 HTML | 11 | 10 |
| DuckDuckGo HTML: `"Pemex" investigación` | 200 HTML | 10 | 10 |

Los recuentos anteriores usan un límite de prueba de 100 para inspeccionar la
respuesta; el catálogo limita a 50. Se excluyen enlaces HTTP, credenciales,
direcciones privadas y URLs inseguras, sin transformarlos artificialmente a
HTTPS. Son candidatos antes de la atribución, no una estimación de precisión,
recall, riesgo, cobertura social ni rendimiento de 300 personas. HTML y Lite
se comprobaron como formatos independientes; no hubo fallback tras un bloqueo.

La red de contenido tardó 0.38–0.58 s en Mwmbl y 0.64–0.72 s en DuckDuckGo en
estas cinco observaciones. El recorrido completo incluyó comprobaciones de
robots, DNS y cortesía y fue más largo. En comprobaciones DNS separadas algunos
hosts tardaron aproximadamente cinco segundos; estos valores dependen del
entorno y no constituyen un SLA.

Mwmbl respondió 404 para `robots.txt`, tratado conforme a la política de ausencia
de reglas; DuckDuckGo publicó reglas que permitían esos paths. Bing publicó
`Disallow: /search`, por lo que **no se consultó Bing RSS**. La única instancia
SearXNG pública comprobada, `opnxng.com`, denegaba búsquedas mediante un comodín:
se leyó robots y **no se ejecutó la consulta ni se probó otra instancia**.
Se corrigió el matcher de robots para interpretar comodines, ancla final y la
regla más específica; un parser que sólo compara prefijos interpreta mal esas
reglas.

## Alternativas que no sustituyen un buscador textual

[Common Crawl CDXJ](https://www.commoncrawl.org/cdxj-index) y
[Wayback CDX](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server)
permiten localizar capturas por URL o dominio. No buscan automáticamente un
nombre dentro del texto de toda la web. Construir ese índice exige descargar
y procesar un corpus; la fecha de captura tampoco acredita la publicación.
Common Crawl recomienda espaciar consultas y usar sus índices descargables
para trabajo amplio en su [FAQ](https://commoncrawl.org/faq). La prueba CDX de
Wayback agotó 12 s y no se reintentó. No se integra como fallback de búsqueda.

Los feeds y sitemaps de editores siguen siendo complementos útiles y acotados.
Scrapy, Crawlee y YaCy ayudan a operar un crawler o índice propio, pero instalar
una biblioteca no crea la cobertura histórica de un buscador global. Véase
[crawlers y descubrimiento compartido](CRAWLERS_Y_DESCUBRIMIENTO.md).

## Comprobaciones adicionales del 17 de octubre de 2026

El catálogo 2.9.5 incluye dos perfiles opcionales nuevos, deshabilitados y con
sus permisos de consulta y cuerpos sin conceder. La API pública WordPress de
Sin Línea MX funciona con el adaptador existente: una consulta nominal de Clara
Brugada Molina devolvió 38 candidatos, incluidos los ocho localizados por RSS.
Es una comprobación de descubrimiento para una figura conocida; no mide la
cobertura de notarios, la pertinencia de los cuerpos ni su identidad.

El endpoint distinto `https://www.bing.com/news/search?format=rss` respondió con
RSS real en tres de cinco consultas y con HTML en otras dos. Su adaptador nuevo
señala el HTML como error de formato, conserva el enlace de procedencia y sólo
extrae destinos HTTPS explícitos del wrapper conocido, sin solicitarlo. El
resultado anterior sobre `/search` permanece válido para ese otro path. El
copyright del RSS restringe su presentación a agregadores personales y no
comerciales; otros usos requieren permiso expreso y escrito de Microsoft.
El perfil permanece deshabilitado y la prueba masiva de 300 personas no lo usó.
Los editores de los artículos requieren permisos separados. No se evitan
verificaciones, bloqueos, reglas de robots ni pausas de las fuentes.
