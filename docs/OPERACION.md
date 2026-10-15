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

`example.org` es ilustrativo y no acredita una fuente real. `entity_ids` puede señalar explícitamente IDs existentes; `entity_name` exige una coincidencia inequívoca con el banco. Si no se especifican, el índice intenta encontrar nombres y alias en el documento. La aplicación no descarga automáticamente las URLs al importar JSONL: el operador debe garantizar que el texto y la procedencia sean auténticos.

Utilice `metadata` para procedencia adicional, identificador de expediente, alcance del extracto, tipo de paráfrasis, fecha de actualización y limitaciones. Los campos personalizados al nivel raíz no se conservan como metadatos. Una fecha vacía debe permanecer desconocida; no sustituya la fecha de publicación por la de descarga o por el año cubierto por un informe.

La combinación de URL y cuerpo identifica una versión documental. Reimportar la misma URL con el mismo cuerpo conserva la fecha y los metadatos originales; cambiar el contenido genera una versión nueva. Corregir únicamente fecha o metadatos exige revisión explícita y un procedimiento controlado: la reimportación no los sobrescribe automáticamente. Conserve la justificación de cualquier corrección en la revisión y el registro operativo.

## Modo cerrado

```powershell
python -m peps --data-dir .\runtime import-entities .\transferencia\entidades.csv
python -m peps --data-dir .\runtime import-documents .\transferencia\documentos.jsonl
python -m peps --data-dir .\runtime reanalyze
python -m peps --data-dir .\runtime ui
```

Sin `--online`, el worker no realiza recolección. La UI y el análisis funcionan con los documentos locales. Para actualizar un entorno sin salida a internet, transfiera paquetes de documentos y un manifiesto con URLs, fechas, hashes y alcance mediante el procedimiento aprobado de la organización. El sistema no puede obtener noticias nuevas de internet desde una red completamente aislada.

`reanalyze` ejecuta el motor actual sobre el banco y recalcula resultados. Conserve un respaldo y una exportación antes de una actualización de reglas, para poder contrastar versiones.

## Importación histórica y listas oficiales

```powershell
python -m peps --data-dir .\runtime import-entities .\notebooks\output\00_peps_normalized.csv
python -m peps --data-dir .\runtime import-legacy .\notebooks\output\04_master_evidence.csv
```

La importación histórica se vincula por `nombre_persona_input`. Sus snippets y resúmenes no sustituyen la lectura de la fuente original; se marcan para revalidación. No ejecute notebooks antiguos esperando que sus puntuaciones equivalgan al modelo actual.

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

En el primer inicio se crea `sources.json` dentro de `--data-dir`. Se toma como base `peps/defaults/sources.json`. Edite ese archivo o pase `--config` con una configuración explícita. Un cambio posterior a los defaults del programa no reemplaza automáticamente la configuración guardada del operador.

Antes de habilitar una fuente, registre términos, finalidad, dominio exacto, tipo documental, intervalo, límite de tamaño y contacto identificable en el User-Agent. Las fuentes necesitan `enabled=true` y `terms_approved=true`. El flag documenta una decisión del operador; no verifica por sí mismo la licencia ni constituye autorización del editor.

GDELT y RSS localizan URLs. Cada editor necesita una fuente habilitada cuya lista `allowed_hosts` incluya exactamente su host; de otro modo se conserva sólo un resultado de descubrimiento. `gob.mx` o DOF, configurados como fuentes directas, no recorren por sí solos todo su sitio. El extractor actual procesa HTML/RSS/JSON; para PDF use un extractor local revisado y luego JSONL.

```powershell
python -m peps --data-dir .\runtime --online queue
python -m peps --data-dir .\runtime --online worker --workers 4 --seconds 240 --max-jobs 1000
python -m peps --data-dir .\runtime stats
```

Las peticiones usan HTTPS y direcciones IP públicas previamente validadas, con verificación de certificado y SNI del host original. Se admite `ca_bundle` para una CA aprobada. El transporte actual no implementa proxies HTTP CONNECT ni utiliza proxies obligatorios del entorno: una red que exija ese mecanismo necesita una adaptación administrada o transferencia offline. No desactive TLS para resolverlo.

## Ejecución continua y Programador de tareas

```powershell
python -m peps --data-dir .\runtime --online worker --daemon --daily --workers 4 --seconds 240 --max-jobs 1000
```

`--daemon` mantiene ciclos de trabajo; `--daily` encola una ventana UTC diaria. La deduplicación de claves impide crear repetidamente el mismo trabajo para entidad, fuente y ventana. Cierre con Ctrl+C o con los controles de la aplicación. La UI, un worker o varios procesos deben compartir exactamente el mismo directorio de datos para compartir cola y control por dominio.

El script de ejemplo registra una ejecución periódica que procesa la cola existente:

```powershell
.\scripts\register_worker.ps1 -Executable .\dist\PEPS-MX\PEPS-CLI.exe -DataDirectory .\runtime -Name PEPS-MX-Worker
```

El directorio de datos debe existir. Este script **no encola nuevas búsquedas diarias**: drena trabajos ya creados y evita instancias superpuestas de esa tarea. Para generación diaria use el modo continuo anterior o una tarea separada, autorizada por el operador, que ejecute `--online queue`. No deje simultáneamente servicios duplicados sin una razón operativa. Registrar la tarea altera el Programador de tareas de Windows y debe hacerse al decidir activar la automatización.

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

La API de respaldo SQLite produce una copia consistente del banco y cierra las conexiones al terminar. Una restauración debe realizarse con la aplicación y workers detenidos, en un directorio nuevo y con los archivos originales preservados. El respaldo de la base no incluye automáticamente configuración ni estado HTTP; consérvelos por separado conforme a su procedimiento. `audit` verifica la cadena de eventos, los hashes del cuerpo documental y las puntuaciones actuales; mantenga un hash o respaldo externo para detectar una eventual reescritura completa del banco.

SQLite admite múltiples lectores y un escritor a la vez; los índices y lotes ayudan, pero la aplicación no ofrece cómputo distribuido horizontal. El modelo de capacidad CLI permite estimar peticiones y días bajo supuestos; faltan latencias reales, saturación, bloqueos, descarga histórica y revisión humana. Valide el plazo requerido con una muestra representativa y cuotas verificadas de cada fuente antes de comprometer un SLA.
