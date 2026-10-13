"""Problem Radar – GUI (Streamlit). Spustenie: python -m radar gui (iba 127.0.0.1, bez telemetrie)."""

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:  # balík radar pri spustení cez streamlit run
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Problem Radar", page_icon=":material/radar:", layout="wide")

page = st.navigation(
    {
        "": [
            st.Page("app_pages/overview.py", title="Prehľad", icon=":material/dashboard:", default=True),
            st.Page("app_pages/actions.py", title="Správca akcií", icon=":material/memory:"),
            st.Page("app_pages/problems.py", title="Problémy", icon=":material/lightbulb:"),
            st.Page("app_pages/evidence.py", title="Dôkazy", icon=":material/format_quote:"),
            st.Page("app_pages/documents.py", title="Dokumenty", icon=":material/description:"),
            st.Page("app_pages/sources.py", title="Zdroje", icon=":material/travel_explore:"),
        ],
        "Brána 0": [
            st.Page("app_pages/orgs.py", title="Organizácie", icon=":material/apartment:"),
            st.Page("app_pages/people.py", title="Ľudia a kontakty", icon=":material/group:"),
            st.Page("app_pages/outreach.py", title="Oslovenia", icon=":material/mail:"),
            st.Page("app_pages/interviews.py", title="Rozhovory", icon=":material/record_voice_over:"),
            st.Page("app_pages/gate0.py", title="Brána 0", icon=":material/flag:"),
            st.Page("app_pages/gate1.py", title="Brána 1 – dáta", icon=":material/database:"),
            st.Page("app_pages/gate2.py", title="Brána 2 – experimenty", icon=":material/science:"),
        ],
        "Kvalita a súkromie": [
            st.Page("app_pages/review.py", title="Kontrola", icon=":material/fact_check:"),
            st.Page("app_pages/eval.py", title="Eval", icon=":material/analytics:"),
            st.Page("app_pages/privacy.py", title="Súkromie", icon=":material/shield_lock:"),
        ],
    },
    position="sidebar",
)
st.title(page.title)
page.run()
