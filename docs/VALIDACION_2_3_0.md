# Validación del flujo guiado 2.3.0

La versión separa carga, preparación de fuentes, adquisición, análisis y consulta.
Importar ya no calcula puntuaciones. Los pasos muestran su estado y avance; el
servidor rechaza acciones incompatibles aunque se soliciten desde otra pestaña o
directamente por API. La [guía del usuario](FLUJO_GUIADO.md) describe el recorrido.

## Evidencia local

Comprobaciones del 15 de octubre de 2026 en Linux con Python 3.12.14 y Chromium.
El [comprobante JSON](benchmarks/guided_workflow_2_3_0.json) conserva resultados,
condiciones y hashes del código probado.

| Comprobación | Resultado |
| --- | --- |
| Suite completa | 400 pruebas: 393 correctas y 7 opcionales de spaCy omitidas; 26.539 s. |
| Flujo guiado, Chromium | 63 comprobaciones: cinco pasos, seis barras, datos sin autoscore, permisos, operaciones rápidas y bloqueos. |
| Importación, Chromium | 32 comprobaciones: se conservan codificaciones, mapeo, duplicados, carreras y flujo explícito posterior. |
| Explicabilidad, Chromium | 19 comprobaciones: citas, historial, replay, revisión obsoleta y selección de identidad vacía. |
| Experiencia visual | Escritorio y móvil sin desbordamiento; navegación con teclado y paso actual accesible. |
| Adquisición controlada | Pausa espera al request en curso; reanudar conserva la cola; doble clic, segunda pestaña y recarga no inician otro trabajo. |
| Estimación visible | Fixture de diez fragmentos: tres unidades terminadas después de al menos 3.2 s producen una ETA calculada y visible. |
| Wheel instalado | Entorno limpio, cero dependencias runtime, 13 comandos, 10 recursos y 29 archivos idénticos al código fuente. |
| Carga local | 1,000 entidades/documentos y 1,000 tareas; scoring, cola y auditoría correctos. |

Las pruebas de navegador usan únicamente nombres, fuentes y documentos ficticios.
No hubo solicitudes externas ni errores de página. La adquisición se controla
con eventos para observar estados intermedios de verdad; no se reproducen noticias
reales ni se mide cobertura web.

## Controles que se verifican

- No hay scoring automático al importar, abrir una entidad o consultar progreso.
- No se ofrecen resultados actuales si la adquisición está activa, pausada con
  pendientes o si el análisis no corresponde a los datos vigentes. Los snapshots
  históricos se pueden leer explícitamente sin recalcularlos.
- Importación, cambios de fuentes, cálculo, revisión y exportación se bloquean
  cuando corresponde. Los rechazos de estado usan HTTP 409; los permisos JSON
  malformados siguen usando 400.
- Una petición de pausa identifica su operación; una petición obsoleta no puede
  detener la siguiente. La pausa no libera controles antes de salir del worker.
- Los pasos se vinculan a revisiones del banco, fuentes, corpus y revisiones
  humanas. Descubrimientos o cambios externos durante el análisis invalidan su
  finalización. Un lease expirado permite reanudar, pero no dar por completo el
  trabajo pendiente.
- Las unidades se reportan después de guardar sus transacciones. El total de
  adquisición permanece desconocido durante descubrimiento o backoff; pasa a ser
  conocido cuando el trabajo restante se puede contar. Se contemplan commits de
  otros workers aún no reportados para mantener estable el denominador.
- La ETA necesita al menos tres unidades y tres segundos observados en la fase.
  Los fallos terminales no aumentan el contador de éxitos. Pausa y fallo no se
  presentan como 100 % completado.

## Reproducir y revisar

```powershell
python -m unittest discover -s tests -v
python scripts/test_workflow_ui.py
python scripts/test_import_ui.py
python scripts/test_explainability_ui.py
```

Los scripts de navegador requieren Playwright 1.62.0 y Chromium. Guardan reportes
y capturas en `runtime/`; no reemplazan benchmarks anteriores. La CI ejecuta los
tres recorridos, además de la matriz Windows/Linux, wheel instalado, NLP opcional
y portable Windows. Sus capturas e informes se conservan como artefactos.

La estimación de tiempo es orientativa: el tamaño de documentos, latencia y
restricciones de fuentes pueden cambiar el ritmo. No se convierte el porcentaje
de entidades procesadas en una probabilidad de riesgo ni el fin de un lote en una
promesa de cobertura de internet.
