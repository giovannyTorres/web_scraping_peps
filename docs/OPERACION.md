# Operación local y automatización

## Datos y formatos

Todos los comandos usan el mismo directorio de datos. Es recomendable una ruta local con permisos de Windows restringidos a los operadores autorizados. Las bases SQLite no están cifradas por esta aplicación; proteja el disco y sus respaldos mediante los controles de la organización.

Los ejemplos desde código fuente usan `python -m peps`. En el paquete portable use `PEPS-CLI.exe` para importaciones, workers, exportación y diagnóstico por consola. Abra `PEPS-MX.exe` con doble clic para la interfaz gráfica. Ambos ejecutables requieren la carpeta `_internal` que se distribuye junto a ellos; no copie un `.exe` aislado. Sin `--data-dir`, ambos usan la misma ubicación predeterminada de Windows.

Ejemplo de CSV de entidades:

```csv
id,name,kind,aliases,context
demo-001,Entidad de Prueba MX,company,Empresa Demo MX,"{""country"":""MX"",""state"":""Nuevo León""}"
demo-002,Persona Pública de Prueba,person,Alias de Prueba,"{""country"":""MX"",""position"":""Cargo de prueba""}"
```

Los nombres del ejemplo son sintéticos. Incluya contexto legítimamente disponible para reducir homónimos. No use un RFC inventado ni trate un nombre exacto como identificación definitiva. Un ID estable permite actualizar la misma entidad; cambiar el ID crea otro registro.

Cada línea de documentos JSONL contiene un objeto:

```json
{"entity_name":"Entidad de Prueba MX","url":"https://example.org/documento-publico","title":"Documento de prueba","text":"Entidad de Prueba MX publica una memoria de actividades.","source_kind":"news","source_id":"fuente_demo","published_at":"2026-10-01","metadata":{"test_only":true,"full_text_archived":false}}
```

`example.org` es ilustrativo y no acredita una fuente real. `entity_ids` puede señalar explícitamente IDs existentes; `entity_name` exige una coincidencia inequívoca con el banco. La importación local de noticias usa el mismo `IdentityResolver` y conserva resoluciones y metadatos de identidad; especificar un ID no confirma la identidad ni autoriza scoring por sí mismo. Los documentos oficiales y `legacy` mantienen sus adaptadores separados. La aplicación no descarga automáticamente las URLs al importar JSONL: el operador debe garantizar que el texto y la procedencia sean auténticos.

Utilice `metadata` para procedencia adicional, identificador de expediente, alcance del extracto, tipo de paráfrasis, fecha de actualización y limitaciones. Los campos personalizados al nivel raíz no se conservan como metadatos. Una fecha vacía debe permanecer desconocida; no sustituya la fecha de publicación por la de descarga o por el año cubierto por un informe.

La combinación de URL y cuerpo identifica una versión documental. Reimportar la misma URL con el mismo cuerpo conserva la fecha y los metadatos originales; cambiar el contenido genera una versión nueva. Corregir únicamente fecha o metadatos exige revisión explícita y un procedimiento controlado: la reimportación no los sobrescribe automáticamente. Conserve la justificación de cualquier corrección en la revisión y el registro operativo.

## Modo cerrado

```powershell
python -m peps --data-dir .\runtime import-entities .\transferencia\entidades.csv
python -m peps --data-dir .\runtime import-documents .\transferencia\documentos.jsonl
python -m peps --data-dir .\runtime reanalyze
python -m peps --data-dir .\runtime ui
```

Sin `--online`, el worker no realiza recolección. La UI y el análisis funcionan con los documentos locales. El módulo de noticias separa `news-acquire` (red autorizada) de `news-attribute` (atribución local sin red). Para actualizar un entorno sin salida a internet, transfiera paquetes de documentos y un manifiesto con URLs, fechas, hashes y alcance mediante el procedimiento aprobado de la organización. El sistema no puede obtener noticias nuevas de internet desde una red completamente aislada.

`reanalyze` ejecuta el motor actual sobre el banco y recalcula resultados. Conserve un respaldo y una exportación antes de una actualización de reglas, para poder contrastar versiones.

## Importación histórica y listas oficiales

```powershell
python -m peps --data-dir .\runtime import-entities .\notebooks\output\00_peps_normalized.csv
python -m peps --data-dir .\runtime import-legacy .\notebooks\output\04_master_evidence.csv
```

La importación de entidades conserva `seed_id` como ID cuando no se proporciona `id`. La evidencia histórica se vincula primero por ese ID, comprobando que el nombre corresponda; para bancos importados antes de 2.2.1, admite el nombre normalizado sólo cuando identifica una entidad única. Los nombres ambiguos, conflictos entre ID y nombre, entidades desconocidas y filas sin texto se omiten con contadores explícitos en `legacy_skipped`. Se conservan la fila de origen en `metadata.legacy_record`, las fechas, los hashes declarados y el fragmento o resumen disponible; un hash histórico declarado no equivale a verificar nuevamente su archivo. Sus snippets y resúmenes no sustituyen la lectura de la fuente original; toda esa evidencia se marca para revalidación y no puntúa. No ejecute notebooks antiguos esperando que sus puntuaciones equivalgan al modelo actual.

`import-official` procesa una copia local de una lista oficial y valida el archivo completo en un área temporal antes de publicar documentos en el banco. No realiza la descarga de la lista:

```powershell
python -m peps --data-dir .\runtime import-official .\transferencia\lista-onu.xml --source un --source-url https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list
python -m peps --data-dir .\runtime import-official .\transferencia\sdn.xml --source ofac --list-name sdn --source-url https://sanctionslist.ofac.treas.gov/Home/SdnList
python -m peps --data-dir .\runtime import-official .\transferencia\sat-69b.csv --source sat69b --source-url https://wwwmat.sat.gob.mx/consultas/76674/consulta-la-relacion-de-contribuyentes-con-operaciones-presuntamente-inexistentes
```

Registre la URL real del recurso o su publicación oficial de origen. El comando conserva el SHA-256 del archivo, estados y nombres alternativos. Un archivo truncado o esquema desconocido termina con código distinto de cero y no publica sus registros parcialmente parseados. La importación agrega evidencia histórica: no elimina automáticamente registros que desaparecen de otra copia.

La API Python `peps.official` también permite convertir snapshots en streaming para procedimientos de transferencia. Ejemplo:

```python
import json
from pathlib import Path
from peps.official import load_official_file, official_record_document

with Path("lista-onu.jsonl").open("w", encoding="utf-8") as output:
    def emit(record):
        doc = official_record_document(
            record,
            "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list",
        )
        output.write(json.dumps(doc, ensure_ascii=False) + "\n")
    result = load_official_file("lista-onu.xml", "un", on_record=emit)
print(result)
```

Use la URL real del recurso importado y conserve el SHA-256 del archivo original en el manifiesto. `load_official_file` devuelve `ok`, `partial`, `invalid` o `missing`; sólo `ok` y `complete=true` permiten considerar completa una actualización. Una salida parcial debe ponerse en cuarentena, no reemplazar una copia válida. Los formatos avanzados de OFAC fallan explícitamente; los estados SAT desconocidos permanecen desconocidos. Las fechas antiguas y las bajas requieren actualización posterior: este importador no sincroniza automáticamente eliminaciones entre snapshots.

## Fuentes y recolección autorizada

El módulo 2.1 usa `news_sources.json` dentro de `--data-dir`, creado desde `peps/defaults/news_sources.json`. Edite esa copia o use `--news-config` después de un subcomando `news-*`. La configuración anterior `sources.json` y `--config` continúa disponible para adaptadores previos; no sustituye a la de noticias. Un cambio posterior en los defaults no reemplaza automáticamente la configuración guardada del operador.

Antes de habilitar una fuente, registre términos, finalidad, dominio exacto, tipo documental, intervalo, límite de tamaño y contacto identificable en el User-Agent. La lectura requiere `enabled=true` y `terms_approved=true`; guardar cuerpos editoriales exige además `download_articles=true` y `article_terms_approved=true`. Estos flags documentan decisiones del operador; no verifican por sí mismos la licencia ni constituyen autorización del editor. Los permisos RSS no autorizan automáticamente un archivo comercial. El catálogo mantiene ambas autorizaciones desactivadas y documenta restricciones en [FUENTES_NOTICIAS.md](FUENTES_NOTICIAS.md).

RSS se obtiene una vez por feed y ventana. GDELT agrupa nombres y alias en consultas OR y conserva los IDs de homónimos. Cada editor necesita permiso de cuerpo y una lista `allowed_hosts` con su host exacto; de otro modo se conserva sólo descubrimiento sin scoring. `source_country=mexico` filtra medios mexicanos, no personas mexicanas: use un valor vacío cuando el objetivo incluya prensa extranjera en español sobre estas entidades. El prefiltro por título/resumen puede omitir artículos sin nombres; `--all-articles` lo omite sólo para fuentes con permiso de cuerpo. `gob.mx` o DOF, configurados como fuentes directas, no recorren por sí solos todo su sitio. Para PDF use un extractor local revisado y luego JSONL.

```powershell
python -m peps --data-dir .\runtime news-queue
python -m peps --data-dir .\runtime --online news-acquire --workers 4 --seconds 240 --max-tasks 1000 --no-queue
python -m peps --data-dir .\runtime news-attribute --limit 1000
python -m peps --data-dir .\runtime news-stats
```

Repita atribución para continuar el banco; `--force` permite reanalizarlo. Cambiar el padrón invalida resoluciones y revisiones anteriores para scoring: reatribuir actualiza la resolución, pero una revisión humana anterior debe renovarse. Cambiar las reglas también provoca reatribución por su hash. `candidate`, `ambiguous` y `rejected` no puntúan automáticamente. Cuando un documento contiene varias menciones homónimas, la revisión humana debe seleccionar los offsets `identity_spans` correctos; confirmar todo el texto no identifica todas sus menciones. Véase el flujo completo en [NOTICIAS.md](NOTICIAS.md).

Las ventanas de GDELT saturadas se subdividen y los límites no resueltos quedan truncados. Los feeds pueden limitar registros; el banco informa esos cortes en sus estadísticas. Ni una tarea terminada ni una ventana vacía demuestran cobertura histórica de una persona. Los comandos anteriores `queue`/`worker` conservan compatibilidad con la cola de adaptadores previos.

Las peticiones usan HTTPS y direcciones IP públicas previamente validadas, con verificación de certificado y SNI del host original. Se admite `ca_bundle` para una CA aprobada. El transporte actual no implementa proxies HTTP CONNECT ni utiliza proxies obligatorios del entorno: una red que exija ese mecanismo necesita una adaptación administrada o transferencia offline. No desactive TLS para resolverlo.

## Ejecución continua y Programador de tareas

```powershell
python -m peps --data-dir .\runtime --online worker --daemon --daily --workers 4 --seconds 240 --max-jobs 1000
```

`--daemon` mantiene ciclos; `--daily` encola una ventana UTC diaria mediante el módulo de noticias. Después de procesar el lote, ese modo ejecuta atribución local de las noticias disponibles. El worker también puede drenar trabajos de adaptadores previos que ya existan. La deduplicación de claves impide repetir la misma tarea de feed o consulta y ventana. Cierre con Ctrl+C o con los controles de la aplicación. La UI, un worker o varios procesos deben compartir exactamente el mismo directorio de datos para compartir cola y límites por dominio. Para el modo continuo mantenga las fuentes revisadas en `runtime/news_sources.json`; una ruta `--news-config` usada en otro comando no cambia por sí sola esa configuración persistida.

El script de ejemplo registra una ejecución cada cinco minutos con `worker --daily`: encola noticias por ventana UTC, procesa la cola y atribuye después del lote:

```powershell
.\scripts\register_worker.ps1 -Executable .\dist\PEPS-MX\PEPS-CLI.exe -DataDirectory .\runtime -Name PEPS-MX-Worker
```

El directorio de datos debe existir y contener `news_sources.json` revisado. El script evita instancias superpuestas; las claves persistidas impiden duplicar la misma tarea de feed o consulta y ventana al reiniciar cada proceso. Use el Programador de tareas o el modo continuo, según su operación. Un worker manual sin `--daily` sólo drena la cola y necesita `news-attribute` por separado. Registrar la tarea altera el Programador de tareas de Windows y debe hacerse al decidir activar la automatización.

## Bloqueos y recuperación

Revise los estados `blocked`, `deferred` y `dead` y los errores recientes. Un 401, 403 o desafío CAPTCHA detiene el acceso; un 429 o fallo transitorio programa una espera. Reintentar no corrige una denegación del editor ni concede permiso.

```powershell
python -m peps --data-dir .\runtime retry
python -m peps --data-dir .\runtime --online worker --workers 4 --seconds 240 --max-jobs 1000
```

Use `retry` después de resolver la causa. El circuito persistido del dominio puede seguir posponiendo peticiones aunque se reactive el trabajo. No rote IPs, no use resolutores de CAPTCHA y no intente sortear restricciones. La política del producto es conservadora y puede reducir cobertura incluso cuando un recurso sea visible manualmente.

## Revisión, respaldo y escala

La UI muestra hasta 200 evidencias por entidad; utilice exportación para examinar el banco completo. La UI obtiene un ticket de descarga de un solo uso, válido 30 segundos, y el navegador guarda la respuesta transmitida a disco sin construir un Blob global. La CLI también escribe registro a registro. El formato JSONL diferencia `record_type=entity`, `document` y `evidence`; los IDs vinculan los registros. No es el mismo formato que la entrada de `import-documents` y no debe reimportarse directamente con ese comando. Confirmar una evidencia exige revisor y motivo. Una confirmación humana no convierte automáticamente una alegación en hecho judicial ni sustituye actualizar su estado procesal.

```powershell
python -m peps --data-dir .\runtime backup .\respaldos\banco.sqlite3
python -m peps --data-dir .\runtime export .\exportaciones\banco.jsonl
python -m peps --data-dir .\runtime audit
python -m peps capacity --entities 40000
```

La exportación CLI se publica sólo al terminar: si falla la escritura, conserva la exportación anterior. Tanto exportación como respaldo rechazan el banco activo y sus archivos auxiliares como destino, incluidos enlaces al mismo archivo.

La API de respaldo SQLite produce una copia consistente del banco y cierra las conexiones al terminar. Una restauración debe realizarse con la aplicación y workers detenidos, en un directorio nuevo y con los archivos originales preservados. El respaldo de la base no incluye automáticamente configuración, estado HTTP ni `news_snapshots/`; consérvelos por separado. No borre el estado HTTP para eludir circuitos o mejorar un benchmark. `audit` verifica la cadena de eventos, los hashes del cuerpo documental y las puntuaciones actuales; devuelve código de salida 2 si detecta una inconsistencia, para que las automatizaciones puedan detenerse. Mantenga un hash o respaldo externo para detectar una eventual reescritura completa del banco.

SQLite admite múltiples lectores y un escritor a la vez; los índices y lotes ayudan, pero la aplicación no ofrece cómputo distribuido horizontal. `news-plan` estima peticiones y días bajo distribución equilibrada entre fuentes autorizadas. Su resultado es optimista: faltan latencias sostenidas, consultas adicionales por saturación, bloqueos, descarga histórica y revisión humana. Una proyección dentro de siete días no constituye SLA ni demuestra noticias atribuidas a las 40 mil identidades.
