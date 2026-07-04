#!/usr/bin/env python3
"""
museum_illustrator.py

Illustrate ANY PDF with open-access artwork drawn ONLY from museum collection
APIs. For each section you define, the tool finds a fitting public-domain
object, places it inline in the blank space near where that section begins
(spilling to a dedicated page only when the page has no room), and cites it
in full. A "List of Plates" credits page is appended at the end.

Sources (official museum APIs, no key required, public-domain/CC0 objects only):
  - The Metropolitan Museum of Art  (encyclopedic: all cultures & periods)
  - The Cleveland Museum of Art
  - The Art Institute of Chicago

Two commands:

  1. scan  -- read a PDF, detect its headings, and write a STARTER config you
              then edit (fill in / refine the search queries):
                  python museum_illustrator.py scan book.pdf -o book.json

  2. build -- read a config and produce the illustrated PDF:
                  python museum_illustrator.py build book.pdf --config book.json
                  python museum_illustrator.py build book.pdf --config book.json --list

The config maps sections of YOUR text to search queries. Each section has:
    "marker"  : a phrase that literally appears in your PDF (locates the section)
    "queries" : search queries tried in order until a suitable object is found
    "note"    : optional context line printed above the citation
Optional top-level "prefer": a list of words that nudge scoring toward a period
    or culture (e.g. ["roman","greek"] for a classical text, ["gothic",
    "medieval"] for a medieval one). Omit it for no bias.
"""

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import requests

USER_AGENT = "MuseumIllustrator/1.1 (personal scholarly project)"
CACHE_DIR = Path(".artwork_cache")
REQUEST_PAUSE = 0.05
EXCLUDE_TERMS = ("photograph", "postcard", "reproduction print", "sample book")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@dataclass
class Artwork:
    museum: str
    title: str
    maker: str
    date: str
    medium: str
    accession: str
    object_url: str
    image_url: str
    license: str
    score: float = 0.0
    image_path: Path | None = None

    def citation(self) -> str:
        bits = [self.title.rstrip(".")]
        for x in (self.maker, self.date, self.medium):
            if x:
                bits.append(x.rstrip("."))
        head = ". ".join(bits)
        return (f"{head}. {self.museum}, {self.accession}. {self.license}. "
                f"{self.object_url}")


@dataclass
class Section:
    sid: str
    marker: str
    queries: list[str]
    note: str = ""
    page_index: int | None = None
    artwork: Artwork | None = None


# --------------------------------------------------------------------------
# Museum clients
# --------------------------------------------------------------------------

class MuseumClient:
    name = "museum"

    def __init__(self, session):
        self.s = session

    def _get(self, url, **kw):
        time.sleep(REQUEST_PAUSE)
        r = self.s.get(url, timeout=30, **kw)
        r.raise_for_status()
        return r.json()

    def search(self, query, limit=8):
        raise NotImplementedError


class MetClient(MuseumClient):
    name = "The Metropolitan Museum of Art"
    BASE = "https://collectionapi.metmuseum.org/public/collection/v1"

    def search(self, query, limit=8):
        try:
            data = self._get(f"{self.BASE}/search",
                             params={"q": query, "hasImages": "true"})
        except requests.RequestException:
            return []
        # Only inspect the first handful of hits, and fetch their details in
        # parallel -- the per-object lookups were the main source of slowness.
        ids = (data.get("objectIDs") or [])[:8]
        if not ids:
            return []

        def fetch(oid):
            try:
                r = self.s.get(f"{self.BASE}/objects/{oid}", timeout=15)
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                return None

        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for obj in ex.map(fetch, ids):
                if not obj or not obj.get("isPublicDomain") or not obj.get("primaryImage"):
                    continue
                out.append(Artwork(
                    museum=self.name,
                    title=obj.get("title") or "Untitled",
                    maker=obj.get("artistDisplayName") or obj.get("culture") or "",
                    date=obj.get("objectDate") or "",
                    medium=obj.get("medium") or "",
                    accession=obj.get("accessionNumber") or "",
                    object_url=obj.get("objectURL") or "",
                    image_url=obj["primaryImage"],
                    license="Open Access (CC0)"))
                if len(out) >= limit:
                    break
        return out


class ArticClient(MuseumClient):
    name = "The Art Institute of Chicago"
    BASE = "https://api.artic.edu/api/v1/artworks/search"
    FIELDS = ("id,title,image_id,date_display,artist_display,medium_display,"
              "main_reference_number,is_public_domain")

    def search(self, query, limit=8):
        try:
            data = self._get(self.BASE, params={
                "q": query, "limit": limit, "fields": self.FIELDS,
                "query[term][is_public_domain]": "true"})
        except requests.RequestException:
            return []
        out = []
        for it in data.get("data", []):
            if not it.get("image_id") or not it.get("is_public_domain"):
                continue
            out.append(Artwork(
                museum=self.name,
                title=it.get("title") or "Untitled",
                maker=(it.get("artist_display") or "").replace("\n", ", "),
                date=it.get("date_display") or "",
                medium=it.get("medium_display") or "",
                accession=it.get("main_reference_number") or "",
                object_url=f"https://www.artic.edu/artworks/{it['id']}",
                image_url=(f"https://www.artic.edu/iiif/2/{it['image_id']}"
                           f"/full/1200,/0/default.jpg"),
                license="Public domain (CC0)"))
        return out


class ClevelandClient(MuseumClient):
    name = "The Cleveland Museum of Art"
    BASE = "https://openaccess-api.clevelandart.org/api/artworks/"

    def search(self, query, limit=8):
        try:
            data = self._get(self.BASE, params={
                "q": query, "cc0": "1", "has_image": "1", "limit": limit})
        except requests.RequestException:
            return []
        out = []
        for it in data.get("data", []):
            img = (((it.get("images") or {}).get("web")) or {}).get("url")
            if not img:
                continue
            cult = it.get("culture") or []
            out.append(Artwork(
                museum=self.name,
                title=it.get("title") or "Untitled",
                maker=cult[0] if cult else "",
                date=it.get("creation_date") or "",
                medium=it.get("technique") or "",
                accession=it.get("accession_number") or "",
                object_url=it.get("url") or "",
                image_url=img,
                license="CC0"))
        return out


class RijksClient(MuseumClient):
    name = "Rijksmuseum"
    BASE = "https://www.rijksmuseum.nl/api/en/collection"

    def __init__(self, session, api_key):
        super().__init__(session)
        self.key = api_key  # free key from rijksmuseum.nl/en/rijksstudio/my/data

    def search(self, query, limit=8):
        try:
            data = self._get(self.BASE, params={
                "key": self.key, "q": query, "imgonly": "true",
                "ps": limit, "p": 0})
        except requests.RequestException:
            return []
        out = []
        for it in data.get("artObjects", []):
            img = (it.get("webImage") or {}).get("url")
            if not img:
                continue
            out.append(Artwork(
                museum=self.name,
                title=it.get("title") or "Untitled",
                maker=it.get("principalOrFirstMaker") or "",
                date="",
                medium="",
                accession=it.get("objectNumber") or "",
                object_url=it.get("links", {}).get("web", "") or "",
                image_url=img,
                license="Public domain (CC0)"))
        return out


class NGAClient(MuseumClient):
    name = "National Gallery of Art, Washington"
    BASE = "https://api.nga.gov/art/tms/objects"

    def search(self, query, limit=8):
        try:
            data = self._get(self.BASE, params={
                "q": query, "isPublicDomain": "true",
                "hasImage": "true", "limit": limit})
        except requests.RequestException:
            return []
        out = []
        for it in (data.get("data") or data.get("objects") or []):
            iiif = it.get("iiifUrl") or it.get("iiifThumbUrl")
            if not iiif:
                continue
            img = iiif.rstrip("/") + "/full/!1200,1200/0/default.jpg"
            out.append(Artwork(
                museum=self.name,
                title=it.get("title") or "Untitled",
                maker=it.get("attribution") or "",
                date=it.get("displayDate") or "",
                medium=it.get("medium") or "",
                accession=it.get("accessionNum") or "",
                object_url=f"https://www.nga.gov/collection/art-object-page.{it.get('objectId','')}.html",
                image_url=img,
                license="Open Access (CC0)"))
        return out


class ParisMuseesClient(MuseumClient):
    name = "Paris Musees"
    BASE = "https://apicollections.parismusees.paris.fr/fr/search"

    def search(self, query, limit=8):
        try:
            data = self._get(self.BASE, params={"query": query, "limit": limit})
        except requests.RequestException:
            return []
        out = []
        for it in (data.get("results") or data.get("data") or []):
            img = it.get("image_url") or it.get("thumbnail")
            lic = (it.get("license") or "").lower()
            if not img or "cc0" not in lic and "public" not in lic and lic:
                continue
            if not img:
                continue
            out.append(Artwork(
                museum=self.name,
                title=it.get("title") or "Untitled",
                maker=it.get("author") or "",
                date=it.get("date") or "",
                medium=it.get("medium") or "",
                accession=it.get("inventory") or "",
                object_url=it.get("url") or "",
                image_url=img,
                license="Open Content"))
        return out


class SmithsonianClient(MuseumClient):
    name = "Smithsonian"
    BASE = "https://api.si.edu/openaccess/api/v1.0/search"

    def __init__(self, session, api_key):
        super().__init__(session)
        self.key = api_key  # free key from api.data.gov

    def search(self, query, limit=8):
        try:
            data = self._get(self.BASE, params={
                "api_key": self.key,
                "q": f'{query} AND online_media_type:"Images" AND usage_flag_CC0:true',
                "rows": limit})
        except requests.RequestException:
            return []
        out = []
        for row in (((data.get("response") or {}).get("rows")) or []):
            c = row.get("content", {})
            media = (((c.get("descriptiveNonRepeating") or {})
                      .get("online_media") or {}).get("media") or [])
            img = media[0].get("content") if media else None
            if not img:
                continue
            fs = c.get("freetext", {})
            def _ft(k):
                v = fs.get(k) or []
                return v[0].get("content") if v else ""
            out.append(Artwork(
                museum=(_ft("dataSource") or "Smithsonian"),
                title=row.get("title") or "Untitled",
                maker=_ft("name"),
                date=_ft("date"),
                medium=_ft("physicalDescription"),
                accession=_ft("identifier"),
                object_url=((c.get("descriptiveNonRepeating") or {})
                            .get("record_link") or ""),
                image_url=img,
                license="CC0"))
        return out


class HarvardClient(MuseumClient):
    name = "Harvard Art Museums"
    BASE = "https://api.harvardartmuseums.org/object"

    def __init__(self, session, api_key):
        super().__init__(session)
        self.key = api_key  # free key from harvardartmuseums.org/collections/api

    def search(self, query, limit=8):
        try:
            data = self._get(self.BASE, params={
                "apikey": self.key, "keyword": query, "hasimage": 1,
                "size": limit, "fields": ("title,people,dated,medium,"
                "objectnumber,url,primaryimageurl,accesslevel")})
        except requests.RequestException:
            return []
        out = []
        for it in (data.get("records") or []):
            img = it.get("primaryimageurl")
            if not img:
                continue
            people = it.get("people") or []
            maker = people[0].get("name") if people else ""
            out.append(Artwork(
                museum=self.name,
                title=it.get("title") or "Untitled",
                maker=maker or "",
                date=it.get("dated") or "",
                medium=it.get("medium") or "",
                accession=it.get("objectnumber") or "",
                object_url=it.get("url") or "",
                image_url=img,
                license="Public domain"))
        return out


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def _norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


# Preset period vocabularies. The user picks one on the page (or "any").
_PERIOD_TERMS = {
    "ancient": {
        "cultures": ("roman", "greek", "etruscan", "hellenistic", "cypriot",
                     "minoan", "mycenaean", "pompeian", "herculaneum", "italic",
                     "attic", "corinthian", "apulian", "gallo-roman", "romano",
                     "graeco", "greco", "classical", "antiquity", "byzantine"),
        "year_max": 600, "allow_bc": True,
    },
    "medieval": {
        "cultures": ("medieval", "gothic", "romanesque", "carolingian",
                     "byzantine", "merovingian", "ottonian", "norman"),
        "year_min": 500, "year_max": 1500, "allow_bc": False,
    },
    "renaissance": {
        "cultures": ("renaissance", "italian", "florentine", "venetian",
                     "flemish", "northern renaissance", "mannerist"),
        "year_min": 1400, "year_max": 1600, "allow_bc": False,
    },
    "baroque": {
        "cultures": ("baroque", "rococo", "dutch golden age"),
        "year_min": 1600, "year_max": 1750, "allow_bc": False,
    },
}


def _in_period(a, period):
    """True if the object fits the chosen period. period='any' accepts all."""
    if not period or period == "any":
        return True
    spec = _PERIOD_TERMS.get(period)
    if not spec:
        return True
    blob = _norm(" ".join([a.title, a.maker, a.medium, a.date]))
    if any(c in blob for c in spec["cultures"]):
        return True
    date = _norm(a.date)
    if not date:
        return False
    is_bc = ("b.c" in date or "bce" in date
             or bool(re.search(r"\bbc\b", date)))
    if is_bc:
        return spec.get("allow_bc", False)
    years = [int(y) for y in re.findall(r"\b(\d{3,4})\b", date)]
    if not years:
        return False
    y = min(years)
    lo = spec.get("year_min", 0)
    hi = spec.get("year_max", 3000)
    return lo <= y <= hi


def score_artwork(a, query, prefer):
    blob = _norm(" ".join([a.title, a.maker, a.medium, a.date]))
    if any(t in blob for t in EXCLUDE_TERMS):
        return -1.0
    terms = [t for t in _norm(query).split() if len(t) > 3]
    score = sum(1 for t in terms if t in blob) / max(len(terms), 1)
    if prefer and any(_norm(p) in blob for p in prefer):
        score += 0.5
    return score


def find_artwork(section, clients, prefer, min_score, verbose, period="any"):
    best = None
    for q in section.queries:
        results = []
        with ThreadPoolExecutor(max_workers=len(clients) or 1) as ex:
            for arts in ex.map(lambda c: c.search(q), clients):
                results.extend(arts)
        for art in results:
            # Period gate: skip anything outside the document's era.
            if not _in_period(art, period):
                if verbose:
                    print(f"      [skip off-period] {art.museum}: {art.title[:55]}")
                continue
            art.score = score_artwork(art, q, prefer)
            if verbose:
                print(f"      [{art.score:+.2f}] {art.museum}: {art.title[:70]}")
            if best is None or art.score > best.score:
                best = art
        if best and best.score >= min_score:
            return best
    # Nothing in-period cleared the bar -> skip the section.
    return best if (best and best.score >= min_score) else None


def download_image(art, session):
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / (hashlib.sha1(art.image_url.encode()).hexdigest()[:16] + ".jpg")
    if path.exists():
        art.image_path = path
        return path
    try:
        time.sleep(REQUEST_PAUSE)
        r = session.get(art.image_url, timeout=60)
        r.raise_for_status()
        path.write_bytes(r.content)
        art.image_path = path
        return path
    except requests.RequestException as e:
        print(f"      ! image download failed: {e}")
        return None


# --------------------------------------------------------------------------
# SCAN: detect headings and emit a starter config
# --------------------------------------------------------------------------

_NUM_PREFIX = re.compile(
    r"^\s*(chapter|section|part|canto|book|scene|act|ch\.?|sec\.?)?\s*"
    r"[\dIVXLC]+[\.\):\u2014\-]*\s*", re.I)


# Latin function words that essentially never appear as standalone English words.
_LATIN_WORDS = {
    "est", "sunt", "cum", "sed", "quod", "quae", "qui", "quam", "atque",
    "enim", "autem", "ergo", "igitur", "tamen", "etiam", "iam", "nunc",
    "hic", "haec", "hoc", "ille", "illa", "illud", "ipse", "ipsa", "esse",
    "erat", "fuit", "habet", "habebat", "dixit", "inquit", "ad", "ex",
    "per", "pro", "sub", "ab", "de", "in", "cum", "ut", "ne", "nec",
    "neque", "aut", "vel", "nam", "quia", "quoniam", "dum", "tunc",
    "omnia", "omnes", "res", "rem", "eius", "eum", "eam", "nos", "vos",
}
_MACRONS = set("āēīōū")


def _looks_latin(text):
    """True if a body passage reads as Latin rather than English/structural."""
    if not text or len(text) < 40:
        return False
    low = text.lower()
    words = re.findall(r"[a-zāēīōū]+", low)
    if len(words) < 8:
        return False
    latin_hits = sum(1 for w in words if w in _LATIN_WORDS)
    macron = any(c in _MACRONS for c in low)
    ratio = latin_hits / len(words)
    return macron or latin_hits >= 4 or ratio >= 0.06


# Content vocabulary: Latin word-stems (macron-stripped) -> a concrete museum
# search query. The scanner reads each section's Latin and, for every stem it
# finds, proposes the matching object. This drives images from what the text
# actually describes rather than from chapter titles. Ordered by how strongly
# each object reads as "illustratable"; the first confident museum match wins.
# Stems are matched at word starts, so "apr" catches aper/apri/aprum/apro.
_OBJECT_VOCAB = [
    (("cave canem", "canis ingens", "canis", "canem", "canes", "cane "), "roman mosaic dog"),
    (("larvam argent", "laruam argent", "larva", "laruam"), "roman silver skeleton"),
    (("apr", "aper"), "roman boar bronze"),
    (("porc", "sue ingent", "sus "), "roman pig bronze"),
    (("gladiator", "gladiatoribus", "essedari", "thraex", "bestiari"), "gladiator relief roman"),
    (("gladi", "cultr", "ferrum noric"), "roman sword"),
    (("amphor",), "roman amphora"),
    (("vitre", "phial", "vitream"), "roman glass"),
    (("scyph", "calic", "calix", "poculum", "pocul"), "roman silver cup"),
    (("corinth",), "roman bronze vessel corinthian"),
    (("lucern", "candelabr"), "roman bronze lamp"),
    (("armill",), "roman gold bracelet"),
    (("anul", "reticulum aureum", "aureol"), "roman gold ring"),
    (("periscelid", "crotali", "inaurat"), "roman gold jewelry"),
    (("corona", "coronis", "coronas"), "roman gold wreath"),
    (("duodecim signa", "zodiac", "signa in orbe"), "roman zodiac"),
    (("statu", "signum marmore", "statua"), "roman marble statue"),
    (("pictus", "pictur", "pariete", "parietem"), "roman fresco"),
    (("monument", "sepulcr", "sarcophag"), "roman funerary relief"),
    (("larēs", "lares argent", "larıs"), "roman lares bronze"),
    (("priap",), "roman priapus"),
    (("fortuna cum cornu", "cornu abundant"), "roman fortuna cornucopia"),
    (("mercuri",), "roman mercury bronze"),
    (("minerva", "minervā"), "roman minerva"),
    (("veneris signum", "signum veneris", "venerisque"), "roman venus statue"),
    (("pavon", "pavo"), "roman peacock mosaic"),
    (("gallin", "gallus"), "roman rooster bronze"),
    (("lepor", "lepus"), "roman hare"),
    (("pisc", "mullo", "mullos"), "roman fish mosaic"),
    (("urs",), "roman bear"),
    (("cornicin", "bucina", "bucinator", "tuba"), "roman trumpet cornu"),
    (("fasces", "secur"), "roman fasces"),
    (("horologium",), "roman sundial"),
    (("balne", "solium", "cisterna"), "roman bath"),
    (("triclini", "lectus", "lectum", "torus", "toris"), "roman dining couch"),
    (("toga", "praetext", "pallium", "pallio"), "roman toga statue"),
    (("catena", "vinctus"), "roman chain"),
    (("mola", "molā"), "roman mill"),
    (("lanx", "lance", "paropsid", "catill", "repositori"), "roman silver platter"),
    (("caduce",), "roman caduceus"),
    (("navis", "naves", "navem"), "roman ship relief"),
    (("piper", "cicer", "oliv"), "roman still life food fresco"),
    (("vinum", "falernum", "mulsum"), "roman wine amphora"),
]


def _object_queries(body):
    """From a section's Latin body, return ordered museum search queries for the
    concrete objects it mentions. Empty if nothing illustratable is found."""
    low = _norm(body)
    queries, seen = [], set()
    for stems, query in _OBJECT_VOCAB:
        for stem in stems:
            s = _norm(stem)
            # match at a word boundary so stems catch inflected forms
            if re.search(r"(^|[^a-z])" + re.escape(s), low):
                if query not in seen:
                    queries.append(query)
                    seen.add(query)
                break
    return queries


def _body_after(page, marker, doc, page_index):
    """Grab the Latin that follows a heading marker on its own page, so we can
    confirm it's Latin and mine it for concrete objects. Only borrows a little
    of the next page when this page's body is too short, to avoid pulling the
    following section's content into this one."""
    full = page.get_text()
    idx = full.find(marker)
    body = full[idx + len(marker):] if idx >= 0 else full
    if len(body) < 300 and page_index + 1 < doc.page_count:
        body += " " + doc[page_index + 1].get_text()[:400]
    return body[:1600]


def _clean_query(text):
    q = _NUM_PREFIX.sub("", text).strip(" .:\u2014-\u2013")
    return re.sub(r"\s+", " ", q)


def scan_pdf(pdf_path, max_marker_words=12):
    """Detect likely headings by font size and emit section dicts."""
    doc = fitz.open(pdf_path)

    # Determine body font size = most common size, weighted by character count.
    sizes = Counter()
    for pno in range(doc.page_count):
        for blk in doc[pno].get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    sizes[round(span["size"], 1)] += len(span["text"].strip())
    if not sizes:
        return []
    body_size = sizes.most_common(1)[0][0]

    sections, seen = [], set()
    for pno in range(doc.page_count):
        for blk in doc[pno].get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text or len(text) < 3:
                    continue
                max_size = max(s["size"] for s in spans)
                is_bold = any(s["flags"] & 2 ** 4 for s in spans)  # bold flag
                words = text.split()
                # Heading heuristic: clearly larger than body, OR bold + short
                # + no terminal period (so we skip ordinary emphasised sentences).
                looks_heading = (
                    max_size >= body_size + 1.2
                    or (is_bold and len(words) <= max_marker_words
                        and not text.endswith("."))
                )
                if not looks_heading or len(words) > max_marker_words:
                    continue
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                # Never illustrate structural headings, regardless of body text.
                if any(w in key for w in ("content", "table of contents",
                        "introduction", "preface", "index", "bibliography",
                        "glossary", "appendix", "notes", "acknowledg",
                        "characters", "dramatis personae")):
                    continue
                # Only illustrate sections whose body is real Latin narrative.
                body = _body_after(doc[pno], text, doc, pno)
                if not _looks_latin(body):
                    continue
                # Choose images from what the Latin actually describes, not the
                # chapter title. If the passage names no concrete object, skip it
                # -- this keeps images tied to content and avoids clutter.
                queries = _object_queries(body)
                if not queries:
                    continue
                sections.append({
                    "id": f"p{pno + 1}: {text[:50]}",
                    "marker": text,
                    "note": "",
                    "queries": queries,
                })
    return sections


def write_starter_config(pdf_path, out_path, prefer):
    sections = scan_pdf(pdf_path)
    config = {
        "_readme": [
            "STARTER CONFIG produced by 'scan'. Review before building:",
            "1. Delete sections you don't want illustrated.",
            "2. Refine 'queries' -- these are auto-seeded from the heading text",
            "   and usually need sharpening (add a medium, culture, or synonym).",
            "3. Optionally set 'prefer' to bias toward a period/culture.",
            "4. Each 'marker' must appear verbatim in the PDF; leave as-is unless",
            "   you change the text.",
        ],
        "prefer": prefer or [],
        "sections": sections,
    }
    out_path.write_text(json.dumps(config, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    return len(sections)


# --------------------------------------------------------------------------
# PDF assembly
# --------------------------------------------------------------------------

def load_sections(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    secs = [Section(sid=d["id"], marker=d["marker"], queries=d.get("queries", []),
                    note=d.get("note", "")) for d in raw["sections"]]
    return secs, raw.get("prefer", []), raw.get("period", "any")


def locate_sections(doc, sections):
    for sec in sections:
        for pno in range(doc.page_count):
            if doc[pno].search_for(sec.marker):
                sec.page_index = pno
                break


def _image_aspect(path):
    """width / height of the artwork file."""
    try:
        pm = fitz.Pixmap(str(path))
        ar = pm.width / pm.height if pm.height else 1.0
        return ar
    except Exception:
        return 1.0


def _find_gaps(page, margin=54):
    """Return vertical blank intervals (top, bottom) on the page, largest-first,
    computed from the existing text/image blocks so nothing is overlapped."""
    h = page.rect.height
    blocks = [b for b in page.get_text("blocks") if b[4].strip()]
    # include image blocks so we don't stack on existing pictures
    for img in page.get_image_info():
        bb = img.get("bbox")
        if bb:
            blocks.append((bb[0], bb[1], bb[2], bb[3], "", 0, 1))
    spans = sorted((b[1], b[3]) for b in blocks)
    gaps, cursor = [], margin
    for y0, y1 in spans:
        if y0 - cursor > 40:
            gaps.append((cursor, y0))
        cursor = max(cursor, y1)
    if h - margin - cursor > 40:
        gaps.append((cursor, h - margin))
    gaps.sort(key=lambda g: g[1] - g[0], reverse=True)
    return gaps


def _place_in_gap(page, gap, art, sec, plate_no, margin=54):
    """Fit the image (tool-picked size) into a blank gap, caption below."""
    w = page.rect.width
    cap_h = 30
    top, bot = gap
    avail_h = (bot - top) - cap_h
    avail_w = w - 2 * margin
    ar = _image_aspect(art.image_path)
    if avail_w / avail_h > ar:
        ih = avail_h
        iw = ih * ar
    else:
        iw = avail_w
        ih = iw / ar
    cx = w / 2
    box = fitz.Rect(cx - iw / 2, top + 4, cx + iw / 2, top + 4 + ih)
    page.insert_image(box, filename=str(art.image_path), keep_proportion=True)
    lines = [f"Plate {plate_no}."]
    if sec.note:
        lines.append(sec.note)
    lines.append(art.citation())
    page.insert_textbox(
        fitz.Rect(margin, box.y1 + 3, w - margin, box.y1 + 3 + cap_h),
        "\n".join(lines), fontsize=8, fontname="Times-Italic",
        align=fitz.TEXT_ALIGN_CENTER, color=(0.15, 0.15, 0.15))


def _spill_page(doc, after_page, art, sec, plate_no):
    """No room on the page: create a dedicated plate page right after it."""
    w, h = doc[after_page].rect.width, doc[after_page].rect.height
    page = doc.new_page(pno=after_page + 1, width=w, height=h)
    margin, cap_h = 54, 110
    page.insert_image(fitz.Rect(margin, margin, w - margin, h - margin - cap_h),
                      filename=str(art.image_path), keep_proportion=True)
    lines = [f"Plate {plate_no}."]
    if sec.note:
        lines.append(sec.note)
    lines.append(art.citation())
    page.insert_textbox(
        fitz.Rect(margin, h - margin - cap_h + 8, w - margin, h - margin + 10),
        "\n".join(lines), fontsize=8.5, fontname="Times-Italic",
        align=fitz.TEXT_ALIGN_CENTER, color=(0.15, 0.15, 0.15))


# Minimum gap height (points) worth placing an image into before we spill.
MIN_INLINE_GAP = 150


def insert_plate(doc, page_index, sec, plate_no):
    """Place the artwork in the largest blank gap on the section's own page,
    preferring gaps that sit at or below the section marker so the image lands
    near the relevant text. Returns True if a new (spill) page was added."""
    page = doc[page_index]
    gaps = _find_gaps(page)

    # Only consider blank space at or below the section heading, so the image
    # never lands before the chapter starts.
    marker_bottom = 0
    hits = page.search_for(sec.marker)
    if hits:
        marker_bottom = max(r.y1 for r in hits)
    below = [g for g in gaps
             if g[0] >= marker_bottom and (g[1] - g[0]) >= MIN_INLINE_GAP]
    candidate = min(below, key=lambda g: g[0]) if below else None

    if candidate:
        _place_in_gap(page, candidate, sec.artwork, sec, plate_no)
        return False
    # No suitable space below the heading -> dedicated page after this one.
    _spill_page(doc, page_index, sec.artwork, sec, plate_no)
    return True


def append_credits(doc, placed):
    w, h = doc[0].rect.width, doc[0].rect.height
    page = doc.new_page(width=w, height=h)
    margin = 60
    page.insert_textbox(fitz.Rect(margin, margin, w - margin, margin + 30),
                        "List of Plates", fontsize=16, fontname="Times-Bold")
    y = margin + 44
    for n, sec in placed:
        box = fitz.Rect(margin, y, w - margin, y + 70)
        used = page.insert_textbox(
            box, f"Plate {n}  ({sec.sid}).  {sec.artwork.citation()}",
            fontsize=9, fontname="Times-Roman")
        y += (70 - used) + 10
        if y > h - margin - 70:
            page = doc.new_page(width=w, height=h)
            y = margin


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def build_clients(session, args):
    """Lean default: two fast, classical-rich museums queried in parallel.
    Extra museums stay available behind keys but are off by default for speed."""
    import os
    clients = [
        MetClient(session),
        ClevelandClient(session),
    ]
    rijks = getattr(args, "rijks_key", None) or os.environ.get("RIJKS_API_KEY")
    if rijks:
        clients.append(RijksClient(session, rijks))
    si = getattr(args, "smithsonian_key", None) or os.environ.get("SMITHSONIAN_API_KEY")
    if si:
        clients.append(SmithsonianClient(session, si))
    harvard = getattr(args, "harvard_key", None) or os.environ.get("HARVARD_API_KEY")
    if harvard:
        clients.append(HarvardClient(session, harvard))
    return clients


def cmd_scan(args):
    out = args.output or args.pdf.with_suffix(".json")
    n = write_starter_config(args.pdf, out,
                             args.prefer.split(",") if args.prefer else [])
    if n == 0:
        print("No headings detected (the PDF may have no font-size structure, "
              "e.g. a scan). Write the config by hand from the template in the "
              "README instead.")
    else:
        print(f"Detected {n} heading(s). Wrote starter config: {out}\n"
              "Open it, refine the queries, delete unwanted sections, then run "
              "'build'.")


def cmd_build(args):
    out = args.output or args.pdf.with_name(args.pdf.stem + "_illustrated.pdf")
    sections, prefer, period = load_sections(args.config)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    clients = build_clients(session, args)
    print("Museums queried: " + ", ".join(c.name for c in clients) + "\n")

    doc = fitz.open(args.pdf)
    locate_sections(doc, sections)
    found = [s for s in sections if s.page_index is not None]
    missing = [s for s in sections if s.page_index is None]
    if missing:
        print("Markers not found (fix these in the config to match the PDF):")
        for s in missing:
            print(f"  - {s.sid}: \"{s.marker}\"")
    if not found:
        sys.exit("No section markers matched; nothing to do.")

    print(f"\nSearching museum collections for {len(found)} section(s)...\n")
    searchable = [s for s in found if s.queries]

    def _do(sec):
        return sec, find_artwork(sec, clients, prefer, args.min_score,
                                 args.verbose, period)

    # Search all sections concurrently.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(searchable)))) as ex:
        for sec, art in ex.map(_do, searchable):
            if art is None:
                print(f"  {sec.sid}: no suitable object; skipping.")
                continue
            sec.artwork = art
            print(f"  {sec.sid} -> {art.title}  [{art.museum}]  score {art.score:.2f}")

    matched = [s for s in found if s.artwork]
    if args.list:
        print(f"\nPreview only ({len(matched)} plates would be inserted). "
              "Re-run without --list to build.")
        return

    # Download all chosen images concurrently.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(matched)))) as ex:
        list(ex.map(lambda s: download_image(s.artwork, session), matched))
    matched = [s for s in matched if s.artwork.image_path]
    matched.sort(key=lambda s: s.page_index)
    placed = []
    spill_offset = 0  # each spill page pushes later sections down by one
    for i, sec in enumerate(matched):
        added = insert_plate(doc, sec.page_index + spill_offset, sec, i + 1)
        if added:
            spill_offset += 1
        placed.append((i + 1, sec))
    doc.save(out, deflate=True)
    print(f"\nWrote {out}  ({len(placed)} images placed).")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Illustrate any PDF with cited museum artwork.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="detect headings and write a starter config")
    sp.add_argument("pdf", type=Path)
    sp.add_argument("-o", "--output", type=Path, default=None)
    sp.add_argument("--prefer", default="", help="comma-separated bias terms, e.g. 'roman,greek'")
    sp.set_defaults(func=cmd_scan)

    bp = sub.add_parser("build", help="produce the illustrated PDF from a config")
    bp.add_argument("pdf", type=Path)
    bp.add_argument("--config", type=Path, required=True)
    bp.add_argument("-o", "--output", type=Path, default=None)
    bp.add_argument("--min-score", type=float, default=0.6)
    bp.add_argument("--list", action="store_true", help="preview without writing")
    bp.add_argument("--verbose", action="store_true")
    bp.add_argument("--rijks-key", default=None,
                    help="Rijksmuseum API key (or set RIJKS_API_KEY)")
    bp.add_argument("--smithsonian-key", default=None,
                    help="Smithsonian/data.gov API key (or SMITHSONIAN_API_KEY)")
    bp.add_argument("--harvard-key", default=None,
                    help="Harvard Art Museums API key (or HARVARD_API_KEY)")
    bp.set_defaults(func=cmd_build)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
