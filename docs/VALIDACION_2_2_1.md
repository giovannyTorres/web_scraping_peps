# Validación de PEPS MX 2.2.1

Comprobaciones del 15 de octubre de 2026 sobre la versión consolidada, en Linux
con Python 3.12.14. El [comprobante JSON](benchmarks/consolidation_2_2_1.json)
incluye resultados, condiciones y hashes del código de producción. Las mediciones
son locales; no estiman precisión sobre prensa real ni cobertura de internet.

## Correcciones verificadas

- Las fuentes RSS/Atom/GDELT entregan sus artículos al parser HTML correcto. La
  caché conserva la URL final después de redirecciones, tanto fresca como al
  revalidar con HTTP 304. Las cachés anteriores se revalidan una vez.
- La extracción editorial omite cabecera, títulos, plantillas e iframes. Se
  registra `article-main-2`; los cuerpos históricos permanecen archivados con su
  extracción original. Los errores permanentes consumen el presupuesto de
  reintentos; las esperas de cortesía lo conservan.
- Las revisiones ligadas a otro cuerpo o identidad dejan de validar hallazgos.
  Una selección humana de menciones vacía permanece vacía en cálculo e interfaz.
  Se rechazan evaluaciones obsoletas, incompletas o vinculadas a entradas nuevas.
  Las reglas se identifican como `review-priority-1.2.1`.
- Los permisos de red de la API requieren booleanos JSON. Rechazar una tarea
  ocupada no activa la conexión; tokens no ASCII reciben 403.
- La migración histórica conserva `seed_id`, procedencia, fragmentos y fechas;
  omite nombres ambiguos y conflictos explícitos. Los registros históricos tienen
  identidad documental propia para no heredar el tipo de una fuente oficial ni
  perder campos de otra fila con el mismo texto y URL.
- Exportación y respaldo rechazan el banco activo, sus archivos auxiliares y
  enlaces al mismo archivo. La exportación CLI se publica de forma atómica y
  conserva el archivo anterior si falla la escritura. `audit` devuelve código 2
  cuando encuentra inconsistencias.

## Resultados locales

| Comprobación | Resultado |
| --- | --- |
| Suite completa | 364 pruebas: 357 correctas y 7 opcionales de spaCy omitidas; 21.031 s. |
| Chromium, importación | 30 comprobaciones, incluidas codificaciones, móvil, doble envío y auditoría. |
| Chromium, explicación | 19 comprobaciones, incluidas citas, historial y persistencia de selección vacía con score cero. |
| Navegador | Cero errores de página y cero solicitudes externas. |
| Migración de CSV preservados | 10 entidades, 38 evidencias y 38 vínculos; cero filas omitidas, auditoría correcta y todos los scores históricos en cero. |
| Carga de scoring | 1,000 entidades y documentos; 100 positivos sintéticos esperados y observados, cero discrepancias, ninguna validación sin revisión. |
| Cola multiproceso | 1,000 tareas únicas terminadas. |
| Adquisición simulada | 1,000 URLs, banco de 40,000 entidades y cuatro workers; deduplicación y procedencia correctas. Repetición con cero peticiones HTTP. |
| Explicación/API | 21 entidades, 365 documentos y una entidad con 205 notas; citas y acceso a una nota fuera de las primeras 200 verificados, 20 replays correctos. |

La importación guiada, con un proceso independiente por tamaño, tardó 33.821 ms
en preview y 54.136 ms en guardar 1,000 filas; para 10,000 filas, 351.196 ms y
555.132 ms. Es una repetición por tamaño, sin scoring, modelos, HTTP ni navegador.
La adquisición simulada tomó 12.859 s en frío y 4.442 s al repetirla; esos tiempos
miden ingestión local con HTTP ficticio, no velocidad web. Las comprobaciones de
carga y la suite se ejecutaron en el mismo entorno, algunas en paralelo.

## Distribución y reproducción

El wheel se construye y valida instalado fuera del checkout, en un entorno limpio
sin dependencias de ejecución. El smoke comprueba 13 comandos y nueve recursos:
configuración, modelo, licencias, HTML, CSS y JavaScript. El script rechaza importar
el paquete fuente por accidente y ejecuta la CLI desde una carpeta temporal.

```powershell
python -m unittest discover -s tests -v
python scripts/test_import_ui.py
python scripts/test_explainability_ui.py
python scripts/benchmark_import.py --repetitions 1 --output runtime/import-validation.json
```

Las pruebas de navegador requieren Playwright y Chromium de desarrollo. Sus
reportes se guardan en `runtime/`; para reemplazar un comprobante histórico se
requiere indicar `--output` explícitamente. La ejecución base de la aplicación
sigue sin dependencias pip.

La CI ejecuta suite, cargas e instalación del wheel en Windows/Linux con Python
3.10/3.12; prueba por separado el NLP opcional y construye el portable Windows con
su smoke final. Esas ejecuciones remotas y sus artefactos se consultan en la
[pestaña Actions](https://github.com/giovannyTorres/web_scraping_peps/actions).

Los benchmarks previos siguen identificando la versión que midieron. Cambiar
reglas invalida el replay de scoring antiguo si ya no es compatible; su snapshot
archivado y bitácora permanecen disponibles. La extracción corregida no vuelve a
descargar fuentes ni reescribe documentos históricos automáticamente.
