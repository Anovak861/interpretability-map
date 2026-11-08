"""Controlled, non-importing pilot; three documents x three model/effort settings."""
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interp_map import db, extract
from interp_map.fetchers import Fetched
from interp_map.models import DocumentMeta, Extraction

OUT = db.ROOT / "data/model-comparison-2026-10-25"
VARIANTS = [("sol-default", "gpt-6.1-sol", "default"),
            ("sol-high", "gpt-6.1-sol", "high"),
            ("astra-high", "gpt-6-astra", "high")]
DOCS = [587, 703, 486]


def rate_limits():
    """Read account-wide quota snapshots; do not start a model turn or expose auth."""
    with tempfile.TemporaryDirectory(prefix="interp-quota-") as tmp:
        p = subprocess.Popen([extract.find_codex(), "app-server", "--stdio"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", cwd=tmp,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        messages = queue.Queue()
        def reader():
            for line in p.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        threading.Thread(target=reader, daemon=True).start()
        def send(value):
            p.stdin.write(json.dumps(value) + "\n"); p.stdin.flush()
        def response(request_id):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                msg = messages.get(timeout=max(.01, deadline-time.monotonic()))
                if msg.get("id") == request_id:
                    return msg.get("result", {"error": msg.get("error")})
            raise TimeoutError("quota snapshot timed out")
        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "interp_map_eval", "version": "0.1.0"}}})
            response(1)
            send({"method": "initialized", "params": {}})
            send({"id": 2, "method": "account/rateLimits/read", "params": {}})
            result = response(2)
            return {"rateLimitsByLimitId": {
                key: {field: value.get(field) for field in ("limitId", "primary", "secondary", "individualLimit")}
                for key, value in result.get("rateLimitsByLimitId", {}).items()},
                "error": result.get("error")}
        except Exception as e:
            return {"error": type(e).__name__ + ": " + str(e)}
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    baseline = db.connect(db.ROOT / "data/interp_map.before-codex-batch-2026-10-25.db")
    vocab = db.vocabulary(baseline); baseline.close()
    inputs = {}
    for doc_id in DOCS:
        packet = json.loads((db.ROOT / f"data/codex-qa/{doc_id}.json").read_text(encoding="utf-8"))
        f = Fetched(DocumentMeta.model_validate(packet["extraction"]["document"]),
                    packet["document"]["full_text"], [])
        prompt = extract.build_prompt(f, vocab)
        (OUT / f"prompt-{doc_id}.txt").write_text(prompt, encoding="utf-8")
        inputs[doc_id] = (f, prompt)
    manifest = {"documents": DOCS, "variants": VARIANTS,
                "design": "Same revised prompt and pre-batch vocabulary; no model-output references supplied; no imports.",
                "prompt_sha256": {i: hashlib.sha256(p.encode()).hexdigest() for i, (_, p) in inputs.items()},
                "quota_caveat": "Account-wide integer percentages; other account activity and delayed reporting can confound deltas.",
                "groups": []}
    def run(doc_id, name, model, effort):
        f, prompt = inputs[doc_id]
        metrics = {"document_id": doc_id, "variant": name}
        try:
            raw = extract.run_codex_cli(prompt, Extraction.model_json_schema(), model=model,
                                        reasoning_effort=effort, telemetry=metrics)
            ex = extract.parse_extraction(raw, f, extract.codex_provenance(model, effort))
            (OUT / f"{name}-{doc_id}.json").write_text(ex.model_dump_json(indent=2), encoding="utf-8")
            metrics["claims"] = len(ex.claims); metrics["valid"] = True
        except Exception as e:
            metrics["valid"] = False; metrics["error"] = str(e); metrics["error_type"] = type(e).__name__
        (OUT / f"{name}-{doc_id}.metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        return metrics
    for name, model, effort in VARIANTS:
        group = {"variant": name, "before": rate_limits(), "results": []}
        print(f"START {name}: 3 workers", flush=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for fut in as_completed([pool.submit(run, i, name, model, effort) for i in DOCS]):
                result = fut.result();group["results"].append(result)
                print(json.dumps(result), flush=True)
        group["after"] = rate_limits(); manifest["groups"].append(group)
        (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if any(r.get("error_type") in ("UsageLimitError", "NotLoggedInError") for r in group["results"]):
            break
    print("Comparison outputs: " + str(OUT), flush=True)


if __name__ == "__main__":
    main()
