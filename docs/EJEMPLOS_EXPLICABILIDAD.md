# Demostración local con explicaciones reconstruibles

Las seis entidades, once notas, expedientes y URLs de esta demostración son
**completamente inventados**. No describen personas, empresas, sentencias ni
fuentes oficiales reales. Todos los dominios terminan en `.invalid` y el script
no los consulta.

## Ejecutar y abrir

Desde la raíz del repositorio, sin dependencias opcionales ni conexión:

```sh
python scripts/demo_explainability.py --data-dir runtime/demo-explicabilidad
```

El destino debe ser nuevo o estar vacío, dentro de `runtime/`. El script rechaza
un banco existente y no borra archivos. Omitir `--data-dir` genera un nombre
nuevo. Para abrir la misma aplicación del proyecto con este banco aislado:

```sh
python scripts/demo_explainability.py --data-dir runtime/demo-explicabilidad-ui --ui
```

También se puede abrir un banco ya generado con:

```sh
python -m peps --data-dir runtime/demo-explicabilidad ui
```

La interfaz existente escucha en loopback; no se añade otro servidor o base de
datos. Seleccione una entidad y abra «¿Por qué obtuvo este score?». Las notas se
leen desde sus copias locales; sus enlaces `.invalid` no corresponden a páginas
web reales.

Para añadir extracciones spaCy locales, instale previamente el componente según
`NLP_LOCAL.md` y ejecute el script con ese intérprete y `--mode enriched`. Los
modos `advanced` y `hybrid` también están disponibles para inspección; no cambian
el algoritmo de scoring. No se instala ni descarga nada desde esta demo.

## Resultados medidos

Se ejecutó con la fecha de evaluación UTC **2026-10-15**, modo
`discriminative`, mediante `Engine.import_documents`, `Engine.rescore`,
`Engine.explanation` y `Engine.replay_score`. No se fabricaron resultados ni se
inyectaron análisis almacenados. Se reprodujeron correctamente las seis
evaluaciones finales y la evaluación histórica anterior a la resolución.

| Entidad sintética | Score medido | Interpretación y causa real |
|---|---:|---|
| Consorcio Boreal Inventado (`demo-alta`) | 82 | Prioridad alta provisional: cinco eventos distintos de condenas reportadas en fixtures. |
| Comercializadora Ámbar Ficticia (`demo-media`) | 33 | Prioridad media provisional: un evento de violencia con condena reportada en un fixture. |
| Cooperativa Marfil Imaginaria (`demo-victima`) | 0 | Nota de víctima sin conducta adversa propia puntuable. |
| Transportes Vértice Simulados (`demo-denunciante`) | 0 | La denuncia contra terceros no se atribuye al denunciante como conducta adversa propia. |
| Renata Olmo Inventada (`demo-homonimo`) | 0 | La noticia solo aporta un nombre; identidad sin corroboración, evidencia excluida. |
| Laboratorio Cedro Simulado (`demo-resolucion`) | 0 | La absolución posterior suspende provisionalmente el mismo evento; se conserva el antecedente histórico de 10. |

Todos los documentos permanecen pendientes de revisión humana; ninguna
contribución positiva está validada. `source_kind="official"` en diez fixtures
permite ejercitar las ponderaciones y reglas oficiales del scorer. **No acredita
procedencia oficial ni identidad real**: el título y los metadatos los identifican
como simulación. La nota de homonimia usa `source_kind="news"` y pasa por el
resolvedor de identidad real del proyecto; el resultado nominal es `candidate`
y el contexto se abstiene con `identity_status="unresolved"`.

Un cero no acredita ausencia de riesgo y 82 no significa una probabilidad del
82 %. El scorer usa una escala efectiva 0–95, presentada sobre 100. El script
emplea la fecha UTC real del motor: las cifras pueden disminuir por antigüedad
si se ejecuta después. Las fechas de publicación son fixtures fijos; no se
presentan identificadores de ejecución o marcas de tiempo como expectativas de CI.

### Cálculo de 82 y factores que realmente intervienen

En las cinco notas de `demo-alta`, la etapa tiene peso 1.0 (`conviction`), la
fuente simulada peso 0.95, la coincidencia literal del scorer peso 0.82, la
antigüedad peso 1.0 y la incertidumbre peso 1.0. Por ejemplo, crimen organizado:

```text
42 × 1.0 × 0.95 × 0.82 × 1.0 × 1.0 = 32.718 puntos del evento
```

| Evento independiente | Puntos provisionales | Score sin ese evento | Cambio al retirarlo |
|---|---:|---:|---:|
| Crimen organizado | 32.718 | 73 | 9 |
| Violencia | 32.718 | 73 | 9 |
| Lavado de dinero | 29.602 | 74 | 8 |
| Corrupción | 26.486 | 76 | 6 |
| Fraude | 23.370 | 77 | 5 |

La agregación real es:

```text
round(100 × (1 − ∏evento(1 − puntos_evento/100))) = 82
score = max(rama_provisional=82, rama_validada=0)
```

Cada nota tiene contenido y `event_id` distintos. Los deltas al retirar eventos
**no son sumables**: son simulaciones del scorer con cada grupo excluido, con
su redondeo y límites originales. Las citas exactas, selecciones y multiplicadores
de cada nota están en el snapshot, sin explicaciones inventadas por el modelo.

El clasificador auxiliar se abstuvo en las cinco notas de prioridad alta de esta
medición y sí distinguió víctima y denunciante en sus respectivos casos. Esta
limitación se conserva visible: los factores de 82 proceden de la traza del
scoring, no de una clasificación auxiliar favorable elegida para la demostración.

### Antecedente y absolución reportados

El script importa primero una acusación ficticia del 10 de octubre y genera una
evaluación de `demo-resolucion`: 10 puntos, prioridad baja provisional. Después
añade una absolución ficticia del 15 de octubre con el mismo
`event_id="demo-cedro-evento-001"`. La agrupación explícita permite aplicar la
resolución al evento correcto, con contribución provisional final cero y aviso
`resolution_review_required=true`.

No se elimina la nota anterior ni se declara acreditada la inocencia. Se guardan
dos evaluaciones y ambas se reproducen con sus entradas respectivas. Los modelos
auxiliares conservan por separado la acusación y la resolución; no establecen su
cronología por sí solos. En este ejemplo la relación entre documentos procede
del identificador de evento de la fixture, disponible para auditoría.

## Archivos para revisar el resultado

Los insumos versionados están en:

- `examples/explainability/entities.jsonl`: seis perfiles ficticios.
- `examples/explainability/documents.jsonl`: once notas ficticias, fechas, tipo de
  fuente, `event_id` y marcador de etapa de la demo.
- `examples/explainability/measured_summary_2026-10-15.json`: resumen observado,
  sin identificadores de ejecución usados como expectativas.

Dentro del directorio nuevo, `exports/` contiene:

- `summary.json`: resultados actuales, factores medidos, exclusiones, advertencias,
  hashes de las fixtures y referencia a la evaluación histórica.
- `runs/demo-alta.json` y los otros cinco archivos por entidad: snapshots completos
  entregados por `Store.get_score_run`, con la configuración, manifest de
  evidencias, identidad, análisis auxiliar y desglose real.
- `runs/demo-resolucion-before-update.json`: evaluación antes de añadir la
  resolución; conserva únicamente la nota que estaba disponible entonces.
- `notes/<document_id>.json`: texto original y metadatos, exportados una sola vez
  por documento; los manifiestos permiten llegar de cada resultado a su nota.
- `replay/*.json`: comprobaciones realizadas por `Engine.replay_score`, con
  `matched=true` en las siete evaluaciones de esta ejecución.

Para reproducir una evaluación desde la CLI, copie su identificador real desde
el resumen generado:

```sh
python -m peps --data-dir runtime/demo-explicabilidad replay IDENTIFICADOR_DE_EVALUACION
python -m peps --data-dir runtime/demo-explicabilidad explain demo-resolucion
python -m peps --data-dir runtime/demo-explicabilidad score-history demo-resolucion
```

Estos ejemplos demuestran trazabilidad, suspensión por resolución, exclusión de
homónimos y conservación histórica. No son una medición de precisión sobre noticias
reales; esa validación requiere un corpus revisado independiente.
