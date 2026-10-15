#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline Laya ONNX experiment; no Hugging Face software, downloads or inference.

Explicit preparation downloads a pinned, checksum-verified GitHub release. Normal
execution has no network code path. Research use only: the English checkpoint is
not calibrated for Spanish risk assessment and does not explain the legacy score.

Protocol adapted from MstyAI/laya-onnx v0.3.0 (Apache-2.0): sequence.go,
batch.go and calibration.go. Laya by Convai Innovations (Apache-2.0).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import platform
import socket
import time
import unicodedata


RELEASE = "https://github.com/MstyAI/laya-onnx/releases/download/model-c5d78730/"
REVISION = "c5d78730f3493e4fe16d61507ef4b78eef7318cf"
FILES = {
    "model.onnx": (3564788, "55813eecb0318474f9ef109c145315d23ccf18f9952d05118dea4472c9eb9f35"),
    "model.onnx.data": (842072064, "3970321cdad29349fca999b6ab2f302472a3f26c54b14d8cc47aae31f03cb31d"),
    "rl_agent_config.json": (745, "ae287b56bbcf5f8c4f4541ae9dfd00c914c4c48b940b8398c3058af37ba92bbd"),
    "tokenizer.json": (3583228, "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30"),
    "validation.json": (1108, "5c3c008e5082be6255d50becb65642da5cc6008211ef30c001bd953b29b632ef"),
}


def verify(directory: Path) -> None:
    for name, (size, digest) in FILES.items():
        path = directory / name
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1048576), b""):
                hasher.update(block)
        if path.stat().st_size != size or hasher.hexdigest() != digest:
            raise ValueError(f"Artifact integrity check failed: {name}")


def prepare(directory: Path) -> None:
    """Only this explicitly requested operation accesses the network."""
    import urllib.request
    directory.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = directory / name
        if path.exists():
            continue
        temporary = path.with_name(path.name + ".part")
        expected_size, expected_hash = FILES[name]
        hasher, received = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(RELEASE + name, timeout=60) as response, temporary.open("wb") as output:
                for block in iter(lambda: response.read(1048576), b""):
                    received += len(block)
                    if received > expected_size:
                        raise ValueError(f"Download exceeds expected size: {name}")
                    hasher.update(block)
                    output.write(block)
            if received != expected_size or hasher.hexdigest() != expected_hash:
                raise ValueError(f"Downloaded artifact failed integrity verification: {name}")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(path)
    for name in ("LICENSE", "NOTICE"):
        source = "https://raw.githubusercontent.com/MstyAI/laya-onnx/v0.3.0/" + name
        (directory / name).write_bytes(urllib.request.urlopen(source, timeout=30).read())
    verify(directory)


@contextmanager
def blocked_python_network():
    """Fail closed on Python socket access; ORT native telemetry is disabled too.

    This is a runtime guard, not packet capture of native-library network calls.
    """
    attempts = []
    def deny(*args, **kwargs):
        attempts.append("blocked socket operation")
        raise RuntimeError("Network disabled during local model evaluation")
    originals = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo)
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo = originals


class ByteLevelBPE:
    """Byte-level BPE for this pinned NFC tokenizer, implemented from its JSON.

    This implements the GPT-2 byte alphabet and merge algorithm directly. It
    refuses unsupported tokenizer configuration instead of silently approximating.
    Special token insertion belongs to the decision protocol, not encode().
    """

    def __init__(self, path: Path):
        import regex
        self.regex = regex
        spec = json.loads(path.read_text(encoding="utf-8"))
        if spec["normalizer"] != {"type": "NFC"} or spec["pre_tokenizer"] != {
            "type": "ByteLevel", "add_prefix_space": False, "trim_offsets": True, "use_regex": True
        }:
            raise ValueError("Unsupported tokenizer configuration")
        model = spec["model"]
        if model["type"] != "BPE" or model.get("dropout") or model.get("ignore_merges"):
            raise ValueError("Unsupported BPE variant")
        self.vocab = model["vocab"]
        self.ranks = {tuple(pair): i for i, pair in enumerate(model["merges"])}
        byte_values = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
        codepoints = byte_values.copy()
        for value in range(256):
            if value not in byte_values:
                byte_values.append(value)
                codepoints.append(256 + len(codepoints) - 188)
        # 188 printable bytes precede the 68 escaped bytes.
        self.byte_map = dict(zip(byte_values, map(chr, codepoints)))
        self.pattern = regex.compile(r"'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+")
        self.added = {entry["content"]: entry["id"] for entry in spec["added_tokens"]}
        self.vocab.update(self.added)
        self.added_pattern = regex.compile("(" + "|".join(regex.escape(s) for s in sorted(self.added, key=len, reverse=True)) + ")")

    @lru_cache(maxsize=32768)
    def _merge(self, token: str) -> tuple[int, ...]:
        symbols = list(token)
        while len(symbols) > 1:
            pairs = list(zip(symbols, symbols[1:]))
            best = min(pairs, key=lambda pair: self.ranks.get(pair, float("inf")))
            if best not in self.ranks:
                break
            merged, i = [], 0
            while i < len(symbols):
                if i + 1 < len(symbols) and (symbols[i], symbols[i + 1]) == best:
                    merged.append(symbols[i] + symbols[i + 1])
                    i += 2
                else:
                    merged.append(symbols[i])
                    i += 1
            symbols = merged
        return tuple(self.vocab[symbol] for symbol in symbols)

    def encode(self, text: str) -> list[int]:
        text = unicodedata.normalize("NFC", text.replace("[MASK]", " "))
        output = []
        for part in self.added_pattern.split(text):
            if part in self.added:
                output.append(self.added[part])
                continue
            for piece in self.pattern.findall(part):
                output.extend(self._merge("".join(self.byte_map[b] for b in piece.encode("utf-8"))))
        return output


class LocalLaya:
    def __init__(self, directory: Path, threads: int = 2):
        import numpy as np
        import onnxruntime as ort
        ort.disable_telemetry_events()
        self.np = np
        verify(directory)
        self.tokenizer = ByteLevelBPE(directory / "tokenizer.json")
        self.cfg = json.loads((directory / "rl_agent_config.json").read_text())
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model.onnx"), options, providers=["CPUExecutionProvider"])
        self.threads = threads

    def sequence(self, state: str, question: dict) -> tuple[list[int], list[int], list[str], bool]:
        kind, criteria = question["type"], question.get("criteria")
        if kind == "choice":
            keys = list(criteria)
            options = [f"{key}: {criteria[key]}" if criteria[key] else key for key in keys]
        elif kind == "score":
            keys = [str(i) for i in range(len(criteria))]
            options = [f"level {i}: {value}" for i, value in enumerate(criteria)]
        elif kind == "noul":
            keys = ["false", "true"]
            options = ["false: no, the statement does not hold", "true: yes, the statement holds"]
        else:
            raise ValueError("Unknown question type")
        tok = self.tokenizer
        head = tok.encode(f"{kind} question: {question['instructions']}")
        option_ids = [[tok.vocab["[MASK]"]] + tok.encode(" " + option)[:48] for option in options]
        budget = 192 - sum(map(len, option_ids))
        if budget < 16:
            option_ids = [option[:max(4, (192 - 16) // len(options))] for option in option_ids]
            budget = 192 - sum(map(len, option_ids))
        if budget < 8:
            raise ValueError("Question options exceed the context budget")
        ids = [tok.vocab["[CLS]"]] + head[:budget] + [tok.vocab["[SEP]"]]
        markers = []
        for option in option_ids:
            markers.append(len(ids))
            ids.extend(option)
        ids.append(tok.vocab["[SEP]"])
        state_ids = tok.encode(state)
        room = 512 - len(ids) - 1
        ids.extend(state_ids[:room])
        ids.append(tok.vocab["[SEP]"])
        return ids, markers, keys, len(state_ids) > room

    def predict(self, state: str, questions: dict) -> dict:
        np = self.np
        prepared = {key: self.sequence(state, question) for key, question in questions.items()}
        length = max(len(row[0]) for row in prepared.values())
        count = max(len(row[1]) for row in prepared.values())
        size = len(questions)
        feeds = {
            "input_ids": np.full((size, length), self.tokenizer.vocab["[PAD]"], dtype=np.int64),
            "attention_mask": np.zeros((size, length), dtype=np.int64),
            "marker_pos": np.zeros((size, count), dtype=np.int64),
            "marker_mask": np.zeros((size, count), dtype=bool),
            "qtype": np.array([{"choice": 0, "score": 1, "noul": 2}[q["type"]] for q in questions.values()], dtype=np.int64),
        }
        for i, (ids, markers, _, _) in enumerate(prepared.values()):
            feeds["input_ids"][i, :len(ids)] = ids
            feeds["attention_mask"][i, :len(ids)] = 1
            feeds["marker_pos"][i, :len(markers)] = markers
            feeds["marker_mask"][i, :len(markers)] = True
        logits = self.session.run(["logits"], feeds)[0]
        answers = {}
        for i, (key, question) in enumerate(questions.items()):
            ids, _, keys, truncated = prepared[key]
            kind = question["type"]
            n = len(keys)
            bucket = "2" if n == 2 else "3-5" if n <= 5 else "6-10" if n <= 10 else "11+"
            temp = self.cfg["temperature_by_options"].get(kind + ":" + bucket, self.cfg["temperature"][feeds["qtype"][i]])
            values = logits[i, :n].astype(float) / temp
            probs = np.exp(values - values.max())
            probs /= probs.sum()
            answer = {"probabilities": dict(zip(keys, map(float, probs))), "tokens": len(ids), "truncated": truncated, "choice": keys[int(probs.argmax())]}
            if kind == "noul":
                answer["noul"] = float(probs[1])
            if kind == "score":
                answer["score"] = float(np.dot(np.arange(n), probs))
            answers[key] = answer
        return answers


def golden_cases() -> list[tuple[str, dict]]:
    return [
        ("I was charged twice and need the duplicate refunded today.", {
            "department": {"type": "choice", "instructions": "Which team should handle this?", "criteria": {"billing": "payments and refunds", "sales": "purchases", "technical": "bugs"}},
            "refund": {"type": "noul", "instructions": "Does the customer ask for money back?"},
            "urgency": {"type": "score", "instructions": "How urgent is this?", "criteria": ["not urgent", "soon", "urgent", "critical"]},
        }),
        (json.dumps({"request": "Refactor this service and investigate its deadlock."}), {"difficulty": {"type": "choice", "instructions": "How difficult is the requested work?", "criteria": {f"level_{i}": f"difficulty level {i}" for i in range(12)}}}),
        ("The release notes contain routine fixes. " * 180, {"relevant": {"type": "noul", "instructions": "Is this about a payment failure?"}}),
    ]


def role_question(entity: str) -> dict:
    return {"role": {"type": "choice", "instructions": f"What role does {entity} have in the reported wrongdoing?", "criteria": {
        "accused": "Entity is accused, investigated or allegedly responsible for wrongdoing",
        "reported_fact": "A court conviction or established wrongdoing by the entity is reported",
        "victim": "Entity suffered the wrongdoing as a victim",
        "complainant": "Entity reported or denounced wrongdoing by others",
        "negated": "Wrongdoing by the entity is explicitly denied, disproved or entity acquitted",
        "incidental": "Entity is mentioned incidentally or linked only indirectly",
        "homonym": "A similar name refers to a different entity",
        "insufficient": "The text provides insufficient evidence of the entity role",
    }}}


def comparison_question(entity: str) -> dict:
    """Frozen before reading challenge labels; same eight roles as local baseline."""
    return {"role": {"type": "choice", "instructions": f"What role does {entity} have in this report? Select the most specific role supported by the text.", "criteria": {
        "accused": "Target is accused, investigated or convicted of wrongdoing; a conviction is a reported judicial outcome",
        "victim": "Target suffered wrongdoing committed by others",
        "complainant": "Target reported or denounced wrongdoing by others",
        "incidental": "Target is mentioned without involvement in the reported wrongdoing",
        "negated": "The target denies wrongdoing or the text explicitly negates the allegation",
        "resolved": "The case against the target was dismissed or target was acquitted by an authority",
        "insufficient": "No target mention, ambiguity, hypothetical allegations or contradictory roles prevent identifying a role",
        "indirect": "Target has only an indirect relationship to another entity involved in wrongdoing",
    }}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--cases", type=Path, help="JSON/JSONL cases with id, text, entity and role/expected_role")
    parser.add_argument("--output", type=Path, default=Path("docs/benchmarks/phase2_laya.json"))
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--abstain-threshold", type=float, default=0.65, help="Fixed experimental max-probability threshold; not calibrated")
    parser.add_argument("--include-text", action="store_true", help="Include original fixture text in the output; use only for shareable synthetic cases")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.model_dir)
    with blocked_python_network() as network_attempts:
        run_experiment(args, network_attempts)


def run_experiment(args: argparse.Namespace, network_attempts: list) -> None:
    import numpy as np
    import onnxruntime as ort
    ort.disable_telemetry_events()
    import psutil
    import regex
    process = psutil.Process()
    initial_rss_mb = process.memory_info().rss / 1048576
    started = time.perf_counter()
    model = LocalLaya(args.model_dir, args.threads)
    load_ms = (time.perf_counter() - started) * 1000
    golden = []
    expected = json.loads((args.model_dir / "validation.json").read_text())["comparisons"]
    for case_id, (state, questions) in enumerate(golden_cases()):
        actual = model.predict(state, questions)
        for baseline in (row for row in expected if row["case"] == case_id):
            answer = actual[baseline["question"]]
            idx = list(answer["probabilities"]).index(answer["choice"])
            row = {"case": case_id, "question": baseline["question"], "tokens": answer["tokens"], "expected_tokens": baseline["tokens"], "actual": idx, "expected": baseline["actual"]}
            row["passed"] = row["tokens"] == row["expected_tokens"] and row["actual"] == row["expected"]
            golden.append(row)
    if not all(row["passed"] for row in golden):
        raise RuntimeError("Official golden protocol checks failed: " + json.dumps(golden))
    if args.cases:
        contents = args.cases.read_text(encoding="utf-8")
        cases = [json.loads(line) for line in contents.splitlines() if line.strip()] if args.cases.suffix == ".jsonl" else json.loads(contents)
        cases = [{**case, "expected_role": case.get("expected_role", case.get("role"))} for case in cases]
    else:
        cases = [
        {"id": "es_accused", "entity": "Empresa Aurora", "text": "La fiscalía acusó a Empresa Aurora de fraude. La investigación sigue abierta y no existe sentencia.", "expected_role": "accused"},
        {"id": "es_victim", "entity": "Empresa Aurora", "text": "Empresa Aurora fue víctima de un fraude cometido por sus proveedores.", "expected_role": "victim"},
        {"id": "es_complainant", "entity": "Empresa Aurora", "text": "Empresa Aurora denunció a un proveedor por fraude ante la fiscalía.", "expected_role": "complainant"},
        {"id": "es_negated", "entity": "Empresa Aurora", "text": "El tribunal absolvió a Empresa Aurora: no cometió fraude y no fue responsable.", "expected_role": "negated"},
        {"id": "es_incidental", "entity": "Empresa Aurora", "text": "La noticia sobre el fraude de Grupo Delta mencionó que Empresa Aurora patrocinó un concierto.", "expected_role": "incidental"},
        {"id": "es_homonym", "entity": "Empresa Aurora", "text": "Aurora Logistics fue acusada de fraude. Es una entidad distinta de Empresa Aurora y no tienen relación.", "expected_role": "homonym"},
        {"id": "es_fact", "entity": "Empresa Aurora", "text": "El tribunal condenó a Empresa Aurora por fraude tras comprobar que falsificó facturas.", "expected_role": "reported_fact"},
        {"id": "es_insufficient", "entity": "Empresa Aurora", "text": "Empresa Aurora presentó su nueva identidad visual. No hay más datos disponibles.", "expected_role": "insufficient"},
        {"id": "en_accused", "entity": "Aurora Company", "text": "Prosecutors accused Aurora Company of fraud. The investigation remains open and no verdict has been issued.", "expected_role": "accused"},
        {"id": "en_victim", "entity": "Aurora Company", "text": "Aurora Company was the victim of fraud committed by its suppliers.", "expected_role": "victim"},
        ]
    results = []
    cpu_start = process.cpu_times()
    for case in cases:
        t0 = time.perf_counter()
        entity = case["entity"]
        entity_name = entity["name"] if isinstance(entity, dict) else entity
        questions = comparison_question(entity_name) if args.cases else role_question(entity_name)
        state = case["text"]
        if args.cases and isinstance(entity, dict):
            state = "Target entity: " + json.dumps(entity, ensure_ascii=False, sort_keys=True) + "\nNote: " + state
        answer = model.predict(state, questions)["role"]
        abstained = answer["truncated"] or max(answer["probabilities"].values()) < args.abstain_threshold
        case_reference = {"id": case["id"], "expected_role": case["expected_role"], "text_sha256": hashlib.sha256(case["text"].encode("utf-8")).hexdigest()}
        if isinstance(entity, dict):
            case_reference["entity_id"] = entity.get("id")
        if args.include_text:
            case_reference.update(case)
        results.append({**case_reference, "predicted_role": answer["choice"], "correct": answer["choice"] == case["expected_role"], "abstained": abstained, "latency_ms": (time.perf_counter() - t0) * 1000, "answer": answer, "rss_mb": process.memory_info().rss / 1048576})
    labels = sorted({case["expected_role"] for case in cases})
    per_label = {}
    for label in labels:
        tp = sum(r["predicted_role"] == label and r["expected_role"] == label for r in results)
        fp = sum(r["predicted_role"] == label and r["expected_role"] != label for r in results)
        fn = sum(r["predicted_role"] != label and r["expected_role"] == label for r in results)
        p, r = tp / (tp + fp) if tp + fp else 0, tp / (tp + fn) if tp + fn else 0
        per_label[label] = {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0, "support": tp + fn}
    report = {
        "phase": 4 if args.cases else 2, "completed_at": datetime.now(timezone.utc).isoformat(), "model": "Laya-English-421M-ONNX-FP16", "revision": REVISION,
        "source": RELEASE, "hashes_verified": True,
        "experiment_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "network_verification": {"python_socket_guard_enabled": True, "blocked_python_socket_attempts": len(network_attempts), "onnxruntime_native_telemetry_disabled": True, "method": "Python socket connect/connect_ex/create_connection/getaddrinfo blocked during model load and all predictions; native library packet traffic not measured"},
        "privacy": {"original_text_included": args.include_text, "corpus_file": str(args.cases) if args.cases else "built-in synthetic pilot", "notice": "Keep outputs local when using private cases. Default outputs contain case identifiers, text hashes and predictions, without original notes."},
        "environment": {"platform": platform.platform(), "python": platform.python_version(), "onnxruntime": ort.__version__, "numpy": np.__version__, "regex": regex.__version__, "psutil": psutil.__version__, "threads": args.threads, "provider": "CPUExecutionProvider", "initial_rss_mb": initial_rss_mb},
        "protocol": {"context_tokens": 512, "prompt": comparison_question("{entity_name}") if args.cases else role_question("{entity_name}"), "abstain_threshold": args.abstain_threshold, "abstain_rule": "truncated or max option probability below threshold; experimental, uncalibrated", "corpus_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest() if args.cases else None},
        "golden_protocol_checks": golden, "load_ms": load_ms, "disk_model_bytes": sum(size for size, _ in FILES.values()),
        "cpu_seconds": process.cpu_times().user + process.cpu_times().system - cpu_start.user - cpu_start.system,
        "metrics": {"n": len(results), "accuracy": sum(r["correct"] for r in results) / len(results), "macro_f1": sum(m["f1"] for m in per_label.values()) / len(per_label), "per_label": per_label, "latency_p50_ms": float(np.percentile([r["latency_ms"] for r in results], 50)), "latency_p95_ms": float(np.percentile([r["latency_ms"] for r in results], 95)), "max_observed_rss_mb": max(r["rss_mb"] for r in results), "rss_measurement": "current fresh process sampled after each note, not peak allocation or historical ru_maxrss", "abstained": sum(r["abstained"] for r in results), "truncated": sum(r["answer"]["truncated"] for r in results)},
        "limitations": ["Small synthetic hand-labelled challenge set; no claim of real-world accuracy.", "English checkpoint tested directly in Spanish, without translation.", "Golden checks verify five token counts and predicted options, not all possible tokenizer inputs or full probability parity.", "Model distributions are not calibrated risk probabilities for this task.", "No NER, relation or evidence-span quality measured by this phase.", "512 tokens include instructions/options; truncation is surfaced explicitly.", "Model outputs do not contribute to the existing score."],
        "predictions": [{"id": result["id"], "role": result["predicted_role"], "latency_ms": result["latency_ms"], "abstained": result["abstained"], "truncated": result["answer"]["truncated"]} for result in results],
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "metrics": report["metrics"], "golden_passed": len(golden)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
