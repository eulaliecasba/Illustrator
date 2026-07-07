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
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import fitz
from flask import Flask, jsonify, request, send_file, abort
from flask_cors import CORS

import museum_illustrator as mi

app = Flask(__name__)
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "*"))

JOBS = {}  # job_id -> dict


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
        period = request.form.get("period", "any")
        sections = mi.scan_pdf(tmp, period=period)
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
    try:
        with open(tmp_in, "wb") as f:
            f.write(pdf_bytes)
        job["pdf_path"] = tmp_in
        job["config"] = config
        job["state"] = "running"
        with contextlib.redirect_stdout(Tee()):
            placements = mi.decide_placements(tmp_in, config, min_score=min_score)
        job["placements"] = placements
        # render the initial PDF
        _render_job(job_id)
        job["state"] = "done"
        job["done"] = True
    except SystemExit as e:
        job["error"] = str(e) or "no sections matched"
        job["state"] = "error"
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        job["state"] = "error"


def _render_job(job_id):
    job = JOBS[job_id]
    out = f"/tmp/{job_id}_out.pdf"
    mi.render_from_placements(job["pdf_path"], job["placements"], out)
    with open(out, "rb") as f:
        job["pdf_bytes"] = f.read()
    try:
        os.remove(out)
    except OSError:
        pass


@app.post("/api/build")
def build():
    body = request.get_json(force=True)
    pdf_bytes = base64.b64decode(body["pdf_b64"])
    config = body["config"]
    min_score = float(body.get("min_score", 0.35))
    filename = body.get("filename", "illustrated.pdf")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"state": "queued", "log": [], "done": False, "error": None,
                    "pdf_bytes": None, "filename": filename, "placements": []}
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


def _placements_public(job):
    """Placement info safe to send to the browser (no local file paths)."""
    out = []
    for i, p in enumerate(job["placements"]):
        out.append({
            "index": i, "page": p["page"] + 1, "title": p["title"],
            "maker": p["maker"], "date": p["date"], "museum": p["museum"],
            "blurb": p.get("blurb", ""), "removed": p.get("removed", False),
            "citation": _cite(p),
        })
    return out


def _cite(p):
    bits = [p["title"]]
    for x in (p["maker"], p["date"], p["medium"]):
        if x:
            bits.append(x)
    head = ". ".join(b for b in bits if b)
    return f"{head}. {p['museum']}, {p['accession']}. {p['license']}."


@app.get("/api/placements/<job_id>")
def placements(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(placements=_placements_public(job))


@app.post("/api/edit/<job_id>")
def edit(job_id):
    """Queue-free single edit: remove or replace one image. The browser calls
    this per edit while the user works; the PDF is only re-rendered on /done."""
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    body = request.get_json(force=True)
    idx = int(body.get("index", -1))
    action = body.get("action")
    if idx < 0 or idx >= len(job["placements"]):
        abort(400, "bad index")
    p = job["placements"][idx]
    if action == "remove":
        p["removed"] = True
        return jsonify(ok=True, removed=True)
    if action == "restore":
        p["removed"] = False
        return jsonify(ok=True, removed=False)
    if action == "replace":
        desc = (body.get("description") or "").strip()
        if not desc:
            abort(400, "no description")
        period = job.get("config", {}).get("period", "any")
        ok = mi.replace_placement(p, desc, period)
        return jsonify(ok=ok, citation=_cite(p) if ok else None,
                       blurb=p.get("blurb", "") if ok else None)
    abort(400, "unknown action")


@app.post("/api/done/<job_id>")
def done(job_id):
    """Re-render the PDF with all queued edits applied."""
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    try:
        _render_job(job_id)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


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


@app.get("/api/preview/<job_id>")
def preview(job_id):
    """Inline PDF for the embedded viewer (not an attachment)."""
    job = JOBS.get(job_id)
    if not job or not job.get("pdf_bytes"):
        abort(404)
    return send_file(io.BytesIO(job["pdf_bytes"]),
                     mimetype="application/pdf", as_attachment=False,
                     download_name="preview.pdf")


@app.get("/api/pages/<job_id>")
def pages(job_id):
    """Render each page of the current PDF to a PNG (base64). Reliable preview
    that displays in any browser, unlike an embedded cross-origin PDF."""
    job = JOBS.get(job_id)
    if not job or not job.get("pdf_bytes"):
        abort(404)
    doc = fitz.open(stream=job["pdf_bytes"], filetype="pdf")
    out = []
    for pg in doc:
        pix = pg.get_pixmap(dpi=96)
        out.append("data:image/png;base64," +
                   base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return jsonify(pages=out)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

