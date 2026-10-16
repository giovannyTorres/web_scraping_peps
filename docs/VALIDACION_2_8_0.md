# Validación pública de WebScope 2.8.0

Se ejecutaron tres sesiones de red con **Enrique Peña Nieto, Pemex, Odebrecht,
AHMSA, Grupo Bimbo, FEMSA, Cemex y OHL México**, en un banco aislado de ejemplos
públicos. Se usaron el transporte, SQLite, extracción, atribución y cálculo de
producción. El observador sólo registró y acotó HTTP: no sustituyó respuestas,
relojes ni textos. No se emplearon datos internos ni se confirmaron identidades.

## Resultado medido

| Sesión real | Período y recorrido | Adquisición | Candidatos únicos | Cuerpos | Entidades con cuerpo |
| --- | --- | ---: | ---: | ---: | ---: |
| Primera, con arreglos iniciales | 2014–2022, índices compartidos | 119.02 s | 123 | 8 | 5/8 |
| Diagnóstico dirigido | 2022, ventanas mensuales automáticas anteriores | 119.72 s | 26 | 7 | 3/8 |
| Final, código estable | 2022, primer barrido anual dirigido | **118.57 s** | **48** | **19** | **8/8** |

Las fuentes fueron Contralínea, Aristegui Noticias y Desinformémonos. La primera
sesión leyó 49 páginas de índice, contrastó 4,900 registros y excluyó 4,777 sin
menciones pertinentes. La sesión final consultó los ocho nombres y excluyó 145
registros. Atribuir sus 48 candidatos tomó **0.93 s** y calcular las ocho
entidades **0.38 s**, además del tiempo de adquisición.

Cada sesión tuvo un presupuesto de 120 s y 60 intentos HTTP observados. La final
obtuvo 52 respuestas 200, siete 404 y un timeout de 15 s. Empezó con una copia
de la caché HTTP real y del estado de cortesía del diagnóstico anterior: seis
lecturas de contenido usaron caché. Los 19 cuerpos proceden de páginas públicas
reales, incluidas esas lecturas; no representan 19 solicitudes nuevas.

Estas muestras tienen distintos recorridos y fechas. **La tabla no estima
precisión, recall ni una aceleración causal comparable entre corpus.** Los
límites RSS, los 404 y las tareas pendientes mantienen la cobertura parcial.
Los 19 cuerpos incluyen notas neutras; no todos contienen conductas puntuables.

## Errores encontrados y corregidos

1. **Empresas omitidas.** El filtro anterior exigía contexto adicional a nombres
   como Pemex y Odebrecht. Con los mismos 4,900 metadatos reales sólo retenía
   menciones de Peña y AHMSA. El filtro corregido recupera candidatos de siete
   entidades, sin modificar el resolver de identidad. Búsqueda y filtrado usan
   nombres comerciales y núcleos sin `S.A.B. de C.V.`, conservan la razón social
   e incluyen `business_names`. Los alias personales siguen siendo contextuales.
2. **Explosión de consultas.** Para los ocho ejemplos, tres archivos y un año,
   la planificación pasó de **648 consultas mensuales a 54 anuales**. El modo
   rápido automático empieza por años; la selección mensual explícita sigue
   disponible. El filtro local comprueba las fechas exactas, incluido el final
   exclusivo, aunque un editor ignore parte del filtro solicitado.
3. **Cuerpos poco útiles primero.** La cuota podía agotarse con vídeos, resúmenes
   internacionales o menciones incidentales. Ahora prioriza información procesal
   cercana al nombre y ordena el rescate de candidatos guardados. Una reserva
   sólo puede sustituirse si sigue en cola y no tiene intentos. No se desplazan
   descargas activas ni se adelanta `Retry-After`. Negaciones, absoluciones y
   noticias de víctimas conservan prioridad y metadatos; no generan puntos por
   tener prioridad de lectura.
4. **Tarjetas laterales en lugar del artículo.** En el mismo HTML real sobre
   expresidentes, el extractor anterior elegía 267 caracteres de una tarjeta
   ajena y ninguna mención completa a Peña. El extractor actual conserva el
   cuerpo editorial, sus párrafos y la mención, excluyendo aquella tarjeta.
5. **Contaminación por enlaces y titulares.** Una nota sobre jubilación de Pemex
   incluía un enlace a otra denuncia; un resumen internacional mezclaba titulares
   de países distintos en una sola frase. La reextracción del mismo HTML elimina
   esos candidatos falsos, conserva párrafos y no elimina la mención real de Peña
   ni el control neutral de Cemex. También se corrigió NLP para analizar líneas
   sin punto final conservando los offsets: separar párrafos no debe perderlas.
6. **Proxy y pausas.** El transporte ignoraba el proxy heredado del entorno. Ahora
   usa CONNECT y TLS verificado, sin publicar credenciales. Un corte de sesión
   no consume intentos ni penaliza al host; un fallo transitorio de red sí aplica
   su espera. Los 404/410 reales requieren revisar la URL, en lugar de repetirla
   automáticamente o presentarla como una búsqueda vacía satisfactoria.
7. **Evidencia antigua aún activa.** Cambiar el cuerpo de una URL dejaba sus
   vínculos anteriores en el cálculo. Ahora se retiran al historial cuando ninguna
   noticia actual comparte el documento anterior. Los cuerpos, revisiones y
   evaluaciones históricas se conservan; una revisión no se copia a offsets nuevos.
8. **Desconexión del navegador.** Cancelar una consulta al navegar podía imprimir
   un `BrokenPipeError`. El servidor cierra esa respuesta sin interrumpir la
   operación que continúa registrada.

## Aplicación a bancos existentes

Al ejecutar **Analizar y calcular**, antes de atribuir se revisan las capturas
locales de los extractores anteriores conocidos. No se necesita otra descarga.
Se validan la ruta dentro de `news_snapshots`, el hash y la descompresión acotada;
se mantienen las fechas originales. Los recibos evitan reprocesar un texto sin
cambios. La deduplicación distingue cambios de párrafos aunque las palabras
normalizadas coincidan.

En una copia aislada del primer banco real se actualizaron **los ocho cuerpos en
0.97 s**, incluida la atribución. Se retiraron diez vínculos anteriores, se
conservó el prefijo completo de auditoría y todos los documentos originales. La
segunda pasada encontró cero capturas pendientes. El banco original quedó idéntico.
Una captura ausente, inválida o sin texto editorial suficiente deja la noticia
como texto insuficiente y conserva su historia para volver a adquirirla.

## Interpretación del scoring

Los ocho casos finales quedan en **identidad pendiente de corroboración**, con
`display_score=null`; la interfaz debe mostrar **—**. No son ocho resultados
evaluados de riesgo cero. Las consultas encuentran candidatos y el cálculo
respeta la revisión de identidad. La investigación periodística no acredita una
condena. No se confirmaron personas o empresas automáticamente para conseguir
puntuaciones positivas.

Se reprodujeron las ocho evaluaciones generadas con el código final y se verificó
la auditoría. NLP cambia a `es-gazetteer-rules-1.0.1`: los snapshots de cálculo
anteriores permanecen archivados y su reproducción con código diferente devuelve
`unsupported`, sin reinterpretar silenciosamente la historia.

## Regresión y rendimiento local

| Comprobación | Resultado |
| --- | --- |
| Suite completa | 616 pruebas: 609 correctas, 7 opcionales de spaCy omitidas |
| Navegador local: flujo, descarga, recuperación y fuentes compartidas | 170 comprobaciones correctas |
| Actualización desde wheel auténtico 2.3.1 | 12 comprobaciones correctas |
| Wheel instalado sin dependencias | 15 comandos, 16 recursos, 40 archivos idénticos al código validado |
| Índice sintético de 300 entidades, reloj y SQLite reales | 74 condiciones correctas; 10.51 s sin caché y 0.21–0.42 s con caché |

El benchmark de 300 utiliza tres páginas HTTP **sintéticas**, prohíbe sockets y
respeta intervalos reales de cinco segundos. Mide procesamiento local y cola;
no demuestra cobertura web completa de 300 nombres en ese tiempo. Los resultados
de red anteriores sí proceden de editores públicos. El ranking sigue siendo
léxico, puede repetir asuntos y no garantiza diversidad, veracidad ni exhaustividad.

También se comprobó el transporte sintético con 1,000 URLs: 7.44 s en frío y
0.95 s con caché, ambas fases completas y sin trabajos pendientes. Se corrigió
su reloj de prueba y su verificador: antes podía declarar éxito dejando tareas
en cola. Es una corrección del benchmark, no una aceleración de red productiva.

La [evidencia técnica](benchmarks/real_news_2_8_0.json) incluye URLs, estados,
tiempos, hashes, metodología y limitaciones, sin publicar cuerpos de artículos.

```powershell
python -m unittest discover -s tests -q
python scripts/validate_real_news.py --online --data-dir runtime/prueba-publica-aislada --output runtime/prueba-publica.json
python scripts/benchmark_shared_discovery.py --output runtime/benchmark-300.json
```

La prueba pública requiere el checkout Git para comparar el filtro de 2.7 y
fuentes accesibles. Sus permisos se habilitan sólo en el banco aislado de pruebas;
no cambia los permisos de una instalación existente. La caché inicial usada en
la sesión final se identifica expresamente en su evidencia.
