"""Smoke-test an installed wheel or a frozen bundle using only the stdlib.

Run with the installed environment's Python and -I. Every CLI process runs from
a fresh directory outside the checkout, without PYTHONPATH or optional NLP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
from urllib.request import Request, urlopen
from unittest.mock import patch


RESOURCES = (
    "defaults/sources.json", "defaults/news_sources.json",
    "models/context-role-es-v1.json", "licenses/CONTEXT_MODEL_CODE.txt",
    "licenses/CONTEXT_MODEL_DATA.txt", "web/index.html", "web/app.js",
    "web/import.js", "web/style.css",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate(executable=None):
    checkout = Path(__file__).resolve().parents[1]
    if executable:
        executable = executable.resolve(strict=True)
        package_dir = executable.parent / "_internal" / "peps"
        kind = "frozen"
    else:
        import peps
        package_dir = Path(peps.__file__).resolve().parent
        require(package_dir != checkout / "peps", "Se importó el checkout; instale el wheel y use python -I.")
        executable = Path(sys.executable).with_name("peps.exe" if os.name == "nt" else "peps")
        require(executable.is_file(), "No se encontró el punto de entrada peps instalado.")
        kind = "wheel"

    resources = {}
    for name in RESOURCES:
        raw = (package_dir / name).read_bytes()
        require(bool(raw), "Recurso vacío: " + name)
        resources[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    env["PYTHONUTF8"] = "1"
    commands = []
    with tempfile.TemporaryDirectory(prefix="peps-distribution-") as staging:
        cwd = Path(staging)
        data_dir = cwd / "data"

        def command(*args, parse=True):
            result = subprocess.run([str(executable), "--data-dir", str(data_dir), *map(str, args)],
                cwd=cwd, env=env, capture_output=True, encoding="utf-8", timeout=60, check=True)
            commands.append(args[0])
            return json.loads(result.stdout) if parse else result.stdout

        require("import-entities" in command("--help", parse=False), "La ayuda CLI no está disponible.")
        entities = cwd / "entities.csv"
        entities.write_text("id,name,kind\ndistribution,Entidad Sintética Aurora,company\n", encoding="utf-8")
        require(command("import-entities", entities)["imported"] == 1, "Importación CLI fallida.")
        documents = cwd / "documents.jsonl"
        documents.write_text(json.dumps({"url": "https://fixture.invalid/distribution",
            "source_kind": "official", "entity_ids": ["distribution"],
            "text": "Entidad Sintética Aurora fue acusada de fraude.",
            "published_at": "2026-10-10"}, ensure_ascii=False) + "\n", encoding="utf-8")
        command("import-documents", documents)
        require(command("score")["scored"] == 1, "Scoring CLI fallido.")
        explanation = command("explain", "distribution")
        method = explanation["result"]["provenance"]["context_method"]
        require(method["available"] and method["components"]["discriminative"]["available"],
            "El modelo incluido no pudo cargarse desde la distribución.")
        require(method["components"]["discriminative"]["model_hash"] ==
            resources["models/context-role-es-v1.json"]["sha256"], "Se cargó otro artefacto de modelo.")
        require(command("replay", explanation["run"]["id"])["matched"], "Replay no reproducible.")
        require(len(command("score-history", "distribution")) == 1, "Historial incompleto.")
        require(command("audit")["ok"], "Auditoría inválida.")
        stats = command("stats")
        require(stats["entities"] == 1 and stats["documents"] == 1, "Conteos incoherentes.")
        exported = cwd / "export.jsonl"
        command("export", exported)
        require(any(json.loads(line)["record_type"] == "document" for line in exported.read_text(
            encoding="utf-8").splitlines()), "La exportación perdió el documento.")
        backup = cwd / "backup.sqlite3"
        command("backup", backup)
        require(backup.is_file() and backup.stat().st_size > 0, "Respaldo ausente.")
        command("news-stats")
        for name in ("sources.json", "news_sources.json"):
            config = json.loads((data_dir / name).read_text(encoding="utf-8"))
            require(all(not item.get("enabled") and not item.get("terms_approved")
                for item in config["sources"]), "Las fuentes deben iniciar desactivadas.")

        # A separate installed-package run fails immediately on any DNS lookup
        # or TCP connection; the CLI subprocesses above use closed mode too.
        offline_guard = False
        if kind == "wheel":
            from peps.engine import Engine
            with patch("socket.socket.connect", side_effect=RuntimeError("Unexpected network access")), \
                    patch("socket.getaddrinfo", side_effect=RuntimeError("Unexpected DNS lookup")):
                engine = Engine(data_dir)
                engine.rescore("distribution")
                guarded = engine.explanation("distribution")
                require(engine.replay_score(guarded["run"]["id"])["matched"], "Replay offline fallido.")
                require(engine.store.verify_audit()["ok"], "Auditoría offline inválida.")
                offline_guard = True

        # Exercise the installed server and every shipped asset, never a browser.
        output = queue.Queue()
        process = subprocess.Popen([str(executable), "--data-dir", str(data_dir), "ui", "--no-browser"],
            cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")

        def read_output():
            for line in process.stdout:
                output.put(line)
            output.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            first = output.get(timeout=30)
            require(first is not None, "El servidor terminó antes de anunciar su URL.")
            match = re.search(r"http://127\.0\.0\.1:\d+", first)
            require(match is not None, "No se anunció la URL local: " + first)
            base = match[0]
            with urlopen(base + "/", timeout=10) as response:
                html = response.read().decode("utf-8")
                require(response.status == 200, "La página local no cargó.")
            token = re.search(r'<meta name="peps-token" content="([^"]+)"', html)
            require(token is not None and token[1] != "__TOKEN__", "Token de sesión ausente.")
            for name in ("app.js", "import.js", "style.css"):
                with urlopen(base + "/" + name, timeout=10) as response:
                    require(response.status == 200 and bool(response.read()), "Asset no servido: " + name)
            headers = {"X-PEPS-Token": token[1]}
            with urlopen(Request(base + "/api/stats", headers=headers), timeout=10) as response:
                require(json.load(response)["entities"] == 1, "La UI no abrió el banco de la CLI.")
            with urlopen(Request(base + "/api/shutdown", data=b"{}", headers=headers), timeout=10) as response:
                require(json.load(response)["ok"], "El cierre de la UI falló.")
            require(process.wait(timeout=10) == 0, "El servidor terminó con error.")
            commands.append("ui --no-browser")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()

    return {"ok": True, "kind": kind, "executable": str(executable), "package_dir": str(package_dir),
        "resources": resources, "commands": commands, "context_method": method,
        "network_mode": "closed; disabled sources; HTTP smoke only on 127.0.0.1",
        "offline_socket_and_dns_guard": offline_guard,
        "limits": ["Synthetic fixture; no live acquisition or predictive quality measurement.",
                   "Frozen smoke exercises the shared runtime via the console executable."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, help="PEPS-CLI.exe dentro de la carpeta portable final")
    parser.add_argument("--output", type=Path, help="Reporte JSON; use runtime/ para validaciones locales")
    args = parser.parse_args()
    result = validate(args.executable)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "kind": result["kind"], "commands": len(result["commands"]),
        "resources": len(result["resources"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
