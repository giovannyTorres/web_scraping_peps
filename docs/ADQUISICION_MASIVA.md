# Listas grandes: descargar información pertinente

Para una lista de 300 entidades, el cuello de botella principal es el número de
consultas y los límites de cada fuente. WebScope 2.6 filtra por menciones de las
entidades antes de guardar resultados o solicitar cuerpos; las noticias ajenas
ya no aumentan la cobertura aparente del banco.

## Elegir el tiempo antes de empezar

En **Preparar fuentes**, elija el presupuesto y guárdelo antes de continuar:

| Recorrido | Presupuesto de sesión | Selección de cuerpos en la interfaz |
| --- | --- | --- |
| Rápido | 120 segundos | Hasta 3 por entidad |
| Ampliado | 30 minutos, 1 hora o 2 horas | Hasta 10 por entidad |

El rápido hace un primer barrido con consultas agrupadas cuando la fuente lo
admite. No expande automáticamente cada resultado saturado en meses y días. El
ampliado permite profundizar en el período y los alias. Los bancos anteriores
conservan su configuración y su límite de 900 segundos hasta que el usuario
elija otra opción; las instalaciones nuevas comienzan con búsqueda adversa rápida.

El límite controla la sesión y las solicitudes, incluidos robots y redirecciones.
La resolución DNS y las esperas de disco dependen del sistema operativo. No es
una promesa de consultar exhaustivamente las 300 entidades en dos minutos.

Una fuente que permite una petición cada cinco segundos necesita **al menos
24 min 55 s para iniciar 300 consultas individuales**, sin cuerpos, robots,
latencia ni reintentos. GDELT admite lotes; los buscadores WordPress se consultan
individualmente. Varias fuentes autorizadas pueden trabajar en paralelo.

## Preparar fuentes que puedan encontrar a las entidades

Las fuentes con búsqueda dirigida consultan nombres, alias y fechas. El catálogo
incluye GDELT reciente y archivos RSS de Desinformémonos, Contralínea y Aristegui.
Los feeds generales sólo aportan sus noticias disponibles; no son una búsqueda
histórica de cada persona. Los nuevos adaptadores comienzan desactivados.

Distinga los permisos de **consultar referencias** y **descargar texto para
analizar**. Los índices y agregadores aportan enlaces; conservar un artículo
requiere una fuente editorial habilitada y su permiso de texto. La interfaz
advierte cuando la selección sólo podrá obtener referencias.

Un nombre completo o un alias contextual permite recuperar candidatos. No
corrobora identidad. Se conservan también notas neutras, negaciones y contexto
de víctima; la relevancia de adquisición no agrega puntos al scoring. Un nombre
que aparece únicamente en el cuerpo y no en los metadatos puede quedar fuera de
este primer filtro: el recorrido acotado no acredita ausencia de información.

## Leer el avance

El paso de descarga distingue:

- Entidades consultadas: consultas que realmente devolvieron una respuesta;
  los trabajos planeados no cuentan como ejecutados.
- Entidades con candidatos: metadatos pertinentes o vínculos locales conservados.
- Entidades con texto: cuentan con cuerpo original disponible para revisión.
- Referencias y exclusiones: separadas del texto y de la identidad corroborada.

La cifra bruta de 250 noticias puede permanecer en un banco antiguo como registro
histórico. La interfaz prioriza la cobertura por entidad y presenta las notas
pertinentes, sin borrar el corpus ni la auditoría previos.

## Continuar y recuperar

**Reanudar** conserva la cola y los aplazamientos de las fuentes. También puede
recuperar cuerpos de candidatos guardados si ahora existen permisos de texto,
sin repetir el descubrimiento. No reinicia las cuotas a cada clic.

**Iniciar una búsqueda nueva** sustituye las consultas pendientes del plan
anterior. Conserva los cuerpos y las tareas con reserva activa; registra las
consultas sustituidas sin borrar trabajos ni adelantar reintentos. Esto evita que
un plan antiguo de diez años retrase una sesión rápida recién elegida.

Al finalizar el presupuesto pueden quedar consultas o cuerpos pendientes. Se
puede reanudar o cerrar explícitamente con la cobertura disponible cuando no haya
trabajadores activos. El análisis y los resultados conservan esa limitación.

## Entender los resultados

**—** indica que falta evidencia suficiente, cuerpo original o corroboración de
identidad. **0** se muestra cuando el material admisible evaluado no activa
puntos. El cálculo numérico y su traza se conservan; no se aumentan scores para
compensar falta de información. Abra un expediente para ver el motivo concreto,
las citas, las exclusiones y las tareas de revisión.

Los [resultados de validación](VALIDACION_2_6_0.md) separan las pruebas sintéticas
con 300 entidades de las verificaciones públicas de acceso a fuentes.
