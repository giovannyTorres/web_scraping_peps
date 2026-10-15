# WebScope

Nombre de la herramienta: **WebScope**. Subtítulo: **Web scraping y análisis
documental**. La versión 2.3.1 actualiza la identidad visual y conserva el flujo
guiado de adquisición, análisis y consulta.

## Color y tipografía

La paleta usa los azules y el acento aqua solicitados, inspirados en BBVA:

| Uso | Color |
| --- | --- |
| Navegación y títulos | `#072146` |
| Acción principal | `#004481` |
| Interacción y enlaces | `#1464A5` |
| Acentos y progreso | `#2DCCCD` |
| Superficies azules claras | `#D4EDFC` |
| Fondo neutro | `#F4F4F4` |

**Lato** se utiliza en texto, tablas y controles, con archivos regular y negrita.
**Source Serif 4** se utiliza en títulos, con peso variable de 200 a 900. Ambas
familias se cargan desde `peps/web/fonts/`, se incluyen en el wheel y el portable
y funcionan sin conexiones a servicios de tipografía.

Las fuentes proceden del repositorio público Google Fonts, fijado en el commit
`1d0e46c7933245c8180fe83e47917961c913bbdb`. Se conservaron glifos y nombres al
convertir los archivos TrueType a WOFF2. Las licencias OFL y la procedencia con
hashes están en `peps/licenses/LATO_OFL.txt`, `SOURCE_SERIF_4_OFL.txt` y
`FONT_SOURCES.txt`.

## Nombre y compatibilidad

El portable se distribuye como `WebScope`, con `WebScope.exe` y
`WebScope-CLI.exe`. La interfaz, la ayuda, las exportaciones y el mensaje de cierre
usan el nuevo nombre. El comando instalado `webscope` está disponible junto al
alias existente `peps`.

El módulo Python `peps`, el nombre técnico del paquete `peps-mx`, los identificadores
internos de sesión y la ubicación de datos se conservan para mantener los bancos
y automatizaciones existentes. En Windows se sigue abriendo
`%LOCALAPPDATA%\PEPS-MX` si no se proporciona `--data-dir`.

## Comprobaciones

La suite local mantuvo sus 400 casos: 393 correctos y siete opcionales del modelo
spaCy omitidos. Chromium aprobó 122 comprobaciones: 71 de flujo e identidad visual,
32 de importación y 19 de explicabilidad. Verificó carga real de las tres fuentes,
familias computadas en texto y títulos, colores renderizados, ausencia de
solicitudes externas y presentación en escritorio/móvil. La [guía del flujo](FLUJO_GUIADO.md)
incluye las capturas actualizadas.

El wheel instalado en un entorno limpio pasó 14 comandos y 16 recursos: ambos
alias, tres archivos WOFF2, sus licencias, modelo y recursos web. Las respuestas
de fuentes devolvieron `font/woff2` con bytes idénticos a los incluidos en el paquete.
El [comprobante JSON](benchmarks/webscope_identity_2_3_1.json) conserva hashes y
resultados. La CI mantiene las comprobaciones de Linux/Windows y del portable.
