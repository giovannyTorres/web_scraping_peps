"""Ship the offline extractor's configuration, language data and notices.

No model or data download belongs to extraction. Dateparser loads language
modules dynamically; Babel, jusText and tld read package resources. The source
of the unmodified MPL-covered tld/certifi packages is included alongside the
binary bundle so users receive those covered source files and data directly.
"""
import importlib.util
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


datas = []
for package in ("trafilatura", "justext", "babel", "dateparser", "htmldate", "pytz", "tld", "certifi"):
    datas += collect_data_files(package, include_py_files=package in ("tld", "certifi"))

# Version/runtime checks and complete third-party LICENSE/NOTICE files remain
# usable in the frozen program. The recursive walk applies platform markers.
datas += copy_metadata("trafilatura", recursive=True)
# The extra lxml[html_clean] may be reached after lxml was already visited by
# a metadata walker. Keep this required extra's distribution notice explicitly.
datas += copy_metadata("lxml_html_clean")
hiddenimports = collect_submodules("dateparser.data")
hiddenimports += ["lxml.html.clean", "lxml_html_clean"]

if importlib.util.find_spec("tzdata") is not None:
    datas += collect_data_files("tzdata")
    datas += copy_metadata("tzdata")
    hiddenimports += collect_submodules("tzdata")
elif sys.platform == "win32":
    raise RuntimeError("El paquete Windows requiere tzdata del lock de extracción.")
