"""Synthetic warehouse tools with a durable, idempotent reservation ledger."""

import json
import sqlite3

from .engine import Tool


def fields(args, expected):
    if set(args) != set(expected):
        raise ValueError("Unexpected or missing tool fields")
    for key, kind in expected.items():
        if type(args[key]) is not kind:
            raise ValueError(f"Invalid {key}")


def inventory_args(args):
    fields(args, {"sku": str})
    if args["sku"] not in {"sensor-kit", "relay-board"}:
        raise ValueError("Unknown synthetic SKU")


def quantity_args(args):
    fields(args, {"sku": str, "quantity": int})
    inventory_args({"sku": args["sku"]})
    if not 1 <= args["quantity"] <= 100:
        raise ValueError("Quantity must be 1..100")


def warehouse_tools(path: str):
    db = sqlite3.connect(path)
    with db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS stock(sku TEXT PRIMARY KEY, available INTEGER);
        CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY, args TEXT, output TEXT);
        INSERT OR IGNORE INTO stock VALUES('sensor-kit',12),('relay-board',20);
        """)
    db.close()

    def inventory(args, key):
        conn = sqlite3.connect(path)
        try:
            return {
                "sku": args["sku"],
                "available": conn.execute(
                    "SELECT available FROM stock WHERE sku=?", (args["sku"],)
                ).fetchone()[0],
            }
        finally:
            conn.close()

    def quote(args, key):
        price = {"sensor-kit": 4900, "relay-board": 1800}[args["sku"]]
        return {**args, "total_cents": price * args["quantity"], "currency": "USD"}

    def reserve(args, key):
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT args,output FROM receipts WHERE key=?", (key,)
            ).fetchone()
            encoded = json.dumps(args, sort_keys=True)
            if existing:
                if existing[0] != encoded:
                    raise ValueError("Idempotency key reused with different arguments")
                conn.commit()
                return json.loads(existing[1])
            cur = conn.execute(
                "UPDATE stock SET available=available-? WHERE sku=? AND available>=?",
                (args["quantity"], args["sku"], args["quantity"]),
            )
            if cur.rowcount != 1:
                raise ValueError("Insufficient synthetic stock")
            output = {"receipt": key, **args, "reserved": True, "synthetic": True}
            conn.execute("INSERT INTO receipts VALUES(?,?,?)", (key, encoded, json.dumps(output)))
            conn.commit()
            return output
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    return [
        Tool(
            "inventory",
            "Read available units. args: {sku:string}",
            False,
            inventory,
            inventory_args,
        ),
        Tool(
            "quote", "Price units. args: {sku:string,quantity:integer}", False, quote, quantity_args
        ),
        Tool(
            "reserve",
            "Reserve synthetic stock; approval required. args: {sku:string,quantity:integer}",
            True,
            reserve,
            quantity_args,
        ),
    ]
