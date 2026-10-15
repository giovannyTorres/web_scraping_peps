# Alta de nombres e importación guiada

El banco permite agregar un nombre individual o importar una lista con columnas propias. La carga y su evaluación se ejecutan en la aplicación local. Las columnas seleccionadas aportan pistas para distinguir entidades; el nombre, cargo, RFC o alias ingresados no se convierten en identidad verificada ni en evidencia de una conducta.

## Agregar un nombre

1. En **Banco de entidades**, pulse **Agregar nombre**. También puede entrar en **Agregar nombres y listas → Un nombre**.
2. Escriba el nombre completo o la razón social y seleccione **Persona**, **Empresa** u **Organización o institución**. El tipo es obligatorio: no se supone que cualquier nombre corresponde a una persona.
3. Si dispone de ellos, agregue alias separados por `|` y abra **Pistas de identidad opcionales** para indicar cargo, organización, ubicación, RFC, fecha de nacimiento o contexto.
4. Pulse **Guardar y consultar**. El formulario valida sus datos, los guarda y abre el expediente con **«¿Por qué obtuvo este score?»**. Si ya existe el mismo registro, ofrece consultar la entidad conservada.

Un nombre admite hasta 300 caracteres. Los alias admiten hasta 50 valores de 300 caracteres cada uno. Un RFC se comprueba sólo por su estructura, sin consulta fiscal ni confirmación de titularidad. Una edad o fecha debe ser válida; una fecha de nacimiento futura se rechaza.

## Importar una lista

### 1. Cargar y leer

En **Importar CSV** o **Agregar nombres y listas → Lista CSV o tabla**, seleccione un archivo CSV/TSV o pegue una tabla desde una hoja de cálculo. No necesita renombrar ni reordenar las columnas.

**Ajustes de lectura del archivo** permite elegir:

| Ajuste | Uso |
|---|---|
| Codificación | Detección de UTF-8, UTF-16 LE/BE y Windows-1252, o selección manual. Revise que los acentos se vean bien. |
| Separador | Detección o selección de coma, punto y coma, tabulación o `|`. |
| Filas iniciales que se omiten | De 0 a 100 líneas físicas para títulos o comentarios previos a la tabla. No use este ajuste para cortar una celda entre comillas con saltos de línea. |
| La primera fila contiene los títulos | Desmárquelo si la lista comienza directamente con datos. |

Pulse **Leer columnas**. La muestra expone los valores tal como se interpretaron. Se respetan delimitadores y saltos de línea dentro de celdas correctamente entrecomilladas. Una estructura de comillas inválida se rechaza para evitar una lectura engañosa.

Se admiten hasta 10,000 filas y 128 columnas. La petición HTTP completa tiene un límite de 16 MB, que incluye el JSON y sus escapes; un archivo cercano al límite puede requerir dividirse. Para cargas mayores use la CLI con el [formato técnico](OPERACION.md). El asistente no lee archivos `.xlsx`: expórtelos a CSV o copie y pegue la tabla.

### 2. Identificar qué contiene cada columna

Cada tarjeta muestra su posición, título y ejemplos. Elija el tipo de dato que contiene. Los títulos repetidos o vacíos se distinguen por posición. Las sugerencias sirven como punto de partida; debe revisarlas y marcar la casilla de revisión antes de validar.

| Dato | Tratamiento |
|---|---|
| Nombre completo o razón social | Nombre principal de la entidad. |
| Nombre(s), primer apellido, segundo apellido | Se unen en ese orden. Use estos componentes o el nombre completo, sin asignar ambos formatos a la vez. |
| Tipo de entidad | Persona, empresa u organización. Los valores propios, como `PF` o `SOC`, pueden identificarse en los selectores de equivalencias. |
| Identificador propio | ID estable para distinguir entidades y reconocerlas al volver a cargar. |
| Otros nombres o alias | Nombres alternativos. Puede asignar varias columnas a alias; seleccione `|` o `;` como separador dentro de una celda. |
| Cargo, organización, ubicación, país, RFC, edad, fecha, razón social adicional, nombres comerciales, siglas o contexto | Pistas de identidad que se guardan bajo `context`. |
| Omitir esta columna | El valor no se incorpora al banco ni a la bitácora de importación. |

El **tipo de dato de una columna** y el **tipo de entidad** son elecciones distintas: una columna puede contener un nombre, un RFC o una ciudad; la entidad puede ser persona, empresa u organización.

Si no hay columna de tipo, seleccione un tipo común para la lista. Ese valor también cubre celdas de tipo vacías. Un valor de tipo desconocido requiere identificar su equivalencia; no se convierte silenciosamente a persona. Las fechas pueden configurarse como `AAAA-MM-DD` o `DD/MM/AAAA`; elija el formato de su fuente para evitar interpretaciones equivocadas. En modo automático, una fecha ambigua como `01/02/2000` exige seleccionar expresamente día/mes/año.

Para distinguir homónimos conviene conservar IDs propios y pistas suficientes. Sin un ID, la aplicación genera uno a partir del nombre, tipo y contexto normalizados. Agregar o cambiar contexto puede generar otro ID; no use la igualdad del nombre como mecanismo de actualización. Dos registros idénticos sin información que los diferencie se deduplican, aunque en la realidad pudieran existir personas distintas con ese mismo nombre.

### 3. Validar y confirmar

Pulse **Validar y ver resumen**. El resumen incluye todas las filas, aunque la tabla muestra hasta 20 y el detalle de errores hasta 100. Revise las acciones por fila: nueva, actualización, existente, duplicada, omitida o con error.

- Las filas con distinto número de celdas muestran una advertencia. Las columnas adicionales aparecen en el mapeo y las celdas ausentes quedan vacías; revise su asignación.
- Los campos obligatorios faltantes, valores inválidos y combinaciones no admitidas impiden guardar el lote. Puede activar expresamente **Omitir las filas con errores y guardar únicamente las válidas**.
- Un mismo ID repetido con datos distintos bloquea la confirmación incluso al omitir errores. Resuelva la discrepancia o asigne IDs diferentes.
- Los duplicados idénticos se procesan una sola vez. Los nombres compartidos con otros IDs se mantienen separados y generan una advertencia de homónimo.
- Los IDs existentes se conservan por defecto. **Actualizar entidades existentes con los datos de esta lista** habilita cambios sobre los IDs coincidentes: conserva alias y contexto previos, une los alias y agrega o actualiza sólo pistas no vacías. Una celda vacía no borra un dato existente. Los cambios de nombre o tipo tienen una advertencia adicional. La vista previa muestra el registro combinado que se guardará.

Una pista modificada no hereda la verificación del valor anterior. Las verificaciones de datos que no cambian se conservan; cambiar el nombre o el tipo del mismo ID revoca todas las verificaciones heredadas. El asistente avisa de estos cambios y requiere revisar nuevamente las fuentes.

Marque la confirmación de revisión y pulse **Confirmar importación**. Si no hay entidades nuevas ni actualizaciones, no hay datos pendientes de guardar. La confirmación se vincula al contenido, mapeo, opciones y estado del banco. Cambiar un ajuste invalida la vista previa; si otro proceso modifica el banco antes de guardar, deberá validar de nuevo. La escritura del lote aprobado es atómica.

La aplicación bloquea envíos simultáneos desde el asistente y muestra el progreso hasta terminar. Tras guardar, prepara los scores de las entidades nuevas o actualizadas. Si el scoring falla después de guardarlas, informa que la evaluación quedó pendiente: las entidades ya están guardadas y no debe repetir la importación. Si se interrumpe la conexión local, consulte el estado o compruebe el banco antes de reintentar.

## Consultar el score y su evidencia

El comprobante ofrece **Ir al banco**, **Consultar primera entidad** y accesos a una muestra de hasta 20 entidades de la carga. El alta individual abre su expediente automáticamente. También puede buscar cualquier entidad en el banco.

**«¿Por qué obtuvo este score?»** conserva el cálculo real, sus factores y enlaces a las notas y fragmentos de evidencia existentes. La importación de nombres no activa búsquedas web, no descarga noticias y no inventa documentos. Si aún no hay evidencia admisible, el score puede ser cero: eso indica ausencia de factores activados en la información disponible, sin acreditar ausencia de riesgo. Para agregar notas locales use **Avanzado → Documentos sin conexión**; para adquirir fuentes autorizadas consulte [NOTICIAS.md](NOTICIAS.md).

Los alias y el contexto del asistente son pistas sin verificar. No pueden introducir anclas verificadas, aprobaciones o procedencia probatoria. Las evaluaciones y advertencias de identidad deben revisarse con sus fuentes. Consulte [EXPLICABILIDAD.md](EXPLICABILIDAD.md) para los límites del scoring y de los análisis auxiliares.

## Ejemplos ficticios

Los archivos de [examples/importacion](../examples/importacion) contienen sólo datos inventados:

- [columnas_irregulares.csv](../examples/importacion/columnas_irregulares.csv): separador `;`, una línea inicial de título, cabeceras repetidas/vacías y una fila con una celda adicional. Omita **1** línea inicial y mantenga la casilla de encabezados. Asigne `Referencia` a identificador, la primera columna `Nombre` a nombre completo, la segunda a alias, `Clase` a tipo y `Localidad` a ciudad. Identifique `PF` como persona y `SOC` como empresa; omita las columnas sin título y el comentario adicional. La fila sin nombre debe corregirse u omitirse expresamente.
- [nombres_separados.csv](../examples/importacion/nombres_separados.csv): nombres y apellidos separados, tipo, ID propio, ciudad y alias. Revise las sugerencias y seleccione un tipo común si decide omitir la columna de tipo.
- [sin_encabezados.tsv](../examples/importacion/sin_encabezados.tsv): ID, nombre y ciudad sin fila de títulos. Desmarque encabezados, asigne las tres columnas y seleccione **Persona** como tipo común.

Puede cargar estos ejemplos en un directorio de datos de prueba, separado de su banco habitual:

```powershell
python -m peps --data-dir .\runtime\prueba-importacion ui
```

## Privacidad, trazabilidad y mantenimiento

El navegador lee el archivo y envía el contenido a la API en `127.0.0.1`; el asistente no usa servicios de inferencia ni solicitudes externas. El servidor aplica token por sesión, comprobación de Host/Origin y una política de contenido que restringe las conexiones al mismo origen. Estos controles no sustituyen permisos de acceso al disco ni cifrado del equipo.

La previsualización no escribe entidades ni auditoría. La confirmación guarda los campos seleccionados normalizados y una entrada `guided_entities_imported` con versión del importador, hash del contenido, posiciones y campos del mapeo, opciones y conteos. La bitácora no conserva el CSV original, sus títulos ni las celdas ignoradas. Los registros de entidad sí contienen los datos personales seleccionados; un hash del archivo no los anonimiza. Proteja SQLite, exportaciones y respaldos conforme a [PRIVACIDAD_Y_FUENTES.md](PRIVACIDAD_Y_FUENTES.md).

El contenido cargado permanece en el formulario del navegador hasta limpiarlo o cerrar la página. **Limpiar lista** elimina ese contenido del formulario; no elimina entidades ya guardadas. La aplicación conserva temporalmente comprobantes de operaciones en memoria, hasta 30 minutos y con un máximo de 32; se pierden al cerrar el proceso. La auditoría persistente y las entidades permiten comprobar qué se incorporó después.

El asistente usa biblioteca estándar en el backend y JavaScript local en el frontend, sin dependencias de ejecución nuevas. El formato técnico CSV/JSONL y los documentos JSONL permanecen disponibles en **Avanzado** y en la CLI; conservan su contrato anterior y no incorporan el mapeo guiado.

## Contrato HTTP para integraciones locales

Las rutas requieren el mismo token de sesión y origen local que el resto de la aplicación. Una integración debe conservar exactamente el payload aprobado al confirmar:

| Ruta | Respuesta y finalidad |
|---|---|
| `POST /api/import-preview` | `200`: columnas, muestras, errores, advertencias, conteos, `ready` y `preview_hash`. Sin `mapping`, sólo inspecciona la lista; para validarla requiere un campo por índice de columna. No escribe. |
| `POST /api/import-confirm` | `202`: `operation_id`. Recibe el mismo payload con su `preview_hash`. La validación final y la escritura se ejecutan en segundo plano. |
| `GET /api/import-status?id=...` | Estado `queued`, `running`, `completed` o `error`, fase `saving`, `scoring` o `done`, y resultado o error. Un `202` inicial todavía no acredita que se haya guardado el lote. |

Ejemplo mínimo de payload CSV para previsualización con mapeo explícito:

```json
{
  "mode": "csv",
  "text": "Referencia;Titular;Localidad\nprueba-001;Persona Ficticia Lirio;Ciudad de Prueba\n",
  "delimiter": ";",
  "has_header": true,
  "skip_rows": 0,
  "mapping": ["id", "name", "city"],
  "default_kind": "person",
  "existing_policy": "skip",
  "skip_invalid": false
}
```

Para un alta individual se usa `mode: "single"` y `entity` con `name`, `kind`, `aliases` y un objeto opcional `context` de campos admitidos. El frontend también obtiene una vista previa antes de confirmar. El resultado público de la operación limita los IDs a 100 y la muestra a 20 entidades; el backend evalúa todos los IDs aceptados, sin usar esa muestra como límite del lote.

Las pruebas de regresión están en `tests/test_importing.py` y `tests/test_import_api.py`. La validación visual reproducible está en `scripts/test_import_ui.py`; requiere Playwright y Chromium sólo en desarrollo. Su [reporte](benchmarks/import_ui_validation.json) contiene comprobaciones y tiempos medidos con datos sintéticos, no una estimación de calidad sobre identidades reales.

## Validación y rendimiento medidos

La suite local ejecutó 329 pruebas correctamente, con 7 opcionales de NLP omitidas. El navegador pasó 26 comprobaciones de importación y 18 de regresión de explicabilidad: incluyen identificación de columnas, nombres separados, codificaciones, móvil, doble clic, respuestas obsoletas y ausencia de peticiones externas. Un wheel se construyó e instaló sin red ni dependencias nuevas en un entorno vacío; alta, recursos web, scoring y auditoría funcionaron desde ese paquete.

El [benchmark reproducible](benchmarks/import_performance.json) usa CSV ficticios con celdas entrecomilladas, notas multilínea y filas irregulares. Cada tamaño se ejecutó tres veces, en procesos y bancos vacíos independientes:

| Filas | Vista previa, mediana | Confirmación y guardado, mediana | Pico de memoria del proceso |
|---|---:|---:|---:|
| 1,000 | 28 ms | 42 ms | 19 MiB |
| 10,000 | 295 ms | 479 ms | 46 MiB |

Estos tiempos miden validación y persistencia SQLite en el entorno Linux documentado. Excluyen scoring, interfaz, modelos y adquisición de noticias; no predicen el rendimiento de un banco poblado. El reporte conserva muestras individuales, rangos, tiempo de CPU y almacenamiento. La CI repite las pruebas y ambas cargas en Windows y Linux.

```powershell
python -m unittest discover -s tests -v
python scripts/benchmark_import.py
# Sólo para desarrollo, con Playwright y Chromium instalados:
python scripts/test_import_ui.py
```
