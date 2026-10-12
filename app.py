"""
PhD Position Finder, Skills Analyzer & PDF Merger  (Streamlit)

Run:
    pip install -r requirements.txt
    playwright install chromium        # optional: better page->PDF rendering
    streamlit run app.py

Workflow
 1. Tick sites (or add your own domains), type keywords, press Search.
    Each site becomes a query like
        site:jobbnorge.no/en/available-jobs/joblisting ("PhD" OR "doctoral") "reservoir"
    run through DuckDuckGo (no key) or Google Programmable Search (API key).
 2. Every hit is downloaded and analysed (SSRF-safe, size-limited): title, organisation,
    posted date, deadline (+ where it came from), open/closed, PhD / keyword match.
 3. Filters (open/closed, date interval, PhD-only, keyword-only) are applied live.
 4. Results are stored in a local SQLite database (review status, manual deadlines).
 5. Skills analysis counts recurring skill areas / tools with auditable evidence snippets.
 6. Tick positions -> pages become PDFs (or originals are downloaded) -> ONE merged PDF
    with a cover index + bookmarks, plus a ZIP with the separate PDFs and CSVs.

Extracted fields are CANDIDATES, not verified facts. Always check the source page.
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
import zipfile
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.sax.saxutils import escape

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

st.set_page_config(page_title="PhD Position Finder", page_icon="🎓", layout="wide")

DB_PATH = Path(os.environ.get("PHD_FINDER_DB", "phd_positions.sqlite3"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"}
REQUEST_TIMEOUT = 20
MAX_BYTES = 25 * 1024 * 1024

# ======================================================================================
# 1. SITE LIST (edit freely!)   query = what goes after  site:   ("" = whole web)
# ======================================================================================


@dataclass(frozen=True)
class Site:
    name: str
    query: str
    region: str
    default: bool = False


SITES = [
    # ---- Europe / global aggregators
    Site("EURAXESS", "euraxess.ec.europa.eu/jobs", "Europe", True),
    Site("Academic Positions", "academicpositions.com", "Europe", True),
    Site("Times Higher Education Unijobs", "timeshighereducation.com/unijobs", "Europe"),
    Site("FindAPhD", "findaphd.com", "Global", True),
    Site("PhDportal", "phdportal.com", "Global"),
    Site("ScholarshipDB", "scholarshipdb.net", "Global"),
    Site("Nature Careers", "jobs.nature.com", "Global"),
    Site("Science Careers", "jobs.sciencecareers.org", "Global"),
    # ---- Norway
    Site("Jobbnorge", "jobbnorge.no/en/available-jobs/joblisting", "Norway", True),
    Site("NTNU", "ntnu.edu", "Norway", True),
    Site("University of Stavanger", "uis.no", "Norway"),
    Site("University of Oslo", "uio.no", "Norway"),
    Site("University of Bergen", "uib.no", "Norway"),
    Site("UiT", "uit.no", "Norway"),
    # ---- Netherlands
    Site("AcademicTransfer", "academictransfer.com", "Netherlands", True),
    Site("TU Delft", "tudelft.nl", "Netherlands"),
    Site("Wageningen (WUR)", "wur.nl", "Netherlands"),
    # ---- Denmark / Finland
    Site("DTU", "dtu.dk", "Denmark"),
    Site("University of Copenhagen", "ku.dk", "Denmark"),
    Site("Aalto", "aalto.fi", "Finland"),
    Site("University of Helsinki", "helsinki.fi", "Finland"),
    Site("University of Oulu", "oulu.fi", "Finland"),
    # ---- UK / Ireland
    Site("jobs.ac.uk", "jobs.ac.uk", "UK"),
    Site("Heriot-Watt", "hw.ac.uk", "UK"),
    Site("Imperial College", "imperial.ac.uk", "UK"),
    Site("University of Manchester", "manchester.ac.uk", "UK"),
    Site("All UK universities (ac.uk)", "ac.uk", "UK"),
    # ---- Germany
    Site("academics.de", "academics.de", "Germany"),
    Site("DAAD", "daad.de", "Germany"),
    Site("RWTH Aachen", "rwth-aachen.de", "Germany"),
    Site("TU Munich", "tum.de", "Germany"),
    Site("University of Stuttgart", "uni-stuttgart.de", "Germany"),
    # ---- Sweden
    Site("Varbi (Swedish universities)", "varbi.com", "Sweden"),
    Site("Chalmers", "chalmers.se", "Sweden"),
    Site("KTH", "kth.se", "Sweden"),
    Site("Lund University", "lu.se", "Sweden"),
    Site("Uppsala University", "uu.se", "Sweden"),
    # ---- Canada
    Site("University Affairs (CA)", "universityaffairs.ca", "Canada"),
    # ---- Australia / NZ
    Site("All Australian universities (edu.au)", "edu.au", "Australia"),
    Site("All NZ universities (ac.nz)", "ac.nz", "New Zealand"),
    # ---- USA
    Site("AcademicJobsOnline", "academicjobsonline.org", "USA"),
    Site("HigherEdJobs", "higheredjobs.com", "USA"),
    Site("Chronicle Jobs", "jobs.chronicle.com", "USA"),
    Site("Inside Higher Ed Careers", "careers.insidehighered.com", "USA"),
    Site("AcademicKeys", "academickeys.com", "USA"),
    Site("All US universities (.edu)", "edu", "USA"),
    # ---- Whole web
    Site("Open web (no site filter)", "", "Open web"),
]

REGION_ORDER = ["Europe", "Global", "Norway", "Netherlands", "Denmark", "Finland", "UK", "Germany",
                "Sweden", "Canada", "Australia", "New Zealand", "USA", "Open web"]
COUNTRY_REGIONS = {"Norway": "Norway", "Netherlands": "Netherlands", "Denmark": "Denmark",
                   "Finland": "Finland", "UK": "United Kingdom", "Germany": "Germany",
                   "Sweden": "Sweden", "Canada": "Canada", "Australia": "Australia",
                   "New Zealand": "New Zealand", "USA": "United States"}

KEYWORD_PRESETS = {
    "(none)": "",
    "Reservoir simulation": "reservoir simulation, reservoir modelling",
    "CCS / CO2 storage": "CO2 storage, carbon capture",
    "Geothermal": "geothermal",
    "Drilling & wells": "drilling, wellbore, well",
    "Porous media / multiphase flow": "porous media, multiphase flow",
    "Subsurface + machine learning": "machine learning, reservoir, subsurface",
}

# ======================================================================================
# 2. SKILL TAXONOMY (screening heuristic; evidence snippets are always provided)
# ======================================================================================

SKILL_TAXONOMY = {
    "Reservoir simulation": [
        "reservoir simulation", "reservoir modelling", "reservoir modeling", "flow simulation",
        "compositional simulation", "black-oil", "history matching", "cmg", "eclipse",
        "tnavigator", "intersect", "opm flow", "mrst", "imex", "gem", "stars"],
    "Programming & software development": [
        "python", "matlab", "c++", "fortran", "julia", "software development", "git",
        "github", "version control", "jupyter", "linux"],
    "Machine learning & data science": [
        "machine learning", "deep learning", "artificial intelligence", "random forest",
        "neural network", "data science", "data-driven", "surrogate model", "shap",
        "pytorch", "tensorflow", "scikit-learn", "physics-informed"],
    "Numerical methods & HPC": [
        "numerical methods", "numerical modelling", "numerical modeling", "finite element",
        "finite volume", "computational fluid dynamics", "cfd", "high-performance computing",
        "hpc", "parallel computing", "inverse problem", "pde", "partial differential"],
    "Porous media & multiphase flow": [
        "porous media", "porous medium", "multiphase flow", "relative permeability",
        "capillary pressure", "darcy", "pore-scale", "pore scale", "reactive transport",
        "digital rock", "core flooding", "core-flooding"],
    "History matching, uncertainty & optimization": [
        "history matching", "uncertainty quantification", "uncertainty analysis",
        "data assimilation", "ensemble smoother", "ensemble kalman", "parameter estimation",
        "optimization", "optimisation", "sensitivity analysis", "monte carlo", "bayesian"],
    "CCS & subsurface storage": [
        "carbon capture", "ccs", "ccus", "co2 storage", "co2-storage", "geological storage",
        "carbon sequestration", "hydrogen storage", "underground hydrogen", "subsurface storage",
        "co₂"],
    "Geothermal energy": [
        "geothermal", "enhanced geothermal", "heat storage", "thermal energy storage"],
    "Drilling & well engineering": [
        "drilling", "well control", "well integrity", "drilling fluids", "cementing",
        "managed pressure drilling", "wellbore", "borehole", "directional drilling"],
    "Production, EOR & flow assurance": [
        "production engineering", "enhanced oil recovery", "eor", "flow assurance",
        "artificial lift", "well test", "production optimization", "chemical flooding",
        "polymer flooding", "gas injection", "low salinity"],
    "Geomechanics & rock physics": [
        "geomechanics", "rock mechanics", "rock physics", "fracture", "hydraulic fracturing",
        "stress field", "poroelasticity", "geomechanical"],
    "Geochemistry & reactive transport": [
        "geochemistry", "geochemical", "mineralization", "mineral trapping", "phreeqc"],
    "Scientific writing & collaboration": [
        "scientific writing", "peer-reviewed", "publications", "interdisciplinary",
        "collaboration", "communication skills", "teamwork", "english"],
}
SKILL_PATTERNS = {
    cat: [(t, re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.I)) for t in terms]
    for cat, terms in SKILL_TAXONOMY.items()}

NEG_CUES = ["not required", "not essential", "no experience required", "no prior experience",
            "not necessary", "not a requirement"]
REQ_CUES = ["required", "must have", "must possess", "must hold", "essential", "minimum requirement",
            "we require", "mandatory", "prerequisite", "you have", "you hold", "you will have",
            "candidate must", "applicant must", "solid knowledge", "strong background", "proven"]
PREF_CUES = ["preferred", "desirable", "advantage", "ideally", "beneficial", "is a plus",
             "nice to have", "we welcome", "an asset", "meritorious", "considered a plus"]
REQ_HEADERS = ["requirements", "qualifications", "your profile", "we expect", "what you bring",
               "who you are", "what we are looking for", "you have"]
PREF_HEADERS = ["desirable", "preferred", "advantageous", "nice to have", "would be a plus",
                "additional qualifications", "meritorious"]
RANK = {"Required cue": 4, "Preferred cue": 3, "Mentioned only": 2, "Negated": 1}

# ======================================================================================
# 3. URL SAFETY + HTTP
# ======================================================================================


FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


@lru_cache(maxsize=1024)
def is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if ip in FAKE_IP_NET:  # VPN/proxy "fake-ip" DNS (198.18.0.0/15) - not a real private network
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def is_safe_url(url: str) -> bool:
    """HTTP(S) only, no credentials, no local/private targets (basic SSRF guard)."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return False
        host = p.hostname.lower().rstrip(".")
        if host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".localhost")):
            return False
        return is_public_host(host)
    except Exception:
        return False


def safe_get(url: str, max_bytes: int = MAX_BYTES, timeout: int = REQUEST_TIMEOUT):
    """GET with manual redirect validation and a hard size cap. -> (bytes, final_url, content_type)"""
    cur = url
    with requests.Session() as s:
        s.headers.update(HEADERS)
        for _ in range(6):
            if not is_safe_url(cur):
                raise ValueError("URL blocked by safety checks")
            r = s.get(cur, timeout=timeout, stream=True, allow_redirects=False)
            try:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if not loc:
                        raise ValueError("redirect without Location")
                    cur = urljoin(cur, loc)
                    continue
                r.raise_for_status()
                try:
                    if int(r.headers.get("Content-Length") or 0) > max_bytes:
                        raise ValueError("file too large")
                except ValueError as e:
                    if "too large" in str(e):
                        raise
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise ValueError("file too large")
                return bytes(buf), cur, r.headers.get("content-type", "").lower()
            finally:
                r.close()
    raise ValueError("too many redirects")


def canonicalize_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        host = p.netloc.lower().removeprefix("www.")
        path = re.sub(r"/{2,}", "/", p.path or "/")
        if path != "/":
            path = path.rstrip("/")
        drop = {"fbclid", "gclid", "ref", "mc_cid", "mc_eid"}
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in drop]
        return urlunparse((p.scheme.lower(), host, path, "", urlencode(sorted(q)), ""))
    except Exception:
        return ""


def safe_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "; ".join(safe_text(x) for x in v if x is not None)
    if isinstance(v, dict):
        return safe_text(v.get("name") or v.get("value") or "")
    return re.sub(r"\s+", " ", str(v)).strip()


# ======================================================================================
# 4. SEARCH
# ======================================================================================

PHD_TERMS = '("PhD" OR "doctoral" OR "doctorate" OR "PhD position")'
PHD_RE = re.compile(r"\bph\.?\s?d\b|doctoral|doctorate|promovend|doktorand|doktorgrad|stipendiat|"
                    r"\bdphil\b|research fellow.{0,20}(?:phd|doctoral)", re.I)
POSTDOC_RE = re.compile(r"post[\s-]?doc|postdoctoral|professor|lecturer|assistant professor", re.I)


def build_query(site: Site, kws, mode, add_phd):
    """Build a search-engine-friendly query without forcing PhD terms when disabled."""
    quoted = [f'"{k.strip()}"' for k in kws if k.strip()]
    if len(quoted) > 1 and mode == "any":
        kw_part = "(" + " OR ".join(quoted) + ")"
    else:
        # In ALL mode, requiring each phrase is intentional.
        kw_part = " ".join(quoted)
    parts = []
    if site.query:
        # Use the host for the main query. URL paths are inconsistent in search indexes.
        host = site.query.split("/")[0].strip()
        if host:
            parts.append(f"site:{host}")
    if add_phd:
        parts.append(PHD_TERMS)
    if kw_part:
        parts.append(kw_part)
    return " ".join(parts)


def _patch_ddgs_google():
    """Remove accidental US/English-only restrictions from ddgs' Google payload, if supported."""
    try:
        from ddgs.engines.google import Google
        orig = getattr(Google, "build_payload", None)
        if not orig or getattr(orig, "_phdfinder_patched", False):
            return

        def build_payload(self, *args, **kwargs):
            payload = orig(self, *args, **kwargs)
            if isinstance(payload, dict):
                payload.pop("cr", None)
                payload.pop("lr", None)
            return payload

        build_payload._phdfinder_patched = True
        Google.build_payload = build_payload
    except Exception:
        # Different ddgs versions have different internals; search must still work without this patch.
        pass


def available_text_backends():
    """Return backend names supported by the installed ddgs version."""
    try:
        from ddgs.engines import ENGINES
        names = ENGINES.get("text", {})
        return set(names.keys() if isinstance(names, dict) else names)
    except Exception:
        # Let DDGS itself validate names on versions that don't expose ENGINES.
        return set()


def search_engine(q, n, backend):
    """Run one explicit ddgs text backend and normalize its result format."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("Package 'ddgs' is missing. Add 'ddgs' to requirements.txt and redeploy.") from exc

    _patch_ddgs_google()
    valid = available_text_backends()
    # The package's auto mode can choose among providers instead of relying on one.
    if backend != "auto" and valid and backend not in valid:
        raise RuntimeError(f"Backend '{backend}' is not supported by this ddgs version. Available: {', '.join(sorted(valid))}")

    # Avoid forcing US-only results; selected domains are checked locally afterward.
    with DDGS() as d:
        results = d.text(q, region="wt-wt", max_results=n, backend=backend)

    out = []
    for result in results or []:
        url = result.get("href") or result.get("url")
        if not url or not url.startswith(("http://", "https://")):
            continue
        if re.search(r"//[^/]*(wikipedia|grokipedia)\.", url, re.I):
            continue
        out.append({
            "url": url,
            "title": result.get("title", ""),
            "snippet": result.get("body", "") or result.get("snippet", ""),
            "engine": backend,
        })
    return out


# Try multiple explicit providers. The installed ddgs package may not support every backend;
# unsupported backends are recorded in diagnostics and skipped without disabling other engines.
SEARCH_CHAINS = {
    "google": ["auto", "google", "duckduckgo", "brave", "bing", "mojeek"],
    "duckduckgo": ["auto", "duckduckgo", "brave", "google", "bing", "mojeek"],
}


def search_chain(q, n, first, dead=None, siteq=""):
    """Collect and deduplicate results across providers until enough eligible hits are found."""
    notes = []
    collected = {}
    candidates = SEARCH_CHAINS.get(first, [first])
    valid = available_text_backends()
    if valid:
        candidates = [backend for backend in candidates if backend == "auto" or backend in valid]
    if not candidates:
        return [], [("ddgs", 0, "ERROR: no compatible text backend; upgrade ddgs in requirements.txt")]

    for backend in candidates:
        try:
            results = search_engine(q, n, backend)
            added = 0
            for result in results:
                result_url = result.get("url", "")
                # When searching a particular website, don't let off-domain results stop the fallback chain.
                if siteq and not url_matches_site(result_url, siteq):
                    continue
                key = canonicalize_url(result_url) or result_url
                if key and key not in collected:
                    collected[key] = result
                    added += 1
            notes.append((backend, len(results), f"{added} new eligible unique URLs"))
            if len(collected) >= n:
                break
        except Exception as exc:
            message = str(exc).replace("\n", " ")[:130]
            notes.append((backend, 0, "ERROR: " + message))
        # Small pause helps reduce rate-limit/CAPTCHA risk; don't mark an engine permanently dead
        # because a single request can fail transiently.
        time.sleep(0.35)
    return list(collected.values())[:n], notes


def url_matches_site(url, siteq):
    """Locally enforce the requested domain when a site-specific search is selected."""
    host_query = (siteq or "").split("/")[0].lower().strip().removeprefix("www.")
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return bool(host_query) and (host == host_query or host.endswith("." + host_query))


def query_variants(site, kws, mode, add_phd):
    """Progressively broaden a query: host + terms, host + first term, then general terms."""
    clean_kws = [str(k).strip() for k in kws if str(k).strip()]
    if not clean_kws:
        return []

    host = site.query.split("/")[0].strip() if site.query else ""
    variants = [build_query(site, clean_kws, mode, add_phd)]
    first_kw = f'"{clean_kws[0]}"'
    phd = PHD_TERMS if add_phd else ""
    if host:
        variants.append(" ".join(p for p in [f"site:{host}", phd, first_kw] if p))
        variants.append(" ".join(p for p in [f"site:{host}", first_kw] if p))
        # Broaden the engine query but keep the local domain check in search_chain.
        variants.append(" ".join(p for p in [phd, first_kw] if p))
        variants.append(" ".join(clean_kws))
    else:
        variants.append(" ".join(p for p in [phd, first_kw] if p))
        variants.append(" ".join(clean_kws))

    seen, unique = set(), []
    for query in variants:
        query = query.strip()
        if query and query not in seen:
            seen.add(query)
            unique.append(query)
    return unique


def search_google(q, n, key, cx):
    out, start = [], 1
    while len(out) < n and start <= 91:
        r = requests.get("https://www.googleapis.com/customsearch/v1",
                         params={"key": key, "cx": cx, "q": q, "num": 10, "start": start}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Google API error {r.status_code}: {r.text[:200]}")
        items = r.json().get("items", [])
        if not items:
            break
        out += [{"url": i["link"], "title": i.get("title", ""), "snippet": i.get("snippet", "")}
                for i in items]
        start += 10
    return out[:n]


# ======================================================================================
# 5. PAGE ANALYSIS
# ======================================================================================

DATE_RE = (r"(?P<d>\d{4}-\d{2}-\d{2}"
           r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
           r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?[A-Za-z]{3,9}\.?,?\s+\d{4}"
           r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})")
DEADLINE_LABEL = (r"(?:application\s+deadline|deadline|closing\s+date|closes?|apply\s+(?:by|before)|"
                  r"applications?\s+(?:close|due)|due\s+date|last\s+day\s+to\s+apply|"
                  r"søknadsfrist|frist|sluitingsdatum|bewerbungsfrist|ansökan\s+senast)")
POSTED_LABEL = (r"(?:date\s+posted|posted(?:\s+on)?|published(?:\s+on)?|publication\s+date|"
                r"date\s+published|publisert|listed(?:\s+on)?)")
CLOSED_RE = re.compile(r"no longer (?:available|accepting)|position (?:has been )?(?:closed|filled)|"
                       r"(?:job|vacancy|position|advert) (?:has )?expired|applications? (?:are |is )?closed|"
                       r"this (?:job|vacancy) is closed", re.I)


def parse_date(s):
    try:
        d = dtparser.parse(s, dayfirst=not re.match(r"\s*\d{4}-", s), fuzzy=True)
        if 2000 <= d.year <= 2100:
            return d.date()
    except Exception:
        pass
    return None


def labeled_date(flat, label):
    for m in re.finditer(label + r".{0,60}?" + DATE_RE, flat, re.I):
        d = parse_date(m.group("d"))
        if d:
            return d
    return None


def find_jobposting(obj):
    if isinstance(obj, list):
        for o in obj:
            x = find_jobposting(o)
            if x:
                return x
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return obj
        return find_jobposting(obj.get("@graph"))
    return None


def keyword_hit(text_l, kws, mode):
    if not kws:
        return True
    hits = [k.lower() in text_l for k in kws]
    return any(hits) if mode == "any" else all(hits)


def phd_flag(title, text):
    if PHD_RE.search(title):
        return True
    if POSTDOC_RE.search(title):
        return False
    return bool(PHD_RE.search(text[:25000]))


def compute_status(deadline, page_closed=False, fallback="unknown"):
    if deadline:
        return "open" if deadline >= dt.date.today() else "closed"
    return "closed" if page_closed else fallback


def new_rec(url, hit=None):
    hit = hit or {}
    return dict(url=url, canonical_url=canonicalize_url(url) or url, title=hit.get("title", ""),
                site=hit.get("site", ""), org="", location="", country=hit.get("country", ""),
                posted=None, deadline=None, deadline_source="", status="unknown", is_pdf=False,
                content=None, text="", snippet=hit.get("snippet", ""), has_phd=False, has_kw=False,
                search_query=hit.get("query", ""), review_status="Needs review", error="",
                upload=False)


def analyze(hit):
    """Download one hit and extract fields. Never raises."""
    rec = new_rec(hit["url"], hit)
    page_closed = False
    try:
        data, final_url, ctype = safe_get(hit["url"])
        rec["url"] = final_url
        rec["canonical_url"] = canonicalize_url(final_url) or rec["canonical_url"]
        if "pdf" in ctype or data[:5] == b"%PDF-":
            rec["is_pdf"] = True
            rec["content"] = data
            try:
                reader = PdfReader(io.BytesIO(data), strict=False)
                rec["text"] = "\n".join((p.extract_text() or "") for p in reader.pages[:6])
            except Exception:
                pass
            if not rec["title"]:
                rec["title"] = Path(urlparse(final_url).path).name or final_url
        else:
            soup = BeautifulSoup(data, "html.parser")
            jp = None
            for tag in soup.find_all("script", type="application/ld+json"):
                try:
                    jp = find_jobposting(json.loads(tag.string or tag.get_text() or ""))
                except Exception:
                    jp = None
                if jp:
                    break
            title = ""
            if jp:
                title = safe_text(jp.get("title"))
                rec["org"] = safe_text(jp.get("hiringOrganization"))
                loc = jp.get("jobLocation", "")
                if isinstance(loc, list):
                    loc = loc[0] if loc else ""
                if isinstance(loc, dict):
                    addr = loc.get("address", {})
                    if isinstance(addr, dict):
                        rec["location"] = ", ".join(filter(None, [safe_text(addr.get("addressLocality")),
                                                                  safe_text(addr.get("addressRegion"))]))
                        rec["country"] = safe_text(addr.get("addressCountry")) or rec["country"]
                if jp.get("datePosted"):
                    rec["posted"] = parse_date(str(jp["datePosted"]))
                if jp.get("validThrough"):
                    rec["deadline"] = parse_date(str(jp["validThrough"]))
                    if rec["deadline"]:
                        rec["deadline_source"] = "JSON-LD validThrough (verify)"
            if not title:
                og = soup.find("meta", property="og:title")
                h1 = soup.find("h1")
                title = (og.get("content") if og and og.get("content")
                         else h1.get_text(" ", strip=True) if h1
                         else soup.title.get_text(strip=True) if soup.title else rec["title"])
            if not rec["org"]:
                og = soup.find("meta", property="og:site_name")
                rec["org"] = safe_text(og.get("content")) if og and og.get("content") else ""
            rec["title"] = re.sub(r"\s+", " ", title or "").strip()[:200] or rec["title"]
            for t in soup(["script", "style", "noscript", "svg"]):
                t.decompose()
            rec["text"] = soup.get_text("\n", strip=True)[:40000]
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        rec["text"] = hit.get("snippet", "")

    blob = rec["title"] + "\n" + rec["text"] + "\n" + rec.get("snippet", "")
    flat = re.sub(r"\s+", " ", blob[:60000])
    rec["has_phd"] = phd_flag(rec["title"], rec["text"] + "\n" + rec.get("snippet", ""))
    if not rec["deadline"]:
        rec["deadline"] = labeled_date(flat, DEADLINE_LABEL)
        if rec["deadline"]:
            rec["deadline_source"] = "Text near deadline label (verify)"
    if not rec["posted"]:
        rec["posted"] = labeled_date(flat, POSTED_LABEL)
    page_closed = bool(CLOSED_RE.search(flat[:5000]))
    rec["status"] = compute_status(rec["deadline"], page_closed)
    return rec


def refresh_flags(recs, kws, mode):
    """Keyword match is recomputed live, so changing keywords re-filters instantly."""
    for r in recs:
        blob = (r["title"] + "\n" + r["text"] + "\n" + r.get("snippet", "")).lower()
        r["has_kw"] = keyword_hit(blob, kws, mode)
        if r["deadline"]:
            r["status"] = compute_status(r["deadline"])


# ======================================================================================
# 6. FILTERING
# ======================================================================================


def passes(r, f):
    if f["require_phd"] and not r["has_phd"]:
        return False
    if f["require_kw"] and not r["has_kw"]:
        return False
    if f["status"] == "Open only":
        if r["status"] == "closed" or (r["status"] == "unknown" and not f["include_unknown"]):
            return False
    elif f["status"] == "Closed only" and r["status"] != "closed":
        return False
    if f["use_dates"]:
        d = r["deadline"] if f["basis"] == "Deadline" else r["posted"]
        if d is None:
            return f["include_unknown"]
        if not (f["start"] <= d <= f["end"]):
            return False
    return True


# ======================================================================================
# 7. SQLITE STORAGE
# ======================================================================================


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS positions (
            canonical_url TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, org TEXT, location TEXT,
            country TEXT, site TEXT, posted TEXT, deadline TEXT, deadline_source TEXT, status TEXT,
            is_pdf INTEGER, has_phd INTEGER, text TEXT, search_query TEXT,
            review_status TEXT DEFAULT 'Needs review', first_seen TEXT, last_seen TEXT)""")


def d2s(d):
    return d.isoformat() if d else ""


def s2d(s):
    try:
        return dt.date.fromisoformat(s) if s else None
    except ValueError:
        return None


UPSERT = """
INSERT INTO positions (canonical_url,url,title,org,location,country,site,posted,deadline,deadline_source,
    status,is_pdf,has_phd,text,search_query,first_seen,last_seen)
VALUES (:canonical_url,:url,:title,:org,:location,:country,:site,:posted,:deadline,:deadline_source,
    :status,:is_pdf,:has_phd,:text,:search_query,:now,:now)
ON CONFLICT(canonical_url) DO UPDATE SET
    url=excluded.url, title=excluded.title, org=excluded.org, location=excluded.location,
    country=excluded.country, site=excluded.site, posted=excluded.posted, is_pdf=excluded.is_pdf,
    has_phd=excluded.has_phd, text=excluded.text, search_query=excluded.search_query,
    last_seen=excluded.last_seen,
    deadline=CASE WHEN positions.deadline_source='Manual' THEN positions.deadline ELSE excluded.deadline END,
    status=CASE WHEN positions.deadline_source='Manual' THEN positions.status ELSE excluded.status END,
    deadline_source=CASE WHEN positions.deadline_source='Manual' THEN 'Manual' ELSE excluded.deadline_source END
"""


def save_records(recs):
    now = dt.datetime.now().replace(microsecond=0).isoformat()
    n = 0
    with db() as con:
        for r in recs:
            if r.get("upload") or r.get("error"):
                continue  # unreadable pages / uploads are not persisted
            con.execute(UPSERT, dict(
                canonical_url=r["canonical_url"], url=r["url"], title=r["title"], org=r["org"],
                location=r["location"], country=r["country"], site=r["site"], posted=d2s(r["posted"]),
                deadline=d2s(r["deadline"]), deadline_source=r["deadline_source"], status=r["status"],
                is_pdf=int(r["is_pdf"]), has_phd=int(r["has_phd"]), text=r["text"][:40000],
                search_query=r["search_query"], now=now))
            n += 1
    return n


def load_db_records():
    with db() as con:
        rows = [dict(x) for x in con.execute("SELECT * FROM positions ORDER BY last_seen DESC")]
    out = []
    for x in rows:
        r = new_rec(x["url"])
        r.update(canonical_url=x["canonical_url"], title=x["title"] or "", org=x["org"] or "",
                 location=x["location"] or "", country=x["country"] or "", site=x["site"] or "",
                 posted=s2d(x["posted"]), deadline=s2d(x["deadline"]),
                 deadline_source=x["deadline_source"] or "", is_pdf=bool(x["is_pdf"]),
                 has_phd=bool(x["has_phd"]), text=x["text"] or "", search_query=x["search_query"] or "",
                 review_status=x["review_status"] or "Needs review")
        r["status"] = compute_status(r["deadline"], fallback=x["status"] or "unknown")
        out.append(r)
    return out


def db_update_review(canon, status):
    with db() as con:
        con.execute("UPDATE positions SET review_status=? WHERE canonical_url=?", (status, canon))


def db_set_deadline(canon, d):
    with db() as con:
        con.execute("UPDATE positions SET deadline=?, deadline_source='Manual', status=? WHERE canonical_url=?",
                    (d.isoformat(), compute_status(d), canon))


def db_delete(canons):
    with db() as con:
        con.executemany("DELETE FROM positions WHERE canonical_url=?", [(c,) for c in canons])


# ======================================================================================
# 8. SKILLS ANALYSIS
# ======================================================================================


def _context(text, s, e, w=170):
    left, right = text[max(0, s - w):s], text[e:e + w]
    lm = list(re.finditer(r"[.!?;•]\s", left))
    if lm:
        left = left[lm[-1].end():]
    rm = re.search(r"[.!?;•](\s|$)", right)
    if rm:
        right = right[:rm.end()]
    return left, right


def _classify(text, s, e):
    left, right = _context(text, s, e)
    ctx = (left + text[s:e] + right).lower()
    if any(c in ctx for c in NEG_CUES):
        return "Negated", left + text[s:e] + right
    if any(c in ctx for c in REQ_CUES):
        return "Required cue", left + text[s:e] + right
    if any(c in ctx for c in PREF_CUES):
        return "Preferred cue", left + text[s:e] + right
    back = text[max(0, s - 600):s].lower()
    req_pos = max((back.rfind(h) for h in REQ_HEADERS), default=-1)
    pref_pos = max((back.rfind(h) for h in PREF_HEADERS), default=-1)
    if pref_pos > req_pos:
        return "Preferred cue", left + text[s:e] + right
    if req_pos >= 0:
        return "Required cue", left + text[s:e] + right
    return "Mentioned only", left + text[s:e] + right


def analyze_skills(recs):
    """-> (area summary, term summary, evidence table). One best snippet per term per vacancy."""
    ev = []
    for r in recs:
        text = re.sub(r"[ \t]+", " ", (r["title"] + ". " + r["text"]))
        text = re.sub(r"\s*\n\s*", " • ", text)
        for cat, pats in SKILL_PATTERNS.items():
            for term, pat in pats:
                best = None
                for m in pat.finditer(text):
                    cls, snip = _classify(text, m.start(), m.end())
                    if best is None or RANK[cls] > RANK[best[0]]:
                        best = (cls, snip)
                if best:
                    ev.append({"Skill area": cat, "Matched term": term, "Classification (heuristic)": best[0],
                               "Vacancy": r["title"], "Organisation": r["org"], "URL": r["url"],
                               "Evidence snippet": best[1].strip()[:420]})
    if not ev:
        e = pd.DataFrame()
        return e, e, e
    evd = pd.DataFrame(ev)
    per = (evd.assign(rank=evd["Classification (heuristic)"].map(RANK))
              .sort_values("rank", ascending=False)
              .drop_duplicates(["Skill area", "URL"]))
    n = max(1, len(recs))
    summ = per.groupby("Skill area").agg(
        **{"Vacancies mentioning": ("URL", "nunique"),
           "Required cue": ("Classification (heuristic)", lambda s: int((s == "Required cue").sum())),
           "Preferred cue": ("Classification (heuristic)", lambda s: int((s == "Preferred cue").sum())),
           "Mentioned only": ("Classification (heuristic)", lambda s: int((s == "Mentioned only").sum())),
           "Negated": ("Classification (heuristic)", lambda s: int((s == "Negated").sum()))}
    ).reset_index()
    summ["Share of vacancies (%)"] = (summ["Vacancies mentioning"] / n * 100).round(1)
    summ = summ.sort_values(["Vacancies mentioning", "Required cue"], ascending=False)
    terms = (evd.groupby(["Skill area", "Matched term"])["URL"].nunique().reset_index()
                .rename(columns={"URL": "Vacancies"}).sort_values("Vacancies", ascending=False))
    return summ, terms, evd


# ======================================================================================
# 9. PDF CREATION + MERGING
# ======================================================================================


def clean(s):
    return (s or "").encode("cp1252", "replace").decode("cp1252")


def fallback_pdf(rec):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50)
    ss = getSampleStyleSheet()
    flow = [Paragraph(escape(clean(rec["title"] or rec["url"])), ss["Title"]),
            Paragraph(f'<font color="blue">{escape(clean(rec["url"]))}</font>', ss["BodyText"]), Spacer(1, 12)]
    for line in [ln for ln in rec["text"].split("\n") if ln.strip()][:600]:
        flow.append(Paragraph(escape(clean(line[:1500])), ss["BodyText"]))
    doc.build(flow)
    return buf.getvalue()


def playwright_pdfs(urls):
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA)
        for u in urls:
            page = None
            try:
                page = ctx.new_page()
                page.goto(u, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.emulate_media(media="screen")
                out[u] = page.pdf(format="A4", print_background=True,
                                  margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
            except Exception:
                pass
            finally:
                if page:
                    page.close()
        browser.close()
    return out


def make_cover(recs):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=30, rightMargin=30, topMargin=36, bottomMargin=30)
    s = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=s["BodyText"], fontSize=7.5, leading=9)
    flow = [Paragraph("PhD positions - compiled " + dt.date.today().isoformat(), s["Title"]),
            Paragraph(f"{len(recs)} positions. Deadlines/status are extracted automatically and may be "
                      "wrong - verify on the source page.", s["BodyText"]), Spacer(1, 10)]
    data = [["#", "Title", "Site / organisation", "Deadline", "Status", "URL"]]
    for i, r in enumerate(recs, 1):
        data.append([str(i), Paragraph(escape(clean(r["title"])), small),
                     Paragraph(escape(clean(r["site"] + (" / " + r["org"] if r["org"] else ""))), small),
                     str(r["deadline"] or "?"), r["status"], Paragraph(escape(clean(r["url"])), small)])
    t = Table(data, colWidths=[20, 140, 95, 52, 45, 183], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f9")])]))
    flow.append(t)
    doc.build(flow)
    return buf.getvalue()


def recs_to_csv(recs):
    s = io.StringIO()
    wr = csv.writer(s)
    wr.writerow(["title", "site", "organisation", "country", "status", "deadline", "deadline_source",
                 "posted", "review_status", "url"])
    for r in recs:
        wr.writerow([r["title"], r["site"], r["org"], r["country"], r["status"], r["deadline"] or "",
                     r["deadline_source"], r["posted"] or "", r["review_status"], r["url"]])
    return s.getvalue()


def build_outputs(recs, use_pw, progress):
    pdfs, warn = {}, None
    need = [r["url"] for r in recs if not r["is_pdf"] and not r["upload"] and is_safe_url(r["url"])]
    if use_pw and need:
        for i in range(0, len(need), 4):
            try:  # separate thread -> no asyncio-loop clash with Streamlit
                with ThreadPoolExecutor(1) as ex:
                    pdfs.update(ex.submit(playwright_pdfs, need[i:i + 4]).result())
            except Exception as e:
                warn = f"Playwright unavailable ({str(e)[:120]}). Using plain-text fallback for pages."
                break
            progress(min(1.0, (i + 4) / len(need)) * 0.7, "Rendering pages with headless Chrome...")

    final, report = [], []
    for i, r in enumerate(recs):
        b, how = None, ""
        if r["is_pdf"]:
            b, how = r.get("content"), "original PDF"
            if b is None and not r["upload"]:
                try:
                    b = safe_get(r["url"])[0]
                except Exception:
                    b = None
        else:
            b, how = pdfs.get(r["url"]), "rendered page (Chrome)"
        if b:
            try:
                rd = PdfReader(io.BytesIO(b), strict=False)
                if rd.is_encrypted or len(rd.pages) == 0:
                    b = None
            except Exception:
                b = None
        if not b:
            b, how = fallback_pdf(r), "plain-text fallback"
        final.append(b)
        report.append({"title": r["title"], "url": r["url"], "method": how})
        progress(0.7 + 0.2 * (i + 1) / len(recs), "Checking PDFs...")

    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(make_cover(recs))), outline_item="Index")
    for r, b, rp in zip(recs, final, report):
        try:
            w.append(PdfReader(io.BytesIO(b), strict=False), outline_item=(r["title"] or r["url"])[:90])
        except Exception as e:
            rp["method"] += f" - MERGE FAILED ({type(e).__name__})"
    merged = io.BytesIO()
    w.write(merged)

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (r, b) in enumerate(zip(recs, final), 1):
            name = re.sub(r"[^\w\- ]+", "", r["title"])[:70].strip() or "position"
            z.writestr(f"{i:03d}_{name}.pdf", b)
        z.writestr("positions.csv", recs_to_csv(recs))
        z.writestr("processing_report.csv", pd.DataFrame(report).to_csv(index=False))
    progress(1.0, "Done")
    return merged.getvalue(), zbuf.getvalue(), warn, pd.DataFrame(report)


# ======================================================================================
# 10. UI HELPERS
# ======================================================================================


def show_df(df, **kw):
    try:
        st.dataframe(df, hide_index=True, width="stretch", **kw)
    except Exception:
        st.dataframe(df, hide_index=True, use_container_width=True, **kw)


def editor(df, **kw):
    try:
        return st.data_editor(df, hide_index=True, width="stretch", **kw)
    except Exception:
        return st.data_editor(df, hide_index=True, use_container_width=True, **kw)


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")


def secret(name):
    try:
        v = st.secrets.get(name)
    except Exception:
        v = None
    return v or os.environ.get(name, "")


def merge_recs(new):
    store = st.session_state.setdefault("recs", {})
    for r in new:
        store[r["canonical_url"]] = r


def run_analysis(hits, bar=None, workers=8):
    out = []
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(analyze, h) for h in hits]
        for j, fu in enumerate(as_completed(futs), 1):
            out.append(fu.result())
            if bar:
                bar.progress(j / len(futs), f"Reading pages {j}/{len(futs)}")
    return out


# ======================================================================================
# 11. APP
# ======================================================================================

init_db()
st.title("🎓 PhD Position Finder, Skills Analyzer & PDF Merger")

try:
    import playwright  # noqa: F401
    PW_OK = True
except ImportError:
    PW_OK = False

# ---------------- sidebar
with st.sidebar:
    st.header("Websites")
    selected_sites, by_region = [], {}
    for s in SITES:
        by_region.setdefault(s.region, []).append(s)
    for reg in REGION_ORDER:
        if reg not in by_region:
            continue
        with st.expander(reg, expanded=reg in ("Europe", "Norway", "Netherlands")):
            for s in by_region[reg]:
                if st.checkbox(s.name, value=s.default, key=f"site_{s.name}", help=s.query or "whole web"):
                    selected_sites.append(s)
    custom = st.text_area("Custom sites / domains (one per line)", placeholder="jobs.ethz.ch\nuni-heidelberg.de")
    for line in custom.splitlines():
        line = re.sub(r"^https?://", "", line.strip()).strip("/")
        if line:
            selected_sites.append(Site(line, line, "Custom"))

    st.header("Search engine")
    engine = st.radio("Backend", ["Google direct (no key, may get blocked)",
                                  "DuckDuckGo (free, no key)",
                                  "Google Programmable Search (API - closed to new users)"])
    use_api = engine.startswith("Google Programmable")
    g_key = g_cx = ""
    if engine.startswith("Google direct"):
        st.caption("Uses ddgs search providers with automatic fallback across supported engines. Search engines may still rate-limit automated requests.")
    if use_api:
        g_key = st.text_input("API key", type="password", value=secret("GOOGLE_CSE_API_KEY"))
        g_cx = st.text_input("Search engine ID (cx)", value=secret("GOOGLE_CSE_ID"))
        st.caption("Set the engine to 'Search the entire web'. 100 free queries/day. "
                   "Tip: put keys in .streamlit/secrets.toml or env vars.")
    per_site = st.slider("Max results per site", 5, 50, 20, 5)
    max_pages = st.slider("Max pages to download per search", 20, 400, 150, 10)
    pause = st.slider("Pause between queries (s)", 0.5, 10.0, 3.0, 0.5)
    if st.button("🧪 Test search engines & downloads"):
        test_backends = ["auto", "google", "duckduckgo", "brave", "bing", "mojeek"]
        supported = available_text_backends()
        if supported:
            test_backends = [b for b in test_backends if b == "auto" or b in supported]
        if not test_backends:
            st.error("The installed ddgs package exposes no compatible text backends. Upgrade ddgs and redeploy.")
        for backend in test_backends:
            try:
                results = search_engine("site:jobbnorge.no PhD", 5, backend)
                st.write(f"{'✅' if results else '⚠️'} {backend}: {len(results)} results")
            except Exception as exc:
                st.write(f"❌ {backend}: {type(exc).__name__}: {str(exc)[:140]}")
        try:
            data, final_url, content_type = safe_get("https://www.jobbnorge.no/en/available-jobs")
            st.write(f"✅ page download works ({len(data):,} bytes; {content_type or 'unknown content type'})")
        except Exception as exc:
            st.write(f"❌ page download: {type(exc).__name__}: {str(exc)[:140]}")

# ---------------- keywords
def _apply_preset():
    st.session_state["kw_text"] = KEYWORD_PRESETS[st.session_state["preset"]]


c1, c2, c3 = st.columns([3, 2, 2])
with c1:
    kw_text = st.text_input("Keywords (comma-separated)", key="kw_text",
                            placeholder="reservoir, CO2 storage, geomechanics")
with c2:
    mode = st.radio("Keyword logic", ["any", "all"], horizontal=True,
                    format_func=lambda x: "ANY keyword" if x == "any" else "ALL keywords")
with c3:
    st.selectbox("Preset keywords", list(KEYWORD_PRESETS), key="preset", on_change=_apply_preset)
add_phd = st.checkbox('Add PhD terms ("PhD" / "doctoral") to every search query', value=True)
with st.expander("Extra raw search queries (optional, run on the open web)"):
    extra_text = st.text_area("One per line, used exactly as typed",
                              placeholder='"PhD" "reservoir simulation" Europe 2026')
KWS = [k.strip() for k in kw_text.split(",") if k.strip()]

# ---------------- filters
with st.expander("Filters (applied live to the results)", expanded=True):
    f1, f2, f3 = st.columns(3)
    with f1:
        status = st.radio("Position status", ["Open only", "Open + closed", "Closed only"])
        include_unknown = st.checkbox("Keep positions whose date I couldn't detect", value=True)
    with f2:
        use_dates = st.checkbox("Filter by date interval", value=False)
        basis = st.selectbox("Date to filter on", ["Deadline", "Posted date"])
    with f3:
        today = dt.date.today()
        default_rng = (today, today + dt.timedelta(days=180)) if basis == "Deadline" \
            else (today - dt.timedelta(days=60), today)
        rng = st.date_input("Interval", value=default_rng, key=f"rng_{basis}")
        require_phd = st.checkbox("Page must mention PhD / doctoral", value=True)
        require_kw = st.checkbox("Page must contain the keyword(s)", value=True)

start, end = (rng[0], rng[1]) if isinstance(rng, (list, tuple)) and len(rng) == 2 else (today, today)
FILTERS = dict(status=status, include_unknown=include_unknown, use_dates=use_dates, basis=basis,
               start=start, end=end, require_phd=require_phd, require_kw=require_kw)

# ---------------- search
extra_queries = [q.strip() for q in extra_text.splitlines() if q.strip()]
sc1, sc2 = st.columns([1, 3])
with sc1:
    do_search = st.button("🔍 Search", type="primary", disabled=not (selected_sites or extra_queries))
with sc2:
    append = st.checkbox("Add to existing results instead of replacing them", value=False)

if do_search:
    if not KWS and not extra_queries:
        st.error("Enter at least one keyword (or an extra query).")
        st.stop()
    if use_api and not (g_key and g_cx):
        st.error("Google backend needs API key and cx.")
        st.stop()
    jobs = []
    if KWS:
        for site in selected_sites:
            code = COUNTRY_REGIONS.get(site.region, "")
            jobs.append((site.name, query_variants(site, KWS, mode, add_phd), code, site.query))
    for q in extra_queries:
        jobs.append(("Custom query", [q], "", ""))

    hits, errors, slog, empty_sites, dead = {}, [], [], [], set()
    first_engine = "google" if engine.startswith("Google direct") else "duckduckgo"
    bar = st.progress(0.0, "Searching...")
    for i, (name, variants, country, siteq) in enumerate(jobs):
        bar.progress(i / len(jobs), f"Searching {name}")
        got = []
        for vi, q in enumerate(variants, 1):
            if use_api:
                try:
                    got, notes = search_google(q, per_site, g_key, g_cx), []
                    notes = [("google-api", len(got), "")]
                except Exception as e:
                    got, notes = [], [("google-api", 0, "ERROR: " + str(e)[:130])]
            else:
                got, notes = search_chain(q, per_site, first_engine, dead, siteq=siteq)
            raw_n = len(got)
            if siteq:
                got = [h for h in got if url_matches_site(h["url"], siteq)]
            for eng, n_, note in notes:
                slog.append({"Site": name, "Try": vi, "Query": q, "Engine": eng, "Results": n_,
                             "Note": note + (f" ({raw_n - len(got)} off-site dropped)" if raw_n != len(got) and n_ else "")})
            if got:
                break
            time.sleep(min(pause, 1.5))
        if not got:
            empty_sites.append(name)
        for h in got:
            u = canonicalize_url(h["url"])
            if u and u not in hits:
                hits[u] = {**h, "site": name, "country": country, "query": variants[0]}
        if not use_api:
            time.sleep(pause)
    if dead:
        errors.append("Engine(s) blocked or failing this run and skipped afterwards: " + ", ".join(sorted(dead))
                      + " (use the sidebar test button for details)")
    if empty_sites:
        errors.append("No results for: " + ", ".join(empty_sites[:12]) + (" ..." if len(empty_sites) > 12 else "")
                      + "  (see Search diagnostics below)")
    hit_list = list(hits.values())[:max_pages]
    if len(hits) > max_pages:
        errors.append(f"{len(hits)} URLs found; only the first {max_pages} were downloaded (see sidebar slider).")
    new = run_analysis(hit_list, bar) if hit_list else []
    bar.empty()
    if not append:
        st.session_state["recs"] = {}
    merge_recs(new)
    saved = save_records(new)
    st.session_state["errors"] = errors
    ok_n = sum(1 for r in new if not r["error"])
    bad = [r["error"] for r in new if r["error"]]
    st.session_state["search_log"] = slog
    st.session_state["funnel_urls"] = len(hits)
    st.session_state["notice"] = (
        f"Search engines returned {len(hits)} URLs; {ok_n} pages read, {len(bad)} unreadable"
        + (f" (e.g. {bad[0]})" if bad else "") + f"; {saved} saved to the local database."
        + (" Open 'Search diagnostics' below to see why nothing came back." if not hits else "")
        + (" Search providers returned no URLs; this is different from finding URLs that could not be downloaded." if not hits else ""))
    st.session_state.pop("outputs", None)

for e in st.session_state.get("errors", []):
    st.warning(e)
if st.session_state.get("notice"):
    st.info(st.session_state["notice"])
if st.session_state.get("search_log"):
    with st.expander("🔧 Search diagnostics (what each query returned)",
                     expanded=st.session_state.get("funnel_urls", 1) == 0):
        show_df(pd.DataFrame(st.session_state["search_log"]))

# ---------------- working set
all_recs = list(st.session_state.get("recs", {}).values())
refresh_flags(all_recs, KWS, mode)
shown = [r for r in all_recs if passes(r, FILTERS)]
shown.sort(key=lambda r: (r["deadline"] is None, r["deadline"] or dt.date.max))
chosen = []

tab_res, tab_skill, tab_db, tab_add, tab_notes = st.tabs(
    ["📋 Results & PDF pack", "🧠 Skills analysis", "🗃️ Saved database", "➕ Add URLs / PDFs", "ℹ️ Notes"])

# ====================== results + PDF
with tab_res:
    if not all_recs:
        st.info("No results yet - pick sites, enter keywords and press Search (or load saved records / add URLs).")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Found", len(all_recs))
        m2.metric("After filters", len(shown))
        m3.metric("Open", sum(r["status"] == "open" for r in shown))
        m4.metric("Closed", sum(r["status"] == "closed" for r in shown))
        if not shown:
            n_nophd = sum(not r["has_phd"] for r in all_recs)
            n_nokw = sum(not r["has_kw"] for r in all_recs)
            n_closed = sum(r["status"] == "closed" for r in all_recs)
            n_err = sum(bool(r["error"]) for r in all_recs)
            st.info(f"Search found {len(all_recs)} page(s), but none pass the current filters. Of these pages: "
                    f"{n_nophd} lack PhD terms, {n_nokw} lack your keywords, {n_closed} look closed, "
                    f"{n_err} could not be downloaded. Temporarily untick the PhD and keyword requirements "
                    f"and choose 'Open + closed' to diagnose filters.")
        else:
            pre = st.checkbox("Pre-tick all rows", value=True)
            df = pd.DataFrame([{
                "Select": pre, "Title": r["title"], "Site": r["site"], "Organisation": r["org"],
                "Status": r["status"], "Deadline": str(r["deadline"] or ""),
                "Deadline source": r["deadline_source"], "Posted": str(r["posted"] or ""),
                "Note": ("page not readable - snippet only" if r["error"] else ""), "URL": r["url"]}
                for r in shown])
            edited = editor(
                df, key="ed_" + hashlib.md5(("|".join(df["URL"]) + str(pre)).encode()).hexdigest(),
                column_config={"Select": st.column_config.CheckboxColumn(width="small"),
                               "URL": st.column_config.LinkColumn("URL", width="medium")},
                disabled=[c for c in df.columns if c != "Select"])
            chosen_urls = set(edited.loc[edited["Select"], "URL"])
            chosen = [r for r in shown if r["url"] in chosen_urls]

            b1, b2 = st.columns([2, 3])
            with b1:
                use_pw = st.checkbox("High-quality rendering (Playwright / headless Chrome)", value=PW_OK,
                                     disabled=not PW_OK,
                                     help="pip install playwright && playwright install chromium")
                if st.button(f"📄 Build merged PDF ({len(chosen)} selected)", type="primary", disabled=not chosen):
                    pb = st.progress(0.0, "Starting...")
                    merged, zipped, warn, report = build_outputs(
                        chosen, use_pw, lambda v, t: pb.progress(min(v, 1.0), t))
                    st.session_state["outputs"] = dict(merged=merged, zipped=zipped, csv=recs_to_csv(chosen),
                                                       report=report)
                    if warn:
                        st.warning(warn)
                    pb.empty()
            out = st.session_state.get("outputs")
            if out:
                with b2:
                    st.success("Ready!")
                    d1, d2, d3 = st.columns(3)
                    d1.download_button("⬇️ Merged PDF", out["merged"], "phd_positions_merged.pdf", "application/pdf")
                    d2.download_button("⬇️ ZIP (PDFs + CSVs)", out["zipped"], "phd_positions.zip", "application/zip")
                    d3.download_button("⬇️ CSV list", out["csv"], "phd_positions.csv", "text/csv")
                with st.expander("How each PDF was produced"):
                    show_df(out["report"])

# ====================== skills
with tab_skill:
    st.subheader("Which skills recur across these vacancies?")
    scope = st.radio("Analyse", ["All filtered rows", "Only ticked rows"], horizontal=True)
    pool = chosen if (scope == "Only ticked rows" and chosen) else shown
    pool = [r for r in pool if r["text"]]
    st.caption(f"{len(pool)} vacancies analysed. Counts describe THIS sample, not Europe-wide prevalence. "
               "Cue labels are keyword/context heuristics - audit the evidence snippets.")
    if not pool:
        st.info("Run a search first (or relax the filters).")
    else:
        summ, terms, evd = analyze_skills(pool)
        if summ.empty:
            st.info("No skill terms from the taxonomy were found in the page texts.")
        else:
            show_df(summ)
            st.bar_chart(summ.set_index("Skill area")[["Vacancies mentioning"]])
            st.markdown("**Most frequent specific terms / tools**")
            show_df(terms.head(40))
            st.markdown("**Evidence snippets**")
            area = st.multiselect("Filter evidence by skill area", list(summ["Skill area"]))
            show_df(evd[evd["Skill area"].isin(area)] if area else evd)
            k1, k2, k3 = st.columns(3)
            k1.download_button("⬇️ Skill summary CSV", csv_bytes(summ), "skill_summary.csv", "text/csv")
            k2.download_button("⬇️ Term frequency CSV", csv_bytes(terms), "skill_terms.csv", "text/csv")
            k3.download_button("⬇️ Evidence CSV", csv_bytes(evd), "skill_evidence.csv", "text/csv")

# ====================== database
with tab_db:
    st.subheader("Saved positions (local SQLite)")
    st.caption(f"File: `{DB_PATH.resolve()}`")
    saved_recs = load_db_records()
    refresh_flags(saved_recs, KWS, mode)
    if not saved_recs:
        st.info("Nothing saved yet. Every successful search is stored automatically.")
    else:
        q1, q2, q3 = st.columns(3)
        needle = q1.text_input("Search title / organisation / text").lower().strip()
        all_rs = sorted({r["review_status"] for r in saved_recs} | {"Needs review"})
        rs = q2.multiselect("Review status", all_rs, default=all_rs)
        only_phd = q3.checkbox("Only likely PhD positions", value=True)
        view = [r for r in saved_recs
                if r["review_status"] in rs and (r["has_phd"] or not only_phd)
                and (not needle or needle in (r["title"] + r["org"] + r["text"]).lower())]
        st.caption(f"{len(view)} of {len(saved_recs)} saved records match.")
        dfv = pd.DataFrame([{"Title": r["title"], "Organisation": r["org"], "Site": r["site"],
                             "Country": r["country"], "Status": r["status"], "Deadline": str(r["deadline"] or ""),
                             "Deadline source": r["deadline_source"], "Review": r["review_status"],
                             "URL": r["url"]} for r in view])
        show_df(dfv, column_config={"URL": st.column_config.LinkColumn("URL")})
        a1, a2 = st.columns(2)
        if a1.button("⬆️ Load these records into Results / PDF / Skills", disabled=not view):
            merge_recs(view)
            st.session_state["notice"] = f"Loaded {len(view)} saved records into the results table."
            st.rerun()
        a2.download_button("⬇️ Export listed records (CSV)", csv_bytes(dfv), "saved_positions.csv", "text/csv")

        if view:
            st.markdown("**Review a record**")
            rec = st.selectbox("Record", view, format_func=lambda r: f"{r['title'][:80]} - {r['org'][:40]}")
            with st.form("review_form"):
                st.write(f"Source: {rec['url']}")
                opts = ["Needs review", "Verified open", "Verified closed", "Not relevant", "Duplicate",
                        "Applied", "Contacted"]
                cur = rec["review_status"] if rec["review_status"] in opts else "Needs review"
                new_status = st.selectbox("Review status", opts, index=opts.index(cur))
                man = st.text_input("Manually verified deadline (YYYY-MM-DD, blank = unchanged)")
                ok = st.form_submit_button("Save")
            if ok:
                db_update_review(rec["canonical_url"], new_status)
                if man.strip():
                    d = s2d(man.strip())
                    if d:
                        db_set_deadline(rec["canonical_url"], d)
                    else:
                        st.warning("Deadline not saved - use YYYY-MM-DD.")
                st.success("Saved.")
                st.rerun()

        with st.expander("Danger zone"):
            if st.checkbox("I understand: permanently delete the listed records") and st.button("Delete listed records"):
                db_delete([r["canonical_url"] for r in view])
                st.rerun()

# ====================== add URLs / PDFs
with tab_add:
    st.subheader("Add known vacancy URLs")
    url_text = st.text_area("One URL per line", height=140, placeholder="https://university.example/jobs/phd-position")
    if st.button("Extract pasted URLs", type="primary"):
        urls = list(dict.fromkeys(u.strip() for u in url_text.splitlines() if u.strip()))
        if urls:
            hits = [{"url": u, "title": "", "snippet": "", "site": "Manual URL", "country": "",
                     "query": "Manual URL import"} for u in urls]
            bar = st.progress(0.0, "Reading pages...")
            new = run_analysis(hits, bar, workers=6)
            bar.empty()
            merge_recs(new)
            n = save_records(new)
            bad = [r for r in new if r["error"]]
            st.session_state["notice"] = f"Added {len(new)} URLs ({n} saved, {len(bad)} unreadable)."
            st.rerun()
    st.divider()
    st.subheader("Upload vacancy PDFs")
    ups = st.file_uploader("PDF advertisements", type=["pdf"], accept_multiple_files=True)
    if ups and st.button("Add uploaded PDFs to results"):
        new = []
        for f in ups:
            try:
                data = f.getvalue()
                if len(data) > MAX_BYTES:
                    raise ValueError("file larger than 25 MB")
                rd = PdfReader(io.BytesIO(data), strict=False)
                r = new_rec("upload://" + f.name, {"title": f.name.rsplit(".", 1)[0], "site": "Upload"})
                r.update(canonical_url="upload:" + hashlib.sha1(data).hexdigest()[:12], is_pdf=True,
                         content=data, upload=True,
                         text="\n".join((p.extract_text() or "") for p in rd.pages[:6]))
                r["has_phd"] = phd_flag(r["title"], r["text"])
                dl = labeled_date(re.sub(r"\s+", " ", r["text"]), DEADLINE_LABEL)
                if dl:
                    r["deadline"], r["deadline_source"] = dl, "Text near deadline label (verify)"
                r["status"] = compute_status(r["deadline"])
                new.append(r)
            except Exception as e:
                st.error(f"{f.name}: {type(e).__name__}: {e}")
        if new:
            merge_recs(new)
            st.session_state["notice"] = f"Added {len(new)} uploaded PDFs (session only, not saved to the database)."
            st.rerun()

# ====================== notes
with tab_notes:
    st.markdown("""
- **Dates** are read from structured data (JSON-LD `validThrough`) first, then from text next to labels like
  *"Application deadline: 15 Nov 2026"*. The *Deadline source* column tells you which; `validThrough` is sometimes the
  posting's validity, not the real deadline. Use the *Saved database* tab to enter a verified deadline.
- Search engines don't know posting dates, so the interval filter uses dates read from the page itself.
  Positions with no detectable date are kept or dropped with the *"couldn't detect"* checkbox.
- Skills analysis counts keyword mentions; "Required cue / Preferred cue" is a context heuristic, not understanding.
  Always check the evidence snippets. Counts are for your sample, not for Europe as a whole.
- `All ... universities` entries (`edu.au`, `ac.nz`, `ac.uk`, `.edu`) cover whole countries at once but return noisier hits.
- Search uses multiple supported `ddgs` providers and merges unique URLs until enough results are found. Search engines may still rate-limit automated requests; use Search diagnostics to see which provider failed.
- Pages that could not be read (login walls, bot protection) show *snippet only* and are not saved to the database.
- Downloads block private/local addresses and cap file size, but this is a personal tool, not a hardened public service.
- Cookie banners may appear on rendered pages; the position text is still included.
""")
