# Banco de noticias: adquirir, atribuir y revisar

El módulo tiene dos fases independientes. `NewsAcquirer.acquire()` obtiene
metadatos y cuerpos autorizados y conserva su procedencia.
`NewsAcquirer.attribute()` trabaja con el banco local, identifica menciones y
actualiza la prioridad de revisión con las exclusiones correspondientes.
La segunda fase funciona sin acceso a Internet.

## 1. Obtener información con una cola reanudable

Importe un padrón con identificadores estables antes de adquirir noticias:

```powershell
python -m peps --data-dir runtime import-entities work/padron.json
```

El archivo puede ser JSON/JSONL o CSV compatible con el importador. Para distinguir
homónimos es necesario conservar distintos `id`, aunque compartan exactamente el
mismo nombre. `aliases` acepta textos; los vínculos comerciales revisados y su
procedencia se guardan en `context.verified_aliases`.

Las fuentes de noticias tienen configuración propia, separada del recolector
anterior. La aplicación crea `runtime/news_sources.json` con las fuentes
desactivadas. También se puede utilizar una copia local explícita:

```powershell
python -m peps --data-dir runtime news-plan --news-config work/fuentes_revisadas.json
python -m peps --data-dir runtime news-queue --news-config work/fuentes_revisadas.json
python -m peps --data-dir runtime --online news-acquire --news-config work/fuentes_revisadas.json --workers 4 --seconds 300 --max-tasks 1000 --no-queue
```

`--news-config` va después del subcomando; `--data-dir` y `--online` van antes.
`news-queue` sólo organiza tareas y no descarga. `news-acquire` también encola por
defecto; `--no-queue` permite continuar únicamente la cola existente. El límite
`--max-tasks` cuenta tareas de descubrimiento y artículos, no personas investigadas.
Al alcanzar tiempo o cantidad se conserva el trabajo pendiente para otra ejecución.
`--workers` distribuye tareas entre hosts y mantiene la cortesía compartida por
dominio. Aumentarlo no reduce los intervalos que exige cada origen.

Para RSS se encola una tarea por feed y ventana diaria. No se solicitan 40 mil
copias del mismo feed. Para GDELT se agrupan nombres y alias en consultas OR,
incluyendo en cada lote los IDs de los homónimos. Las ventanas saturadas se
subdividen; el mínimo todavía saturado queda como truncado. La configuración no
garantiza que una fuente tenga documentos de cada entidad. El catálogo usa
`source_country=mexico`, que filtra el país del editor; puede perder prensa
extranjera sobre entidades mexicanas. Deje ese filtro vacío cuando el alcance
autorizado incluya medios extranjeros en español.

Para una ventana histórica explícita, las fechas son UTC en
`YYYYMMDDHHMMSS`, dentro del período que admita la fuente:

```powershell
python -m peps --data-dir runtime news-queue --news-config work/fuentes_revisadas.json --start 20261001000000 --end 20261014000000
```

No se establece una fecha de terminación por el mero hecho de encolar un nombre.
RSS ofrece su ventana actual; los índices externos tienen límites de cobertura,
de cantidad y de servicio. Véase [fuentes y benchmark](FUENTES_NOTICIAS.md).

### Permisos y alcance de descarga

Para leer una fuente se requieren `enabled=true` y `terms_approved=true`, después
de registrar términos, alcance, responsable y fecha de revisión. Para guardar su
cuerpo editorial se requieren además `download_articles=true` y
`article_terms_approved=true`. Los hosts de cada editor se enumeran de forma
exacta; descubrir una URL mediante otro servicio no habilita al editor.

El modo predeterminado utiliza menciones en título/resumen como prefiltro de
descarga. Puede perder noticias cuyos titulares no nombren al sujeto. Cuando
esté autorizado guardar todas las notas de una fuente, `--all-articles` omite ese
prefiltro. El permiso sobre cuerpos y los límites de red siguen siendo necesarios:

```powershell
python -m peps --data-dir runtime --online news-acquire --news-config work/fuentes_revisadas.json --workers 4 --seconds 300 --max-tasks 1000 --all-articles
```

El fetcher consulta robots, verifica HTTPS y hosts, limita bytes y tiempos,
descomprime con límites y reutiliza conexiones verificadas. Caché, ETag y
Last-Modified reducen transferencias cuando el servidor lo permite. 429/503
aplazan la tarea; 403/451 o desafíos de acceso abren un circuito para revisión.
No se evaden CAPTCHA, paywalls ni bloqueos mediante identidades o proxies nuevos.
El permiso de robots no sustituye una licencia de uso.

### Duplicados y procedencia

La URL canónica elimina fragmentos y parámetros de seguimiento conocidos; conserva
parámetros funcionales que pueden identificar noticias distintas. Las observaciones
registran cada fuente de descubrimiento, URL original, URL del feed o consulta,
fecha y hash, junto con una instantánea pública de política de la fuente,
términos y alcance autorizado. Los cuerpos conservan esa procedencia por separado.
Una misma URL no vuelve a generar una nota independiente.

En un mismo editor, cuerpos equivalentes comparten documento mediante una huella
normalizada. Entre editores distintos se conservan las notas, incluso cuando
comparten texto; el grupo de contenido impide presentarlas automáticamente como
corroboraciones independientes. La agrupación por titulares es un tema candidato,
no una prueba de que se trate del mismo hecho. El scoring conserva editores y
grupos de contenido y evita sumar repetidamente el mismo evento.

Los cuerpos autorizados conservan texto, hash y una copia comprimida del HTML
original en `runtime/news_snapshots/`. La extracción se centra en `article` o
`main` y excluye navegación y otros elementos ajenos. La huella del HTML permite
revisar qué recibió el sistema y qué extrajo; no demuestra veracidad de la noticia.

## 2. Identificar y atribuir de forma local

```powershell
python -m peps --data-dir runtime news-attribute --limit 1000
python -m peps --data-dir runtime news-stats
python -m peps --data-dir runtime audit
```

Repita `news-attribute` para continuar el banco. `--force` vuelve a analizar las
notas aun cuando su firma de padrón ya esté actualizada. Cambiar el padrón invalida
resoluciones y revisiones anteriores para scoring; reatribuir actualiza la
resolución, pero la revisión humana debe renovarse. El hash de reglas también
provoca reatribución. Esto evita mantener una decisión anterior a un nuevo
homónimo o a una corrección de reglas.

El resolver indexa el padrón una vez y busca secuencias de tokens; no compara
cada documento con cada una de las 40 mil entidades. Respeta los offsets del
cuerpo original, acentos y `ñ`; normaliza puntuación de siglas y sufijos societarios
sin fusionar los IDs de empresas o personas distintas. La similitud de nombre o
un alias comercial ingresado sin verificar sólo produce candidatos.

Cada resolución conserva estado, menciones, anclas, procedencia declarada de los
atributos, competidores, firma del registro, hash del cuerpo y versión de reglas:

| Estado | Uso |
|---|---|
| `candidate` | Existe coincidencia, falta evidencia de identidad. |
| `ambiguous` | Hay registros competidores sin una atribución exclusiva. |
| `rejected` | Un identificador verificado contradice la mención. |
| `matched` | Nombre/alias del cuerpo y anclas revisadas concuerdan localmente. Sigue siendo una regla revisable. |

Un dato opcional de entrada no queda verificado por tener formato correcto. Una
ancla requiere verificación explícita y procedencia, y debe corresponder al sujeto
de esa mención. Por ejemplo, un RFC de un proveedor, la edad de un aniversario o
el cargo de una testigo no identifican a la persona que los menciona. Los cargos y
alias fechados se comprueban contra la fecha de publicación; no se usa la fecha
de descarga para inventar vigencia.

Ejemplo de estructura de una entidad ficticia, con procedencia ilustrativa que
debe reemplazarse por la evidencia real revisada:

```json
{
  "id": "persona-ejemplo-1",
  "name": "Persona Ficticia Ejemplar",
  "kind": "person",
  "aliases": [],
  "context": {
    "role": "contralora",
    "state": "Durango",
    "verified_fields": {
      "role": {"verified": true, "source": "REEMPLAZAR: documento y revisión de cargo"},
      "state": {"verified": true, "source": "REEMPLAZAR: documento y revisión territorial"}
    }
  }
}
```

Para empresas, `context.verified_aliases` contiene objetos con `name`, `verified`,
`source` y, cuando corresponda, `valid_from`/`valid_to`. Una adquisición, una marca
o un acrónimo compartido no autorizan a fusionar razones sociales. La procedencia
declarada es auditable; el programa no certifica por sí mismo el documento citado.

### Qué llega al scoring

Un título/resumen de descubrimiento no puntúa, incluso con revisión humana: hace
falta el cuerpo de la fuente. Candidatos, identidades ambiguas o rechazadas tampoco
puntúan sin revisión suficiente. `matched` permite prioridad provisional sólo en
las menciones del cuerpo que sus anclas sostienen; la prioridad validada requiere
confirmación humana con revisor y motivo.

Cuando hay varias menciones homónimas, el revisor debe seleccionar las ocurrencias
correspondientes mediante `identity_spans` (`start`/`end` del cuerpo original).
Una confirmación general del documento no convierte todas sus menciones en la
misma persona. El scoring excluye hallazgos cuyo sujeto esté fuera de las
ocurrencias corroboradas o seleccionadas. La revisión queda ligada al contenido y
al registro vigentes; una firma o hash diferente obliga a revisar nuevamente.

La importación manual de noticias con `import-documents` usa el mismo resolver y
registra sus resoluciones. Un `entity_id` explícito sólo dirige el vínculo; no
confirma identidad. Los adaptadores de documentos oficiales y `legacy` siguen
separados. Tras modificar el padrón o las reglas, ejecute atribución y renueve las
revisiones que hayan quedado obsoletas.

`news-stats` separa notas, observaciones, estados de descarga, alcance de contenido,
editores, grupos de contenido y estados de identidad. Esos contadores describen
el banco; no son una certificación de ausencia de riesgo en las identidades sin
noticias. El historial de auditoría permite inspeccionar operaciones y hashes.

## Capacidad y validación

```powershell
python -m peps --data-dir runtime news-plan --articles-per-entity 20 --unique-url-ratio 0.6 --target-days 7
```

El plan calcula una proyección optimista bajo supuestos explícitos: distribución
equilibrada entre hosts, artículos por identidad y proporción de URLs únicas.
No incluye toda la partición de consultas saturadas, bloqueos, revisión humana ni
datos inexistentes. `model_within_target=true` sólo significa que ese escenario
aritmético cabe en el objetivo; `web_coverage_demonstrated` permanece falso.
Un plazo inferior a una semana debe demostrarse con fuentes autorizadas,
volumen histórico definido, tasas sostenidas y cobertura de identidad medida.

Para automatizar ambas fases, use la configuración revisada guardada en el mismo
directorio de datos y el worker diario:

```powershell
python -m peps --data-dir runtime --online worker --daemon --daily --workers 4 --seconds 240 --max-jobs 1000
```

Ese modo encola noticias por ventana UTC diaria y ejecuta atribución local después
de cada lote. El límite del lote no acredita que la investigación de una entidad
esté completa. Las instrucciones del Programador de tareas y los respaldos de
configuración, estado HTTP y snapshots están en [OPERACION.md](OPERACION.md).

La auditoría independiente añade 20 desafíos sintéticos que no proceden del
fixture principal y ocho pruebas de integración entre atribución y scoring.
La primera iteración detectó ocho fallos, siete de falsa atribución positiva. Se
corrigieron las reglas de adjunción y los mismos 20 casos pasan; las ocho
integraciones también pasan. Se conservan los resultados iniciales en la auditoría
para hacer visible la iteración. Esto no estima precisión sobre prensa real.

```powershell
python -m unittest discover -s tests -p test_identity_holdout.py -v
python tests/test_identity_holdout.py --audit work/auditoria_identidad_independiente.json
```
