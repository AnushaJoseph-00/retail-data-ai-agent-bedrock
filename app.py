"""Streamlit chat interface for the AI Data Analyst Agent.

Run:  streamlit run app.py
"""

import os

import boto3
import pandas as pd
import streamlit as st
from botocore.exceptions import ClientError, NoCredentialsError

import agent

# Approximate prices (USD) for the cost estimate shown in the UI - check AWS pricing pages.
PRICE_IN_PER_M = 0.06      # Nova Lite input tokens, per million
PRICE_OUT_PER_M = 0.24     # Nova Lite output tokens, per million
ATHENA_PER_TB = 5.0
ATHENA_MIN_BYTES = 10 * 1024 * 1024  # Athena bills at least 10 MB per query

EXAMPLES = [
    "Which store city generated the most revenue in 2023?",
    "Show monthly revenue for 2022. Which month was strongest?",
    "What percentage of shipments are late, by store city?",
    "Which supplier country has the highest refund value as a share of its sales revenue?",
    "Do orders with bigger promotion discounts have a higher average order value?",
]

st.set_page_config(page_title="AI Data Analyst", page_icon="📊", layout="wide")


# ---------- helpers ----------

def to_frame(result):
    header, rows = result
    df = pd.DataFrame(rows, columns=header)
    for col in df.columns:  # Athena returns every value as text
        converted = pd.to_numeric(df[col], errors="coerce")
        if converted.notna().all():
            df[col] = converted
    return df


def chart_for(df):
    """Bar chart when the result is a label column plus at least one numeric column."""
    if len(df) < 2 or len(df) > 50 or len(df.columns) < 2:
        return
    label = df.columns[0]
    values = [c for c in df.columns[1:] if pd.api.types.is_numeric_dtype(df[c])]
    if not values or label.lower().endswith("_id"):
        return
    st.bar_chart(df.set_index(df[label].astype(str))[values[0]])


def estimate_cost(stats):
    tokens = stats["tokens_in"] * PRICE_IN_PER_M / 1e6 + stats["tokens_out"] * PRICE_OUT_PER_M / 1e6
    queries = stats.get("queries", 0)
    scanned = max(stats["bytes_scanned"], queries * ATHENA_MIN_BYTES)
    return tokens + scanned / 1e12 * ATHENA_PER_TB


def render_step(ev):
    icon = "✅" if ev["status"] == "success" else "⚠️"
    if ev["tool"] == "run_sql":
        st.markdown(f"{icon} **Step {ev['step']} · run SQL**")
        st.code(ev["input"].get("query", ""), language="sql")
        if ev["result"]:
            st.dataframe(to_frame(ev["result"]), hide_index=True, width="stretch")
        else:
            st.error(ev["output"][:500])
    else:
        target = ev["input"].get("table", "")
        st.markdown(f"{icon} **Step {ev['step']} · {ev['tool'].replace('_', ' ')}** {target}")


def render_answer(turn):
    st.markdown(turn["answer"] or "_The agent stopped before reaching an answer._")
    final = next((e for e in reversed(turn["steps"]) if e.get("result")), None)
    if final:
        df = to_frame(final["result"])
        chart_for(df)
    with st.expander(f"How the agent worked it out ({len(turn['steps'])} tool calls)"):
        for ev in turn["steps"]:
            render_step(ev)
    s = turn["stats"]
    st.caption(f"{s['steps']} steps · {s['tokens_in']:,} tokens in / {s['tokens_out']:,} out · "
               f"{s['bytes_scanned'] / 1e6:.1f} MB scanned · ≈ ${estimate_cost(s):.4f}")


# ---------- sidebar ----------

with st.sidebar:
    st.header("Settings")
    backend_name = st.radio("Data source", ["athena", "duckdb"],
                            format_func=lambda b: "Amazon Athena" if b == "athena" else "Local CSVs (DuckDB)")
    if backend_name == "athena":
        bucket = st.text_input("S3 bucket", os.environ.get("RETAIL_BUCKET", "retail-dw-anu"))
    else:
        data_dir = st.text_input("CSV folder", "./data")
    region = st.text_input("AWS region", "us-east-1")
    model = st.text_input("Bedrock model", agent.DEFAULT_MODEL)

    st.divider()
    st.subheader("Try a question")
    for q in EXAMPLES:
        if st.button(q, width="stretch"):
            st.session_state.pending = q

    st.divider()
    total = st.session_state.get("total_cost", 0.0)
    st.metric("Estimated cost this session", f"${total:.4f}")
    st.caption("Read-only: the agent can only run SELECT queries, max 200 rows each.")
    if st.button("Clear chat"):
        st.session_state.history = []
        st.session_state.total_cost = 0.0
        st.rerun()


# ---------- connections (cached per settings) ----------

@st.cache_resource(show_spinner=False)
def get_clients(backend_name, location, region):
    session = boto3.Session(region_name=region)
    backend = agent.AthenaBackend(session, location) if backend_name == "athena" else agent.DuckDBBackend(location)
    return session.client("bedrock-runtime"), backend


# ---------- main ----------

st.title("📊 AI Data Analyst")
st.write("Ask a business question about the retail data warehouse (12 tables, 1.6M rows). "
         "The agent writes SQL, runs it on Amazon Athena, checks the results and explains the answer.")

st.session_state.setdefault("history", [])
st.session_state.setdefault("total_cost", 0.0)

for turn in st.session_state.history:
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        render_answer(turn)

question = st.chat_input("e.g. Which product category had the highest revenue in 2023?")
question = question or st.session_state.pop("pending", None)

if question:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        location = bucket if backend_name == "athena" else data_dir
        try:
            bedrock, backend = get_clients(backend_name, location, region)
        except Exception as e:
            st.error(f"Could not connect: {e}")
            st.stop()

        steps, stats = [], {}

        def on_event(ev):
            if ev["type"] == "tool":
                steps.append(ev)
                with status_box:
                    render_step(ev)
            else:
                stats.update(ev)

        status_box = st.status("Analysing…", expanded=True)
        try:
            answer = agent.ask(question, bedrock, model, backend, verbose=False, on_event=on_event)
        except NoCredentialsError:
            status_box.update(label="No AWS credentials", state="error")
            st.error("No AWS credentials found. Run `aws configure` in a terminal, then restart the app.")
            st.stop()
        except ClientError as e:
            status_box.update(label="AWS error", state="error")
            st.error(f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}")
            st.stop()
        status_box.update(label=f"Done in {stats.get('steps', '?')} steps", state="complete", expanded=False)

        stats["queries"] = sum(1 for s in steps if s["tool"] == "run_sql" and s["status"] == "success")
        turn = {"question": question, "answer": answer, "steps": steps, "stats": stats}
        st.session_state.history.append(turn)
        st.session_state.total_cost += estimate_cost(stats)
    st.rerun()  # redraw from history so the answer and sidebar cost update together
