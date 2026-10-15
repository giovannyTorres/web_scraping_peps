# Validación de recuperación y búsqueda dirigida 2.4.0

La versión corrige el paso de descarga que quedaba pausado con noticias guardadas
y trabajos pendientes, sin una salida hacia el análisis. La
[guía de recuperación](RECUPERACION_DESCARGA.md) muestra las acciones disponibles
y las capturas de escritorio y móvil. La información de las pruebas es ficticia.

## Casos comprobados

La regresión principal crea **235 referencias, cero cuerpos y 64 tareas
aplazadas** en SQLite. Usa el scheduler, los estados de trabajo, el servidor HTTP
local, la atribución y el cálculo reales. Sustituye únicamente el transporte a
fuentes externas. Comprueba el backoff sin alterar fechas ni intentos, la
reanudación sin duplicados, el cierre parcial explícito, los bloqueos durante
trabajo activo, el cálculo con la cola conservada y el replay del resultado.
También verifica el cambio de período, que exige planificar una búsqueda nueva.

Un segundo ensayo genera el banco y su recibo con un **wheel 2.3.1 instalado en
otro entorno Python**, y abre después la misma carpeta con 2.4.0. Sus doce
comprobaciones confirman que el catálogo ampliado conserva permisos, noticias,
cola y alcance anterior. El recibo heredado sigue sin período histórico cuando
ese dato nunca se guardó. Una fuente nueva deshabilitada no obliga a empezar la
búsqueda de nuevo para poder recuperar la descarga anterior.

La migración sólo actualiza la referencia al catálogo cuando las definiciones previas
son idénticas, las adiciones tienen todos sus permisos desactivados y coinciden
las revisiones registradas de fuentes, banco y corpus. Una URL modificada, un
cambio de permiso, una revisión posterior o datos distintos no se reinterpretan
como el alcance antiguo. Un lease activo sigue bloqueando el análisis aunque se
haya migrado el catálogo. Las evaluaciones históricas no se reescriben.

## Evidencia local

Resultados en Linux, Python 3.12 y Chromium. El
[comprobante JSON](benchmarks/news_recovery_2_4_0.json) conserva los conteos,
condiciones, comprobaciones y hashes del código validado.

| Comprobación | Resultado |
| --- | --- |
| Suite completa | 437 pruebas: 430 correctas y 7 opcionales de spaCy omitidas; 34.809 s. |
| Flujo guiado | 72 comprobaciones de navegador. |
| Descarga de 235 referencias y 64 pendientes | 39 comprobaciones de navegador, con scheduler y HTTP locales reales. |
| Importación | 32 comprobaciones de navegador. |
| Explicación y revisión | 19 comprobaciones de navegador. |
| Actualización desde wheel 2.3.1 | 12 comprobaciones de HTTP, persistencia y replay. |
| Wheel 2.4.0 instalado | 14 comandos y 16 recursos; sin dependencias de ejecución adicionales. |
| CI | Matriz Windows/Linux, navegador, NLP opcional y portable Windows en la misma solicitud de integración. |

Las pruebas de navegador no generan solicitudes externas ni errores de página.
El porcentaje se obtiene de unidades guardadas. Una espera por una fuente no
publica una ETA de procesamiento falsa. Los trabajos agotados pasan a fallo
explícito y los trabajadores siguen disponibles cuando un feed descubre nuevos
artículos. La duración máxima de sesión limita nuevas solicitudes; las activas
terminan conforme al timeout del transporte.

La tarea de portable en CI depende de la matriz unitaria del mismo commit y
utiliza `-SkipTests` para construir después de esa aprobación. Conserva siempre
las comprobaciones del ejecutable congelado. La construcción local ejecuta la
suite por defecto.

## Comprobación pública del adaptador histórico

La consulta RSS de Desinformémonos por la frase «López Obrador» y el año 2020
respondió HTTP 200 con diez entradas. Entre las fechas publicadas observadas
estuvieron el 31 y el 21 de diciembre de 2020. La ruta estaba permitida en su
`robots.txt`. Se verificó acceso técnico y metadatos; no se midió cobertura de las
personas del usuario ni se descargaron sus bancos.

El adaptador divide resultados saturados por año, mes y día. El presupuesto
reparte consultas entre las entidades antes de avanzar a ventanas más antiguas,
agrupa homónimos sin perder sus IDs y muestra entidades o ventanas sin planificar.
El plan y sus previews están acotados; los recibos no duplican miles de consultas
en cada lectura de progreso.

Google News RSS queda fuera de la automatización por su política robots
verificada. GDELT limita sus consultas a la ventana reciente documentada, con
frases separadas que evitan OR anidados. La fuente histórica nueva comienza
deshabilitada y no convierte resultados de búsqueda en cuerpos de artículos ni
en evidencia puntuable.

## Reproducir

```powershell
python -m unittest discover -s tests -v
python scripts/test_workflow_ui.py
python scripts/test_download_recovery_ui.py
python scripts/test_import_ui.py
python scripts/test_explainability_ui.py
```

Los recorridos de navegador requieren Playwright 1.62.0 y Chromium. Para repetir
la actualización genuina use `scripts/test_legacy_download_upgrade.py` con
`--legacy-python` apuntando a una instalación separada de 2.3.1; el script verifica
esa versión antes de crear la base. Los reportes y capturas se escriben en
`runtime/`. Los casos equivalentes de migración también forman parte de la suite
unitaria y no dependen del wheel antiguo para ejecutarse en CI.
