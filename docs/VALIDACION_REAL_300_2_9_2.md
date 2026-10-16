# Validación real de 300 personas públicas — WebScope 2.9.2

La prueba usó **300 personas reales: 150 presidentes municipales y 150 notarios mexicanos**, con transporte HTTP y reloj reales. La primera pasada **no completó la cobertura**: consultó por red a 187 personas, terminó 164 consultas y conservó 831 referencias únicas, pero obtuvo **cero cuerpos originales en el banco de 300 personas**. Los controles de integridad aprobaron; el control de cobertura falló y el proceso devolvió `2`.

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

La atribución tardó 5,94 segundos y el cálculo interno de 300 resultados, 0,26 segundos. **Las 300 evaluaciones quedaron como `insufficient_evidence`**: los valores numéricos internos de cero no acreditan ausencia de riesgo. No se publica una tabla de puntuaciones personales.

## Por qué no hubo cuerpos y qué comprobó la caché

Los buscadores conservaron títulos, extractos y URLs. Esos metadatos se mantuvieron como descubrimiento, sin convertirlos en texto original ni evidencia puntuable. En el piloto previo de cuatro personas, los 21 candidatos conservados pertenecían a 17 hosts sin una fuente editorial de cuerpos autorizada en la configuración de esa prueba. No se observó un fallo de descarga de esos cuerpos: no se intentó solicitarlos. Los índices editoriales configurados tampoco encontraron menciones pertinentes en las páginas examinadas.

La prueba de caché se ejecutó sobre una **copia independiente** del banco y de sus archivos, con DNS, TCP y HTTP prohibidos. Duró 4,64 segundos, reutilizó 164 respuestas HTTP almacenadas y detectó 835 candidatos ya existentes. No agregó referencias, observaciones ni cuerpos; preservó el banco original, su cola y sus pausas de host. La cadena de auditoría y la reproducción de los 300 cálculos aprobaron.

La cola de la copia local pudo cerrarse al marcar como bloqueadas las consultas sin respuesta almacenada. Esto **no completa la cobertura por internet**: las 113 personas sin consulta por red siguen sin haberse consultado en esa primera pasada.

## Correcciones derivadas de las pruebas

**Plan de búsqueda.** Se reparten las primeras consultas nominales entre proveedores antes de añadir las variantes con contexto. Para el mismo padrón y un presupuesto de 300 consultas, la programación pasó de 300 consultas en Mwmbl a 150 en Mwmbl y 150 en DuckDuckGo. El mínimo teórico por intervalos de host pasó de 1.495 a 745 segundos. Es un cálculo de programación sin HTTP: excluye latencia, robots, fallos y cuerpos, y no demuestra que la búsqueda real termine en ese tiempo. El plan ampliado requiere 1.200 consultas; el presupuesto de 300 sigue siendo parcial.

**Transporte.** Las tareas ya aplazadas por intervalo de host o circuito se comprueban antes de resolver DNS. En una reproducción local de 1.000 aplazamientos, las llamadas DNS simuladas bajaron de 1.000 a cero; se conservaron las 1.000 filas de auditoría y no se hicieron solicitudes HTTP. La validación de destino y la protección SSRF continúan aplicándose antes de cada solicitud efectiva. Un desafío de acceso identificado explícitamente en HTML con estado `202` ahora detiene el acceso. Un `202` por sí solo no se clasifica como CAPTCHA; las 33 respuestas reales no conservaban su cuerpo y no permiten esa conclusión retrospectiva.

**Recuperación nominal.** `metadata-relevance-1.2.0` permite una variante personal acotada sólo con apoyo geográfico. Al reproducir sin red las 24 resultados reales de metadatos del piloto, conservó 22 candidatos frente a 21 y recuperó una nota editorial plausible. No perdió candidatos anteriores; mantuvo fuera un resultado ajeno y una variante de directorio sin confirmar. Este cambio recupera candidatos, sin validar identidad ni modificar puntuaciones. La muestra no mide precisión o exhaustividad poblacional.

**Limpieza editorial.** Una prueba técnica aislada realizó siete solicitudes HTTP y extrajo tres cuerpos de páginas editoriales reales, **fuera del banco de 300 personas y sin calcular puntuaciones**. Se inspeccionaron contenedores, comienzo y final del texto y bloques de recomendación. El ensayo detectó enlaces relacionados mezclados con el cuerpo en dos capturas. La corrección, reproducida sin red sobre los HTML guardados, obtuvo:

| Captura | Caracteres antes | Después | Comprobación |
| --- | ---: | ---: | --- |
| SDP Noticias | 8.343 | 8.343 | Control sin cambios |
| Milenio | 5.309 | 5.215 | Retira un bloque de recomendación |
| El Heraldo de México | 1.771 | 1.569 | Retira etiqueta y dos párrafos de navegación |

Se conservaron exactamente el texto y el orden de los párrafos retenidos. La versión `article-main-5` vuelve a extraer capturas anteriores durante la atribución, sin red; conserva la captura original y el historial, y una segunda pasada es idempotente. Los textos completos de estas páginas permanecen fuera del repositorio. Una lectura HTTP exitosa no concede licencia de reutilización: el aviso legal revisado de Milenio impone restricciones expresas, por lo que la prueba no habilita su uso rutinario o comercial.

## Verificación y reanudación

Después de integrar las correcciones, la suite local ejecutó **835 pruebas: 828 aprobaron y 7 se omitieron**, sin fallos ni errores, en 49,63 segundos. Las regresiones cubren DNS, pausas, desafíos de acceso, variantes nominales, navegación editorial, conservación de capturas y reextracción. Esta cifra corresponde a una ejecución local; no sustituye el resultado de CI de la versión final.

**La reanudación de la búsqueda real está en curso.** Sus resultados aún no están disponibles y no se usan en las cifras iniciales anteriores. La validación final de CI y el artefacto portable también están pendientes en esta versión del informe.

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

La primera pasada demuestra importación, búsqueda parcial real, conservación de procedencia, caché sin duplicados y detección correcta de evidencia insuficiente. **Todavía no demuestra una revisión completa con cuerpos originales de las 300 personas ni un tiempo de dos minutos para esa revisión por internet.** Los resultados de buscadores y las coincidencias nominales necesitan corroboración en fuentes y revisión humana.
