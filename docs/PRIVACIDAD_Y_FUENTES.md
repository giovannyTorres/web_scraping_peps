# Privacidad, fuentes y límites de interpretación

Documento técnico de operación, consultado al 15 de octubre de 2026. El proyecto no ofrece una certificación de cumplimiento legal; la organización responsable debe definir finalidad, base de tratamiento, acceso, conservación y régimen aplicable antes de utilizarlo con datos reales.

## Finalidad y tratamiento

La [LFPDPPP vigente](https://www.diputados.gob.mx/LeyesBiblio/pdf/LFPDPPP.pdf) fue expedida el 20 de marzo de 2025 y muestra reforma de 14 de noviembre de 2025. Sus artículos 5 a 15 regulan principios, excepciones al consentimiento, calidad, finalidad, proporcionalidad e información. La disponibilidad pública de datos no elimina las demás obligaciones del responsable.

Como diseño de operación se recomienda: documentar qué decisión documental se pretende apoyar; registrar procedencia y alcance; limitar campos al mínimo necesario; definir roles y permisos; atender solicitudes de rectificación y supresión; fijar conservación por tipo de registro; y revisar periódicamente noticias rectificadas, sanciones revocadas y resoluciones favorables. Estas recomendaciones son de diseño y requieren concretarse en políticas de la organización.

La versión actual tiene revisión humana y respaldos, pero no incluye administración multiusuario, cifrado de base de datos, un portal de derechos ARCO ni borrado automático por plazos. La interfaz en loopback y su token por sesión no sustituyen seguridad del sistema operativo. Restrinja el acceso al directorio local, exports, logs y respaldos. Evite datos sensibles incidentales, domicilios particulares y perfiles privados cuando no resulten necesarios para la finalidad definida.

## PEP, familia y reputación

La [guía PEP del GAFI](https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Peps-r12-r22.html) trata controles preventivos; ser PEP no acredita actividad criminal. El proyecto separa exposición política de señales de hechos. Un cargo, partido, declaración patrimonial o contrato no debe aumentar por sí solo la puntuación adversa.

Los parentescos o asociaciones requieren procedencia y contexto. Una acusación dirigida a un tío, primo o socio no se traslada al sujeto analizado. El motor intenta excluir referencias familiares, víctimas, denunciantes y menciones contextuales; las reglas son incompletas y requieren revisión. No reconstruya redes familiares privadas a partir de rumores ni trate una coincidencia de apellidos como parentesco probado.

La [reforma LFPIORPI de 16 de julio de 2025](https://sidof.segob.gob.mx/notas/docFuente/5763161) y la [reforma de su Reglamento de 27 de marzo de 2026](https://sidof.segob.gob.mx/notas/docFuente/5783547) contienen disposiciones PEP. La consulta de la lista UIF y las reservas no equivalen a un padrón nominativo abierto para cualquier usuario. La [publicación UIF sobre cargos PEP nacionales](https://www.gob.mx/uif/documentos/uif-marco-juridico-personas-politicamente-expuestas-nacionales-337833) es una lista enunciativa de cargos y criterios; contraste nombres y periodos con directorios y documentos públicos legítimos.

## Scoring e identidad

El resultado representa prioridad de revisión documental, no probabilidad de culpa, solvencia, lavado de dinero ni incumplimiento futuro. Sus pesos y umbrales son decisiones heurísticas de implementación. Las pruebas de regresión no los convierten en un modelo predictivo calibrado. No utilice la salida como decisión automática de empleo, crédito, admisión bancaria o prestación de servicios.

Revise por separado coincidencia de identidad, naturaleza del documento, papel del sujeto, estado procesal, fecha y alcance. Una detención no es condena; una denuncia no acredita su contenido; una sanción administrativa no es sentencia penal. Un comentario público puede servir como pista, pero no como corroboración de identidad o culpabilidad. Una nota sobre absolución o rectificación debe considerarse junto con el evento anterior.

El sistema conserva citas y offsets, hashes, reglas, exclusiones, contribuciones y revisión humana. Un documento duplicado no constituye corroboración independiente; dos dominios pueden republicar la misma fuente. Los IDs de evento y una resolución de identidad documentada necesitan revisión para impedir acumulaciones engañosas.

Un resultado cero sólo describe las reglas y documentos disponibles. Puede deberse a falta de cobertura, homónimos, negación, contenido inaccesible, fecha desconocida o una limitación del motor. Nunca debe mostrarse como certificado de ausencia de riesgo.

## Fuentes primarias y estados que deben conservarse

| Fuente | Uso | Diferencias relevantes |
|---|---|---|
| [SAT: consulta 69-B](https://wwwmat.sat.gob.mx/consultas/76674/consulta-la-relacion-de-contribuyentes-con-operaciones-presuntamente-inexistentes) | Descarga e importación local de registros fiscales públicos. | Presunto, definitivo, desvirtuado y sentencia favorable son estados distintos; no sustituirlos por una etiqueta de delito. |
| [PDN: especificación S3 oficial](https://github.com/PDNMX/api_docs/blob/master/S3/oas/OAS_API_Particulares_Sancionados.json) | Contrato técnico para particulares sancionados. | La documentación no prueba acceso anónimo a una API operativa; contempla seguridad y respuesta 401. No hay conector autenticado PDN implementado. |
| [ONU: lista consolidada](https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list) | Snapshot XML con referencias de listas. | Conservar régimen, ID, fecha de inclusión y modificaciones; la inclusión no es condena penal. |
| [OFAC: Sanctions List Service](https://sanctionslist.ofac.treas.gov/Home/ConsolidatedList) | Archivos oficiales y esquemas para listas no-SDN. | Conservar programa y distinguir SDN/no-SDN; las medidas de todos los programas no son idénticas. |
| [OFAC: especificaciones XML](https://ofac.treasury.gov/sdn-list-data-formats-data-schemas/frequently-asked-questions-on-advanced-sanctions-list-standard) | Verificación de formatos. | El adaptador del proyecto admite XML clásico; falla ante otros esquemas. |
| [OFAC: archivo de cambios](https://sanctionslist.ofac.treas.gov/Home/DeltaFileArchive) | Referencia para altas, bajas y modificaciones. | La sincronización incremental automática de bajas aún no está implementada. |
| [GDELT DOC API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) | Descubrimiento de noticias. | Ventana, límites y selección de resultados pueden reducir cobertura; una URL o snippet no es cuerpo verificado. |
| [GDELT Context](https://blog.gdeltproject.org/announcing-the-gdelt-context-2-0-api/) | Referencia metodológica para contexto. | Coincidencia en un artículo largo no acredita que el término negativo describa al sujeto. |

Las fuentes originales de noticias, ONG o comentarios requieren sus propios términos de acceso y reutilización. Almacene únicamente lo necesario para la finalidad y licencia definidas. Una autodeclaración corporativa es fuente primaria sobre lo que publicó la compañía; no es corroboración independiente de todas sus afirmaciones.

## Acceso responsable

El [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html) define robots y su manejo; no concede autorización jurídica. El [RFC 6585](https://www.rfc-editor.org/rfc/rfc6585.html) describe HTTP 429 y `Retry-After`. El recolector usa límites compartidos y esperas persistentes; los bloqueos se registran para revisión.

La aplicación no incorpora rotación de IP, suplantación de navegadores, resolución automatizada de CAPTCHA, acceso a contenido privado ni evasión de muros de pago. Una fuente bloqueada conserva estado de cobertura incompleta. La configuración de términos aprobados es responsabilidad del operador y no neutraliza un bloqueo técnico del editor.

## Español y pruebas

El runtime incluido usa reglas y un gazetteer local, sin Hugging Face ni modelos remotos. Conserva offsets de nombres, fechas y patrones procesales en español. No ofrece NER universal ni una comprensión impecable del español mexicano; homónimos, sarcasmo, correferencias y relaciones complejas pueden fallar.

Como evolución opcional, [spaCy español](https://spacy.io/models/es) y sus [metadatos oficiales del modelo 3.8.0](https://github.com/explosion/spacy-models/blob/master/meta/es_core_news_sm-3.8.0.json) permiten estudiar NER local distribuido por GitHub. Ese modelo no está incluido: sus condiciones de distribución, incluida la licencia GNU GPL 3.0 indicada para el modelo, deben revisarse separadamente.

Distinga tres evidencias de validación: regresiones con ejemplos sintéticos; desempeño de importación/análisis local; y pruebas de recuperación web con fuentes reales. Ninguna sustituye un corpus representativo etiquetado independientemente para medir errores de identidad, atribución y estado procesal. El muestreo mexicano de prueba usa extractos curados y un control histórico de sentencia; no constituye investigación exhaustiva de las entidades seleccionadas.

Los XML del repositorio original fueron generados entre enero y mayo de 2026. Los documentos de Genaro García Luna describen una sentencia comunicada por [DOJ en octubre de 2024](https://www.justice.gov/es/usao-edny/pr/ex-secretario-de-seguridad-publica-mexicana-genaro-garcia-luna-condenado-mas-de-38); no se verificó aquí firmeza, apelación ni custodia actual. Conserve explícitamente esa diferencia entre hecho histórico y estado actual.
