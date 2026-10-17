# Validación completa con trabajo real

`scripts/validate_live_pipeline.py` ejecuta la importación, las búsquedas por
nombre, la descarga de originales permitidos, la atribución de identidad, el
scoring de producción, las explicaciones, la exportación y la reproducción de
cada resultado. No introduce respuestas de buscadores, documentos, identidades,
confirmaciones humanas ni relojes simulados.

El padrón completo exige al menos 150 presidentes municipales y 150 notarios
distintos, con procedencia pública. `--allow-small-roster` identifica un piloto
real y nunca lo convierte en una prueba de 300 personas. Los nombres de los
grupos y las referencias de incidentes no se incorporan a las consultas. El
catálogo debe tener fuentes con permisos explícitos para el experimento y
`robots_required: true`; los cuerpos requieren sus permisos separados. Estos
indicadores no acreditan una licencia comercial ni modifican el catálogo normal.

## Congelar antes de solicitar la web

La planificación usa el código de producción. Rechaza índices generales,
presupuestos de consultas truncados y padrones en los que alguna persona carece
de consulta nominal. `max_queries` debe cubrir nombres, fuentes, alias y ventanas.

```bash
python scripts/validate_live_pipeline.py --plan-only \
  --roster runtime/live-e2e-2026-10-17/roster-frozen.json \
  --sources-config runtime/live-e2e-2026-10-17/source-probes/sources-config.json \
  --data-dir runtime/live-e2e-2026-10-17/bank-cold-v1 \
  --output runtime/live-e2e-2026-10-17/cold-v1-result.json \
  --workers 16 --body-budget 600 --per-entity-body-limit 8
```

El modo anterior no abre un banco ni hace solicitudes. Imprime las huellas del
padrón, catálogo, personas importables y plan completo. La corrida fría usa las
mismas opciones y sustituye `--plan-only` por `--online`:

```bash
python -u scripts/validate_live_pipeline.py --online \
  --roster runtime/live-e2e-2026-10-17/roster-frozen.json \
  --sources-config runtime/live-e2e-2026-10-17/source-probes/sources-config.json \
  --data-dir runtime/live-e2e-2026-10-17/bank-cold-v1 \
  --output runtime/live-e2e-2026-10-17/cold-v1-result.json \
  --seconds 2400 --acquire-seconds 2100 --max-http 5000 \
  --workers 16 --body-budget 600 --per-entity-body-limit 8 \
  --local-seconds 60
```

`--seconds` limita el trabajo online completo y `--acquire-seconds` reserva parte
de ese tiempo para atribución y scoring. La última unidad de trabajo en ejecución
puede terminar después de la parada cooperativa. `--max-http` es acumulativo
entre reanudaciones e incluye robots y redirecciones. Las pausas y restricciones
reales de cada fuente se mantienen: estos límites no prometen cobertura ni una
duración de dos minutos.

## Checkpoints y reanudación

Cada intento crea una carpeta distinta junto al banco, con estos archivos:

- `checkpoint.json`: fase, tiempos, primer cuerpo, conteos, cola y señales de
  interrupción.
- `http-started.jsonl`, `http-completed.jsonl`, `fetches.jsonl` y `progress.jsonl`:
  observaciones escritas durante el trabajo; permanecen aunque falle una fase.
- `response_captures/`: bytes originales de cada transporte, con SHA-256.
- `score-replays.json`: comprobación del scoring contra sus entradas históricas.
- `production-export-private.jsonl` e `individual-explanations-private.json`:
  salidas reales, completas y locales. Pueden contener textos y detalles
  individuales; no se incluyen en publicaciones públicas.
- `report.json`: resultados, limitaciones, estados y errores de ese intento.

`SIGINT` o `SIGTERM` solicita una parada cooperativa. Los trabajos pendientes,
reintentos y tiempos de cortesía se conservan. Reanude con el mismo banco,
`--resume`, el mismo catálogo y padrón, y un `--output` nuevo. Antes de escribir,
el runner verifica huellas, recibo de auditoría, nombres, alias, contexto y leases
vigentes. Rechaza modificaciones del plan o de los presupuestos congelados. Las
consultas terminadas mantienen sus claves y estados; no se importan otra vez las
entidades. Cada intento registra las huellas de su código para distinguir una
corrección posterior del código empleado en la medición original.

## Interpretación

Una tarea terminada, un candidato y un original descargado son cantidades
distintas. El informe separa búsquedas completadas, intentos de fetch, HTTP real,
candidatos actuales, originales únicos, pares entidad/documento y cobertura por
persona y grupo. Las latencias del transporte y del fetch se desglosan por fuente.
`people_with_at_least_one_completed_query` exige un recibo de al menos una
consulta por persona; `people_with_all_queries_completed` exige todas sus
consultas planificadas. Terminar la cola conocida mantiene la cobertura parcial
si existen tareas bloqueadas, fallidas o agotadas, y esos conteos se archivan
también en la procedencia de cada scoring.

Tener 300 filas de scoring no demuestra 300 evaluaciones útiles. Se conservan los
estados de producción `insufficient_evidence`, `insufficient_text`,
`pending_identity` y `evaluated`; los tres primeros muestran
`display_score: null`. Un cero calculado tampoco acredita ausencia de riesgo.
El informe no calcula precisión, recall o accuracy sin revisión independiente de
los textos originales y denominadores de referencia.

La reproducción y las explicaciones usan la API de producción en lotes de hasta
1.000 evaluaciones. Cada lote verifica la cadena de auditoría completa una vez
dentro de la misma transacción de lectura que valida todos los resultados y
cuerpos. No recorta evaluaciones ni reutiliza una comprobación fuera de su
snapshot; las explicaciones históricas completas permanecen en la salida local.

La comprobación posterior de caché usa dos lecturas reales de una copia
independiente con `cache_mode=local`. Informa cualquier crecimiento, cambio de
filas o solicitud inesperada y no modifica el banco frío. Esta fase de integridad
se cronometra por separado del flujo online. Los fallos de integridad y las
coberturas parciales permanecen en los resultados; no se sustituyen por datos
de prueba ni se eliminan para obtener un indicador favorable.
La primera lectura puede registrar exclusivamente un `local_cache_miss` en el
estado de una referencia sin cuerpo original y con su tarea bloqueada ligada.
Ese cambio se enumera con sus campos anteriores y nuevos; la comparación
estricta de filas sigue indicando `false`. IDs, textos, observaciones,
evidencias, identidades, scoring e historia deben permanecer iguales, y la
segunda lectura y la proyección de candidatos deben ser exactamente estables.

El proceso sale con código `1` ante una comprobación operacional fallida y `2`
si las comprobaciones pasan pero la cobertura es parcial. Salir con `0` sólo
significa que terminó el plan conocido y pasó la integridad, nunca que se agotó
la web o se validó la identidad de cada persona.
