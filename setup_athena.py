#!/usr/bin/env python3
"""One-time setup: organise the CSVs in S3 and create Athena tables.

Before running, upload the 12 CSV files to s3://<your-bucket>/raw/
(S3 console -> your bucket -> Create folder "raw" -> Upload).

Usage:
  python3 setup_athena.py --bucket my-retail-data-bucket
"""

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError

from schema import TABLES

DATABASE = "retail_dw"


def run(athena, sql, bucket, database=None):
    params = {
        "QueryString": sql,
        "ResultConfiguration": {"OutputLocation": f"s3://{bucket}/athena-results/"},
    }
    if database:
        params["QueryExecutionContext"] = {"Database": database}
    qid = athena.start_query_execution(**params)["QueryExecutionId"]
    while True:
        state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if state["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if state["State"] != "SUCCEEDED":
        sys.exit(f"Query failed: {state.get('StateChangeReason')}\n{sql}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, help="S3 bucket holding raw/*.csv")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    s3 = session.client("s3")
    athena = session.client("athena")

    # Athena reads every file in a folder as one table, so each CSV gets its own folder.
    for name in TABLES:
        src = f"raw/{name}.csv"
        dst = f"tables/{name}/{name}.csv"
        try:
            s3.copy_object(Bucket=args.bucket, CopySource={"Bucket": args.bucket, "Key": src}, Key=dst)
        except ClientError as e:
            sys.exit(f"Could not copy s3://{args.bucket}/{src} - is it uploaded? ({e})")
        print(f"Copied {src} -> {dst}")

    run(athena, f"CREATE DATABASE IF NOT EXISTS {DATABASE}", args.bucket)
    print(f"Database {DATABASE} ready")

    for name, t in TABLES.items():
        cols = ",\n  ".join(f"`{c}` {ty}" for c, ty in t["columns"])
        run(athena, f"DROP TABLE IF EXISTS {name}", args.bucket, DATABASE)
        run(athena, f"""
CREATE EXTERNAL TABLE {name} (
  {cols}
)
ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
STORED AS TEXTFILE
LOCATION 's3://{args.bucket}/tables/{name}/'
TBLPROPERTIES ('skip.header.line.count'='1')""", args.bucket, DATABASE)
        print(f"Table {name} created")

    print("\nDone. Try: python3 agent.py --bucket", args.bucket)


if __name__ == "__main__":
    main()
