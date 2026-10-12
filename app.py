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

st.title("🎓 Expanded PhD Meta-Search Dork Generator")
st.markdown(
    "Generate targeted **Google Dorks** to search deep inside all major European academic portals, "
    "bypassing clutter and getting straight to indexable job listings."
)

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

st.divider()

# Main generator loop
for region, sites in academic_sites.items():
    if selected_region != "All Regions" and selected_region != region:
        continue

    st.subheader(f"🌍 Region: {region}")

    for site in sites:
        # Construct the Google Dork query format
        if site["path"]:
            dork_query = f"site:{site['domain']}/{site['path']} \"{keyword_input}\" {position_type}"
        else:
            dork_query = f"site:{site['domain']} \"{keyword_input}\" {position_type}"

        google_search_url = f"https://www.google.com/search?q={urllib.parse.quote(dork_query)}"

        # Display layout
        col1, col2, col3 = st.columns([2, 5, 2])

        with col1:
            st.markdown(f"**{site['name']}**")
            st.caption(f"Domain: {site['domain']}")

        with col2:
            st.code(dork_query, language="text")

        with col3:
            st.markdown(
                f"<br><a href='{google_search_url}' target='_blank'>"
                f"<button style='background-color:#4CAF50; color:white; border:none; padding:8px 16px; "
                f"border-radius:4px; cursor:pointer;'>Search Google 🔍</button></a>",
                unsafe_allow_html=True,
            )

    st.markdown("---")

st.info(
    "💡 **Tip:** Feel free to tweak the keyword sidebar (e.g., changing 'reservoir' to 'geothermal', 'flow assurance', or 'petroleum') "
    "to instantly update all search queries across every country!"
)
