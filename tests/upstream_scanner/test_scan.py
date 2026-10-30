"""扫描器验收（阶段二设计 §8.1 S1–S7）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from fdescan import CatalogInvalid, validate
from fdescan.db import scan
from fdescan.ddl import parse

VALUES = ["Exotic Liquids", "Chai", "Chang", "13812345678", "13987654321", "ALFKI-秘密客户"]


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "nw.db"
    c = sqlite3.connect(p)
    c.executescript("""
      CREATE TABLE suppliers (SupplierID INTEGER PRIMARY KEY, CompanyName TEXT NOT NULL);
      CREATE TABLE products (ProductID INTEGER PRIMARY KEY, ProductName TEXT, SupplierID INTEGER REFERENCES suppliers(SupplierID),
                             UnitsInStock INTEGER, ReorderLevel INTEGER, Status TEXT);
      CREATE TABLE customers (CustomerID TEXT PRIMARY KEY, Phone TEXT, CreatedAt DATE);
    """)
    c.execute("INSERT INTO suppliers VALUES (1, 'Exotic Liquids')")
    for i, n in enumerate(["Chai", "Chang"], 1):
        c.execute("INSERT INTO products VALUES (?, ?, 1, ?, 10, ?)", (i, n, 30 + i, "ACTIVE"))
    start = date(2026, 1, 1)
    for i, ph in enumerate(["13812345678", "13987654321", "13711112222", "13633334444", "13555556666"]):
        c.execute("INSERT INTO customers VALUES (?, ?, ?)", (f"ALFKI-秘密客户{i}", ph, (start + timedelta(days=30 * i)).isoformat()))
    c.commit()
    c.close()
    return p


def test_s1_schema_and_fk(db):
    doc = scan(f"sqlite:///{db}", system_hint="ERP")
    validate(doc)
    prod = next(a for a in doc["catalog"]["assets"] if a["name"] == "products")
    assert prod["foreign_keys"] == [{"columns": ["SupplierID"], "ref_table": "suppliers", "ref_columns": ["SupplierID"], "declared": True}]
    assert next(a for a in doc["catalog"]["assets"] if a["name"] == "suppliers")["referenced_by"] == 1
    assert prod["row_count"] == 2


def test_s2_no_values_leave(db):
    text = json.dumps(scan(f"sqlite:///{db}"), ensure_ascii=False)
    for v in VALUES:
        assert v not in text


def test_s3_phone_column_flagged(db):
    doc = scan(f"sqlite:///{db}")
    cust = next(a for a in doc["catalog"]["assets"] if a["name"] == "customers")
    phone = next(c for c in cust["columns"] if c["name"] == "Phone")
    assert phone["looks_like"] == "phone" and phone["sensitive_column"] is True
    created = next(c for c in cust["columns"] if c["name"] == "CreatedAt")
    assert created["shape"] == "date" and created["date_span_days"] == 120


def test_s4_comment_redacted():
    sql = "CREATE TABLE kh (id INT PRIMARY KEY, name VARCHAR(20) COMMENT '联系人电话：13812345678');"
    doc = parse(sql, dialect="mysql")
    col = doc["catalog"]["assets"][0]["columns"][1]
    assert col["comment"] == "[已屏蔽]" and doc["catalog"]["redactions"]
    assert "13812345678" not in json.dumps(doc, ensure_ascii=False)


def test_s5_permission_error_recorded(db):
    def deny_customers(conn):
        def auth(action, arg1, arg2, dbname, source):
            return sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_READ and arg1 == "customers") else sqlite3.SQLITE_OK
        conn.set_authorizer(auth)
    doc = scan(f"sqlite:///{db}", on_connect=deny_customers)
    scope = doc["catalog"]["scope"]
    assert any("customers" in e["object"] for e in scope["permission_errors"])
    assert scope["tables_total"] is None and scope["stats_available"] is False
    assert {a["name"] for a in doc["catalog"]["assets"]} >= {"suppliers", "products"}


def test_s6_ddl_matches_db(db):
    sql = """
      CREATE TABLE suppliers (SupplierID INTEGER PRIMARY KEY, CompanyName TEXT NOT NULL);
      CREATE TABLE products (ProductID INTEGER PRIMARY KEY, ProductName TEXT, SupplierID INTEGER REFERENCES suppliers(SupplierID),
                             UnitsInStock INTEGER, ReorderLevel INTEGER, Status TEXT);
      CREATE TABLE customers (CustomerID TEXT PRIMARY KEY, Phone TEXT, CreatedAt DATE);
    """
    a = scan(f"sqlite:///{db}")["catalog"]["assets"]
    b = parse(sql, dialect="sqlite")["catalog"]["assets"]
    norm = lambda xs: {(x["name"], tuple(c["name"] for c in x["columns"]), tuple(x["primary_key"]),
                        tuple((tuple(f["columns"]), f["ref_table"]) for f in x["foreign_keys"])) for x in xs}
    assert norm(a) == norm(b)


def test_s7_only_reads(db):
    writes = []

    def watch(conn):
        def auth(action, *_):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE):
                writes.append(action)
            return sqlite3.SQLITE_OK
        conn.set_authorizer(auth)
    scan(f"sqlite:///{db}", on_connect=watch)
    assert writes == []


def test_validate_rejects_value_fields(db):
    doc = scan(f"sqlite:///{db}")
    doc["catalog"]["assets"][0]["columns"][0]["sample_values"] = ["x"]
    with pytest.raises(CatalogInvalid):
        validate(doc)


def test_enum_values_opt_in(db):
    off = scan(f"sqlite:///{db}")
    st = lambda d: next(c for a in d["catalog"]["assets"] if a["name"] == "products" for c in a["columns"] if c["name"] == "Status")
    assert "enum_values" not in st(off)
    on = scan(f"sqlite:///{db}", allow_enum=True)
    assert st(on)["enum_values"] == ["ACTIVE"]
    cust = next(a for a in on["catalog"]["assets"] if a["name"] == "customers")
    assert all("enum_values" not in c for c in cust["columns"] if c["sensitive_column"])


def test_guard_rules_identical_to_service():
    import hashlib
    from pathlib import Path
    import fdescan
    from ps.core.guard import RULES_FILE
    mine = Path(fdescan.__file__).with_name("guard_rules.yaml")
    theirs = RULES_FILE
    assert hashlib.sha256(mine.read_bytes()).hexdigest() == hashlib.sha256(theirs.read_bytes()).hexdigest()



def test_dates_are_not_phones():
    from fdescan.features import looks_like
    assert looks_like(["1996-07-04", "1997-01-15", "1998-05-06"]) is None
    assert looks_like(["1996-07-04 00:00:00.000", "1997-01-15 00:00:00.000"]) is None
    assert looks_like(["030-0074321", "(5) 555-4729", "(171) 555-7788"]) == "phone"
    assert looks_like(["10248", "10249", "1000001"]) is None
