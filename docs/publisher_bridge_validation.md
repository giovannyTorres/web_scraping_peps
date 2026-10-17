# Validación real de editores a partir de una búsqueda terminada

`scripts/validate_publisher_bridge.py` amplía una búsqueda nominal real ya
terminada. La prueba conserva el padrón, los enlaces verdaderamente encontrados
y el historial del experimento de origen. No es otra prueba fría, un nuevo
recorrido de 300 nombres ni una medición independiente de eficacia.

El origen necesita su informe final, `frozen_inputs.json`, el catálogo original
intacto y ningún trabajador con lease vigente. Un banco nuevo recibe los
archivos y respaldos consistentes de `peps.sqlite3` y `network.sqlite3` mediante
la API de backup de SQLite. Se comparan sus huellas antes de ejecutar. La
auditoría original permanece como prefijo y se añade el recibo de derivación.

El catálogo adicional de sesión debe incluir únicamente editores públicos
HTTPS revisados. Cada fuente declara hosts exactos, `article_path_globs`,
`article_excluded_path_globs`, una `article_metadata_url_allowlist` exacta de
enlaces ya encontrados y permisos separados de cuerpos para esta sesión
técnica privada. Requiere `automated_search=false` y `discovery=false`. No se
cambia el catálogo predeterminado del producto ni se infiere licencia comercial
de que una página responda HTTP 200.

```bash
python scripts/validate_publisher_bridge.py \
  --online \
  --origin runtime/mi-banco-real-terminado \
  --origin-report runtime/mi-informe-real-final.json \
  --sources-config runtime/mis-editores-revisados-de-sesion.json \
  --data-dir runtime/mi-banco-derivado-nuevo \
  --output runtime/mi-informe-derivado-privado.json \
  --acquire-seconds 240 --seconds 600 \
  --max-http 120 --max-jobs 120 --workers 16 \
  --body-budget 600 --per-entity-body-limit 8
```

La adquisición usa `NewsAcquirer.acquire(queue=False)` y su rescate de
candidatos conservados. Como esa opción todavía podría consumir consultas
heredadas, sus trabajos se aparcan temporalmente en la copia derivada. El
`finally` restaura sus filas completas, con sus fechas, intentos, errores,
estados y propietarios exactos. Las dos operaciones dejan recibos auditables.
No se reinicia la espera de ningún host, incluido un bloqueo por CAPTCHA.

Un observador del transporte real exige que cada petición padre corresponda a
una URL conservada del origen y a un editor agregado. También revisa hosts y
rutas de redirección antes del transporte. Robots, pausas y límites del
`SafeFetcher` de producción siguen activos. Los 120 HTTP máximos incluyen
robots y saltos de redirección nuevos; el historial heredado no se reinicia.
Los bytes reales y sus hashes quedan guardados localmente. No se sustituyen
respuestas, textos, relojes, resoluciones de identidad o cálculos.

Después se ejecutan la atribución de producción, un nuevo cálculo para todas
las entidades, la repetición exacta de cada cálculo, sus explicaciones y la
exportación. La adquisición dura hasta 240 segundos; `--seconds` limita el
transporte y deja margen para procesar. La atribución y el cálculo terminan
sobre lo conservado y sus duraciones reales se reportan por separado. La
ejecución exige al menos 300 entidades; `--allow-small-bank` sólo identifica
un piloto funcional menor y no se usa para representar una prueba masiva.

El informe compara cuerpos, personas con texto, peticiones, estados del
cálculo y tiempos antes y después. Comprueba que el origen siga intacto, los
trabajos heredados estén restaurados, no existan nuevas consultas, el historial
de red se preserve, cada cálculo corresponda a esta ejecución y las repeticiones
coincidan. Un resultado calculado sin evidencia evaluable conserva su estado
de información insuficiente; no se interpreta como una persona sin incidentes.

Los informes, originales, explicaciones y exportaciones individuales son
privados. Para publicar resultados, use sólo agregados. Sin textos revisados y
denominadores independientes no se estima precisión, recall ni accuracy.
Esta ampliación de editores se selecciona después de ver los enlaces
descubiertos y debe identificarse como una reparación técnica retrospectiva.

Las pruebas `tests/test_publisher_bridge_guards.py` verifican el alcance, los
respaldos y la restauración ante errores sin hacer peticiones de red. Son
pruebas funcionales del control de ejecución, no benchmarks de rendimiento.
