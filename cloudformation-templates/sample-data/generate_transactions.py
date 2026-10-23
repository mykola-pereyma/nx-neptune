#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate a small, deterministic, synthetic transactions CSV for nx-neptune smoke tests.

The column layout is modelled on PaySim (E. A. Lopez-Rojas, A. Elmir, S. Axelsson,
"PaySim: A financial mobile money simulator for fraud detection", EMSS 2016; the
PaySim dataset is CC BY-SA 4.0). This file contains no PaySim data: every value is
seeded random output from this script, so the same arguments always give the same
rows and the same expected graph.

    python3 generate_transactions.py --out transactions.csv
    aws s3 cp transactions.csv s3://<staging bucket>/sample/transactions/transactions.csv

Prints the graph the notebook's CREATE PROPERTY GRAPH (customers as vertices,
transfers as edges) should produce from it.
"""
import argparse
import csv
import json
import random

TYPES = ("PAYMENT", "TRANSFER", "CASH_OUT", "DEBIT", "CASH_IN")
HEADER = ("step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig",
          "nameDest", "oldbalanceDest", "newbalanceDest", "isFraud", "isFlaggedFraud")


def generate(rows: int, seed: int) -> list[list]:
    rnd = random.Random(seed)
    senders = [f"C{1000000000 + i}" for i in range(max(1, rows // 5))]
    receivers = [f"C{2000000000 + i}" for i in range(max(1, rows * 3 // 20))] + [f"M{3000000000 + i}" for i in range(max(1, rows // 20))]
    out = []
    for _ in range(rows):
        kind, amount = rnd.choice(TYPES), round(rnd.uniform(1, 200000), 2)
        orig_before, dest_before = round(rnd.uniform(0, 500000), 2), round(rnd.uniform(0, 500000), 2)
        fraud = int(kind in ("TRANSFER", "CASH_OUT") and rnd.random() < 0.02)
        out.append([rnd.randint(1, 743), kind, amount, rnd.choice(senders), orig_before, round(max(orig_before - amount, 0), 2),
                    rnd.choice(receivers), dest_before, round(dest_before + amount, 2), fraud, int(fraud and amount > 150000)])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--out", default="transactions.csv")
    args = ap.parse_args()
    if args.rows < 1:
        ap.error("--rows must be at least 1")
    data = generate(args.rows, args.seed)
    with open(args.out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(data)
    ids = {r[3] for r in data} | {r[6] for r in data}
    print(json.dumps({"file": args.out, "rows": len(data), "expected_graph": {"nodes": len(ids), "edges": len(data)},
                      "fraud_rows": sum(r[9] for r in data)}))


if __name__ == "__main__":
    main()
