#!/usr/bin/env python3
"""q.py <sql文件> — 用 DuckDB 执行 SQL 文件（uv run --with duckdb q.py queries.sql）"""
import sys
import duckdb

path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines()
sql = "\n".join(l for l in lines if not l.strip().startswith("--"))
for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
    duckdb.query(stmt).show()
    print("---")
