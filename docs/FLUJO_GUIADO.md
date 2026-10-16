# De la lista al scoring

La interfaz de WebScope 2.9.3 guía el trabajo en cinco pasos. La barra lateral
muestra qué terminó y qué requisito falta. La descarga tiene un panel principal
con el estado operativo y la cobertura de la lista actual; los contadores de
índices, sesión y tareas quedan en el detalle para consultar su alcance.

## 1. Cargar lista

Agregue un nombre o seleccione **Lista CSV o tabla**. En un archivo, identifique
las columnas, revise los tipos y confirme la vista previa. **Guardar y continuar**
conserva las entidades y abre el paso de fuentes. Guardar no busca noticias ni
calcula resultados. Las pistas de identidad siguen pendientes de verificación.

Para personas, use el nombre completo y conserve municipio, estado y cargo cuando
los conozca. Los alias deben ser variantes comprobadas; las pistas de contexto
orientan la búsqueda, pero no confirman que una noticia corresponda a esa persona.

## 2. Preparar fuentes

Elija el recorrido:

- **Noticias en línea:** prepare fuentes cuyas condiciones haya revisado. Para
  investigar nombres, empiece por **Búsqueda dirigida por entidad** y **Nombre y
  pistas de identidad**. El período, las consultas y el presupuesto se preparan
  localmente. Revise el alcance y pulse **Guardar e iniciar**.
- **Documentos locales:** use información incorporada al banco, sin descargar de
  internet. Confirme ese alcance. Si falta evidencia, el resultado lo debe
  indicar; no se presenta como una búsqueda web completada.

**Consultar** registra el permiso de esa fuente. **Conservar texto** registra por
separado el permiso para sus cuerpos editoriales. Los buscadores que sólo ofrecen
referencias no tienen un permiso ficticio de cuerpo para todos los sitios que
enlazan. Las fuentes empiezan deshabilitadas; elegir el modo online no las
autoriza. La acción principal espera a que se guarden los cambios de permisos.

El recorrido **Índice de medios · complementario** contrasta los nombres con las
entradas del archivo de los editores preparados. No sustituye una búsqueda web
por cada nombre y puede no encontrar a los notarios de una lista. **Novedades de
feeds generales** se limita a las ediciones disponibles. Consulte la
[guía de adquisición masiva](ADQUISICION_MASIVA.md) para elegir el alcance.

Las instalaciones anteriores conservan su estrategia y permisos. Actualizar no
convierte una cola de índices en consultas por nombre. La
[guía de recuperación](RECUPERACION_DESCARGA.md) explica cómo cambiar el recorrido
sin borrar el banco.

## 3. Descargar noticias

El panel principal separa **Nombres consultados en la web**, **Con resultados
candidatos** y **Con texto para analizar**. Son etapas distintas: una consulta
sin resultados puede estar completada, y un candidato sin cuerpo sigue sin ser
evidencia documental puntuable. El archivo histórico del banco no se suma a los
resultados de esta lista.

En **Opciones de esta sesión**, elija el uso de la caché:

- **Reutilizar caché:** aprovecha respuestas vigentes antes de consultar internet.
- **Comprobar cambios:** revalida las URLs procesadas, con peticiones condicionales
  cuando la fuente las admite.
- **Sólo caché local:** usa lo conservado sin DNS ni conexiones a internet. Una
  copia antigua conserva su fecha; un enlace sin copia queda pendiente.

Autorice la conexión para los dos primeros modos. El límite rápido de dos
minutos es un presupuesto de sesión, no una promesa de terminar toda la lista.
Al descubrir enlaces puede crecer el trabajo pendiente; las barras distinguen
avance conocido de cobertura real de nombres.

**Esperando a las fuentes** significa que el proceso conserva la cola, pero no
hay una solicitud ejecutándose en ese instante. El panel muestra la fuente, el
error y el próximo intento cuando se conocen. La operación mantiene el bloqueo
hasta que termine o confirme una pausa; esperar no equivale a estar pausado.

**Pausar proceso** solicita la salida segura. Durante **Confirmando pausa**, una
petición puede necesitar terminar o agotar su timeout. Después, **Reanudar tareas
pendientes** procesa la cola conservada. No hay que volver a importar la lista.
Recargar la página u abrir otra pestaña consulta la misma operación.

Si un recorrido de índices no aporta candidatos, use **Buscar por nombre**.
Con un proceso abierto, **Pausar y buscar por nombre** espera primero su cierre y
prepara consultas dirigidas con el período y los permisos conservados. Si falta
una fuente compatible, vuelve a la preparación; no concede permisos por sí sola.

Con texto pertinente, **Analizar lo disponible** registra el cierre parcial y
empieza el análisis en una acción. Si la búsqueda sigue abierta, **Pausar y
analizar textos disponibles** espera primero la pausa real. Las tareas pendientes
se conservan y las evaluaciones registran sus límites. Con cero textos de la
lista, el modo online pide buscar o incorporar documentos; no habilita un scoring
vacío por el solo hecho de haber recorrido índices.

Abra **Ver tareas y reintentos** para filtrar las tareas de la lista actual y
revisar errores y fechas. El acumulado de tareas incluye sesiones anteriores de
la misma lista; el detalle identifica por separado lo completado en esta sesión.
Las solicitudes de red, los reusos de caché, los cuerpos nuevos o reutilizados y
los candidatos repetidos corresponden a la sesión identificada. **—** significa
que no hay medición disponible. Un reintento no borra las pausas del proveedor.

## 4. Analizar y calcular

El cálculo empieza con **Analizar y calcular score**, **Analizar lo disponible**
o al terminar una adquisición completa si eligió **Analizar al terminar**.
La continuación automática se detiene cuando quedan limitaciones que requieren
aceptar expresamente la cobertura parcial. Primero se atribuyen las noticias al
banco; después se calculan los resultados y sus explicaciones.

Las fases muestran artículos o entidades procesados y guardados. Una estimación
aparece cuando hay una base suficiente de trabajo observado o muestras previas;
puede cambiar si los documentos restantes son más costosos. Los términos de una
consulta y las prioridades de adquisición no agregan puntos al scoring.

Mientras una operación siga abierta, tampoco se pueden importar más datos,
cambiar fuentes, guardar revisiones ni exportar resultados vigentes. Los
controles se aplican también a la API. Un trabajador externo con reserva vigente
impide cerrar cobertura o empezar análisis concurrente.

## 5. Revisar resultados

Abra una entidad para ver el score, los factores, las citas y las exclusiones.
Consulte la evidencia original antes de confirmar una revisión. La exportación
se habilita cuando el análisis de los datos vigentes está completo. Los
históricos siguen disponibles sin recalcularlos.

Agregar entidades, modificar fuentes, incorporar documentos o cambiar revisiones
puede invalidar los resultados anteriores. El sistema conserva el historial y
exige repetir los pasos afectados. **—**, una cobertura parcial o la falta de
hallazgos no acreditan ausencia de riesgo ni una investigación completa.

## Estados y tiempo restante

| Estado | Qué significa |
| --- | --- |
| Preparando consultas | El proceso está organizando o guardando trabajo; aún no tiene una solicitud activa. |
| Consultando fuentes | Hay tareas de consulta o descarga ejecutándose. |
| Esperando a las fuentes | El proceso conserva la operación y espera el próximo intento, sin una solicitud activa. |
| Confirmando pausa | Se espera la salida segura antes de permitir otra operación. |
| Descarga en pausa | No hay proceso ejecutándose; puede quedar trabajo para reanudar. |
| Hay fuentes que no respondieron | Hay errores o bloqueos; puede faltar un reintento automático. |
| Descarga cerrada con cobertura parcial | Se aceptaron los textos disponibles y se conserva la cola pendiente. |
| Búsqueda finalizada | La cola del alcance vigente terminó; su cobertura se revisa por separado. |

El detalle de tiempos usa el rendimiento observado por fase o muestras previas
conservadas en el equipo. Antes de tener base suficiente se indica que la duración
no es estimable. Una descarga que todavía descubre tareas no tiene un total
estable. La hora de reintento de una fuente indica cuándo se permite intentarlo
de nuevo; no estima cuándo terminará toda la investigación ni garantiza que esa
solicitud vaya a aportar un artículo.
