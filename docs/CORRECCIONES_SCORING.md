# Correcciones verificadas del scoring 1.1.1

Las reglas, ponderaciones, umbrales y agregación de prioridad documental se
conservan. Esta revisión corrige dos errores demostrados con textos sintéticos,
sin añadir modelos ni modificar el análisis lingüístico existente.

## Integridad de análisis almacenados

Antes, una caché con `entity_signature`, `content_hash`, `model_version` y
`rules_hash` vigentes podía sustituir las decisiones canónicas. Una cita literal
de «José Peña Villaluz inauguró una biblioteca.» y un hallazgo adulterado de
`violence/conviction` producían 24 puntos, frente a 0 con el análisis legítimo
(fecha de evaluación 2026-10-15, fuente de noticias).

Ahora, si los metadatos están vigentes pero `mentions`, `findings` o
`identity_confidence` difieren del análisis canónico calculado localmente, la
evidencia se excluye con `cached_analysis_content_mismatch`. La advertencia
queda en la auditoría. No se acepta la decisión adulterada ni se oculta la
inconsistencia sustituyéndola silenciosamente. Las cachés con metadatos
obsoletos siguen recalculándose, y las cachés íntegras conservan sus resultados.

## Agrupación determinista de copias idénticas

Antes, la primera copia de un texto elegía el trimestre del grupo. Para una
nota de detención por fraude de 2018-01-01, su republicación idéntica de
2026-10-10 y una paráfrasis de 2026-10-10, permutar las copias producía 17 o
13 puntos. El ajuste posterior de antigüedad no corregía el grupo elegido.

Ahora se asigna el grupo tras validar todos los hallazgos. Entre copias
idénticas se usa la fecha efectiva aceptada más antigua y un desempate estable
por identificador de evidencia, orden canónico del hallazgo y clave de grupo.
El ejemplo produce 17 puntos en las seis permutaciones: un grupo histórico y
otro de la paráfrasis reciente. Se conserva el máximo por evento y el ajuste
de antigüedad. Una copia rechazada, inválida o no atribuida no puede imponer su
fecha. Esto no demuestra que una paráfrasis reciente describa un evento nuevo;
los grupos sin identificador explícito siguen siendo una aproximación
conservadora que requiere revisión humana.

## Reproducir

```bash
python -m unittest tests.test_scoring_integrity -v
python -m unittest discover -s tests -q
```

Los casos contienen personas y URLs de prueba. No usan datos privados, red ni
servicios de inferencia. La versión del scoring cambia a
`review-priority-1.1.1`; el hash de parámetros permanece igual porque los pesos
y la fórmula no cambian. Las evaluaciones antiguas deben recalcularse para
aplicar estas correcciones.

## Instrumentación 1.2.0

La versión final `review-priority-1.2.0` agrega selección de máximos por rama,
empates y resoluciones, así como los operandos de precisión completa usados en
cada multiplicación. No cambia los pesos ni la agregación. La copia devuelta de
parámetros ya no permite modificar accidentalmente el diccionario global.
`tests/test_explainability.py` cubre citas reales, ambas ramas, deduplicación,
contrafactuales no aditivos, saturación a 95 y separación de análisis auxiliares.

La revisión también reprodujo un `TypeError` con `publisher_id` o
`content_cluster` de tipo lista/objeto. Esos campos se usan para contar soporte,
no para calcular puntos. Ahora se registra una advertencia y se conserva la
procedencia/hash documental como respaldo, sin inventar corroboración ni
alterar el score de la entrada válida equivalente.
