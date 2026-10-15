# Historial local de evaluaciones

Cada nueva evaluación se conserva en `score_runs` dentro del mismo archivo
SQLite de la aplicación. No necesita servidores ni otra base de datos. La
migración es aditiva: añade `scores.run_id`, tablas/índices de historial y
protecciones contra actualización o eliminación de sus filas.

Las evaluaciones existentes antes de esta migración siguen disponibles como
resultado actual, pero no se inventan sus entradas históricas. Recalcule para
obtener la primera evaluación con un snapshot reconstruible.

## Qué se conserva

El JSON canónico de cada ejecución contiene:

- `schema_version`: versión del formato histórico, actualmente 1.
- `entity_snapshot`: entidad exacta recibida al evaluar, incluidos alias y
  contexto del banco. El contexto participa en las firmas de identidad.
- `evidence_manifest`: IDs de evidencia/documento, orden original de entrada,
  análisis autoritativo, revisión humana, resolución de identidad, revisión del
  banco, fuente, fechas, URL, metadatos e identificadores de evento utilizados.
- `result`: resultado completo recibido, con contribuciones, exclusiones,
  explicaciones, análisis auxiliares y `provenance` cuando lo añade el motor.

El manifiesto no incorpora un campo con el cuerpo completo. Referencia el
documento ya conservado en `documents`. Las citas que forman parte del análisis
y del resultado permanecen como evidencia; esa redundancia explicativa no se
utiliza para multiplicar el score. La estructura preexistente `scores.result`
conserva una copia del resultado actual por entidad para compatibilidad con la
interfaz y los consumidores anteriores; el historial no duplica cuerpos.

El ID es `SHA256(JSON_canónico(payload))`, sin la fecha de guardado. Repetir
exactamente entidad, entradas, orden y resultado reutiliza una sola fila.
Cambiar revisión, fecha de evaluación, configuración/modelo identificado en
`result.provenance` o cualquier otro contenido crea otro ID. La fecha de creación
se conserva fuera del contenido direccionado y se vincula al evento
`score_run_saved` de la bitácora. Cada actualización del resultado actual
registra además `entity_scored` con el puntero al historial.

## Integridad y reconstrucción

El manifiesto conserva `document_record_hash`, calculado sobre ID, URL, título,
hash del cuerpo, identificador/tipo de fuente, fechas y metadatos del documento.
Al abrir una evaluación se comprueba que:

1. El payload coincide con su ID y con la entidad solicitada.
2. Toda la cadena de eventos es consistente dentro de la misma transacción de
   lectura. Después se localiza el anuncio correspondiente y se valida su hash.
3. La fecha de creación y el hash del resultado coinciden con ese anuncio.
4. Todos los documentos referidos siguen presentes y conservan sus campos,
   además del texto original. Modificar sólo la fecha o los metadatos también
   invalida la reconstrucción; no basta con que el cuerpo conserve su hash.

La operación de guardado, el evento y el puntero actual comparten transacción.
Los triggers `score_runs_no_update` y `score_runs_no_delete` impiden reemplazar
una ejecución mediante SQL ordinario. `verify_audit()` comprueba toda la cadena,
el resultado actual y sus punteros, los documentos, todas las ejecuciones y sus
anuncios. Detecta ejecuciones anunciadas pero eliminadas, filas históricas sin
anuncio y cambios en snapshots o metadatos referidos.

La lectura de una ejecución, sus evidencias, su materialización o un resumen de
historial comprueba la cadena completa antes de devolver resultados. No basta
con que el anuncio tenga un hash válido: modificar un resultado y recalcular
sólo ese anuncio deja roto el enlace siguiente y debe fallar cerrado. Un listado
de hasta 200 resúmenes reutiliza una sola comprobación dentro de su transacción;
no reutiliza el chequeo entre transacciones. El costo por solicitud incluye un
recorrido de todos los eventos de auditoría y debe medirse a escala real.

`verify_audit()` añade el chequeo de todos los documentos y ejecuciones de la
base, incluidos antecedentes ajenos al resultado consultado. El enlace de
anuncios usa un índice por hash en SQLite con JSON1. Si JSON1 no está disponible,
un filtro SQL sobre el JSON canónico utiliza el índice de acción y analiza en
Python solamente las coincidencias. No se introduce otro requisito de
instalación.

Esta auditoría detecta inconsistencias locales. Una persona con control total
del archivo puede quitar protecciones y reescribir toda la base y su cadena.
Para detectar esa reescritura se necesita conservar un hash o respaldo fuera
del archivo; no se afirma que exista una firma externa inviolable.

## API interna de almacenamiento

| Método | Contrato |
|---|---|
| `save_score(entity_id, result, connection=None, entity_snapshot=None, evidence_snapshot=None)` | Guarda la ejecución y devuelve su ID. El motor pasa los snapshots exactos del cálculo en su misma transacción. |
| `get_score(entity_id)` | Resultado actual o `None`; mantiene el contrato anterior de lectura del resultado. |
| `latest_score_run(entity_id)` | Ejecución validada del puntero actual o `None`. No revive resultados históricos invalidados. |
| `score_history(entity_id, limit=50)` | Hasta 200 resúmenes, con fecha, score, modelo y marca `current`. Valida payload/anuncio; comprueba cuerpos al abrir la ejecución. |
| `get_score_run(run_id, entity_id=None)` | Snapshot completo y validado. Devuelve `None` si no existe o pertenece a otra entidad; inconsistencia de integridad provoca `ValueError`. |
| `run_evidence(run_id, evidence_id)` | Recupera una evidencia perteneciente al manifiesto, aunque esté fuera de las primeras 200 notas actuales o su vínculo se haya retirado. Valida el documento solicitado. |
| `materialize_run_evidence(run)` | Recarga el snapshot persistido y reconstruye las entradas originales ordenadas para comprobar el cálculo. No confía en modificaciones del objeto enviado por el llamador. |

El fallback de `save_score` sin snapshots explícitos permite llamadas existentes:
lee entidad y evidencia actuales dentro de la transacción de guardado. No puede
recuperar entradas antiguas si el llamador calculó el resultado antes de que
cambiaran los datos. Para nuevas integraciones deben pasarse siempre los inputs
exactos usados al calcular.

Los vínculos actuales de identidad o evidencia pueden retirarse sin destruir el
historial. Las revisiones históricas se interpretan con su banco y revisión del
momento, sin compararlas con el banco actual al mostrarlas.

Al guardar una revisión desde la interfaz, `review(..., expected_run_id=...)`
comprueba dentro de `BEGIN IMMEDIATE` que sigue vigente la evaluación mostrada,
que pertenece a la entidad y que no cambiaron revisión del banco, identidad,
contenido, revisión previa ni metadatos de la nota. Si otro analista revisó la
nota o cambió el banco, falla con «La evaluación cambió; recargue antes de
revisar» sin sobrescribir la decisión vigente. Abrir un antecedente histórico
no autoriza revisar la evidencia actual. Las llamadas heredadas sin este
parámetro siguen disponibles para CLI e integraciones existentes.

## Reproducir el cálculo

Materializar entradas permite volver a ejecutar el scoring con la entidad y
`as_of` históricos. Antes deben compararse las versiones, parámetros y hashes
de código de la autoridad de scoring conservados en `result.provenance` con
los disponibles. Cuando no coinciden, la aplicación debe indicar que esa
versión no puede reproducirse con el runtime actual. No debe sustituirla
silenciosamente ni presentar el resultado nuevo como reproducción exacta.

Los análisis auxiliares se conservan como salida histórica. Reconstruir el
scoring y su desglose no significa volver a ejecutar un modelo NLP anterior.
La respuesta de replay debe distinguir ambas cosas y señalar expresamente
cuando no se reprodujeron los auxiliares.

Los offsets usan puntos de código Unicode de Python y se refieren al texto
íntegro original, sin normalización. En JavaScript deben convertirse mediante
`Array.from(text)` o un mapa equivalente; `String.slice` usa unidades UTF-16 y
desplaza los fragmentos después de emojis. Siempre debe verificarse que el
fragmento recuperado coincide con la cita antes de resaltarlo.

## Privacidad, exportación y retención

Los snapshots permanecen en el equipo del usuario. El contexto, las revisiones
y las citas pueden contener datos personales: no deben copiarse en logs de
servidores, repositorios de ejemplos ni servicios externos. Proteja el archivo
SQLite y sus respaldos mediante los permisos y cifrado de disco del equipo.

Los nuevos records JSONL `record_type=score_run` se exportan de uno en uno. Los
documentos se exportan como registros separados y las entidades incluyen
`score_run_id`. Una ejecución individual puede ser grande si una entidad tiene
muchas notas; la exportación no carga todo el historial en memoria, pero sí
materializa el payload de cada ejecución.

El historial crece con las evaluaciones distintas. Recalcular sin cambios
reutiliza el payload, aunque conserva el evento de actualización actual. Borrar
o invalidar el resultado actual no elimina antecedentes ni sus datos
personales. No hay purga automática: cualquier política de retención futura
debe definir qué documentos/ejecuciones puede retirar y cómo dejar evidencia de
esa retirada. Los triggers no sustituyen dicha política.

## Verificación

```bash
python -m unittest tests.test_score_history -v
python -m unittest discover -s tests -q
```

Las pruebas usan sólo personas, notas y URLs sintéticas. Cubren deduplicación,
metadatos alterados sin cambiar el cuerpo, ausencia/orfandad de ejecuciones,
protecciones de escritura, rollback, invalidez del puntero actual, retirada de
vínculos, migración, lookup después de 200 notas y offsets Unicode exactos.
Incluyen el ataque que recalcula el ID del resultado y el hash de su anuncio,
sin reescribir la cadena completa: ninguna lectura histórica lo acepta. También
comprueban que el historial valida la cadena una vez por transacción y detecta
una alteración posterior aunque una consulta anterior haya sido válida.
