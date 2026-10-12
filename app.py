import urllib.parse
import streamlit as st

# Page configuration
st.set_page_config(
    page_title="PhD Search Dork Generator", page_icon="🎓", layout="wide"
)

# Categorized academic sites dictionary
academic_sites = {
    "Norway": [
        {
            "name": "Jobbnorge",
            "domain": "jobbnorge.no",
            "path": "en/available-jobs/joblisting/",
        },
        {"name": "NTNU", "domain": "ntnu.edu", "path": "vacancies/"},
    ],
    "Netherlands": [
        {
            "name": "AcademicTransfer",
            "domain": "academictransfer.com",
            "path": "en/jobs/",
        },
        {
            "name": "EURAXESS Netherlands",
            "domain": "euraxess.ec.europa.eu",
            "path": "jobs/search",
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

st.title("🎓 PhD Meta-Search Dork Generator")
st.markdown(
    "Instead of getting stuck in messy university navigation scrapers, use **Google Dorks** "
    "to query deep inside target academic portals for specific research positions."
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
        # Construct the Google Dork query format requested
        dork_query = (
            f"site:{site['domain']}/{site['path']} \"{keyword_input}\" {position_type}"
        )
        google_search_url = f"https://www.google.com/search?q={urllib.parse.quote(dork_query)}"

        # Display using columns for a clean UI layout
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
    "💡 **Tip:** Clicking 'Search Google' will open a pre-filled search query targeting "
    "only indexable job listings and avoiding homepage menu noise."
)
