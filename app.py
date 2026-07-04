#!/usr/bin/env python3
"""
app.py -- web backend for the Museum Illustrator.

Exposes a tiny HTTP API the Netlify page calls:

  POST /api/scan          (multipart: pdf)          -> {config: {...}}
  POST /api/build         (json: {pdf_b64, config}) -> {job_id}
  GET  /api/status/<job>                            -> {state, log, done, error}
  GET  /api/result/<job>                            -> the illustrated PDF (bytes)

Building runs in a background thread because a real run makes many throttled
museum calls and can take minutes -- far longer than any serverless timeout,
which is why this must live on a host that runs Python processes (Render,
Railway, Fly), not on Netlify itself.

Run locally:   pip install -r requirements.txt && python app.py
Deploy:        see README_DEPLOY.md
"""

import base64
import io
import os
import threading
import uuid
from types import SimpleNamespace

import fitz
from flask import Flask, jsonify, request, send_file, abort
from flask_cors import CORS

import museum_illustrator as mi

app = Flask(__name__)
# Lock this down to your Netlify origin in production via ALLOWED_ORIGIN.
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "*"))

JOBS = {}  # job_id -> dict(state, log, done, error, pdf_bytes)


def _keys_from_env(args):
    args.rijks_key = os.environ.get("RIJKS_API_KEY")
    args.smithsonian_key = os.environ.get("SMITHSONIAN_API_KEY")
    args.harvard_key = os.environ.get("HARVARD_API_KEY")
    return args


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.post("/api/scan")
def scan():
    if "pdf" not in request.files:
        abort(400, "no pdf uploaded")
    data = request.files["pdf"].read()
    tmp = f"/tmp/{uuid.uuid4().hex}.pdf"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        sections = mi.scan_pdf(tmp)
    finally:
        os.remove(tmp)
    prefer = [p for p in request.form.get("prefer", "").split(",") if p.strip()]
    return jsonify(config={"prefer": prefer, "sections": sections})


def _run_build(job_id, pdf_bytes, config, min_score):
    job = JOBS[job_id]
    log = job["log"]

    class Tee:
        def write(self, s):
            s = s.strip()
            if s:
                log.append(s)
        def flush(self):
            pass

    import contextlib
    tmp_in = f"/tmp/{job_id}_in.pdf"
    tmp_cfg = f"/tmp/{job_id}.json"
    tmp_out = f"/tmp/{job_id}_out.pdf"
    try:
        with open(tmp_in, "wb") as f:
            f.write(pdf_bytes)
        import json
        with open(tmp_cfg, "w") as f:
            json.dump(config, f)

        args = SimpleNamespace(
            pdf=__import__("pathlib").Path(tmp_in),
            config=__import__("pathlib").Path(tmp_cfg),
            output=__import__("pathlib").Path(tmp_out),
            min_score=min_score, list=False, verbose=True)
        _keys_from_env(args)

        job["state"] = "running"
        with contextlib.redirect_stdout(Tee()):
            mi.cmd_build(args)

        with open(tmp_out, "rb") as f:
            job["pdf_bytes"] = f.read()
        job["state"] = "done"
        job["done"] = True
    except SystemExit as e:
        job["error"] = str(e) or "no sections matched"
        job["state"] = "error"
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        job["state"] = "error"
    finally:
        for p in (tmp_in, tmp_cfg, tmp_out):
            try:
                os.remove(p)
            except OSError:
                pass


@app.post("/api/build")
def build():
    body = request.get_json(force=True)
    pdf_bytes = base64.b64decode(body["pdf_b64"])
    config = body["config"]
    min_score = float(body.get("min_score", 0.6))
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"state": "queued", "log": [], "done": False,
                    "error": None, "pdf_bytes": None}
    threading.Thread(target=_run_build,
                     args=(job_id, pdf_bytes, config, min_score),
                     daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(state=job["state"], log=job["log"][-40:],
                   done=job["done"], error=job["error"])


@app.get("/api/result/<job_id>")
def result(job_id):
    job = JOBS.get(job_id)
    if not job or not job["pdf_bytes"]:
        abort(404)
    return send_file(io.BytesIO(job["pdf_bytes"]),
                     mimetype="application/pdf", as_attachment=True,
                     download_name="illustrated.pdf")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
