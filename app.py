"""
PhD Position Finder & PDF Merger  (Streamlit)

Run:
    pip install -r requirements.txt
    playwright install chromium        # optional, but gives much better page->PDF output
    streamlit run app.py

How it works
 1. For every ticked site, the app builds a search-engine query such as
        site:jobbnorge.no/en/available-jobs/joblisting ("PhD" OR "doctoral") "reservoir"
    and runs it through DuckDuckGo (no key needed) or Google Programmable Search (API key).
 2. Every hit is downloaded and analysed: title, organisation, posted date, deadline,
    open/closed status, does it really mention PhD / your keywords.
 3. Filters (open/closed, date interval, PhD-only ...) are applied live on the results.
 4. You tick the positions you want -> each page is converted to PDF (or downloaded if it
    already is a PDF) -> everything is merged into ONE PDF with a cover index + bookmarks.
"""

import csv
import datetime as dt
import io
import json
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
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

# --------------------------------------------------------------------------------------
# 1. SITE LIST  (edit freely!)  query = what goes after  site:   ("" = whole web)
# --------------------------------------------------------------------------------------


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
    Site("University of Oslo", "uio.no", "Norway"),
    Site("University of Bergen", "uib.no", "Norway"),
    Site("UiT", "uit.no", "Norway"),
    # ---- Netherlands
    Site("AcademicTransfer", "academictransfer.com", "Netherlands", True),
    # ---- UK / Ireland
    Site("jobs.ac.uk", "jobs.ac.uk", "UK"),
    Site("All UK universities (ac.uk)", "ac.uk", "UK"),
    # ---- Germany
    Site("academics.de", "academics.de", "Germany"),
    Site("DAAD", "daad.de", "Germany"),
    # ---- Sweden
    Site("Varbi (Swedish universities)", "varbi.com", "Sweden"),
    # ---- Canada
    Site("University Affairs (CA)", "universityaffairs.ca", "Canada"),
    # ---- Australia
    Site("All Australian universities (edu.au)", "edu.au", "Australia"),
    # ---- New Zealand
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

REGION_ORDER = ["Europe", "Global", "Norway", "Netherlands", "UK", "Germany", "Sweden",
                "Canada", "Australia", "New Zealand", "USA", "Open web"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}

# --------------------------------------------------------------------------------------
# 2. SEARCH
# --------------------------------------------------------------------------------------

PHD_TERMS = '("PhD" OR "doctoral" OR "doctorate" OR "PhD position")'
PHD_RE = re.compile(r"\bph\.?\s?d\b|doctoral|doctorate|promovend|doktorand|doktorgrad|stipendiat|"
                    r"\bdphil\b|research fellow.{0,20}(?:phd|doctoral)", re.I)


def build_query(site: Site, kws, mode, add_phd):
    quoted = [f'"{k}"' for k in kws]
    if len(quoted) > 1 and mode == "any":
        kw_part = "(" + " OR ".join(quoted) + ")"
    else:
        kw_part = " ".join(quoted)
    parts = []
    if site.query:
        parts.append(f"site:{site.query}")
    if add_phd or not site.query:
        parts.append(PHD_TERMS)
    parts.append(kw_part)
    return " ".join(parts)


def search_ddg(q, n):
    try:
        from ddgs import DDGS
    except ImportError:  # old package name
        from duckduckgo_search import DDGS
    last = None
    for attempt in range(3):
        try:
            with DDGS() as d:
                res = d.text(q, max_results=n)
            return [{"url": r.get("href") or r.get("url"), "title": r.get("title", ""),
                     "snippet": r.get("body", "")} for r in res]
        except Exception as e:  # rate limit etc.
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"DuckDuckGo failed: {last}")


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


def norm_url(u):
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query)
         if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", urlencode(q), ""))


# --------------------------------------------------------------------------------------
# 3. PAGE ANALYSIS  (title, org, dates, open/closed)
# --------------------------------------------------------------------------------------

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
        d = dtparser.parse(s, dayfirst=not re.match(r"\d{4}-", s), fuzzy=True)
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
    hits = [k.lower() in text_l for k in kws]
    return any(hits) if mode == "any" else all(hits)


def analyze(hit, kws, mode):
    url = hit["url"]
    rec = dict(url=url, title=hit.get("title", ""), site=hit.get("site", ""), org="",
               posted=None, deadline=None, status="unknown", is_pdf=False, content=None,
               text="", has_phd=False, has_kw=False)
    today = dt.date.today()
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            rec["is_pdf"] = True
            if len(r.content) < 30_000_000:
                rec["content"] = r.content
            reader = PdfReader(io.BytesIO(r.content))
            rec["text"] = "\n".join((p.extract_text() or "") for p in reader.pages[:4])
            if not rec["title"]:
                rec["title"] = url.rsplit("/", 1)[-1]
        else:
            soup = BeautifulSoup(r.text, "html.parser")
            jp = None
            for tag in soup.find_all("script", type="application/ld+json"):
                try:
                    jp = find_jobposting(json.loads(tag.string or ""))
                except Exception:
                    jp = None
                if jp:
                    break
            title = None
            if jp and jp.get("title"):
                title = str(jp["title"])
                org = jp.get("hiringOrganization")
                rec["org"] = org.get("name", "") if isinstance(org, dict) else str(org or "")
                if jp.get("datePosted"):
                    rec["posted"] = parse_date(str(jp["datePosted"]))
                if jp.get("validThrough"):
                    rec["deadline"] = parse_date(str(jp["validThrough"]))
            if not title:
                og = soup.find("meta", property="og:title")
                h1 = soup.find("h1")
                title = (og.get("content") if og and og.get("content")
                         else h1.get_text(" ", strip=True) if h1
                         else soup.title.get_text(strip=True) if soup.title else rec["title"])
            rec["title"] = re.sub(r"\s+", " ", title).strip()[:200]
            for t in soup(["script", "style", "noscript", "svg"]):
                t.decompose()
            rec["text"] = soup.get_text("\n", strip=True)[:40000]
    except Exception:
        rec["text"] = hit.get("snippet", "")

    blob = rec["title"] + "\n" + rec["text"]
    flat = re.sub(r"\s+", " ", blob[:60000])
    rec["has_phd"] = bool(PHD_RE.search(blob))
    rec["has_kw"] = keyword_hit(blob.lower(), kws, mode)
    if not rec["deadline"]:
        rec["deadline"] = labeled_date(flat, DEADLINE_LABEL)
    if not rec["posted"]:
        rec["posted"] = labeled_date(flat, POSTED_LABEL)
    if rec["deadline"]:
        rec["status"] = "open" if rec["deadline"] >= today else "closed"
    elif CLOSED_RE.search(flat[:5000]):
        rec["status"] = "closed"
    return rec


# --------------------------------------------------------------------------------------
# 4. FILTERING
# --------------------------------------------------------------------------------------


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


# --------------------------------------------------------------------------------------
# 5. PDF CREATION + MERGING
# --------------------------------------------------------------------------------------


def clean(s):
    return (s or "").encode("cp1252", "replace").decode("cp1252")


def fallback_pdf(rec):
    """Pure-python text PDF (used when Playwright is unavailable or fails)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50)
    st_ = getSampleStyleSheet()
    flow = [Paragraph(escape(clean(rec["title"])), st_["Title"]),
            Paragraph(f'<font color="blue">{escape(rec["url"])}</font>', st_["BodyText"]), Spacer(1, 12)]
    for line in rec["text"].split("\n"):
        if line.strip():
            flow.append(Paragraph(escape(clean(line[:1500])), st_["BodyText"]))
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
            Paragraph(f"{len(recs)} positions", s["BodyText"]), Spacer(1, 10)]
    data = [["#", "Title", "Site / organisation", "Deadline", "Status", "URL"]]
    for i, r in enumerate(recs, 1):
        data.append([str(i), Paragraph(escape(clean(r["title"])), small),
                     Paragraph(escape(clean(r["site"] + (" / " + r["org"] if r["org"] else ""))), small),
                     str(r["deadline"] or "?"), r["status"],
                     Paragraph(escape(r["url"]), small)])
    t = Table(data, colWidths=[20, 150, 100, 52, 40, 190], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f9")])]))
    flow.append(t)
    doc.build(flow)
    return buf.getvalue()


def build_outputs(recs, use_pw, progress):
    pdfs, warn = {}, None
    need = [r["url"] for r in recs if not r["is_pdf"]]
    if use_pw and need:
        for i in range(0, len(need), 4):
            try:  # separate thread -> no asyncio-loop clash with Streamlit
                with ThreadPoolExecutor(1) as ex:
                    pdfs.update(ex.submit(playwright_pdfs, need[i:i + 4]).result())
            except Exception as e:
                warn = f"Playwright unavailable ({str(e)[:120]}). Using plain-text fallback for pages."
                break
            progress(min(1.0, (i + 4) / len(need)) * 0.7, "Rendering pages with headless Chrome...")
    final, ok = [], []
    for i, r in enumerate(recs):
        b = r["content"] if r["is_pdf"] else pdfs.get(r["url"])
        if not b:
            b = fallback_pdf(r)
        try:
            PdfReader(io.BytesIO(b))  # validate
        except Exception:
            b = fallback_pdf(r)
        final.append(b)
        ok.append(r)
        progress(0.7 + 0.2 * (i + 1) / len(recs), "Checking PDFs...")

    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(make_cover(ok))))
    for r, b in zip(ok, final):
        start = len(w.pages)
        try:
            w.append(PdfReader(io.BytesIO(b)))
            w.add_outline_item(clean(r["title"])[:90] or r["url"], start)
        except Exception:
            pass
    merged = io.BytesIO()
    w.write(merged)

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (r, b) in enumerate(zip(ok, final), 1):
            name = re.sub(r"[^\w\- ]+", "", r["title"])[:70].strip() or "position"
            z.writestr(f"{i:03d}_{name}.pdf", b)
        z.writestr("positions.csv", recs_to_csv(ok))
    progress(1.0, "Done")
    return merged.getvalue(), zbuf.getvalue(), warn


def recs_to_csv(recs):
    s = io.StringIO()
    wr = csv.writer(s)
    wr.writerow(["title", "site", "organisation", "status", "deadline", "posted", "url"])
    for r in recs:
        wr.writerow([r["title"], r["site"], r["org"], r["status"], r["deadline"] or "", r["posted"] or "", r["url"]])
    return s.getvalue()


# --------------------------------------------------------------------------------------
# 6. UI
# --------------------------------------------------------------------------------------

st.set_page_config(page_title="PhD Position Finder", page_icon="🎓", layout="wide")
st.title("🎓 PhD Position Finder & PDF Merger")

try:
    import playwright  # noqa: F401
    PW_OK = True
except ImportError:
    PW_OK = False

# ---------------- sidebar: sites + engine
with st.sidebar:
    st.header("Websites")
    selected_sites = []
    by_region = {}
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
        line = line.strip().replace("https://", "").replace("http://", "").strip("/")
        if line:
            selected_sites.append(Site(line, line, "Custom"))

    st.header("Search engine")
    engine = st.radio("Backend", ["DuckDuckGo (free, no key)", "Google Programmable Search (API)"])
    g_key = g_cx = ""
    if engine.startswith("Google"):
        g_key = st.text_input("API key", type="password")
        g_cx = st.text_input("Search engine ID (cx)")
        st.caption("Set the engine to 'Search the entire web'. 100 free queries/day.")
    per_site = st.slider("Max results per site", 5, 50, 20, 5)

# ---------------- main: keywords + filters
c1, c2 = st.columns([3, 2])
with c1:
    kw_text = st.text_input("Keywords (comma-separated)", placeholder="reservoir, CO2 storage, geomechanics")
with c2:
    mode = st.radio("Keyword logic", ["any", "all"], horizontal=True,
                    format_func=lambda x: "ANY keyword" if x == "any" else "ALL keywords")
add_phd = st.checkbox('Add PhD terms ("PhD" / "doctoral") to every search query', value=True)

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
if st.button("🔍 Search", type="primary", disabled=not selected_sites):
    kws = [k.strip() for k in kw_text.split(",") if k.strip()]
    if not kws:
        st.error("Enter at least one keyword.")
        st.stop()
    if engine.startswith("Google") and not (g_key and g_cx):
        st.error("Google backend needs API key and cx.")
        st.stop()

    hits, errors = {}, []
    bar = st.progress(0.0, "Searching...")
    for i, site in enumerate(selected_sites):
        q = build_query(site, kws, mode, add_phd)
        bar.progress(i / len(selected_sites), f"Searching {site.name}  ->  {q}")
        try:
            res = search_ddg(q, per_site) if engine.startswith("Duck") else search_google(q, per_site, g_key, g_cx)
            for h in res:
                if h["url"]:
                    u = norm_url(h["url"])
                    if u not in hits:
                        hits[u] = {**h, "url": u, "site": site.name}
        except Exception as e:
            errors.append(f"{site.name}: {e}")
        if engine.startswith("Duck"):
            time.sleep(1.5)  # be gentle, avoids rate limiting
    recs = []
    if hits:
        with ThreadPoolExecutor(8) as ex:
            futs = [ex.submit(analyze, h, kws, mode) for h in hits.values()]
            for j, fu in enumerate(as_completed(futs), 1):
                recs.append(fu.result())
                bar.progress(j / len(futs), f"Reading pages {j}/{len(futs)}")
    bar.empty()
    st.session_state["recs"] = recs
    st.session_state["errors"] = errors
    st.session_state.pop("outputs", None)

for e in st.session_state.get("errors", []):
    st.warning(e)

# ---------------- results
recs = st.session_state.get("recs")
if recs is not None:
    shown = [r for r in recs if passes(r, FILTERS)]
    shown.sort(key=lambda r: (r["deadline"] is None, r["deadline"] or dt.date.max))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Found", len(recs))
    m2.metric("After filters", len(shown))
    m3.metric("Open", sum(r["status"] == "open" for r in shown))
    m4.metric("Closed", sum(r["status"] == "closed" for r in shown))

    if not shown:
        st.info("Nothing matches the current filters - try loosening them (they apply instantly).")
    else:
        df = pd.DataFrame([{
            "Select": True, "Title": r["title"], "Site": r["site"], "Organisation": r["org"],
            "Status": r["status"], "Deadline": str(r["deadline"] or ""), "Posted": str(r["posted"] or ""),
            "URL": r["url"]} for r in shown])
        edited = st.data_editor(
            df, hide_index=True, use_container_width=True,
            key="ed_" + str(hash(tuple(df["URL"]))),
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
                merged, zipped, warn = build_outputs(chosen, use_pw, lambda v, t: pb.progress(min(v, 1.0), t))
                st.session_state["outputs"] = dict(merged=merged, zipped=zipped, csv=recs_to_csv(chosen))
                if warn:
                    st.warning(warn)
                pb.empty()

        out = st.session_state.get("outputs")
        if out:
            with b2:
                st.success("Ready!")
                d1, d2, d3 = st.columns(3)
                d1.download_button("⬇️ Merged PDF", out["merged"], "phd_positions_merged.pdf", "application/pdf")
                d2.download_button("⬇️ ZIP (separate PDFs + CSV)", out["zipped"], "phd_positions.zip", "application/zip")
                d3.download_button("⬇️ CSV list", out["csv"], "phd_positions.csv", "text/csv")

with st.expander("Notes & tips"):
    st.markdown("""
- **Dates are detected automatically** (structured data first, then text patterns like *"Application deadline: 15 Nov 2026"*).
  When no date can be found the position is marked *unknown* - use the checkbox to keep or drop those.
- Search engines don't know the real posting date, so the interval filter works on the dates read from the page itself.
- Use the *"All ... universities"* entries (`edu.au`, `ac.nz`, `ac.uk`, `.edu`) to cover whole countries' university sites at once.
- If DuckDuckGo rate-limits you, wait a minute, tick fewer sites, or switch to the Google API backend.
- Cookie banners may appear on some rendered pages; the text of the position is still included.
""")
