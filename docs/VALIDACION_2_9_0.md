# Validación 2.9.0: caché, descubrimiento y control del pipeline

Comprobaciones del 16 de octubre de 2026, UTC. Se distinguen peticiones públicas
reales, reproducción local de capturas reales y datos sintéticos. No se midió
precisión predictiva del scoring ni cobertura exhaustiva de internet.

## Cambios comprobados

- Tres modos explícitos: reutilizar copias vigentes, revalidar las URLs del lote
  o consultar sólo caché local. La revalidación de cuerpos sigue las cuotas y la
  pertinencia; no recorre todo el archivo sin límite.
- Copias locales sin DNS ni HTTP. Las fechas de adquisición y validación se
  conservan separadas. Un fallo de caché local queda reanudable y no consume un
  intento de red. Los permisos actuales y robots conocidos se comprueban también
  para destinos redirigidos, en caché HTTP y en metadatos ya analizados.
- La conexión SQLite de lectura de caché se cierra explícitamente al terminar,
  incluso ante un error de caché. Una regresión conserva referencias fuertes a
  las conexiones para detectar el archivo bloqueado observado en Windows.
- Cuerpos idénticos conservan atribución, revisiones y evidencia. La caché
  mantiene las cabeceras de paginación WordPress; activar el permiso de cuerpos
  no invalida por sí mismo una representación de metadatos equivalente.
- Las coincidencias se proyectan para la revisión actual del banco. Corregir un
  nombre manteniendo su ID no conserva candidatos de su nombre anterior. Hasta
  reconstruir el índice se indica **Por actualizar**. Los originales y la historia
  permanecen conservados.
- La sesión guarda fecha, alcance, revisión de banco, peticiones, caché, cuerpos
  nuevos/reutilizados, duplicados y exclusiones. Publica contadores durante el
  proceso y serializa escrituras para evitar retrocesos por concurrencia. Una
  métrica no medida es `null`, presentada como **—**.
- Los reintentos individuales sólo afectan las tareas de noticias seleccionadas.
  Conservan pausas futuras, robots y bloqueos de transporte; no inician una nueva
  adquisición. Las reservas activas siguen protegidas.
- Buscadores y extractos sociales quedan fuera de NLP y evidencia. Los cuerpos
  originales de editores autorizados mantienen su análisis independiente.

## Red pública real y caché

El [validador reproducible](../scripts/validate_public_pipeline.py) usó el colector,
parsers, reloj y SQLite de producción, con transporte real y presupuesto acotado.
Consultó Mwmbl y DuckDuckGo HTML para **Enrique Peña Nieto, Pemex y Grupo Bimbo**.
Una subclase sólo observó y limitó el transporte; no sustituyó respuestas.

| Medición final | Resultado |
| --- | ---: |
| Consultas nominales terminadas | 6 |
| Peticiones de transporte, incluidos robots | 8 |
| Respuestas | 7 HTTP 200; 1 HTTP 404 de robots Mwmbl |
| Tiempo de descubrimiento real | 21,01 s |
| Referencias con URL canónica única | 132 |
| Observaciones de procedencia | 135 |
| Repetición sólo desde caché local | 0,23 s |
| DNS / conexiones / HTTP en la repetición | 0 / 0 / 0 |
| Lecturas de caché HTTP | 6 |
| Nuevas referencias u observaciones en la repetición | 0 |

Los 132 elementos son **referencias candidatas**, con fechas editoriales
desconocidas; pueden ser páginas generales, no sólo noticias. El filtro nominal
no confirma identidad ni relevancia jurídica. No se descargaron cuerpos en esta
prueba ni se generó evidencia o scoring a partir de extractos. Una primera corrida
independiente obtuvo 133 referencias en 21,69 s y reutilizó la caché en 0,30 s;
el contenido y los tiempos de los proveedores varían.

La [evidencia compacta](benchmarks/public_pipeline_2_9_0.json) contiene recuentos,
URLs de consulta, hashes y comprobaciones, sin cuerpos completos. Las pruebas de
Lemmy, HTML/Lite y las restricciones de otras redes se documentan en
[descubrimiento público](DESCUBRIMIENTO_PUBLICO.md) y
[fuentes sociales](FUENTES_SOCIALES_PUBLICAS.md).

```bash
python scripts/validate_public_pipeline.py --online \
  --data-dir runtime/mi-validacion-publica \
  --output runtime/mi-validacion-publica.json
```

Requiere un directorio vacío; no modifica bancos anteriores. Las fuentes sólo se
habilitan en ese banco de prueba con nombres públicos.

## Rendimiento local masivo

Corpus **sintético**, con SQLite y reloj reales: 300 entidades, 9.000 referencias
y 95.000 observaciones. El rescate de candidatos pasó de **55,39 a 4,89 s**;
las conexiones SQLite bajaron de **7.275 a 3**. Se evita abrir conexiones por cada
artículo y volver a tokenizar repetidamente el mismo extracto; la consulta de
observaciones recientes usa un índice específico.

La reconstrucción completa de la proyección del banco se midió por separado:
**4,28 s**. La lectura de cobertura tardó **17,87 ms** en frío y **0,74 ms** con
resumen reutilizado. No se sustituye una reconstrucción por un cero ficticio.
El [reporte](benchmarks/pipeline_cache_2_9_0.json) conserva baseline, hashes y
método; permite repetir la medición sin el directorio runtime original:

```bash
python scripts/benchmark_pipeline_cache.py \
  --baseline docs/benchmarks/pipeline_cache_2_9_0.json \
  --output runtime/mi-benchmark-cache.json
```

Estos tiempos describen procesamiento local. Consultar 300 nombres distintos en
un único proveedor con cinco segundos de separación exige alrededor de 25 minutos
sólo para iniciar las peticiones. Para listas grandes se mantiene el índice
compartido, que coteja una página contra toda la lista. El presupuesto de dos
minutos conserva avance y pendientes; no garantiza completar toda la web.

## Cuerpos capturados e historia

Sobre una **copia** del banco de la validación pública 2.8 se reprodujeron 19 HTML
editoriales reales ya capturados. Se construyeron recibos de lectura local;
**no fueron nuevas respuestas HTTP 304 reales**. Los 19 cuerpos quedaron sin
cambios, no hubo reatribución redundante, se conservaron documentos, evidencia y
evaluaciones, y las ocho evaluaciones actuales existentes continuaron siendo
reproducibles. DNS y conexiones estuvieron prohibidos. Tardó 0,54 s y la auditoría
permaneció válida; el banco original no se modificó.

## Interfaz y distribución

La interfaz sustituye las cuatro casillas por fuente por dos permisos explícitos,
muestra un catálogo compacto y conserva búsqueda y acceso al catálogo completo.
El inicio fijo guarda alcance, confirma la selección existente y comienza el
proceso en una acción. No autoriza fuentes nuevas por defecto. La continuación al
análisis es opcional; cualquier cobertura incompleta exige **Analizar lo disponible**.

Se comprobaron **263 condiciones de navegador** con datos sintéticos y servidor
real: pasos, bloqueos, pausa/reanudación, reintento individual, métricas, caché,
sesiones anteriores, importación, explicaciones y enlaces manuales. El botón
principal permaneció dentro de 1280 × 900 y 390 × 720, sin desborde ni controles
enfocados tapados. No hubo peticiones externas ni errores de navegador.

La suite final local ejecutó **732 pruebas: 725 aprobadas y 7 omitidas por el
componente NLP opcional**. El wheel se valida en entorno aislado y la CI comprueba
Linux/Windows, Python 3.10/3.12, navegador, NLP opcional y el portable final de Windows.
La guía y capturas actuales están en [FLUJO_GUIADO.md](FLUJO_GUIADO.md).
