# Validación real de 300 personas públicas — WebScope 2.9.2

La prueba usó **300 personas reales: 150 presidentes municipales y 150 notarios mexicanos**, con transporte HTTP y reloj reales. Tras la primera pasada y dos reanudaciones, se consultó por red a **278 personas**, terminaron 273 consultas y quedaron **22 personas sin consulta por red**, con **cero cuerpos originales en el banco de 300 personas**. La comprobación final aprobó **23 de 23 controles de integridad**, pero la cobertura continuó incompleta y la salida correcta fue `2`. Los resultados fallidos de las dos reanudaciones anteriores se conservaron; el diagnóstico distinguió una migración legítima de metadatos de la duplicación.

Este resultado detectó límites reales de las fuentes y tres problemas corregibles: consultas concentradas en un proveedor, resoluciones DNS innecesarias durante las pausas y pérdida de una variante nominal con contexto. Una comprobación editorial aislada detectó además recomendaciones de otros artículos mezcladas con el texto principal; se corrigió su extracción. No se presenta el resultado como una revisión concluida de las 300 personas.

## Lista y procedencia

El [CSV importable](../examples/personas_publicas_mexico_300.csv) y el [JSON con procedencia](../examples/personas_publicas_mexico_300.json) contienen 300 IDs y nombres distintos. La muestra reúne 51 presidencias municipales de Nuevo León, 99 de Jalisco y 150 entradas del directorio de notarios de Ciudad de México. Las fuentes, fechas, hashes y límites de vigencia están en [la ficha del padrón](../examples/PERSONAS_PUBLICAS_300.md).

Es una muestra deliberada, no representativa de todo México. Que una persona figure en un directorio no certifica continuidad en el cargo ni habilitación profesional vigente. El padrón publicado contiene nombres, cargos y procedencia pública; no incluye contactos, acusaciones ni puntuaciones personales.

## Primera pasada por internet

La medición terminó el 16 de octubre de 2026. Se usaron Mwmbl y DuckDuckGo para descubrir enlaces y tres índices editoriales compartidos para buscar candidatos adicionales. El presupuesto fue de 810 segundos de búsqueda pública y 90 segundos de índices, ocho trabajadores y un máximo de 90 cuerpos originales. El intervalo solicitado fue del 1 de enero de 2016 al 16 de octubre de 2026; los buscadores empleados no garantizan archivo exhaustivo ni filtros temporales.

| Medida | Resultado observado |
| --- | ---: |
| Personas importadas | 300 |
| Personas con consulta realmente enviada por red | 187 |
| Personas con consulta procesada hasta terminar su tarea | 164 |
| Personas todavía sin consulta por red | 113 |
| Personas con al menos un candidato conservado | 115 |
| Referencias canónicas únicas / observaciones de procedencia | 831 / 835 |
| Cuerpos originales / evidencias / vínculos de identidad | 0 / 0 / 0 |
| Intentos HTTP, incluidos robots y fallos de transporte | 260 |
| Consultas públicas pendientes al terminar su fase | 136 |
| Comprobaciones de integridad aprobadas | 19 de 19 |
| Tiempo total, incluidas atribución, cálculo, reproducción y prueba local | 922,35 s |

Por grupo, se enviaron consultas de 104 de los 150 presidentes municipales y de 83 de los 150 notarios; terminaron 100 y 64 tareas respectivamente. Terminar una tarea de búsqueda no garantiza encontrar información ni consultar un archivo completo: tres respuestas públicas quedaron marcadas como truncadas.

La fase pública consumió 809,88 segundos y los índices editoriales 88,93 segundos. Los 260 intentos HTTP incluyeron 211 respuestas `200`, 33 respuestas `202`, un `404`, un `502`, dos `503` y 12 tiempos de espera agotados. Se respetaron los intervalos de host, robots y los reintentos persistidos. El código permaneció sin cambios durante toda esta medición.

La atribución tardó 5,94 segundos y el cálculo interno de 300 resultados, 0,26 segundos. El ensayo solicitó expresamente el análisis parcial y dejó esa decisión en la auditoría; no trató la descarga como completa. **Las 300 evaluaciones quedaron como `insufficient_evidence`**: los valores numéricos internos de cero no acreditan ausencia de riesgo. No se publica una tabla de puntuaciones personales.

## Por qué no hubo cuerpos y qué comprobó la caché

Los buscadores conservaron títulos, extractos y URLs. Esos metadatos se mantuvieron como descubrimiento, sin convertirlos en texto original ni evidencia puntuable. En el piloto previo de cuatro personas, los 21 candidatos conservados pertenecían a 17 hosts sin una fuente editorial de cuerpos autorizada en la configuración de esa prueba. No se observó un fallo de descarga de esos cuerpos: no se intentó solicitarlos. Los índices editoriales configurados tampoco encontraron menciones pertinentes en las páginas examinadas.

La prueba de caché se ejecutó sobre una **copia independiente** del banco y de sus archivos, con DNS, TCP y HTTP prohibidos. Duró 4,64 segundos, reutilizó 164 respuestas HTTP almacenadas y detectó 835 candidatos ya existentes. No agregó referencias, observaciones ni cuerpos; preservó el banco original, su cola y sus pausas de host. La cadena de auditoría y la reproducción de los 300 cálculos aprobaron.

La cola de la copia local pudo cerrarse al marcar como bloqueadas las consultas sin respuesta almacenada. Esto **no completa la cobertura por internet**: las 113 personas sin consulta por red siguen sin haberse consultado en esa primera pasada.

## Reanudación real con el código corregido

La reanudación conservó las 164 consultas terminadas y el historial de cálculo previo. Sus cifras acumuladas corresponden al mismo padrón y banco, no a otras 300 personas:

| Medida | Resultado acumulado tras reanudar |
| --- | ---: |
| Personas con consulta enviada por red | 271 de 300 |
| Personas con consulta procesada hasta terminar su tarea | 263 de 300 |
| Personas todavía sin consulta por red | 29 |
| Personas con candidatos conservados | 140 |
| Referencias únicas / observaciones en el banco original | 931 / 937 |
| Cuerpos originales / evidencias | 0 / 0 |
| Intentos HTTP de esta sesión / acumulados | 115 / 375 |
| Controles de integridad aprobados | 19 de 21 |
| Tiempo de esta sesión, con reproducción y copia local | 934,80 s |

La fase pública consumió 900,07 segundos. Se observaron 99 respuestas `200`, una `202` y 15 intentos sin estado HTTP; un desafío explícito quedó bloqueado sin cambiar de endpoint ni eludirlo. Al terminar había diez tareas aplazadas y 27 bloqueadas. Se conservan esas restricciones.

**La reanudación no aprobó integridad.** La copia local sin red pasó de 931 a 943 referencias y de 937 a 1.784 observaciones, por lo que fallaron `local_cache_did_not_duplicate_candidates` y `local_cache_did_not_duplicate_observations`. Los demás 19 controles, incluida la auditoría, la preservación del banco original y la ausencia de DNS/TCP/HTTP en la copia, aprobaron. Las 300 evaluaciones continuaron como `insufficient_evidence`.

Esta medición permanece inmutable: no se sustituye por un resultado aprobado ni se interpreta la mayor cobertura nominal como una revisión completa.

La segunda reanudación terminó las diez consultas todavía aplazadas, con 12 intentos HTTP adicionales. Tardó 148,67 segundos, incluidos 110,62 segundos de adquisición, 0,22 de cálculo, 29,87 de reproducción y 6,91 de comprobación local. El resultado acumulado fue **278 personas consultadas por red, 273 consultas terminadas, 142 personas con candidatos y 387 intentos HTTP**. Por grupo, se consultó a 142 presidentes municipales y 136 notarios; terminaron 141 y 132 tareas respectivamente. Permanecieron 27 tareas bloqueadas de DuckDuckGo, sin tareas pendientes: cerrar una cola con bloqueos no completa la cobertura.

El banco original terminó con 933 referencias, 939 observaciones y 900 ejecuciones históricas de cálculo; continuó sin cuerpos ni evidencias. La segunda reanudación también devolvió `1`: su copia local incorporó las mismas 12 referencias y 847 observaciones adicionales que motivaron los dos controles fallidos. No se presenta ninguna de las dos reanudaciones como una aprobación de esos controles.

**Causa de los dos controles fallidos.** Una auditoría independiente de sólo lectura comprobó que el aumento era una migración legítima de relevancia `1.1.0` a `1.2.0`: las 847 observaciones nuevas se descomponen en 835 recibos de nueva versión para los mismos datos de origen y 12 URLs antes excluidas que la variante nominal contextual recuperó para ocho personas. Los 931 artículos y las 937 observaciones anteriores permanecieron intactos, con el mismo prefijo de auditoría. No hubo URLs canónicas duplicadas ni duplicados semánticos de la misma versión. Las 12 incorporaciones siguen siendo sólo candidatos, sin identidad confirmada ni efecto en puntuaciones. Los controles iniciales comparaban cantidades iguales a través de un cambio de modelo; ese comparador no distinguía enriquecimiento de duplicación.

**Comprobación adicional de estabilidad.** Una copia independiente del banco enriquecido en la primera reanudación se volvió a procesar sin DNS, TCP ni HTTP. Conservó exactamente sus 943 referencias, 1.784 observaciones, documentos, evidencias, resultados e historial de cálculo, comprobados por hashes de todas las filas. Aprobó 12 controles en 10,96 segundos y reutilizó 263 respuestas HTTP reales almacenadas. Esta comprobación acredita idempotencia tras el enriquecimiento; no añade personas consultadas ni cuerpos originales y no cambia los resultados fallidos anteriores.

**Verificación final de la migración.** El verificador corregido reprodujo las 847 observaciones nuevas a partir de los bytes originales de la caché, el modelo y la política de fuente; comprobó los 835 linajes previos y las 12 URLs nuevas, sin admitir campos o fechas inexplicados. Después exigió una segunda pasada exactamente estable. Aprobó los 23 controles de integridad en 41,10 segundos, con cero DNS, TCP o HTTP en toda la ejecución. Los 29 módulos de producción y el hash del verificador se conservaron durante la prueba. La copia terminó con 945 referencias y 1.786 observaciones; el banco original mantuvo sus 933 referencias y 939 observaciones. Esta comprobación reutiliza búsquedas reales ya observadas: no es una cuarta búsqueda por internet, no aumenta las 278 personas consultadas y conserva la salida `2` por cobertura incompleta.

## Correcciones derivadas de las pruebas

**Plan de búsqueda.** Se reparten las primeras consultas nominales entre proveedores antes de añadir las variantes con contexto. Para el mismo padrón y un presupuesto de 300 consultas, la programación pasó de 300 consultas en Mwmbl a 150 en Mwmbl y 150 en DuckDuckGo. El mínimo teórico por intervalos de host pasó de 1.495 a 745 segundos. Es un cálculo de programación sin HTTP: excluye latencia, robots, fallos y cuerpos, y no demuestra que la búsqueda real termine en ese tiempo. El plan ampliado requiere 1.200 consultas; el presupuesto de 300 sigue siendo parcial.

**Transporte.** Las tareas ya aplazadas por intervalo de host o circuito se comprueban antes de resolver DNS. En una reproducción local de 1.000 aplazamientos, las llamadas DNS simuladas bajaron de 1.000 a cero; se conservaron las 1.000 filas de auditoría y no se hicieron solicitudes HTTP. La validación de destino y la protección SSRF continúan aplicándose antes de cada solicitud efectiva. Un desafío de acceso identificado explícitamente en HTML con estado `202` ahora detiene el acceso. Un `202` por sí solo no se clasifica como CAPTCHA; las 33 respuestas reales no conservaban su cuerpo y no permiten esa conclusión retrospectiva.

**Recuperación nominal.** `metadata-relevance-1.2.0` permite una variante personal acotada sólo con apoyo geográfico. Al reproducir sin red los 24 resultados reales de metadatos del piloto, conservó 22 candidatos frente a 21 y recuperó una nota editorial plausible. No perdió candidatos anteriores; mantuvo fuera un resultado ajeno y una variante de directorio sin confirmar. Este cambio recupera candidatos, sin validar identidad ni modificar puntuaciones. La muestra no mide precisión o exhaustividad poblacional.

**Limpieza editorial.** Una prueba técnica aislada realizó siete solicitudes HTTP y extrajo tres cuerpos de páginas editoriales reales, **fuera del banco de 300 personas y sin calcular puntuaciones**. Se inspeccionaron contenedores, comienzo y final del texto y bloques de recomendación. El ensayo detectó enlaces relacionados mezclados con el cuerpo en dos capturas. La corrección, reproducida sin red sobre los HTML guardados, obtuvo:

| Captura | Caracteres antes | Después | Comprobación |
| --- | ---: | ---: | --- |
| SDP Noticias | 8.343 | 8.343 | Control sin cambios |
| Milenio | 5.309 | 5.215 | Retira un bloque de recomendación |
| El Heraldo de México | 1.771 | 1.569 | Retira etiqueta y dos párrafos de navegación |

Se conservaron exactamente el texto y el orden de los párrafos retenidos. La versión `article-main-5` vuelve a extraer capturas anteriores durante la atribución, sin red; conserva la captura original y el historial, y una segunda pasada es idempotente. Los textos completos de estas páginas permanecen fuera del repositorio. Una lectura HTTP exitosa no concede licencia de reutilización: el aviso legal revisado de Milenio impone restricciones expresas, por lo que la prueba no habilita su uso rutinario o comercial.

## Verificación y reanudación

Después de integrar las correcciones y revisar el verificador, la suite local final ejecutó **838 pruebas: 831 aprobaron y 7 se omitieron**, sin fallos ni errores, en 42,05 segundos. Las regresiones cubren DNS, pausas, desafíos de acceso, variantes nominales, navegación editorial, conservación de capturas, reextracción y procedencia exacta de la migración. Esta cifra corresponde a una ejecución local; no sustituye el resultado de CI de la versión final.

La wheel local `peps_mx-2.9.2` se instaló en un entorno aislado. Coincidieron los hashes de sus 29 módulos Python con el código corregido; aprobaron 15 comandos y la presencia e integridad de 16 recursos, sin acceso externo a la red. El SHA-256 de la wheel fue `bb7549ddada93115bc03bdb993988be8f7d104969ac13879febb63c761268821`. El portable de Windows se valida en CI.

La [primera ejecución de CI](https://github.com/giovannyTorres/web_scraping_peps/actions/runs/38867193403) encontró cuatro errores al limpiar bases SQLite temporales en pruebas nuevas de Windows 3.12; no fallaron las aserciones. El portable quedó sin ejecutar. Se corrigió el cierre de conexiones y la [segunda ejecución](https://github.com/giovannyTorres/web_scraping_peps/actions/runs/38868706920) aprobó sus siete trabajos, incluido el portable. Esa ejecución precede al cierre del verificador de migración: no se atribuye al código final posterior. La publicación exige aprobar nuevamente la matriz y el portable del commit a integrar; su resultado se consulta en [las comprobaciones del PR #16](https://github.com/giovannyTorres/web_scraping_peps/pull/16/checks).

Para reproducir con un banco nuevo:

```bash
python scripts/validate_real_people.py --online \
  --roster examples/personas_publicas_mexico_300.json \
  --data-dir runtime/prueba-300-real \
  --output runtime/prueba-300-real.json
```

Para continuar ese mismo padrón, periodo y plan conservando las búsquedas terminadas:

```bash
python scripts/validate_real_people.py --online --resume \
  --roster examples/personas_publicas_mexico_300.json \
  --data-dir runtime/prueba-300-real \
  --output runtime/prueba-300-real-reanudada.json \
  --local-dir runtime/prueba-300-real-local-reanudada \
  --fallback-seconds 0
```

El programa rechaza bancos nuevos no vacíos sin `--resume` y rechaza reanudar con un padrón o plan diferente. La salida `1` indica fallo de integridad; `2`, cobertura incompleta; `0` exige aprobar ambos controles. No se omiten las pausas de proveedores para obtener una salida aprobatoria.

## Evidencia y límites

El [resumen verificable en JSON](benchmarks/real_people_300_2_9_2.json) conserva hashes del padrón, recibos, medición inicial y fuentes del código por separado. Los hashes de la primera pasada corresponden a su código congelado; no se sustituyen por hashes posteriores para atribuirle correcciones que todavía no tenía. El JSON distingue el ensayo editorial aislado, las reproducciones sin red y las comprobaciones locales.

La prueba demuestra importación, búsqueda parcial real, conservación de procedencia y detección correcta de evidencia insuficiente. La auditoría y la segunda pasada local distinguen el enriquecimiento entre versiones de la duplicación y comprueban estabilidad tras la migración. **Todavía no se demuestra una revisión completa con cuerpos originales de las 300 personas ni un tiempo de dos minutos para esa revisión por internet.** Los resultados de buscadores y las coincidencias nominales necesitan corroboración en fuentes y revisión humana.
