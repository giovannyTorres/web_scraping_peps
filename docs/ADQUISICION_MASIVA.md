# Listas grandes: buscar nombres y conservar texto pertinente

En WebScope 2.9.3, empiece por **Búsqueda dirigida por entidad**, especialmente
para listas de notarios o personas con poca cobertura en los medios preparados.
El primer barrido usa los nombres canónicos antes de profundizar en contexto,
alias u otras ventanas. Los buscadores públicos preparados se programan antes
de las búsquedas más estrechas de cada editor.

**Índice de medios · complementario** sigue disponible para contrastar una lista
contra un archivo autorizado. No es el recorrido recomendado por defecto para
investigar nombres: leer cientos de páginas de unos pocos editores puede dar
cero coincidencias aunque existan resultados en un buscador general. La
[guía de crawlers e índices](CRAWLERS_Y_DESCUBRIMIENTO.md) explica esta alternativa
y conserva las mediciones de investigación originales.

## Preparar una primera pasada útil

En **Preparar fuentes**, seleccione **Búsqueda dirigida por entidad** y el perfil
**Nombre y pistas de identidad**. Cargue nombres completos y contexto conocido,
como municipio, estado o cargo. El perfil general conserva candidatos neutrales
además de posibles señales adversas; no presupone conductas.

Revise fuentes que admitan búsqueda por nombre, el período y el presupuesto.
Los buscadores web públicos aportan descubrimiento general; los archivos de los
editores buscan en sus propios sitios. Los feeds generales sólo aportan las
ediciones disponibles. No equivalen a una investigación histórica de la lista.

La búsqueda inicial distribuye las consultas entre entidades y fuentes antes de
multiplicar el trabajo por variantes y ventanas históricas. Cuando la fuente
admite lotes compatibles, como GDELT, puede agrupar nombres. Una cadena con muchos
nombres no se trata como una consulta booleana válida en todos los proveedores.
El plan avisa si el máximo elegido deja entidades o ventanas fuera.

| Recorrido | Presupuesto de sesión | Selección de cuerpos en la interfaz |
| --- | --- | --- |
| Rápido | 120 segundos | Hasta 3 por entidad |
| Ampliado | 15 minutos conservados de una configuración anterior, o 30 minutos, 1 hora o 2 horas | Hasta 10 por entidad |

Las instalaciones nuevas empiezan con búsqueda dirigida rápida y el perfil de
identidad. Las instalaciones anteriores conservan su configuración y sus
permisos; revise el plan antes de reanudar. Para cambiar una búsqueda antigua por
índices sin perder el banco, siga la [guía de recuperación](RECUPERACION_DESCARGA.md).

El modo rápido usa ventanas automáticas anuales para no multiplicar de entrada
cada nombre por doce meses. La opción mensual explícita sigue disponible.
Los filtros temporales tienen el alcance que admita cada fuente: una búsqueda
web sin filtro editorial verificable no se presenta como archivo histórico
completo. GDELT DOC limita su descubrimiento a una ventana reciente de hasta
90 días.

## Dos permisos, dos etapas

**Consultar** habilita el descubrimiento en una fuente cuyas condiciones haya
revisado. **Conservar texto** requiere el permiso correspondiente del editor.
Los índices y agregadores pueden aportar enlaces a sitios para los que no hay
permiso ni adaptador de cuerpo. No se conserva texto de cualquier dominio sólo
porque aparezca en un resultado de búsqueda.

El filtro de pertinencia se aplica antes de solicitar cuerpos: una coincidencia
nominal o una variante contextual admisible produce un candidato, no una identidad
corroborada. Un nombre que sólo aparezca dentro del artículo puede quedar fuera
si el título, el extracto y la URL no permiten recuperarlo. El recorrido acotado
no acredita ausencia de información.

Los cuerpos se priorizan por utilidad documental; también se conserva contexto
neutral, de víctima, negaciones y resoluciones. La prioridad de descarga no agrega
puntos al scoring. Los enlaces y las observaciones conservan su procedencia;
la caché y la deduplicación evitan repetir trabajo ya disponible.

## Leer el avance de la lista actual

El panel principal presenta tres cifras separadas:

- **Nombres consultados en la web:** consultas completadas con respuesta; un
  nombre planeado o contrastado con un índice local no cuenta como consultado.
- **Con resultados candidatos:** nombres con metadatos pertinentes o vínculos
  conservados. Falta comprobar identidad y, cuando corresponda, obtener el cuerpo.
- **Con texto para analizar:** nombres con cuerpo original disponible para
  revisión. No significa que todas las menciones sean de esa persona.

El detalle conserva las páginas de índices y sus registros comparados, las
solicitudes de red, los reusos de caché y las tareas. Los contadores de sesión,
los acumulados de la lista y el archivo histórico están identificados por
separado. Cientos de documentos históricos del banco no aumentan la cobertura
de una lista nueva sin una coincidencia pertinente.

Si aparecen **cero candidatos**, revise nombres y fuentes o cambie el índice por
búsqueda por nombre. Si hay **candidatos y cero cuerpos**, revise el acceso y los
permisos de texto del editor. Añadir tiempo a un recorrido sin resultados no
resuelve por sí mismo ninguno de esos problemas.

## Esperas, recuperación y cierre parcial

**Esperando a las fuentes** indica que el coordinador conserva la operación sin
una solicitud en curso. El próximo intento permitido se muestra aparte del
procesamiento. El análisis permanece bloqueado hasta confirmar la pausa; una
espera no concede permiso para ejecutar dos procesos sobre el mismo banco.

**Reanudar** conserva la cola, la caché y los aplazamientos. **Iniciar una búsqueda
nueva** sustituye las consultas pendientes del plan anterior y conserva cuerpos,
auditoría y tareas con una reserva activa. No adelanta reintentos ni marca esas
consultas como terminadas.

Con textos pertinentes, **Analizar lo disponible** cierra explícitamente la
cobertura parcial e inicia el análisis. **Pausar y analizar textos disponibles**
hace primero la pausa segura si todavía hay un proceso abierto. Con cero textos
de la lista, el modo online no inicia un análisis vacío. La cola conservada puede
reanudarse más tarde; ampliar la cobertura exige volver a analizar para generar
resultados vigentes.

## Tiempos y resultados

El presupuesto limita la sesión; no garantiza un número de artículos, cobertura
completa ni 300 entidades procesadas en dos minutos. El tiempo depende del
intervalo por dominio, la latencia, los reintentos, la pertinencia y la
disponibilidad de cuerpos. A cinco segundos entre solicitudes, iniciar 300
consultas individuales en un solo dominio requiere al menos 24 min 55 s, antes
de sumar robots, respuestas y artículos. Es una cota de programación, no una
medición del pipeline completo.

El tiempo restante sólo se estima con trabajo observado o muestras previas.
Mientras se descubren nuevas tareas o falta una base suficiente, puede no ser
estimable. Una espera o un bloqueo externo se muestra como tal; no se traduce en
un porcentaje ficticio de investigación terminada.

**—** indica falta de evidencia suficiente, cuerpo original o corroboración de
identidad. **0** corresponde a material admisible evaluado que no activa puntos;
no se aumentan scores para compensar falta de información. Abra el expediente
para revisar las citas, exclusiones y tareas humanas pendientes.

Las capturas locales de extractores anteriores conocidos pueden volver a
procesarse sin repetir HTTP. Se conservan originales y evaluaciones históricas;
las revisiones no se transfieren automáticamente a nuevos fragmentos. Consulte
la [validación real con 300 personas](VALIDACION_REAL_300_2_9_2.md), que distingue
cobertura de metadatos, cuerpos disponibles, pruebas locales y límites de red.
