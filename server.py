"""
Fabric Lakehouse MCP Server
============================

Connects Claude Desktop to a Microsoft Fabric lakehouse over the SQL endpoint.

What it does:
  - Authenticates with your personal Azure CLI login (no secrets stored)
  - Exposes read-only tools to Claude (SELECT only, row-capped)
  - Enforces a blocklist so sensitive tables can never be queried

Auth model:
  Uses AzureCliCredential, so each user runs `az login` once with their own
  work account. All access is scoped to that user's existing Fabric permissions.

Safety model (defence in depth):
  1. Only SELECT queries are allowed (no writes/deletes/DDL)
  2. Results are capped (TOP N) so no bulk extraction
  3. blocklist.json names tables the server refuses to touch
  NOTE: the blocklist is a convenience speed-bump, NOT a hard boundary.
        The durable control is database-side permissions on the SQL endpoint.
"""

import os
import json
import struct
import pyodbc
from azure.identity import AzureCliCredential
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------
# Always load .env relative to THIS file, regardless of the working directory
# Claude Desktop launches the server from.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

mcp = FastMCP("Fabric Lakehouse")

# Azure AD scope for the Fabric SQL endpoint (token audience).
FABRIC_SCOPE = "https://database.windows.net/.default"


# -----------------------------------------------------------------------------
# Blocklist  (loaded once at startup)
# -----------------------------------------------------------------------------
# blocklist.json shape:
#   { "blocked_tables": ["dbo.etl_audit_log", "pipeline_logs", ...] }
# Entries may be "schema.table" or just "table". All compared lowercased.
_BLOCKLIST_PATH = os.path.join(os.path.dirname(__file__), "blocklist.json")
try:
    with open(_BLOCKLIST_PATH) as f:
        _BLOCKED = {t.lower() for t in json.load(f).get("blocked_tables", [])}
except FileNotFoundError:
    # No blocklist file => nothing blocked. Server still runs.
    _BLOCKED = set()


def _check_blocklist(sql: str):
    """
    Raise ValueError if the query text references a blocked table.

    This is a substring check on the bare table name, so it is intentionally
    conservative: it errs toward blocking. It is a speed-bump, not a security
    boundary — enforce real access control with SQL endpoint permissions.
    """
    sql_lower = sql.lower()
    for entry in _BLOCKED:
        # entry is either "schema.table" or "table" — match on the table part
        table_part = entry.split(".")[-1]
        if table_part in sql_lower:
            raise ValueError(f"Access to '{entry}' is blocked by server policy.")


# -----------------------------------------------------------------------------
# Connection
# -----------------------------------------------------------------------------
def get_connection():
    """
    Open a pyodbc connection to the Fabric SQL endpoint using an Azure AD
    access token obtained from the local `az login` session.

    The token is packed into the ODBC connection attribute 1256
    (SQL_COPT_SS_ACCESS_TOKEN) as a length-prefixed UTF-16-LE byte struct,
    which is how the MSSQL ODBC driver expects an AAD token.
    """
    server   = os.getenv("FABRIC_SERVER")
    database = os.getenv("FABRIC_DATABASE")

    # Acquire an AAD token for the SQL endpoint using the CLI session.
    credential = AzureCliCredential()
    token = credential.get_token(FABRIC_SCOPE).token
    token_bytes = token.encode("UTF-16-LE")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={server},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;"
        "LoginTimeout=60;"
    )
    # 1256 = SQL_COPT_SS_ACCESS_TOKEN
    return pyodbc.connect(conn_str, attrs_before={1256: token_struct})


# -----------------------------------------------------------------------------
# Core query helper
# -----------------------------------------------------------------------------
def safe_query(sql: str, limit: int = 500):
    """
    Run a vetted SELECT and return (columns, rows).

    Guards applied, in order:
      1. Must start with SELECT (no writes/DDL)
      2. Must not reference a blocked table
      3. Wrapped in `SELECT TOP {limit} * FROM (...)` so the result set is capped

    Returns:
        columns: list[str]
        rows:    list[dict]  (one dict per row, column-name -> value)
    """
    sql_clean = sql.strip().rstrip(";")

    # Guard 1: SELECT only
    if not sql_clean.upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed.")

    # Guard 2: blocklist
    _check_blocklist(sql_clean)

    # Guard 3: cap the row count by wrapping the user's query
    wrapped = f"SELECT TOP {limit} * FROM ({sql_clean}) AS _q"

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(wrapped)
    columns = [col[0] for col in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return columns, rows


# -----------------------------------------------------------------------------
# Tools exposed to Claude
# -----------------------------------------------------------------------------
@mcp.tool()
def list_tables() -> str:
    """List all tables and views in the lakehouse (blocked tables excluded)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        rows = cursor.fetchall()
        conn.close()

        # Filter out anything on the blocklist (by full name OR bare table name)
        results = []
        for r in rows:
            full_name = f"{r[0]}.{r[1]}".lower()
            table_only = r[1].lower()
            if full_name not in _BLOCKED and table_only not in _BLOCKED:
                results.append({"schema": r[0], "table": r[1], "type": r[2]})
        return json.dumps(results, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def describe_table(schema: str, table: str) -> str:
    """Get columns, data types, and nullability for a specific table."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
        """, schema, table)
        rows = cursor.fetchall()
        conn.close()
        return json.dumps(
            [{"column": r[0], "type": r[1], "nullable": r[2], "max_length": r[3]} for r in rows],
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def run_query(sql: str) -> str:
    """
    Run a SELECT query against the Fabric lakehouse.
    Results capped at 500 rows. Use for counting, filtering, aggregating, joining.
    Blocked tables will raise an error.
    """
    try:
        columns, rows = safe_query(sql)
        return json.dumps(
            {"columns": columns, "row_count": len(rows), "rows": rows},
            indent=2,
            default=str,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_sample_rows(schema: str, table: str, n: int = 5) -> str:
    """Get a few sample rows from a table to understand what data it holds."""
    try:
        # Build the query via safe_query so the blocklist + SELECT guard apply.
        sql = f"SELECT * FROM [{schema}].[{table}]"
        columns, rows = safe_query(sql, limit=min(n, 20))
        return json.dumps({"columns": columns, "rows": rows}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# -----------------------------------------------------------------------------
# Entry point — stdio transport is what Claude Desktop connects to.
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")