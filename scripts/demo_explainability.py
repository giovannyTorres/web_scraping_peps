#!/usr/bin/env python3
"""Build an isolated, offline explanation demo from explicitly invented fixtures."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.engine import Engine

FIXTURES = ROOT / "examples" / "explainability"
RUNTIME = ROOT / "runtime"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _empty_demo_directory(value: Path) -> Path:
    path = value.resolve()
    root = RUNTIME.resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("La demo requiere un subdirectorio dentro de runtime/; no se modifican otros bancos.")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ValueError("El directorio de demo debe ser nuevo o estar vacío; no se borra ni sobrescribe un banco existente.")
    else:
        path.mkdir(parents=True, exist_ok=False)
    return path


def run_demo(data_dir: Path, mode: str = "discriminative") -> dict:
    """Import, score, archive and replay using the same local APIs as the app.

    Publication dates are fixed fixtures; Engine uses the actual UTC evaluation
    date. Scores can age naturally, so no run ID/timestamp is an expected value.
    """
    if mode not in {"discriminative", "advanced", "enriched", "hybrid"}:
        raise ValueError("Modo contextual inválido")
    entities = _rows(FIXTURES / "entities.jsonl")
    documents = _rows(FIXTURES / "documents.jsonl")
    for document in documents:
        if (not document.get("metadata", {}).get("synthetic")
                or not (urlsplit(document["url"]).hostname or "").endswith(".invalid")):
            raise ValueError("La demo solo admite sus fixtures sintéticos con dominios .invalid.")
    data_dir = _empty_demo_directory(data_dir)
    config_path = data_dir / "sources.json"
    _write(config_path, {"sources": [], "context_analysis": {"mode": mode}, "synthetic_demo": True})
    engine = Engine(data_dir, config_path=config_path, online=False)
    engine.store.import_entities(entities)
    initial = [row for row in documents if row["metadata"]["demo_stage"] == "initial"]
    updates = [row for row in documents if row["metadata"]["demo_stage"] == "resolution"]
    imported = engine.import_documents(initial)
    engine.rescore()
    before = engine.explanation("demo-resolucion")
    before_id = before["run"]["id"]
    update_result = engine.import_documents(updates)
    engine.rescore("demo-resolucion")
    export_dir = data_dir / "exports"

    def archive(entity_id: str, filename: str, run_id: str | None = None) -> dict:
        response = engine.explanation(entity_id, run_id)
        selected_id = response["run"]["id"]
        snapshot = engine.store.get_score_run(selected_id, entity_id)
        replay = engine.replay_score(selected_id)
        if not replay["matched"]:
            raise RuntimeError(f"No pudo reproducirse la evaluación {entity_id}: {replay}")
        _write(export_dir / "runs" / filename, snapshot)
        _write(export_dir / "replay" / filename, replay)
        for entry in snapshot["evidence_manifest"]:
            note = engine.store.run_evidence(selected_id, entry["id"])
            note_path = export_dir / "notes" / (note["document_id"] + ".json")
            if not note_path.exists():
                # Original text is exported once per document, shared by runs.
                _write(note_path, {"synthetic": True, **{key: note.get(key) for key in
                    ("document_id", "url", "text", "content_hash", "source_id", "source_kind", "published_at", "metadata")}})
        result = response["result"]
        explanation = response["explanation"]
        return {"entity_id": entity_id, "name": response["entity"]["name"], "score": result["score"],
            "label": result["label"], "as_of": result["as_of"],
            "provisional_score": result["provisional_score"], "validated_score": result["validated_score"],
            "review_required": result["review_required"], "evidence_count": response["evidence_count"],
            "run_id": selected_id, "snapshot_file": "runs/" + filename,
            "replay_matched": replay["matched"],
            "factors": [{key: factor[key] for key in ("event_group", "title", "provisional_points",
                "validated_points", "score_without_event", "score_delta_when_removed", "resolution", "resolution_review_required")}
                for factor in explanation["factors"]],
            "auxiliary_roles": [{"evidence_id": item["evidence_id"], "role": item["analysis"]["role"],
                "abstained": item["analysis"]["abstained"], "identity_status": item["analysis"].get("identity_status")}
                for item in result.get("contextual_analysis", [])],
            "exclusions": explanation["exclusions"], "warnings": explanation["warnings"]}

    historical = archive("demo-resolucion", "demo-resolucion-before-update.json", before_id)
    current = [archive(entity["id"], entity["id"] + ".json") for entity in entities]
    summary = {"synthetic": True,
        "notice": "Entidades, sucesos, documentos y URLs completamente inventados; no describen personas reales ni fuentes oficiales verificadas.",
        "created_at": datetime.now(timezone.utc).isoformat(), "data_dir": str(data_dir),
        "context_mode": mode, "network_acquisition_enabled": False,
        "fixture_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (FIXTURES / "entities.jsonl", FIXTURES / "documents.jsonl")},
        "imported": {"initial": imported, "updates": update_result},
        "current": current, "historical_before_resolution": historical,
        "score_interpretation": "Prioridad heurística documental, no probabilidad ni culpabilidad. Los deltas al retirar eventos no se suman.",
        "date_policy": "La fecha real UTC del Engine determina el envejecimiento; no se fijan resultados esperados por run_id ni timestamp."}
    _write(export_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=RUNTIME / ("demo-explainability-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")))
    parser.add_argument("--mode", choices=("discriminative", "advanced", "enriched", "hybrid"), default="discriminative")
    parser.add_argument("--ui", action="store_true", help="Abre la interfaz existente del proyecto en loopback, con este banco de demo.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        summary = run_demo(args.data_dir, args.mode)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Demo no generada: {exc}", file=sys.stderr)
        return 1
    print(summary["notice"])
    print("Banco de demo: " + summary["data_dir"])
    print("Explicaciones y fuentes: " + str(Path(summary["data_dir"]) / "exports" / "summary.json"))
    for item in summary["current"]:
        print(f"{item['entity_id']}: {item['score']}/100 · {item['label']} · replay={item['replay_matched']}")
    previous = summary["historical_before_resolution"]
    print(f"Antecedente histórico de demo-resolucion: {previous['score']}/100; se conserva antes de importar la absolución ficticia.")
    if args.ui:
        from peps.app import serve
        serve(Engine(summary["data_dir"], online=False), open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
