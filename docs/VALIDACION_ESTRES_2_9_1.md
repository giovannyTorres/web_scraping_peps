# Estrés y correcciones de WebScope 2.9.1

Validación del 16 de octubre de 2026. Los ensayos masivos usan bancos temporales,
SQLite y reloj reales, con respuestas HTTP simuladas y DNS/TCP prohibidos.
Los tiempos de procesamiento local no representan velocidad, relevancia ni
cobertura de buscadores públicos. Las consultas públicas reales se miden aparte.

## Fallos corregidos

- **Colas grandes:** los índices de reclamación y tareas agotadas evitan ordenar
  toda la cola por cada trabajo. Se preservan entregas únicas y leases activos.
  Los prefijos SQL tratan `_`, `%` y `\` literalmente: descargar noticias no
  reclama ni modifica tareas como `newsflash`.
- **Tareas antiguas corruptas:** JSON inválido o no estructurado queda en estado
  agotado con ID, motivo y hash auditados, conservando el original. La cola
  continúa con tareas válidas; los listados y cobertura toleran esas filas.
- **Caché e historial:** las menciones anteriores a las últimas diez observaciones
  vuelven a considerarse. La lectura por páginas y una caché temporal acotada
  evitan retener todo el corpus. Se aprovecha la proyección sólo cuando coincide
  con la revisión y el modelo actuales. `all-history-1` obliga a reconstruir
  índices derivados de 2.9.0; conserva observaciones, documentos y evaluaciones.
- **Preparación interrumpida:** el presupuesto y la pausa cubren también la
  preparación del banco y del plan. Una cola vacía antes de terminar de preparar
  no significa descarga completada. El estado persiste al reiniciar y bloquea
  el análisis automático; el análisis parcial exige una decisión explícita.
- **Progreso:** las colas pequeñas mantienen lectura por recibo. En colas grandes
  se limita el muestreo a cuatro lecturas por segundo, indicando su antigüedad;
  el cierre consulta SQLite de nuevo. El total utiliza el número completado
  observado junto al mismo resumen, sin sumar pendientes antiguos a avances nuevos.
- **Sesiones y reintentos:** una publicación se confirma después del commit;
  errores de bloqueo o commit permiten reintentar sin publicar un final sin
  auditoría. Cada artículo de la sesión pertenece a una categoría de cuerpo; una actualización puede
  reclasificarlo de reutilizado a nuevo. Los reintentos conservan esperas futuras
  y las reservas se calculan después de obtener el bloqueo de escritura.
- **Web malformada:** se controlan `Retry-After` extremos, codificaciones inválidas,
  fechas imposibles y Unicode no válido. El parser HTML evita búsquedas
  cuadráticas ante cierres inexistentes. La descompresión conserva sus límites.
- **Control visual:** un reintento pendiente mantiene el bloqueo durante el
  polling. La preparación incompleta muestra su causa y una acción para
  completarla; no se atribuye erróneamente a un cambio del banco.

## Ensayos independientes

La [evidencia final del código distribuido](benchmarks/stress_release_2_9_1.json)
conserva los hashes de todos los módulos y los resultados individuales:

| Carga local | Resultado |
| --- | --- |
| 10.000 URLs, 32 trabajadores, banco de 40.000 entidades | 107,48 s en frío; 12,42 s desde caché |
| Integridad de adquisición | 10.000 referencias y cuerpos; 2.000 documentos tras deduplicar dentro de cada editor; cero pendientes |
| Cola de 100.000 tareas, ejecutar 1.000 | 110,52 → 3,78 s; lecturas completas de progreso 1.008 → 14 |
| Proyección de 90.001 referencias repetitivas, banco de 10.000 | 2,37 s, completa y auditada |
| 90.001 metadatos distintos, presupuesto de 120 s | Incompleto; pausa en 120,03 s, rollback, historia intacta y unos 43 MiB de pico |

El proceso completo de adquisición, incluida la preparación de fixtures,
comprobaciones y caché, tardó 121,40 s y usó unos 316 MiB de pico. El caso de
metadatos distintos sigue siendo un **límite de capacidad**, sin una proyección
completa en dos minutos; se conserva como tal en la evidencia. No se calcula
throughput de una fase que no terminó.

Una comprobación pública pequeña, con Peña Nieto, Pemex y Grupo Bimbo en Mwmbl y
DuckDuckGo, terminó seis consultas en 21,34 s con 132 referencias únicas. Su
repetición local tardó 0,27 s, sin DNS/TCP ni nuevos registros. Son candidatos
de descubrimiento, no cuerpos de noticias ni identidades comprobadas; no se
creó evidencia ni scoring a partir de esos extractos.

El [rescate de candidatos](benchmarks/rescue_stress_2_9_1.json) utilizó 3.000
entidades, 90.000 referencias y 50.000 observaciones sobre una URL. El baseline
agotó 60,03 s sin encolar cuerpos. La versión corregida encoló 300 en 24,38 s,
con una construcción inicial de proyección medida aparte en 18,64 s. Recuperó
24 pistas históricas, conservó hashes de los datos originales y usó unos 59 MiB
de pico. La medición precede únicamente al ajuste posterior de totales de progreso.

La [proyección](benchmarks/projection_stress_2_9_1.json) separa un corpus repetitivo
de otro con metadatos distintos. El caso repetitivo tiene nueve entradas distintas
de relevancia: su mejora no debe generalizarse a 90.001 textos únicos. Con un
presupuesto corto, el caso distinto conserva historia y devuelve explícitamente
`passed:false` y salida `1` al no completar. Un límite alcanzado no se declara éxito.

El parser de HTML malformado de 130 KB pasó de 6,27 a 0,11 s. También se ejercitó
HTML de 1,95 MB. Una respuesta gzip de 19,5 KB que expandiría 20 MB se detuvo al
superar 32.768 bytes, sin guardar una respuesta incompleta. La caché soportó
2.048 lecturas con 64 trabajadores, una sola copia, todas las lecturas auditadas
y cero solicitudes adicionales.

En navegador, bancos de 3.000 y 10.000 entidades con 12.000 tareas conservaron
899 nodos DOM. El caso de 10.000 abrió en 1,14 s y mostró 25 tareas en 168 ms;
durante el polling no reconstruyó las filas ni perdió el foco. Son mediciones
diagnósticas con carga concurrente, no un SLA ni una comparación antes/después.
Pasaron además los controles de doble inicio, pausa, cancelación obsoleta,
reintento concurrente, límites de petición y cobertura parcial.

## Reproducción

Desde un checkout nuevo, sin claves, modelos opcionales ni archivos de runtime:

```bash
python scripts/stress_pipeline.py --profile limit --output runtime/estres.json
python scripts/stress_job_queue.py --output runtime/cola.json
```

El primer comando ejecuta procesos separados: adquisición de 10.000 URLs con
32 trabajadores y banco de 40.000 entidades; cola de 100.000 tareas; escenarios
de 300 identidades con 249 noticias ajenas por respuesta; transporte hostil;
90.001 referencias repetitivas y distintas; y rescate histórico. El tiempo por
caso está acotado y un timeout produce un resultado fallido. La generación de
fixtures y comprobación de integridad forman parte del tiempo del proceso.
Cada reporte conserva hashes, métodos, límites, memoria cuando es medible y
enlaces a los resultados de cada caso. El perfil `quick` reduce los volúmenes.

Para comparar con 2.9.0 puede seleccionar un checkout separado con
`--source-root`. Cada script individual admite `--output` y puede ejecutarse
sin un baseline. Un fallo devuelve salida distinta de cero y mantiene su
reporte; no se sustituyen resultados incompletos por tiempos de throughput.
El caso `unique_projection` del orquestador es un control negativo: fuerza un
presupuesto insuficiente y sólo aprueba el control si se verifica una pausa
íntegra y auditable. Su resultado individual conserva `completed:false`,
`passed:false` y salida `1`; no se afirma que haya completado la proyección.

La suite completa ejecutó **783 pruebas: 776 aprobadas y 7 omitidas por NLP
opcional**. El wheel se instaló en un entorno aislado y pasó 15 comandos y la
comprobación de sus 16 recursos. La CI valida Linux/Windows, Python 3.10/3.12,
interfaz, NLP opcional y el portable de Windows.
