# Lista pública mexicana para comprobar WebScope

El [CSV importable](personas_publicas_mexico_300.csv) y el [JSON con procedencia](personas_publicas_mexico_300.json) contienen 300 personas distintas: 150 presidentes municipales y 150 notarios. El CSV conserva `id`, `name`, `kind`, `aliases` y `context`; los campos de contexto incluyen cargo, ubicación, fuente, hash y fecha de consulta. No contiene puntuaciones, acusaciones ni datos personales de contacto.

La muestra es deliberada y no representa estadísticamente a México. Incluye 51 registros de Nuevo León, 99 de Jalisco y 150 del directorio profesional de notarías de la Ciudad de México. No certifica que las personas continúen en el cargo o que una habilitación profesional esté vigente.

| Grupo | Fuente pública | Referencia temporal |
| --- | --- | --- |
| 51 presidencias municipales de Nuevo León | [Directorio oficial de alcaldías](https://www.nl.gob.mx/es/publicaciones/directorio-de-alcaldias-en-nuevo-leon) | Corte del 1 de octubre de 2026; no publica un periodo individual |
| 99 presidencias municipales de Jalisco | [IIEG, resultados de presidencias municipales](https://iieg.gob.mx/ns/wp-content/uploads/2024/11/PresidentesMunicipalesCoaliciones2Nov24.pdf) | Periodo 2024–2027 según fuente con corte del 11 de noviembre de 2024 |
| 150 notarios de Ciudad de México | [Colegio de Notarios de la Ciudad de México](https://colegiodenotarios.org.mx/directorio) | Consultado el 16 de octubre de 2026; sin fecha de actualización publicada |

Los PDF oficiales se leyeron localmente; la tabla raster de Jalisco se extrajo con OCR y se cotejó visualmente. Del directorio de notarías se excluyeron vacantes y nombres abreviados. Se verificaron los 300 IDs y nombres normalizados distintos y la conservación de contexto al importar el CSV.

```bash
python -m peps --data-dir runtime/prueba-300 import-entities examples/personas_publicas_mexico_300.csv
python scripts/validate_real_people.py --online --roster examples/personas_publicas_mexico_300.json --data-dir runtime/prueba-300-red --output runtime/prueba-300-red.json
```

La validación requiere un banco vacío. Ejecuta el transporte, parsers, atribución y cálculo de producción con reloj real; no importa artículos sintéticos. Conserva las pausas de proveedores y comunica por separado qué personas se consultaron realmente y qué consultas finalizaron. Los extractos no se convierten en evidencia ni una coincidencia nominal confirma identidad. Una cobertura incompleta produce salida `2`; un fallo de integridad, salida `1`.
