# Recuperar una búsqueda sin perder el avance

WebScope 2.9.3 distingue la búsqueda por nombre, los recorridos complementarios de
índices y los textos pertinentes de la lista actual. Una descarga que sólo leyó
índices y obtuvo cero textos todavía no produjo cobertura documental útil.

## Actualizar con el mismo banco

Cierre la versión anterior y extraiga el paquete nuevo en otra carpeta. Mantenga
juntos `WebScope.exe`, `WebScope-CLI.exe` y `_internal`. Abra el ejecutable nuevo
con la misma ubicación de datos. Sin `--data-dir`, Windows sigue utilizando
`%LOCALAPPDATA%\PEPS-MX`; si antes indicaba otra ruta, conserve esa ruta. No borre
la base de datos ni vuelva a importar la lista para actualizar.

La actualización conserva fuentes, permisos, documentos, auditoría y tareas.
También conserva la estrategia elegida: una búsqueda antigua configurada como
índice compartido no se convierte silenciosamente en una consulta por nombre.

## Si leyó muchas páginas y no encuentra a los notarios

1. Abra el paso **Descargar noticias**. El panel principal muestra cuántos nombres
   tienen consultas web ejecutadas, resultados candidatos y texto para analizar.
   Las páginas de índices y los registros comparados aparecen en el detalle.
2. Si no hay resultados y sigue usando un índice, pulse **Buscar por nombre**.
   Mientras la descarga está activa, la acción se llama **Pausar y buscar por
   nombre**: solicita una pausa, espera su confirmación y prepara una búsqueda
   dirigida conservando el período y los permisos existentes.
3. Si no hay fuentes compatibles preparadas, vuelva a **Preparar fuentes**.
   Seleccione **Búsqueda dirigida por entidad** y **Nombre y pistas de identidad**.
   Revise y autorice las fuentes que quiera consultar. El catálogo incluye
   buscadores web públicos y búsquedas por nombre del editor; habilitar sólo un
   feed general o un sitemap no sustituye esas consultas.
4. Para una primera pasada acotada, elija **Rápido**. Revise el alcance y el
   máximo de consultas antes de iniciar. Un límite de dos minutos corresponde a
   la sesión; no promete terminar una lista de 652 personas en ese tiempo.
5. Distinga el siguiente cuello de botella: **cero candidatos** pide revisar las
   fuentes y los nombres; **candidatos sin texto** pide comprobar los permisos y
   la disponibilidad del cuerpo en la fuente editorial. No autorice textos de
   un editor distinto sólo porque su enlace apareció en un buscador.

Las consultas empiezan por el nombre canónico antes de dedicar el presupuesto a
pistas o variantes. Para notarios, use el nombre completo y conserve municipio,
estado y cargo en el contexto cuando los conozca. Un alias debe representar una
variante comprobada del nombre; no cambie automáticamente `ñ` por `n` para forzar
coincidencias. Las pistas ayudan a recuperar candidatos, pero no confirman
identidad ni generan puntos.

Un buscador como Google puede mostrar resultados que no existen en los índices
de los editores preparados. No todos esos resultados son noticias ni incluyen un
cuerpo que esta instalación pueda conservar. La opción de búsqueda manual abre
sitios públicos sin conectar cuentas; sus resultados no se incorporan al banco
automáticamente. No se eluden CAPTCHA, bloqueos ni permisos del editor.

## Entender el estado principal

| Estado | Qué ocurre y qué puede hacer |
| --- | --- |
| Consultando fuentes | Hay tareas ejecutándose. Puede solicitar una pausa. |
| Esperando a las fuentes | El proceso sigue abierto, pero no ejecuta una solicitud en ese instante. Se muestra la fuente y el próximo intento permitido cuando se conoce. Puede pausar esa espera. |
| Confirmando pausa | La pausa fue solicitada. Se espera la salida de las solicitudes antes de permitir cambios o análisis. |
| Descarga en pausa | No hay un proceso ejecutándose; las tareas pendientes están guardadas. Puede reanudarlas. |
| Hay fuentes que no respondieron | Quedaron bloqueos o errores. Revise el detalle; una fuente puede requerir revisión manual y no tener un reintento automático. |
| Descarga cerrada con cobertura parcial | Se aceptó analizar los textos disponibles; las tareas pendientes siguen guardadas para después. |

Una espera no significa que haya una petición de red activa ni que el proceso se
haya pausado. **Pausar proceso** solicita una salida segura; no libera reservas
por la fuerza. Un trabajador externo con reserva vigente también mantiene el
bloqueo. Recargar el navegador consulta el trabajo existente.

## Los contadores tienen alcances distintos

La pantalla principal cuenta la **lista actual**. El detalle separa las tareas
completadas en esta sesión de todas las tareas de la misma lista, incluidas
sesiones anteriores. El archivo histórico del banco se muestra por separado.

Por ejemplo, una pantalla antigua con 133 tareas completadas en la sesión y 719
completadas en el banco no estaba mostrando dos avances de la misma ejecución.
Tampoco 235 referencias conservadas de otras búsquedas equivalen a 235 noticias
sobre los 652 nombres actuales. Si esta lista tiene cero candidatos y cero
textos, esa es su cobertura útil. Comparar los 652 nombres con 52.063 registros
locales de índices no significa haber hecho una consulta web por cada nombre.

Abra **Ver tareas y reintentos** para consultar las tareas de la lista actual,
filtrar sus estados y revisar el error y el próximo intento. Las pausas de las
fuentes se conservan al reintentar; el botón no reinicia las cuotas.

## Analizar sólo cuando hay material pertinente

Con texto de la lista actual, **Analizar lo disponible** registra la cobertura
parcial y empieza el análisis en una sola acción. Si la búsqueda está abierta,
**Pausar y analizar textos disponibles** espera primero el cierre real de las
solicitudes. Ambas acciones conservan documentos y tareas pendientes.

Con cero textos pertinentes, el modo online no ofrece continuar a un scoring
vacío. Busque por nombre o incorpore documentos antes de analizar. Los documentos
locales tienen un recorrido explícito independiente; sus resultados deben indicar
la falta de evidencia cuando corresponda.

Reanudar o ampliar la adquisición conserva las evaluaciones históricas y exige
volver a analizar para obtener resultados vigentes. Los metadatos sin cuerpo no
puntúan como evidencia del artículo. Una cobertura parcial o un score no
concluyente tampoco acreditan ausencia de información o de riesgo.
