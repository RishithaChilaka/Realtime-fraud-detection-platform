"""
Analyst review UI.

A thin Streamlit client over the FastAPI service's `/review` endpoints --
every read and write goes through the API (not directly against
PostgreSQL) so there is exactly one code path that can create/resolve
review cases and record feedback, whether the caller is this UI, a script,
or (in a future phase) another internal tool.

Run locally: `streamlit run streamlit_app/review_ui.py`
Run in Docker: `docker compose up review-ui` (see docker/streamlit/Dockerfile)
"""
from __future__ import annotations

import os

import pandas as pd
import requests
import streamlit as st

API_BASE_URL = os.environ.get("FRAUD_API_BASE_URL", "http://localhost:8080")

st.set_page_config(page_title="Fraud Review Queue", layout="wide")
st.title("Fraud Detection — Analyst Review Queue")
st.caption(f"API: {API_BASE_URL}")

if "access_token" not in st.session_state:
    st.session_state.access_token = None
    st.session_state.role = None


def _auth_headers() -> dict:
    if st.session_state.access_token:
        return {"Authorization": f"Bearer {st.session_state.access_token}"}
    return {}


def login(username: str, password: str) -> bool:
    try:
        resp = requests.post(
            f"{API_BASE_URL}/auth/token", json={"username": username, "password": password}, timeout=5
        )
        resp.raise_for_status()
        body = resp.json()
        st.session_state.access_token = body["access_token"]
        st.session_state.role = body["role"]
        return True
    except requests.RequestException as exc:
        st.error(f"Login failed: {exc}")
        return False


if not st.session_state.access_token:
    st.info(
        "The review queue is role-protected (analyst/admin). Demo credentials: "
        "`analyst1` / `analyst-demo-pass` -- see src/api/auth.py."
    )
    with st.form("login"):
        username = st.text_input("Username", value="analyst1")
        password = st.text_input("Password", value="analyst-demo-pass", type="password")
        if st.form_submit_button("Log in") and login(username, password):
            st.rerun()
    st.stop()

st.caption(f"Logged in as **{st.session_state.role}**")
if st.sidebar.button("Log out"):
    st.session_state.access_token = None
    st.session_state.role = None
    st.rerun()


@st.cache_data(ttl=5)
def fetch_queue(status: str, limit: int, auth_token: str) -> list[dict]:
    # `auth_token` is part of the cache key (Streamlit hashes every
    # non-underscore-prefixed arg) so switching users invalidates the
    # cached queue instead of leaking the previous analyst's cached view.
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    resp = requests.get(
        f"{API_BASE_URL}/review", params={"status": status, "limit": limit}, headers=headers, timeout=5
    )
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=30)
def fetch_transaction(transaction_id: str) -> dict | None:
    resp = requests.get(f"{API_BASE_URL}/transactions/{transaction_id}", timeout=5)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_explanation(transaction_payload: dict) -> dict | None:
    try:
        resp = requests.post(f"{API_BASE_URL}/explain", json=transaction_payload, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        st.warning(f"Could not fetch explanation: {exc}")
        return None


def submit_feedback(case_id: str, analyst_id: str, label: str, notes: str) -> bool:
    try:
        resp = requests.post(
            f"{API_BASE_URL}/review/{case_id}/feedback",
            json={"analyst_id": analyst_id, "label": label, "notes": notes},
            headers=_auth_headers(),
            timeout=5,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        st.error(f"Failed to submit feedback: {exc}")
        return False


with st.sidebar:
    st.header("Filters")
    status_filter = st.selectbox("Case status", ["pending", "resolved"], index=0)
    limit = st.slider("Max cases", 10, 200, 50)
    analyst_id = st.text_input("Your analyst id", value="analyst_1")
    if st.button("Refresh queue"):
        fetch_queue.clear()

try:
    cases = fetch_queue(status_filter, limit, st.session_state.access_token)
except requests.RequestException as exc:
    st.error(f"Could not reach the fraud API at {API_BASE_URL}: {exc}")
    st.stop()

if not cases:
    st.info(f"No {status_filter} cases in the queue.")
    st.stop()

st.subheader(f"{len(cases)} {status_filter} case(s)")
df = pd.DataFrame(cases)
st.dataframe(
    df[["id", "transaction_id", "fraud_score", "risk_level", "reason", "created_at"]],
    use_container_width=True,
    hide_index=True,
)

st.divider()
st.subheader("Review a case")

case_ids = [c["id"] for c in cases]
selected_id = st.selectbox("Case id", case_ids)
selected_case = next(c for c in cases if c["id"] == selected_id)

col1, col2 = st.columns([2, 1])
with col1:
    st.markdown("**Case details**")
    st.json(selected_case)

    st.markdown("**Transaction & explanation**")
    txn = fetch_transaction(selected_case["transaction_id"])
    if txn is None:
        st.warning(
            f"Transaction {selected_case['transaction_id']} not found in PostgreSQL yet "
            "(the Spark consumer may not have persisted it). Try refreshing shortly."
        )
    else:
        st.json(txn, expanded=False)
        explanation = fetch_explanation(txn)
        if explanation:
            st.markdown(
                f"**Explanation type**: `{explanation['explanation_type']}` &nbsp;|&nbsp; "
                f"**Score**: {explanation['fraud_score']:.4f} &nbsp;|&nbsp; "
                f"**Latency**: {explanation['latency_ms']:.1f} ms"
            )
            if explanation["explanation_type"] == "shap":
                contrib_df = pd.DataFrame(explanation["top_features"])
                st.bar_chart(contrib_df.set_index("feature")["contribution"])
                st.dataframe(contrib_df, hide_index=True)
            else:
                st.warning(f"Rule-based reason: {explanation.get('rule_based_reason')}")

with col2:
    st.markdown("**Submit feedback**")
    label = st.radio(
        "Analyst label",
        ["confirmed_fraud", "confirmed_legitimate", "false_positive", "false_negative"],
    )
    notes = st.text_area("Notes", height=100)
    if st.button("Submit feedback", type="primary"):
        if submit_feedback(selected_id, analyst_id, label, notes):
            st.success("Feedback submitted -- case resolved, label stored for retraining.")
            fetch_queue.clear()
            st.rerun()
