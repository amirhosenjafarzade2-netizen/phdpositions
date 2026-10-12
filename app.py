import urllib.parse
import streamlit as st

# Page configuration
st.set_page_config(
    page_title="PhD Search Dork Generator", page_icon="🎓", layout="wide"
)

# Comprehensive categorized academic sites dictionary
academic_sites = {
    "Netherlands": [
        {
            "name": "AcademicTransfer",
            "domain": "academictransfer.com",
            "path": "en/jobs/",
        },
        {
            "name": "Utrecht University",
            "domain": "uu.nl",
            "path": "en/organisation/working-at-utrecht-university/vacancies",
        },
        {
            "name": "TU Delft",
            "domain": "tudelft.nl",
            "path": "en/about-tu-delft/working-at-tu-delft/vacancies",
        },
        {
            "name": "Eindhoven University of Technology (TU/e)",
            "domain": "tue.nl",
            "path": "en/university/working-at-the-tue/vacancies",
        },
        {
            "name": "Vrije Universiteit Amsterdam (VU)",
            "domain": "vu.nl",
            "path": "en/working-at-vu/vacancies",
        },
        {
            "name": "University of Amsterdam (UvA)",
            "domain": "uva.nl",
            "path": "en/about-the-uva/working-at-the-uva/vacancies",
        },
    ],
    "Norway": [
        {
            "name": "Jobbnorge (National Portal)",
            "domain": "jobbnorge.no",
            "path": "en/available-jobs/joblisting/",
        },
        {
            "name": "NTNU (Trondheim/Gjøvik/Ålesund)",
            "domain": "ntnu.edu",
            "path": "vacancies/",
        },
        {
            "name": "University of Oslo (UiO)",
            "domain": "uio.no",
            "path": "english/about/vacancies/",
        },
    ],
    "Belgium": [
        {
            "name": "KU Leuven",
            "domain": "kuleuven.be",
            "path": "en/job-opportunities",
        },
        {
            "name": "Ghent University",
            "domain": "ugent.be",
            "path": "en/job-opportunities",
        },
    ],
    "France": [
        {
            "name": "CNRS",
            "domain": "emploi.cnrs.fr",
            "path": "",
        },
        {
            "name": "ABG (Association Bernard Gregory)",
            "domain": "abg.asso.fr",
            "path": "en/candidat/offres",
        },
    ],
    "Pan-European / General": [
        {
            "name": "EURAXESS Europe",
            "domain": "euraxess.ec.europa.eu",
            "path": "jobs/search",
        },
        {
            "name": "FindAPhD",
            "domain": "findaphd.com",
            "path": "phds/",
        },
    ],
}


def build_dork(site, keyword, position, pdf_only=False, ignore_paths=False):
    """Build a Google dork string for a single site."""
    keyword = keyword.strip()
    path = site["path"].strip("/")

    if path and not ignore_paths:
        parts = [f"site:{site['domain']}/{path}"]
    else:
        parts = [f"site:{site['domain']}"]

    if keyword:
        parts.append(f'"{keyword}"')
    parts.append(position)

    if pdf_only:
        parts.append("filetype:pdf")

    return " ".join(parts)


st.title("🎓 Expanded PhD Meta-Search Dork Generator")
st.markdown(
    "Generate targeted **Google Dorks** to search deep inside all major European academic portals, "
    "bypassing clutter and getting straight to indexable job listings."
)

# ---------------- Sidebar ----------------
st.sidebar.header("Search Parameters")
keyword_input = st.sidebar.text_input(
    "Research Keyword / Topic", value="reservoir"
)
position_type = st.sidebar.selectbox(
    "Position Type", ["PhD", "Postdoc", "Research Fellowship"]
)
selected_region = st.sidebar.selectbox(
    "Filter by Region", ["All Regions"] + list(academic_sites.keys())
)

pdf_only = st.sidebar.checkbox(
    "📄 PDF format only",
    value=False,
    help="Adds filetype:pdf to every search, useful for vacancy announcements published as PDFs.",
)
if pdf_only:
    st.sidebar.caption("✅ `filetype:pdf` will be added to all queries.")

ignore_paths = st.sidebar.checkbox(
    "🌐 Ignore site paths (domain only)",
    value=False,
    help="Searches the whole domain instead of the vacancy section. Helpful with PDFs, which are often stored in other folders.",
)

st.divider()

# ---------------- Main generator loop ----------------
for region, sites in academic_sites.items():
    if selected_region != "All Regions" and selected_region != region:
        continue

    st.subheader(f"🌍 Region: {region}")

    for site in sites:
        dork_query = build_dork(
            site, keyword_input, position_type, pdf_only, ignore_paths
        )
        google_search_url = (
            f"https://www.google.com/search?q={urllib.parse.quote_plus(dork_query)}"
        )

        # Display layout
        col1, col2, col3 = st.columns([2, 5, 2])

        with col1:
            st.markdown(f"**{site['name']}**")
            st.caption(f"Domain: {site['domain']}")

        with col2:
            st.code(dork_query, language="text")

        with col3:
            st.link_button("Search Google 🔍", google_search_url)

    st.markdown("---")

st.info(
    "💡 **Tip:** Feel free to tweak the keyword sidebar (e.g., changing 'reservoir' to 'geothermal', 'flow assurance', or 'petroleum') "
    "to instantly update all search queries across every country!"
)
