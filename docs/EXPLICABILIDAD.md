# Explicaciones verificables y evaluaciones históricas

PEPS MX 2.2 conserva el motor de prioridad documental como autoridad numérica.
Una explicación se construye con los mismos registros que calcula ese motor,
sin pedir a un modelo que invente razones a posteriori. Las correcciones
demostradas y acotadas del scoring están en [CORRECCIONES_SCORING.md](CORRECCIONES_SCORING.md).

## Uso e instalación

La aplicación base requiere Python 3.10 o posterior y ninguna biblioteca
adicional. Desde la raíz del repositorio:

```powershell
python -m peps --data-dir .\runtime ui
```

Seleccione una entidad y abra **¿Por qué obtuvo este score?**. Se muestran la
interpretación, ambas ramas del resultado, factores por evento y sus citas.
El botón de una cita abre el cuerpo guardado de esa evaluación y resalta el
fragmento exacto, incluso si la nota no está entre las primeras 200 del listado.
Los enlaces de procedencia permiten abrir la fuente por decisión del usuario.

La escala nominal es sobre 100, con máximo efectivo 95. Cero no acredita
ausencia de riesgo; 1–19 es prioridad baja, 20–49 media y 50–95 alta. Un 85
frente a 40 distingue prioridades de revisión, sin significar probabilidades.
La fórmula completa está en [SCORING_ACTUAL.md](SCORING_ACTUAL.md).

Las citas distinguen máximos seleccionados, evidencia de apoyo que no suma y
resoluciones aplicadas. Los empates se conservan. **El cambio al retirar un
evento no es una contribución sumable**: se vuelve a aplicar la fórmula real a
las dos ramas, con redondeo y techo. En un resultado saturado, un evento con
puntos puede tener cambio de retiro cero. Los fragmentos describen lo reportado
por la fuente; no verifican por sí mismos una acusación ni una sentencia.

El historial permite consultar una evaluación anterior sin mezclarla con las
revisiones actuales. La reproducción está en el desglose técnico. Desde CLI:

```powershell
python -m peps --data-dir .\runtime explain ID_ENTIDAD
python -m peps --data-dir .\runtime score-history ID_ENTIDAD
python -m peps --data-dir .\runtime explain ID_ENTIDAD --run-id ID_EVALUACION
python -m peps --data-dir .\runtime replay ID_EVALUACION
python -m peps --data-dir .\runtime audit
```

## Análisis contextual local

El predeterminado es un clasificador discriminativo compacto. Sus pesos JSON,
identificados por SHA-256, están en `peps/models`; no usa pickle, descargas,
cuentas, claves ni Hugging Face. Se entrenó con corpus sintético original
separado del challenge. La inferencia sólo requiere biblioteca estándar.
Su margen no es una probabilidad calibrada. Puede proponer acusado reportado,
víctima, denunciante, incidental, negación, resolución, relación indirecta o
información insuficiente. Un nombre coincidente nunca confirma identidad.

**Es una ayuda experimental de lectura.** En 72 casos sintéticos independientes
su macro F1 fue 0.432; sólo detectó 1 de 9 acusaciones con ese papel y se abstuvo
en 42 notas. No se puede usar su silencio para descartar evidencia ni interpretar
cero falsos positivos observados como garantía. Las métricas, denominadores,
recursos y errores están en [BENCHMARK_EXPLICABILIDAD.md](BENCHMARK_EXPLICABILIDAD.md).

Las noticias con identidad sin resolver, revisión rechazada o atribución
obsoleta se abstienen antes de inferir. Las menciones humanas seleccionadas
restringen las cláusulas permitidas; una cláusula con homónimos seleccionados y
no seleccionados se excluye. Los límites de texto/menciones y los errores se
registran como abstenciones, nunca como análisis completo.

Para añadir NER estadístico, dependencias, eventos y relaciones candidatas,
instale explícitamente la pila opcional descrita en [NLP_LOCAL.md](NLP_LOCAL.md).
Después edite el mismo `sources.json` del directorio de datos, conservando su
lista `sources`, para incluir:

```json
"context_analysis": {"mode": "enriched"}
```

Es una propiedad dentro del objeto JSON existente; no sustituya el archivo por
ese fragmento. Ejecute PEPS con el Python del entorno NLP y recalcule con `score`
o abra la entidad. `enriched` conserva el papel del discriminativo y añade las
extracciones de spaCy; los desacuerdos son advertencias. `advanced` permite
examinar spaCy solo y `hybrid` conserva el experimento de acuerdo estricto;
ninguno es el predeterminado. Si faltan pesos/dependencias se informa y se
abstiene; no se descarga nada durante la ejecución.

Laya se probó realmente con pesos ONNX CPU obtenidos de GitHub y comprobados,
antes de la prueba NLP. Su calidad y coste no justificaron hacerlo obligatorio.
Conserve el experimento reproducible de [INVESTIGACION_MODELOS.md](INVESTIGACION_MODELOS.md).

## Trazabilidad, retención y límites

Cada resultado incluye versión del scorer, parámetros completos y hash,
fecha de corte, exclusiones, multiplicadores de precisión completa, deduplicación,
máximos por rama y referencias a los documentos. La procedencia registra hashes
del código ejecutable, del clasificador y de su configuración. SQLite conserva
snapshots de entidad, revisión e identidad utilizados, sin duplicar cuerpos.

`replay` reproduce **scoring y traza**, usando los inputs y fecha guardados. Si
faltan versiones/código compatibles devuelve `unsupported`. El análisis auxiliar
archivado no se reinfiere y el resultado lo dice explícitamente. Consulte
[AUDITORIA_EVALUACIONES.md](AUDITORIA_EVALUACIONES.md) para el formato y verificación.

Las exportaciones JSONL incluyen evaluaciones históricas, entidades, documentos
y vínculos. Contienen datos sensibles: guárdelas junto con respaldos en disco
local con permisos y retención apropiados. Retirar un vínculo actual no borra
sus antecedentes. No existe purga automática ni cifrado propio; la cadena
SHA-256 detecta inconsistencias, pero una reescritura total del archivo requiere
un respaldo o hash externo para detectarse. El historial crece con evaluaciones
distintas; reevaluaciones idénticas reutilizan el mismo snapshot.

La base existente migra de forma aditiva al abrirla. Haga un respaldo antes de
actualizar y conserve la nueva versión para acceder a sus snapshots. Los scores
anteriores se actualizan al consultarlos; sus evaluaciones pasadas no se fabrican.

La aplicación no cambia las autorizaciones de recolección. En modo cerrado no
accede a fuentes. La inferencia contextual no tiene clientes de servicios
externos ni envía notas, entidades o resultados. Las únicas descargas de modelos
son herramientas de instalación explícitas, con URL y hash fijados; pueden
prepararse en otra computadora y copiarse para instalación sin red.

## Licencias y mantenimiento

Los pesos/corpus originales del clasificador están dedicados a CC0; los archivos
nuevos indicados por sus avisos usan MIT. Las licencias se conservan en
`docs/licenses/context-model`. Laya/MstyAI tienen avisos Apache-2.0 separados.
spaCy es MIT, pero los pesos españoles son GPLv3 y sus fuentes tienen avisos
propios: uso comercial no elimina esas obligaciones de distribución.
No se ha asignado una licencia al código preexistente del repositorio ni se
certifica una licencia comercial global que no estaba declarada.

No cambie pesos, reglas o umbrales sin versionarlos, actualizar sus hashes,
repetir regresiones y ejecutar una evaluación independiente. No ajuste sobre
el challenge publicado. Para validar calidad operativa se necesita un corpus
permitido de notas reales, anotación independiente y revisión de desacuerdos;
este trabajo no inventa esos datos ni afirma que esa validación ya exista.
