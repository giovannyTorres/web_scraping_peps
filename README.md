# WebScope 2.9.4

Banco local de noticias y documentos públicos sobre personas, empresas y organizaciones mexicanas. Separa la adquisición de fuentes autorizadas de la atribución local de identidad, conserva procedencia y versiones, y ordena la revisión documental con una puntuación explicable.

**WebScope** es el nuevo nombre de la herramienta de web scraping y análisis documental antes llamada PEPS MX. La interfaz combina azules marino y principal con acentos aqua, Lato para texto y controles y Source Serif 4 para títulos. Las fuentes se distribuyen con sus licencias y se cargan desde el propio paquete, sin servicios de tipografía externos. Tras instalar el wheel puede usar `webscope` o el comando compatible `peps`; el módulo Python y las rutas de datos existentes se conservan.

La interfaz organiza el trabajo en cinco pasos: **cargar lista → preparar fuentes → descargar noticias → analizar y calcular → revisar resultados**. La descarga muestra nombres consultados, candidatos y entidades con texto de la lista actual, con las esperas de fuentes por separado. Puede pausar y analizar los textos pertinentes conservados; el servidor espera el cierre real de los trabajadores y conserva la cobertura pendiente. Con cero textos pertinentes, la pantalla guía hacia la búsqueda por nombre. Consulte [FLUJO_GUIADO.md](docs/FLUJO_GUIADO.md).

La versión 2.9.4 incorpora un [backtest público de 60 notarios](docs/benchmarks/notary_backtest_2026_10_16/README.md), con referencias congeladas, revisión documental independiente y métricas separadas de volumen, precisión, recall y abstención. Corrige los cruces por alias de uno o dos apellidos, los reintentos de páginas sin cuerpo útil y el trabajo repetido de la cola durante las pausas de un dominio. Añade fuentes regionales opcionales y soporte de WordPress en subcarpetas. La navegación respeta la sección que el usuario eligió al terminar el análisis. El informe conserva los fallos de cobertura y del clasificador auxiliar; esta prueba no certifica ausencia de incidentes ni capacidad equivalente a un buscador general.

La versión 2.9.3 corrige el recorrido de índices generales que retrasaba las consultas por nombre y el descarte de artículos cuyo nombre sólo aparece en el cuerpo. Usa la búsqueda del editor como pista acotada y comprueba el texto original antes de contarlo como cobertura. La pantalla reúne el estado de descarga en un panel, identifica la fuente y el próximo reintento y separa el archivo histórico de esta lista. La [validación con notarios](docs/VALIDACION_NOTARIOS_2_9_3.md) documenta la reproducción real, las pruebas de interfaz y los límites de tiempo de los proveedores.

La versión 2.9.2 reparte el primer barrido nominal entre proveedores antes de agotar el presupuesto en uno solo y aplica el contexto configurado a consultas secundarias. Incluye una [lista de 300 personas públicas reales](examples/PERSONAS_PUBLICAS_300.md), con procedencia, y una [validación de red reproducible](docs/VALIDACION_REAL_300_2_9_2.md) que distingue consultas planificadas, realizadas y completadas, cuerpos originales e identidad pendiente.

La versión 2.9.1 corrige fallos reproducidos bajo estrés: colas grandes, tareas antiguas corruptas, interrupciones durante la preparación, escrituras concurrentes de métricas y respuestas web malformadas. Conserva las pausas de los proveedores, recupera menciones históricas y evita que una preparación incompleta habilite el scoring. El método, las cargas y los límites están en [VALIDACION_ESTRES_2_9_1.md](docs/VALIDACION_ESTRES_2_9_1.md).

La versión 2.9 añade control de caché (**reutilizar, comprobar cambios o sólo local**), evita reatribuir cuerpos idénticos, conserva paginación al reutilizar respuestas y reduce el trabajo SQLite repetido al rescatar candidatos. Incorpora descubrimiento web sin claves mediante fuentes públicas y enlaces de búsqueda social; las capacidades comprobadas y sus límites están en [DESCUBRIMIENTO_PUBLICO.md](docs/DESCUBRIMIENTO_PUBLICO.md) y [FUENTES_SOCIALES_PUBLICAS.md](docs/FUENTES_SOCIALES_PUBLICAS.md). Los resultados de buscadores y publicaciones sociales sirven para localizar noticias originales y permanecen como metadatos de descubrimiento. La [validación 2.9.0](docs/VALIDACION_2_9_0.md) distingue pruebas reales, reproducción de capturas y rendimiento local masivo.

La versión 2.8 corrige fallos encontrados con **Peña Nieto y siete empresas reales**: búsquedas por nombres comerciales, selección de textos para revisión, extracción de artículos frente a tarjetas relacionadas, separación de párrafos y reanudación mediante proxy. Conserva los candidatos neutros, negaciones y absoluciones; la prioridad de descarga no agrega puntos. La [validación pública 2.8.0](docs/VALIDACION_2_8_0.md) documenta las peticiones reales, tiempos, correcciones y cobertura pendiente, separándolos del benchmark sintético de 300 entidades.

La versión 2.4 corrige las descargas que quedaban en pausa sin una salida: distingue tareas listas, aplazadas y agotadas, muestra el próximo intento permitido y reanuda sin crear nuevas consultas. También permite **cerrar la descarga con cobertura parcial** cuando ya no hay ningún trabajador activo. Conserva las noticias y la cola pendiente; la evaluación guarda esa limitación. La [guía de recuperación y búsqueda histórica](docs/RECUPERACION_DESCARGA.md) explica cómo retomar un banco existente y preparar consultas dirigidas por nombre y fechas. La [validación 2.4.0](docs/VALIDACION_2_4_0.md) incluye el caso de 235 referencias y 64 pendientes y la actualización desde un wheel 2.3.1 auténtico.

La versión 2.7 incorpora **Índice compartido (listas grandes)**: lee hasta 100 títulos y extractos de WordPress por página o mapas XML, contrasta cada página con toda la lista y reutiliza una caché persistente antes de descargar cuerpos pertinentes. Distribuye las primeras páginas entre años, conserva la paginación y distingue nombres contrastados localmente de búsquedas dirigidas y artículos leídos. Los lotes GDELT también se usan en modo ampliado. No requiere claves ni dependencias adicionales. La [investigación de crawlers y fuentes gratuitas](docs/CRAWLERS_Y_DESCUBRIMIENTO.md) compara Scrapy, Crawlee, Colly, SearXNG y GDELT y documenta las pruebas públicas; la [validación 2.7.0](docs/VALIDACION_2_7_0.md) separa las mediciones públicas de los benchmarks sintéticos. El índice compartido es una vía complementaria: para listas nominales de notarios, empiece por la búsqueda dirigida. Un barrido parcial no acredita ausencia de información.

La versión 2.6 prioriza la adquisición para listas grandes: filtra noticias por menciones de las entidades antes de conservarlas, agrupa consultas GDELT, limita los cuerpos por entidad y permite sesiones rápidas de 120 segundos o ampliadas hasta dos horas. El seguimiento muestra entidades consultadas, candidatas y con texto, separadas de las referencias brutas. La falta de texto o identidad corroborada se presenta como evaluación insuficiente, conservando el cálculo y la traza originales. Consulte [ADQUISICION_MASIVA.md](docs/ADQUISICION_MASIVA.md) para elegir el presupuesto y [VALIDACION_2_6_0.md](docs/VALIDACION_2_6_0.md) para las pruebas con 300 entidades.

La versión 2.5 reduce la transferencia y el trabajo repetido del navegador: consulta sólo los datos de la vista abierta, usa resúmenes para las listas y conserva la explicación completa bajo demanda. El seguimiento muestra las fases y tareas, el tiempo transcurrido y las estimaciones disponibles a partir de muestras reales; las esperas de una fuente se muestran por separado. También mejora la legibilidad, las acciones de cada paso y la presentación en móvil. La [validación de rendimiento y usabilidad 2.5.0](docs/VALIDACION_2_5_0.md) documenta las mediciones y sus límites.

El historial y la explicación de evaluaciones se conservan. La comparación de ramas y el tratamiento de los notebooks originales están en [CONSOLIDACION.md](docs/CONSOLIDACION.md); las comprobaciones anteriores permanecen en [VALIDACION_2_2_1.md](docs/VALIDACION_2_2_1.md).

La condición PEP, un parentesco, un cargo o un contrato público no son conductas adversas por sí mismos. Los resultados son candidatos de revisión: no acreditan identidad, culpabilidad ni ausencia de riesgos. El sistema no debe decidir automáticamente la contratación, la admisión bancaria ni el acceso a servicios.

## Inicio en Windows

El paquete portable usa la carpeta completa `WebScope`, con `WebScope.exe`, `WebScope-CLI.exe` y la carpeta auxiliar `_internal`. Abra `WebScope.exe` con doble clic: inicia un servidor en `127.0.0.1` y abre el navegador predeterminado sin mostrar una consola. No requiere instalar Python en la máquina de destino. Mantenga juntos ambos ejecutables y `_internal`; distribuir únicamente un `.exe` no funciona. El paquete no es un instalador ni un servicio de Windows.

Desde el código fuente se requiere Python 3.10 o posterior. La instalación base utiliza únicamente la biblioteca estándar en ejecución: no necesita Hugging Face, claves de un modelo ni servicios de IA.

La sección **«¿Por qué obtuvo este score?»** conecta cada resultado con sus eventos, multiplicadores reales, notas originales y fragmentos resaltados. Conserva evaluaciones históricas en la misma SQLite y permite reproducir el cálculo. El clasificador contextual incluido (1.43 MB, CPU, sin dependencias adicionales) sólo propone papeles auxiliares y puede abstenerse: no agrega ni elimina puntos. NER y dependencias de spaCy son opcionales, con instalación explícita desde GitHub. Consulte [EXPLICABILIDAD.md](docs/EXPLICABILIDAD.md) para usarlo y [el benchmark comparativo](docs/BENCHMARK_EXPLICABILIDAD.md) para sus limitaciones medidas, incluida la baja cobertura de acusaciones. La investigación y prueba real de Laya se completaron antes de spaCy.

```powershell
python -m peps --data-dir .\runtime ui
```

Ejecute los comandos desde la raíz del proyecto. En el bundle sustituya `python -m peps` por la ruta a `WebScope-CLI.exe`, que conserva salida y errores de consola. Desde la carpeta portable, para diagnóstico de apertura use `.\WebScope-CLI.exe --data-dir .\runtime ui --no-browser`; muestra la URL local. `WebScope.exe` está destinado al doble clic. Para conservar los datos en una ubicación explícita, utilice siempre el mismo `--data-dir`; sin esa opción, Windows conserva `%LOCALAPPDATA%\PEPS-MX` para abrir el banco existente. El paquete Python `peps-mx` y el módulo `peps` mantienen su nombre por compatibilidad; la instalación también ofrece el comando `webscope`.

## Flujo de trabajo

En la interfaz, **Guardar y continuar** da de alta una persona, empresa u organización y conduce a la preparación de fuentes. **Importar CSV** acepta columnas en cualquier orden, títulos repetidos o vacíos y tablas pegadas: muestra ejemplos y pide identificar el contenido de cada columna antes de guardar. Puede separar nombres y apellidos, indicar tipos propios, omitir columnas y revisar errores, duplicados y posibles actualizaciones. Consulte [la guía de importación](docs/IMPORTACION_GUIADA.md), con ejemplos ficticios listos para cargar. Estas altas no activan búsquedas ni calculan scores.

En el paso 2 elija fuentes autorizadas, el período, el enfoque y el uso de caché; **Guardar e iniciar** prepara la búsqueda en una acción. Puede elegir **Analizar al terminar** o conservar sólo la descarga. El paso 3 muestra la actividad real y permite inspeccionar las tareas, pausar y reanudar. **Analizar lo disponible** registra explícitamente la cobertura parcial y pasa al cálculo cuando no hay trabajadores activos. El paso 4 atribuye las noticias y calcula el banco completo con barras de avance. El paso 5 permite consultar las explicaciones, registrar revisiones y exportar. El modo **documentos locales** conserva el análisis sin adquisición.

1. Importe entidades con IDs estables, nombres, tipo, alias y contexto. Los homónimos conservan IDs distintos; los atributos son pistas hasta verificarse con procedencia.
2. Adquiera noticias: un trabajo por RSS y ventana, consultas por lotes cuando la fuente lo permita, URLs deduplicadas y cuerpos sólo con permiso separado.
3. Atribuya localmente las menciones con el padrón. Un nombre exacto no resuelve identidad; los candidatos ambiguos conservan su estado.
4. Revise cuerpo, procedencia, citas y estado procesal. Confirme o rechace con revisor y motivo, seleccionando las menciones cuando haya varios homónimos. Conserve exportaciones y respaldos.

```powershell
python -m peps --data-dir .\runtime import-entities .\entidades.csv
python -m peps --data-dir .\runtime news-plan
python -m peps --data-dir .\runtime --online news-acquire --workers 4 --seconds 300 --max-tasks 1000
python -m peps --data-dir .\runtime news-attribute --limit 1000
python -m peps --data-dir .\runtime news-stats
python -m peps --data-dir .\runtime audit
python -m peps --data-dir .\runtime export .\exportaciones\banco.jsonl
```

Para consultar o auditar una evaluación desde consola:

```powershell
python -m peps --data-dir .\runtime explain ID_ENTIDAD
python -m peps --data-dir .\runtime score-history ID_ENTIDAD
python -m peps --data-dir .\runtime replay ID_EVALUACION
```

`replay` comprueba el scoring y su traza cuando código y parámetros son compatibles; los análisis auxiliares se conservan archivados y no se reinfieren. La interfaz sólo consulta resultados guardados después del paso de análisis; abrir una entidad no inicia un cálculo. El comando CLI `explain` conserva su actualización local explícita, sin consultar internet. Las versiones anteriores a 2.2 no tenían snapshots completos recuperables: se conserva su bitácora y se crea una evaluación nueva; no se reconstruye un historial que nunca se guardó.

Las fuentes comienzan desactivadas en `runtime/news_sources.json`. `enabled` y `terms_approved` habilitan acceso; `download_articles` y `article_terms_approved` autorizan por separado guardar cuerpos. Un RSS gratuito no concede permiso de uso comercial. El catálogo documenta restricciones y mantiene todos esos permisos desactivados. Lea [NOTICIAS.md](docs/NOTICIAS.md) y [FUENTES_NOTICIAS.md](docs/FUENTES_NOTICIAS.md) antes de configurar fuentes.

El formato técnico de entidades, disponible en la CLI y en las opciones avanzadas de la interfaz, admite `kind=person`, `company` u `organization`; los alias se separan con `|` en CSV y `context` contiene JSON. El asistente visual permite mapear otros encabezados a estos datos sin editar el archivo. La importación manual autorizada sigue disponible con `import-documents documentos.jsonl`. Para cargas grandes utilice la CLI con el formato técnico: la interfaz limita cada petición de importación a 16 MB. Los formatos y el modo cerrado están en [OPERACION.md](docs/OPERACION.md).

## Implementación y alcance

- SQLite en modo WAL almacena entidades, documentos, menciones, puntuaciones, trabajos y una bitácora encadenada SHA-256. La cola tiene leases, reintentos acotados y estados de bloqueo. La verificación comprueba cadena, contenido documental y hashes de las puntuaciones actuales; no reemplaza una firma externa ni un control de acceso al disco.
- Un índice de tokens evita cotejar cada texto contra todos los nombres. La identidad conserva competidores, anclas verificadas con procedencia, offsets, firma de padrón y hash del cuerpo. Nombres, RFC o alias ingresados sin verificación son pistas. Las reglas en español no resuelven todas las correferencias, ironías ni variantes desconocidas.
- El scoring mantiene contribuciones, exclusiones y parámetros versionados. Sus pesos son heurísticos y no tienen validación predictiva de conducta futura. Una prioridad cero significa que la evidencia disponible no activó una regla admisible; no significa que una persona esté libre de información adversa.
- La recolección aplica dominios permitidos, TLS, robots, límites de tamaño, gzip/deflate acotados, caché condicional, conexiones verificadas reutilizables, pausas por dominio y circuitos persistentes ante restricciones. Los extractos de buscadores se conservan como descubrimiento y no puntúan. Una misma URL se conserva una vez; notas de distintos medios permanecen separadas, con grupos de contenido para no confundir sindicación con corroboración independiente.
- Los adaptadores locales de `peps.official` leen XML clásico OFAC, XML ONU y CSV SAT 69-B. Conservan naturaleza administrativa, fecha de la copia y estados fiscales. No leen todos los formatos de todas las autoridades ni convierten una sanción en condena penal.

`import-official` valida una copia local completa antes de incorporarla; admite `--source ofac`, `un` o `sat69b`, y exige una URL oficial de procedencia. Los ejemplos están en [OPERACION.md](docs/OPERACION.md).

La arquitectura es de una estación local. Use disco local para SQLite; no coloque el banco en una carpeta de red. La exportación JSONL transmite registros de entidad, documento y evidencia a disco, sin reunir todo el banco en el navegador. PostgreSQL, almacenamiento externo de objetos y workers distribuidos son posibles evoluciones, todavía no implementadas.

## Automatización y capacidad

Las fuentes están deshabilitadas inicialmente. Una vez revisadas y configuradas en el directorio de datos, el modo continuo genera una ventana UTC diaria, encola adquisición de noticias, procesa la cola y ejecuta atribución local después del lote:

```powershell
python -m peps --data-dir .\runtime --online worker --daemon --daily --workers 4 --seconds 240 --max-jobs 1000
```

Los workers comparten límites persistidos por dominio; más hilos no autorizan más peticiones a una fuente. Consulte [OPERACION.md](docs/OPERACION.md) antes de activar el Programador de tareas.

```powershell
python -m peps --data-dir .\runtime news-plan --articles-per-entity 20 --unique-url-ratio 0.6 --target-days 7
python -m unittest discover -s tests
```

`news-plan` muestra un escenario optimista con supuestos, no una medición web ni una promesa de procesar 40 mil personas en una semana. `capacity` conserva el modelo anterior para comparación. RSS cubre su ventana disponible y GDELT puede truncar resultados o aplazar consultas. El filtro `source_country=mexico` selecciona país del medio: para investigar entidades mexicanas en prensa extranjera, quite ese filtro según el alcance autorizado.

La medición real verificó seis RSS en seis hosts: 224 URLs en el lote principal y 40 en el suplemento; sólo se obtuvieron dos cuerpos de una ONG. El lote principal tomó 16.813 s con caché de respuestas fría y 0.266 s en caliente sin HTTP. GDELT devolvió 429 y dos accesos editoriales, 403; se conservaron las restricciones. La identidad superó 120 casos sintéticos principales y 20 independientes tras corregir ocho fallos; esto no estima precisión sobre prensa real. Los detalles y la separación entre red real, carga simulada e identidad local están en [VALIDACION.md](docs/VALIDACION.md).

Los notebooks originales y sus salidas se conservan como material histórico. Sus XML locales tienen fechas de enero y mayo de 2026; no representan listas actualizadas al momento de ejecución. Importe las salidas antiguas con `import-legacy` para mantener esa distinción.

## Construcción del ejecutable

PyInstaller es una dependencia de construcción, no de ejecución. Instálelo en un entorno de desarrollo permitido, o use un wheelhouse aprobado para una construcción sin internet.

```powershell
python -m pip install -r requirements-build.lock.txt
.\scripts\build_windows.ps1 -Python python -Destination dist
```

El script ejecuta las pruebas y construye las variantes gráfica y de consola, colocando `WebScope-CLI.exe` junto a `WebScope.exe` para compartir `_internal`. Comprueba los recursos compartidos y prueba el paquete final desde una carpeta ajena al repositorio: importación, cálculo, explicación, historial, replay, auditoría, exportación, respaldo y servidor local, incluidas las fuentes tipográficas locales. Distribuya `dist\WebScope` completo. El entorno de destino debe permitir ejecución de aplicaciones locales, apertura de navegador y escritura en el directorio de datos; políticas corporativas pueden impedirlo.

La CI verifica además el wheel instalado en un entorno limpio, sin dependencias de ejecución, en Windows/Linux con Python 3.10/3.12. `scripts/validate_distribution.py` admite `--output` para guardar el comprobante y `--executable` para probar la CLI de un paquete portable.

Para finalidad, retención, rectificación, restricciones de acceso y fuentes primarias consulte [PRIVACIDAD_Y_FUENTES.md](docs/PRIVACIDAD_Y_FUENTES.md). El software incorpora controles técnicos; su uso legal depende de la finalidad, organización, fuentes y régimen aplicable.
