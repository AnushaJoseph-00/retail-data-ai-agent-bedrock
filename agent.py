#!/usr/bin/env python3
"""AI Data Analyst Agent - answers business questions over a retail data warehouse.

The model (Amazon Bedrock) decides which tools to call:
  - list_tables / describe_table : learn the schema
  - run_sql                      : run a read-only query on Amazon Athena
It can retry when a query fails, then writes a short answer with the SQL it used.

Usage:
  python3 agent.py --bucket my-retail-data-bucket "Which city had the highest revenue in 2023?"
  python3 agent.py --bucket my-retail-data-bucket            # interactive mode
  python3 agent.py --backend duckdb --data ./data "..."      # local testing, no Athena
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from schema import TABLES, schema_text

DATABASE = "retail_dw"
DEFAULT_MODEL = "amazon.nova-lite-v1:0"
MAX_ROWS = 200      # cap on rows returned per query
MAX_STEPS = 8       # cap on agent loop iterations
TRACE_FILE = "agent_trace.jsonl"

SYSTEM_PROMPT = f"""You are a careful data analyst for a retail business.
Answer the user's business question using ONLY data you retrieve with the run_sql tool.
Never guess or invent numbers.

Data warehouse (Amazon Athena, Trino SQL dialect):
{schema_text()}

SQL tips:
- Revenue from sales = SUM(order_items.qty * order_items.price).
- Dates are DATE columns: use year(order_date), date_trunc('month', order_date), etc.
- Join on the id columns shown above. Aggregate in SQL rather than fetching raw rows.
- Results are capped at {MAX_ROWS} rows, so always aggregate or use ORDER BY ... LIMIT.
- Monetary values are in the store's currency units (currency not specified).

If a query fails, read the error, fix the SQL and try again.

Final answer format:
1. A direct answer in one or two sentences.
2. The key numbers (a short list or small table).
3. "SQL used:" followed by the final query.
4. Any caveat (e.g. assumptions you made).
"""

TOOLS = [
    {"toolSpec": {
        "name": "list_tables",
        "description": "List all tables in the retail data warehouse.",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }},
    {"toolSpec": {
        "name": "describe_table",
        "description": "Show the columns of one table and how it joins to other tables.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Table name"}},
            "required": ["table"],
        }},
    }},
    {"toolSpec": {
        "name": "run_sql",
        "description": "Run ONE read-only SELECT query (Trino SQL) on the warehouse and return the rows.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A single SELECT or WITH query"}},
            "required": ["query"],
        }},
    }},
]

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|merge|grant|revoke|msck|unload|call|repair)\b",
    re.IGNORECASE,
)


# ---------- Guardrails ----------

def validate_sql(query):
    """Allow a single read-only SELECT/WITH query, and enforce a row limit."""
    q = query.strip().rstrip(";").strip()
    if ";" in q:
        raise ValueError("Only one SQL statement is allowed.")
    first = re.match(r"\s*(\w+)", q)
    if not first or first.group(1).lower() not in ("select", "with"):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    if FORBIDDEN.search(q):
        raise ValueError("Query contains a forbidden keyword; this agent is read-only.")
    m = re.search(r"\blimit\s+(\d+)\s*$", q, re.IGNORECASE)
    if m is None:
        q = f"{q}\nLIMIT {MAX_ROWS}"
    elif int(m.group(1)) > MAX_ROWS:
        q = q[: m.start(1)] + str(MAX_ROWS)
    return q


def format_rows(header, rows):
    out = [" | ".join(header)]
    out += [" | ".join("" if v is None else str(v) for v in r) for r in rows]
    return "\n".join(out)


# ---------- Query backends ----------

class AthenaBackend:
    def __init__(self, session, bucket):
        self.athena = session.client("athena")
        self.output = f"s3://{bucket}/athena-results/"
        self.bytes_scanned = 0
        self.last = None

    def query(self, sql):
        qid = self.athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": DATABASE},
            ResultConfiguration={"OutputLocation": self.output},
        )["QueryExecutionId"]
        deadline = time.time() + 90
        while True:
            ex = self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = ex["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.time() > deadline:
                self.athena.stop_query_execution(QueryExecutionId=qid)
                raise RuntimeError("Query timed out after 90 seconds.")
            time.sleep(1)
        if state != "SUCCEEDED":
            raise RuntimeError(ex["Status"].get("StateChangeReason", state))
        self.bytes_scanned += ex.get("Statistics", {}).get("DataScannedInBytes", 0)
        res = self.athena.get_query_results(QueryExecutionId=qid, MaxResults=MAX_ROWS + 1)
        data = [[c.get("VarCharValue") for c in r["Data"]] for r in res["ResultSet"]["Rows"]]
        self.last = (data[0], data[1:])
        return self.last


class DuckDBBackend:
    """Local stand-in for Athena, for testing without AWS query costs."""

    def __init__(self, data_dir):
        import duckdb
        self.con = duckdb.connect()
        self.bytes_scanned = 0
        self.last = None
        for name, t in TABLES.items():
            cols = ", ".join(f"'{c}': '{'INTEGER' if ty == 'int' else ty.upper().replace('STRING', 'VARCHAR')}'"
                             for c, ty in t["columns"])
            self.con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_csv('{data_dir}/{name}.csv', header=true, columns={{{cols}}})"
            )

    def query(self, sql):
        cur = self.con.execute(sql)
        header = [d[0] for d in cur.description]
        self.last = (header, cur.fetchmany(MAX_ROWS))
        return self.last


# ---------- Tools ----------

def run_tool(name, args, backend):
    if name == "list_tables":
        return ", ".join(TABLES)
    if name == "describe_table":
        t = TABLES.get(args.get("table", ""))
        if not t:
            raise ValueError(f"Unknown table. Available: {', '.join(TABLES)}")
        cols = ", ".join(f"{c} ({ty})" for c, ty in t["columns"])
        return f"Columns: {cols}\n{t['about']}"
    if name == "run_sql":
        sql = validate_sql(args.get("query", ""))
        header, rows = backend.query(sql)
        return f"{len(rows)} row(s)\n{format_rows(header, rows)}"
    raise ValueError(f"Unknown tool {name}")


# ---------- Agent loop ----------

def ask(question, bedrock, model_id, backend, verbose=True, on_event=None):
    """Run the agent loop. on_event(dict) is called after each tool call and at the end (used by the UI)."""
    bytes_start = backend.bytes_scanned
    messages = [{"role": "user", "content": [{"text": question}]}]
    tokens_in = tokens_out = 0
    trace = {"time": datetime.now().isoformat(timespec="seconds"), "question": question, "steps": []}

    for step in range(1, MAX_STEPS + 1):
        resp = bedrock.converse(
            modelId=model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig={"tools": TOOLS},
            inferenceConfig={"maxTokens": 1500, "temperature": 0.1},
        )
        usage = resp.get("usage", {})
        tokens_in += usage.get("inputTokens", 0)
        tokens_out += usage.get("outputTokens", 0)
        msg = resp["output"]["message"]
        messages.append(msg)

        if resp["stopReason"] != "tool_use":
            answer = "\n".join(b["text"] for b in msg["content"] if "text" in b).strip()
            scanned = backend.bytes_scanned - bytes_start
            trace.update(answer=answer, tokens_in=tokens_in, tokens_out=tokens_out, bytes_scanned=scanned)
            _write_trace(trace)
            if on_event:
                on_event({"type": "final", "answer": answer, "steps": step, "tokens_in": tokens_in,
                          "tokens_out": tokens_out, "bytes_scanned": scanned})
            if verbose:
                mb = scanned / 1e6
                print(f"\n[{step} steps | tokens {tokens_in} in / {tokens_out} out | {mb:.1f} MB scanned]")
            return answer

        results = []
        for block in msg["content"]:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            if verbose:
                shown = tu["input"].get("query", tu["input"].get("table", ""))
                print(f"\n> Step {step}: {tu['name']} {shown}".rstrip())
            backend.last = None
            try:
                output, status = run_tool(tu["name"], tu["input"], backend), "success"
            except Exception as e:  # any failure goes back to the model so it can retry
                output, status = f"ERROR: {e}", "error"
            if on_event:
                on_event({"type": "tool", "step": step, "tool": tu["name"], "input": tu["input"],
                          "status": status, "output": output,
                          "result": backend.last if tu["name"] == "run_sql" and status == "success" else None})
            if verbose:
                print("  " + output[:400].replace("\n", "\n  "))
            trace["steps"].append({"tool": tu["name"], "input": tu["input"], "status": status})
            results.append({"toolResult": {"toolUseId": tu["toolUseId"],
                                           "content": [{"text": output[:6000]}], "status": status}})
        messages.append({"role": "user", "content": results})

    _write_trace(trace)
    if on_event:
        on_event({"type": "final", "answer": None, "steps": MAX_STEPS, "tokens_in": tokens_in,
                  "tokens_out": tokens_out, "bytes_scanned": backend.bytes_scanned - bytes_start})
    return "Stopped: the agent reached its step limit without a final answer."


def _write_trace(trace):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(json.dumps(trace, default=str) + "\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="AI Data Analyst Agent (Bedrock + Athena)")
    ap.add_argument("question", nargs="?", help="Business question (omit for interactive mode)")
    ap.add_argument("--bucket", help="S3 bucket used for Athena results")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Bedrock model ID (default {DEFAULT_MODEL})")
    ap.add_argument("--backend", choices=["athena", "duckdb"], default="athena")
    ap.add_argument("--data", default="./data", help="CSV folder for the duckdb backend")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    if args.backend == "athena":
        if not args.bucket:
            sys.exit("--bucket is required for the athena backend")
        backend = AthenaBackend(session, args.bucket)
    else:
        backend = DuckDBBackend(args.data)
    bedrock = session.client("bedrock-runtime")

    def answer(q):
        try:
            print("\n" + ask(q, bedrock, args.model, backend))
        except ClientError as e:
            print(f"\nBedrock error: {e.response['Error']['Code']} - {e.response['Error'].get('Message', '')}")

    if args.question:
        answer(args.question)
        return
    print("AI Data Analyst - ask a business question (blank line to quit)")
    while True:
        q = input("\nQuestion: ").strip()
        if not q:
            break
        answer(q)


if __name__ == "__main__":
    main()
