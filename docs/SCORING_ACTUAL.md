# Fuente de verdad del scoring

Revisión del código al iniciar este cambio: `review-priority-1.1.0`, commit
`8f57fc4`. Las correcciones reproducibles posteriores se documentan por separado
en `CORRECCIONES_SCORING.md`. El algoritmo está en `peps/scoring.py` y sus
parámetros completos se incluyen en cada resultado con un SHA-256.

## Interpretación

Es una **prioridad heurística de revisión documental**, entre 0 y 95. Se presenta
sobre una escala nominal de 100, pero el algoritmo impone un techo de 95.
No está calibrada como probabilidad, pérdida esperada ni culpabilidad. Un 85
activa prioridad alta; un 40, media. No implica 85 % o 40 % de probabilidad ni
una razón de riesgos de 85/40.

| Resultado | Etiqueta aplicada por el código |
| --- | --- |
| 0 | Sin señales puntuables |
| 1–19 | Baja |
| 20–49 | Media |
| 50–95 | Alta |

Cero también puede resultar de falta de cuerpo, identidad no resuelta,
documentos rechazados, fuentes de descubrimiento, negación o resolución
exculpatoria. Se deben consultar las exclusiones para distinguir estos casos.

## Cálculo de cada hallazgo admisible

`p = categoría × etapa × fuente × identidad × antigüedad × incertidumbre`

- Categoría: entre 18 (reputacional) y 42 (violencia/crimen organizado).
- Etapa: acusación 0.45; investigación 0.6; detención/búsqueda 0.75;
  condena 1; sanción 0.9. Absolución y sobreseimiento tienen peso cero.
  La negación de acusaciones se conserva como contexto y produce cero puntos
  mediante una regla explícita, aunque el diccionario de etapas contenga 0.25.
- Fuente: oficial/gobierno/judicial 0.95; noticia 0.7; ONG 0.65;
  comentario/red social 0.2; desconocida 0.4.
- Identidad: confianza heurística del resolvedor; una revisión humana completa
  aplica 1. El umbral automático mínimo es 0.65. Para noticias se exige además
  atribución vigente y menciones del cuerpo corroboradas o seleccionadas.
- Antigüedad: `max(0.35, 2 ** (-días / 730))`; sin fecha, 0.5.
  Se usa la fecha más antigua entre publicación y evento. Fechas futuras se
  excluyen. Copias exactas conservan la antigüedad aceptada más antigua.
- Incertidumbre: 0.7 cuando el hallazgo la identifica; en otro caso, 1.

Las citas y menciones deben corresponder literalmente a offsets del cuerpo.
Negaciones, acciones hipotéticas y asociación sin conducta directa no puntúan.
Los pesos son decisiones heurísticas del proyecto, sin validación predictiva.

## Agrupación y resultado

Se agrupa por identificador explícito del evento, expediente o, en su defecto,
categoría y trimestre calendario. Este último es un agrupamiento conservador
pendiente de revisión: no demuestra que dos noticias describan el mismo hecho.
Se toma el máximo por evento, sin sumar copias ni categorías del mismo evento.
Las resoluciones exculpatorias posteriores suspenden la rama provisional de la
categoría; para cancelar su rama validada necesitan revisión humana.

Para los puntos `pᵢ` de eventos distintos:

`score_rama = min(95, max(0, round(100 × (1 − ∏(1 − pᵢ/100)))))`

La función usa los puntos por evento redondeados a seis decimales. Calcula una
rama provisional y otra validada; el resultado visible es su máximo. La rama
provisional también puede incluir documentos revisados: no son poblaciones
disjuntas. Las contribuciones individuales no deben sumarse como si el cálculo
fuera lineal. Tampoco se deben sumar las dos ramas.

## Frontera con el análisis contextual

Los modelos auxiliares nuevos describen roles, relaciones y desacuerdos. No
modifican los pesos, las exclusiones ni los hallazgos del motor existente.
Una señal auxiliar ausente del cálculo debe aparecer identificada como tal.
La explicación numérica se construye exclusivamente con la traza del scorer.
La revisión humana confirma el vínculo documental y la comprobación declarada;
no transforma por sí misma una acusación periodística en hecho judicial probado.
