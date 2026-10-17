# Pipeline masivo real · WebScope 2.9.5

Abra [index.html](index.html) desde la carpeta extraída para consultar el informe interactivo. Los CSV y [summary.json](summary.json) contienen mediciones reales y estados documentales, sin cuerpos ni puntuaciones individuales.

| Resultado final | Conteo |
|---|---:|
| Personas reales | 300 |
| Consultadas por red | 300 |
| Con al menos una consulta completada | 300 |
| Con texto original | 152 |
| Artículos con cuerpo conservado | 401 |
| Scores procesados | 300 |

Entrada: 300 personas públicas, con 150 notarios y 150 registros de directorios municipales. Los cargos son pistas de entrada según sus fuentes y fechas, sin certificación de vigencia individual. Los 60 notarios usados en correcciones anteriores son desarrollo; los comparadores sin notas no son negativos certificados. Consulte [la procedencia](roster-provenance.json).

La adquisición fría tomó 35.00 minutos. Los tiempos de cada reanudación y de la ampliación se muestran separados. La suma medida del banco base y del puente, incluidos caché e informes, es 44.75 minutos; excluye los intervalos para corregir código y el reprocesamiento retrospectivo posterior, informado aparte. La duración de una lectura local no se presenta como una nueva búsqueda fría.

La recuperación de referencias conocidas sigue siendo baja y no se presenta como cobertura equivalente a un buscador general. La precisión del detector requiere leerse junto con recall, abstenciones, estados de identidad y sus denominadores. Ningún cero se interpreta como persona sin incidentes.

Los intentos originales, sus fallos, bloqueos y cobertura parcial permanecen en el informe. Las correcciones posteriores al desbloqueo de etiquetas son desarrollo retrospectivo.

Suplementos públicos:

- [evaluated-nlp-1.0.4.py.txt](evaluated-nlp-1.0.4.py.txt)
- [observed-fixes.json](observed-fixes.json)
- [observed-fixes.md](observed-fixes.md)
- [post-audit-processing-summary.json](post-audit-processing-summary.json)
- [publisher-bridge-report.md](publisher-bridge-report.md)
- [publisher-bridge-summary.json](publisher-bridge-summary.json)
- [roster-provenance.csv](roster-provenance.csv)
- [roster-provenance.json](roster-provenance.json)
- [post-audit-comparison.json](post-audit-comparison.json)
- [post-audit-comparison.md](post-audit-comparison.md)
- [quality-metrics.json](quality-metrics.json)
- [quality-report.md](quality-report.md)
- [reference-recall.json](reference-recall.json)
