#!/usr/bin/env python3
"""
app.py -- minimal web backend for the Museum Illustrator.

Endpoints (that's all):
  GET  /api/health
  POST /api/scan          (multipart: pdf, period)   -> {config, pdf_id}
  POST /api/build         (json: {pdf_id|pdf_b64,    -> {job_id}
                                  config, ...})
  GET  /api/status/<job>                             -> {state, done, error}
  GET  /api/result/<job>                             -> the illustrated PDF

No preview, no per-image editing. Build the PDF (with blurbs) and hand it back.

Job state is persisted to /tmp/mi_jobs/ so a worker restart (common on Render's
free tier when the ~512 MB RAM cap is hit during a build) does not lose the
job or the built PDF from the client's point of view.
"""

import base64
import io
import os
import gc
import json
import time
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request, send_file, abort
from flask_cors import CORS

import museum_illustrator as mi

app = Flask(__name__)
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "*"))

JOB_DIR = Path("/tmp/mi_jobs")
JOB_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = Path("/tmp/mi_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_TTL = 30 * 60
JOB_TTL = 60 * 60


def _job_state_path(job_id): return JOB_DIR / f"{job_id}.json"
def _job_pdf_path(job_id):   return JOB_DIR / f"{job_id}.pdf"
def _job_log_path(job_id):   return JOB_DIR / f"{job_id}.log"


def _read_job(job_id):
    """Load job state from disk. Returns None if unknown."""
    p = _job_state_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_job(job_id, state):
    """Atomically persist job state so a status GET never reads a half-write."""
    p = _job_state_path(job_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(p)


def _gc_old_files():
    now = time.time()
    for p in list(UPLOAD_DIR.iterdir()):
        try:
            if now - p.stat().st_mtime > UPLOAD_TTL:
                p.unlink()
        except OSError:
            pass
    for p in list(JOB_DIR.iterdir()):
        try:
            if now - p.stat().st_mtime > JOB_TTL:
                p.unlink()
        except OSError:
            pass


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
    _gc_old_files()
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
    pdf_id = uuid.uuid4().hex
    (UPLOAD_DIR / f"{pdf_id}.pdf").write_bytes(data)
    return jsonify(pdf_id=pdf_id,
                   config={"prefer": prefer, "sections": sections})


def _run_build(job_id, pdf_bytes, config, min_score, filename):
    state = {"state": "running", "done": False, "error": None,
             "filename": filename, "started": time.time()}
    _write_job(job_id, state)

    class Tee:
        def write(self, s):
            s = s.strip()
            if s:
                try:
                    with open(_job_log_path(job_id), "a") as f:
                        f.write(s + "\n")
                except OSError:
                    pass
        def flush(self):
            pass

    import contextlib
    tmp_in = f"/tmp/{job_id}_in.pdf"
    tmp_cfg = f"/tmp/{job_id}.json"
    out_pdf = _job_pdf_path(job_id)
    try:
        with open(tmp_in, "wb") as f:
            f.write(pdf_bytes)
        with open(tmp_cfg, "w") as f:
            json.dump(config, f)
        args = SimpleNamespace(
            pdf=Path(tmp_in), config=Path(tmp_cfg), output=out_pdf,
            min_score=min_score, list=False, verbose=True)
        _keys_from_env(args)
        with contextlib.redirect_stdout(Tee()):
            mi.cmd_build(args)
        state["state"] = "done"
        state["done"] = True
    except SystemExit as e:
        state["error"] = str(e) or "no sections matched"
        state["state"] = "error"
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        state["state"] = "error"
    finally:
        _write_job(job_id, state)
        for p in (tmp_in, tmp_cfg):
            try:
                os.remove(p)
            except OSError:
                pass
        gc.collect()


@app.post("/api/build")
def build():
    body = request.get_json(force=True)
    pdf_id = body.get("pdf_id")
    if pdf_id:
        upath = UPLOAD_DIR / f"{pdf_id}.pdf"
        if not upath.exists():
            abort(400, "pdf_id not found (scan again)")
        pdf_bytes = upath.read_bytes()
        try:
            upath.unlink()
        except OSError:
            pass
    elif body.get("pdf_b64"):
        pdf_bytes = base64.b64decode(body["pdf_b64"])
    else:
        abort(400, "no pdf provided")
    config = body["config"]
    min_score = float(body.get("min_score", 0.35))
    filename = body.get("filename", "illustrated.pdf")
    job_id = uuid.uuid4().hex
    _write_job(job_id, {"state": "queued", "done": False, "error": None,
                        "filename": filename, "started": time.time()})
    threading.Thread(target=_run_build,
                     args=(job_id, pdf_bytes, config, min_score, filename),
                     daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    state = _read_job(job_id)
    if state is None:
        # Unknown job (either it never existed, or the worker was killed mid-
        # build before the state file was written). Report a soft state the
        # client can poll on rather than 404-ing.
        return jsonify(state="unknown", done=False, error=None,
                       filename="illustrated.pdf")
    return jsonify(state=state.get("state", "running"),
                   done=state.get("done", False),
                   error=state.get("error"),
                   filename=state.get("filename", "illustrated.pdf"))


@app.get("/api/result/<job_id>")
def result(job_id):
    state = _read_job(job_id)
    pdf_path = _job_pdf_path(job_id)
    if not pdf_path.exists():
        abort(404)
    name = (state or {}).get("filename") or "illustrated.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return send_file(str(pdf_path), mimetype="application/pdf",
                     as_attachment=True, download_name=name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
