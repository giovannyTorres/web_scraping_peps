# PEPS MX 2.0

Banco local de documentos públicos sobre personas, empresas y organizaciones mexicanas. Permite importar entidades, mantener una cola de búsqueda, conservar evidencia, detectar menciones en español y ordenar la revisión documental con una puntuación explicable.

La condición PEP, un parentesco, un cargo o un contrato público no son conductas adversas por sí mismos. Los resultados son candidatos de revisión: no acreditan identidad, culpabilidad ni ausencia de riesgos. El sistema no debe decidir automáticamente la contratación, la admisión bancaria ni el acceso a servicios.

## Inicio en Windows

El paquete portable usa la carpeta completa `PEPS-MX`, con `PEPS-MX.exe`, `PEPS-CLI.exe` y la carpeta auxiliar `_internal`. Abra `PEPS-MX.exe` con doble clic: inicia un servidor en `127.0.0.1` y abre el navegador predeterminado sin mostrar una consola. No requiere instalar Python en la máquina de destino. Mantenga juntos ambos ejecutables y `_internal`; distribuir únicamente un `.exe` no funciona. El paquete no es un instalador ni un servicio de Windows.

Desde el código fuente se requiere Python 3.10 o posterior. El programa utiliza únicamente la biblioteca estándar en ejecución: no necesita Hugging Face, claves de un modelo ni servicios de IA.

```powershell
python -m peps --data-dir .\runtime ui
```

Ejecute los comandos desde la raíz del proyecto. En el bundle sustituya `python -m peps` por la ruta a `PEPS-CLI.exe`, que conserva salida y errores de consola. Desde la carpeta portable, para diagnóstico de apertura use `.\PEPS-CLI.exe --data-dir .\runtime ui --no-browser`; muestra la URL local. `PEPS-MX.exe` está destinado al doble clic. Para conservar los datos en una ubicación explícita, utilice siempre el mismo `--data-dir`; sin esa opción, Windows usa `%LOCALAPPDATA%\PEPS-MX`.

## Flujo de trabajo

1. Importe el banco de entidades con nombres, tipo, alias y contexto de identidad.
2. Importe documentos autorizados o habilite fuentes públicas revisadas y procese la cola.
3. Revise el cuerpo, la URL, la fecha, las citas, los posibles homónimos y el estado procesal.
4. Confirme o rechace cada evidencia con nombre de revisor y motivo. Conserve exportaciones y respaldos.

```powershell
python -m peps --data-dir .\runtime import-entities .\entidades.csv
python -m peps --data-dir .\runtime import-documents .\documentos.jsonl
python -m peps --data-dir .\runtime score
python -m peps --data-dir .\runtime audit
python -m peps --data-dir .\runtime export .\exportaciones\banco.jsonl
```

Las entidades admiten `kind=person`, `company` u `organization`; los alias se separan con `|` en CSV y `context` contiene JSON. Para cargas grandes utilice la CLI: la interfaz limita cada petición de importación a 16 MB. Los formatos y el modo cerrado están en [OPERACION.md](docs/OPERACION.md).

## Implementación y alcance

- SQLite en modo WAL almacena entidades, documentos, menciones, puntuaciones, trabajos y una bitácora encadenada SHA-256. La cola tiene leases, reintentos acotados y estados de bloqueo. La verificación comprueba cadena, contenido documental y hashes de las puntuaciones actuales; no reemplaza una firma externa ni un control de acceso al disco.
- Un índice de nombres y alias evita cotejar cada texto contra todos los nombres. La extracción utiliza reglas, un gazetteer y offsets verificables. Es un motor conservador en español; no es NER estadístico general y no resuelve todas las correferencias, ironías, negaciones ni ambigüedades.
- El scoring mantiene contribuciones, exclusiones y parámetros versionados. Sus pesos son heurísticos y no tienen validación predictiva de conducta futura. Una prioridad cero significa que la evidencia disponible no activó una regla admisible; no significa que una persona esté libre de información adversa.
- La recolección aplica dominios permitidos, TLS, robots, límites de tamaño, caché, pausas por dominio y detención ante restricciones. Los extractos de buscadores se conservan como descubrimiento, no como contenido editorial verificado.
- Los adaptadores locales de `peps.official` leen XML clásico OFAC, XML ONU y CSV SAT 69-B. Conservan naturaleza administrativa, fecha de la copia y estados fiscales. No leen todos los formatos de todas las autoridades ni convierten una sanción en condena penal.

`import-official` valida una copia local completa antes de incorporarla; admite `--source ofac`, `un` o `sat69b`, y exige una URL oficial de procedencia. Los ejemplos están en [OPERACION.md](docs/OPERACION.md).

La arquitectura es de una estación local. Use disco local para SQLite; no coloque el banco en una carpeta de red. La exportación JSONL transmite registros de entidad, documento y evidencia a disco, sin reunir todo el banco en el navegador. PostgreSQL, almacenamiento externo de objetos y workers distribuidos son posibles evoluciones, todavía no implementadas.

## Automatización y capacidad

Las fuentes están deshabilitadas inicialmente. Una vez revisadas y configuradas, el modo continuo genera una ventana UTC diaria de descubrimiento y procesa la cola:

```powershell
python -m peps --data-dir .\runtime --online worker --daemon --daily --workers 4 --seconds 240 --max-jobs 1000
```

Los workers comparten límites persistidos por dominio; más hilos no autorizan más peticiones a una fuente. Consulte [OPERACION.md](docs/OPERACION.md) antes de activar el Programador de tareas.

```powershell
python -m peps capacity --entities 40000
python -m unittest discover -s tests
```

`capacity` muestra un modelo con supuestos, no una medición web ni una promesa de procesar 40 mil personas en una semana. Los benchmarks locales y las pruebas web deben evaluarse por separado; los bloqueos, límites, cobertura e inspección humana pueden dominar el plazo.

Los notebooks originales y sus salidas se conservan como material histórico. Sus XML locales tienen fechas de enero y mayo de 2026; no representan listas actualizadas al momento de ejecución. Importe las salidas antiguas con `import-legacy` para mantener esa distinción.

## Construcción del ejecutable

PyInstaller es una dependencia de construcción, no de ejecución. Instálelo en un entorno de desarrollo permitido, o use un wheelhouse aprobado para una construcción sin internet.

```powershell
python -m pip install pyinstaller
.\scripts\build_windows.ps1 -Python python -Destination dist
```

El script ejecuta las pruebas y construye las variantes gráfica y de consola, colocando `PEPS-CLI.exe` junto a `PEPS-MX.exe` para compartir `_internal`. Distribuya `dist\PEPS-MX` completo. El entorno de destino debe permitir ejecución de aplicaciones locales, apertura de navegador y escritura en el directorio de datos; políticas corporativas pueden impedirlo.

Para finalidad, retención, rectificación, restricciones de acceso y fuentes primarias consulte [PRIVACIDAD_Y_FUENTES.md](docs/PRIVACIDAD_Y_FUENTES.md). El software incorpora controles técnicos; su uso legal depende de la finalidad, organización, fuentes y régimen aplicable.
