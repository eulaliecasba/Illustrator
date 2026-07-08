#!/usr/bin/env python3
"""
app.py -- minimal web backend for the Museum Illustrator.

Endpoints (that's all):
  GET  /api/health
  POST /api/scan          (multipart: pdf, period)   -> {config}
  POST /api/build         (json: {pdf_b64, config})  -> {job_id}
  GET  /api/status/<job>                             -> {state, done, error}
  GET  /api/result/<job>                             -> the illustrated PDF

No preview, no per-image editing. Build the PDF (with blurbs) and hand it back.
Kept lean to fit the 512MB free tier.
"""

import base64
import io
import os
import gc
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request, send_file, abort
from flask_cors import CORS

import museum_illustrator as mi

app = Flask(__name__)
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "*"))

JOBS = {}
UPLOADS = {}  # pdf_id -> {"bytes": ..., "ts": ...}
UPLOAD_TTL = 30 * 60  # 30 min

def _gc_uploads():
    import time
    now = time.time()
    stale = [k for k, v in UPLOADS.items() if now - v["ts"] > UPLOAD_TTL]
    for k in stale:
        UPLOADS.pop(k, None)


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
    import time
    _gc_uploads()
    if "pdf" not in request.files:
        abort(400, "no pdf uploaded")
    data = request.files["pdf"].read()
    tmp = f"/tmp/{uuid.uuid4().hex}.pdf"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        period = request.form.get("period", "any")
        sections = mi.scan_pdf(tmp, period=period)
    finally:
        os.remove(tmp)
    prefer = [p for p in request.form.get("prefer", "").split(",") if p.strip()]
    # Cache the PDF so build/ doesn't need it re-uploaded (base64-encoding the
    # same file client-side and shipping it a second time is usually the
    # slowest step of the whole run).
    pdf_id = uuid.uuid4().hex
    UPLOADS[pdf_id] = {"bytes": data, "ts": time.time()}
    return jsonify(pdf_id=pdf_id,
                   config={"prefer": prefer, "sections": sections})


def _run_build(job_id, pdf_bytes, config, min_score):
    job = JOBS[job_id]

    class Tee:
        def write(self, s):
            s = s.strip()
            if s:
                job["log"].append(s)
        def flush(self):
            pass

    import contextlib
    tmp_in = f"/tmp/{job_id}_in.pdf"
    tmp_out = f"/tmp/{job_id}_out.pdf"
    tmp_cfg = f"/tmp/{job_id}.json"
    try:
        with open(tmp_in, "wb") as f:
            f.write(pdf_bytes)
        with open(tmp_cfg, "w") as f:
            json.dump(config, f)
        args = SimpleNamespace(
            pdf=Path(tmp_in), config=Path(tmp_cfg), output=Path(tmp_out),
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
        for p in (tmp_in, tmp_out, tmp_cfg):
            try:
                os.remove(p)
            except OSError:
                pass
        gc.collect()


@app.post("/api/build")
def build():
    body = request.get_json(force=True)
    pdf_id = body.get("pdf_id")
    if pdf_id and pdf_id in UPLOADS:
        pdf_bytes = UPLOADS.pop(pdf_id)["bytes"]  # one-shot
    elif body.get("pdf_b64"):
        pdf_bytes = base64.b64decode(body["pdf_b64"])
    else:
        abort(400, "no pdf provided")
    config = body["config"]
    min_score = float(body.get("min_score", 0.35))
    filename = body.get("filename", "illustrated.pdf")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"state": "queued", "log": [], "done": False, "error": None,
                    "pdf_bytes": None, "filename": filename}
    threading.Thread(target=_run_build,
                     args=(job_id, pdf_bytes, config, min_score),
                     daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(state=job["state"], log=job["log"][-30:],
                   done=job["done"], error=job["error"])


@app.get("/api/result/<job_id>")
def result(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("pdf_bytes"):
        abort(404)
    name = job.get("filename") or "illustrated.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return send_file(io.BytesIO(job["pdf_bytes"]),
                     mimetype="application/pdf", as_attachment=True,
                     download_name=name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
