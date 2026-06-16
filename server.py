"""
Fabric Lakehouse MCP Server
============================

Connects Claude Desktop / Claude Code to a Microsoft Fabric lakehouse.

Two capability groups:
  1. SQL tools  — read-only queries against the Fabric SQL endpoint (lh_silver)
  2. Notebook tools — read, write, create and run notebooks via the Fabric REST API

Auth model:
  Uses AzureCliCredential throughout. Each user runs `az login` once with their
  own work account. No secrets are stored anywhere in this file.

  SQL tools use scope:      https://database.windows.net/.default
  Notebook tools use scope: https://api.fabric.microsoft.com/.default

Safety model (SQL layer):
  1. Only SELECT queries are allowed (no writes/deletes/DDL)
  2. Results are capped at 500 rows — no bulk extraction
  3. blocklist.json names tables the server refuses to touch
  NOTE: the blocklist is a convenience speed-bump, NOT a hard boundary.
        The durable control is database-side permissions on the SQL endpoint.

Notebook tools are intentionally ungated — they operate within whatever
Fabric permissions the authenticated user already holds.

.env keys required:
  FABRIC_SERVER       — SQL endpoint hostname
  FABRIC_DATABASE     — database name (lh_silver)
  FABRIC_WORKSPACE_ID — workspace GUID (used by all notebook tools)
"""

import os
import json
import struct
import base64
import requests
import pyodbc
from azure.identity import AzureCliCredential
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------
# Always load .env relative to THIS file, regardless of working directory.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

mcp = FastMCP("Fabric Lakehouse")

# Azure AD scopes — SQL and Fabric REST API use different token audiences.
SQL_SCOPE     = "https://database.windows.net/.default"
FABRIC_SCOPE  = "https://api.fabric.microsoft.com/.default"


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
    # No blocklist file — nothing blocked. Server still runs.
    _BLOCKED = set()


def _check_blocklist(sql: str):
    """
    Raise ValueError if the query text references a blocked table.

    This is a substring check on the bare table name — intentionally
    conservative. It is a speed-bump, not a security boundary.
    Enforce real access control with SQL endpoint permissions.
    """
    sql_lower = sql.lower()
    for entry in _BLOCKED:
        table_part = entry.split(".")[-1]
        if table_part in sql_lower:
            raise ValueError(f"Access to '{entry}' is blocked by server policy.")


# -----------------------------------------------------------------------------
# Token helpers
# -----------------------------------------------------------------------------
def _get_sql_token() -> bytes:
    """
    Return an AAD access token for the Fabric SQL endpoint, packed as the
    length-prefixed UTF-16-LE byte struct that the MSSQL ODBC driver expects
    in connection attribute 1256 (SQL_COPT_SS_ACCESS_TOKEN).
    """
    credential = AzureCliCredential()
    token = credential.get_token(SQL_SCOPE).token
    token_bytes = token.encode("UTF-16-LE")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def _get_fabric_headers() -> dict:
    """
    Return HTTP headers carrying a Bearer token for the Fabric REST API.
    Used by all notebook tools.
    """
    credential = AzureCliCredential()
    token = credential.get_token(FABRIC_SCOPE).token
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# -----------------------------------------------------------------------------
# SQL connection
# -----------------------------------------------------------------------------
def get_connection():
    """
    Open a pyodbc connection to the Fabric SQL endpoint using an Azure AD
    token from the local az login session.
    """
    server   = os.getenv("FABRIC_SERVER")
    database = os.getenv("FABRIC_DATABASE")

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={server},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;"
        "LoginTimeout=60;"
    )
    return pyodbc.connect(conn_str, attrs_before={1256: _get_sql_token()})


# -----------------------------------------------------------------------------
# Core query helper
# -----------------------------------------------------------------------------
def safe_query(sql: str, limit: int = 500):
    """
    Run a vetted SELECT and return (columns, rows).

    Guards applied in order:
      1. Must start with SELECT (no writes/DDL)
      2. Must not reference a blocked table
      3. Wrapped in SELECT TOP {limit} to cap the result set
    """
    sql_clean = sql.strip().rstrip(";")

    if not sql_clean.upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed.")

    _check_blocklist(sql_clean)

    wrapped = f"SELECT TOP {limit} * FROM ({sql_clean}) AS _q"
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(wrapped)
    columns = [col[0] for col in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return columns, rows


# =============================================================================
# SQL TOOLS (read-only, lakehouse queries)
# =============================================================================

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

        results = []
        for r in rows:
            full_name  = f"{r[0]}.{r[1]}".lower()
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
        sql = f"SELECT * FROM [{schema}].[{table}]"
        columns, rows = safe_query(sql, limit=min(n, 20))
        return json.dumps({"columns": columns, "rows": rows}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# NOTEBOOK TOOLS (Fabric REST API)
# All tools read FABRIC_WORKSPACE_ID from .env.
# Auth scope: https://api.fabric.microsoft.com/.default
# =============================================================================

def _workspace_id() -> str:
    """Return the workspace ID from .env, raising clearly if missing."""
    wid = os.getenv("FABRIC_WORKSPACE_ID")
    if not wid:
        raise ValueError(
            "FABRIC_WORKSPACE_ID is not set in .env. "
            "Find it in the Fabric portal URL: app.fabric.microsoft.com/groups/<GUID>/..."
        )
    return wid


@mcp.tool()
def list_notebooks() -> str:
    """
    List all notebooks in the Fabric workspace.
    Returns each notebook's ID, display name, and folder ID (if in a folder).
    Use the ID with get_notebook, update_notebook, run_notebook, etc.
    """
    try:
        wid = _workspace_id()
        resp = requests.get(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/notebooks",
            headers=_get_fabric_headers(),
        )
        resp.raise_for_status()
        notebooks = resp.json().get("value", [])
        return json.dumps(
            [{"id": n["id"], "name": n["displayName"], "folder_id": n.get("folderId")} for n in notebooks],
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_notebook(notebook_id: str) -> str:
    """
    Download the full content of a Fabric notebook as a .ipynb JSON string.

    Always call this before editing — pull the latest version first, then
    edit the returned content and pass it to update_notebook.

    Args:
        notebook_id: the notebook GUID from list_notebooks
    """
    try:
        wid = _workspace_id()
        resp = requests.post(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/items/{notebook_id}/GetDefinition?format=ipynb",
            headers=_get_fabric_headers(),
        )
        resp.raise_for_status()
        parts = resp.json()["definition"]["parts"]
        for part in parts:
            if part["path"].endswith(".ipynb"):
                content = base64.b64decode(part["payload"]).decode("utf-8")
                return content  # raw .ipynb JSON string
        return json.dumps({"error": "No .ipynb part found in response"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def update_notebook(notebook_id: str, notebook_content: str) -> str:
    """
    Deploy updated notebook content back to Fabric.

    Workflow:
      1. Call get_notebook to pull the current content
      2. Edit the .ipynb JSON string as needed
      3. Call this tool to push it back

    Args:
        notebook_id:      the notebook GUID from list_notebooks
        notebook_content: valid .ipynb JSON string (from get_notebook, then edited)
    """
    try:
        wid = _workspace_id()
        payload_b64 = base64.b64encode(notebook_content.encode("utf-8")).decode("utf-8")
        body = {
            "definition": {
                "format": "ipynb",
                "parts": [{
                    "path": "notebook-content.ipynb",
                    "payload": payload_b64,
                    "payloadType": "InlineBase64",
                }]
            }
        }
        resp = requests.post(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/items/{notebook_id}/UpdateDefinition",
            headers=_get_fabric_headers(),
            json=body,
        )
        resp.raise_for_status()
        return json.dumps({"status": "success", "notebook_id": notebook_id})
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
def create_notebook(display_name: str, notebook_content: str) -> str:
    """
    Create a brand new notebook in the Fabric workspace.

    Args:
        display_name:     name to give the new notebook in Fabric
        notebook_content: valid .ipynb JSON string for the notebook content
    Returns the new notebook's ID and display name.
    """
    try:
        wid = _workspace_id()
        payload_b64 = base64.b64encode(notebook_content.encode("utf-8")).decode("utf-8")
        body = {
            "displayName": display_name,
            "type": "Notebook",
            "definition": {
                "format": "ipynb",
                "parts": [{
                    "path": "notebook-content.ipynb",
                    "payload": payload_b64,
                    "payloadType": "InlineBase64",
                }]
            }
        }
        resp = requests.post(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/items",
            headers=_get_fabric_headers(),
            json=body,
        )
        resp.raise_for_status()
        result = resp.json()
        return json.dumps({
            "status": "success",
            "notebook_id": result.get("id"),
            "display_name": result.get("displayName"),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_run_status(notebook_id: str, job_instance_id: str) -> str:
    """
    Check the status of a notebook run.

    Poll this after run_notebook until status is no longer 'Running'.
    Returns status, exitValue, start/end times, and failureReason if failed.

    Args:
        notebook_id:     the notebook GUID
        job_instance_id: the ID returned by run_notebook
    """
    try:
        wid = _workspace_id()
        resp = requests.get(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/items/{notebook_id}/jobs/instances/{job_instance_id}",
            headers=_get_fabric_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return json.dumps({
            "status":        data.get("status"),
            "exitValue":     data.get("exitValue"),
            "startTime":     data.get("startTimeUtc"),
            "endTime":       data.get("endTimeUtc"),
            "failureReason": data.get("failureReason"),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def cancel_run(notebook_id: str, job_instance_id: str) -> str:
    """
    Cancel a notebook run that is currently in progress.

    Args:
        notebook_id:     the notebook GUID
        job_instance_id: the ID returned by run_notebook
    """
    try:
        wid = _workspace_id()
        resp = requests.post(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/items/{notebook_id}/jobs/instances/{job_instance_id}/cancel",
            headers=_get_fabric_headers(),
        )
        resp.raise_for_status()
        return json.dumps({"status": "cancel_requested"})
    except Exception as e:
        return json.dumps({"error": str(e)})
    

@mcp.tool()
def list_folders() -> str:
    """
    List all folders in the Fabric workspace.
    Returns each folder's ID, display name, and parent folder ID (if nested).
    Use the folder ID with move_notebook_to_folder.
    """
    try:
        wid = _workspace_id()
        resp = requests.get(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/folders",
            headers=_get_fabric_headers(),
        )
        resp.raise_for_status()
        folders = resp.json().get("value", [])
        return json.dumps(
            [
                {
                    "id": f["id"],
                    "name": f["displayName"],
                    "parent_folder_id": f.get("parentFolderId"),
                }
                for f in folders
            ],
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    
@mcp.tool()
def move_notebook_to_folder(notebook_id: str, folder_id: str) -> str:
    """
    Move a notebook into a workspace folder.

    Workflow:
      1. Call list_folders to find the target folder ID
      2. Call this tool with the notebook ID and folder ID

    Args:
        notebook_id: the notebook GUID from list_notebooks
        folder_id:   the folder GUID from list_folders
    """
    try:
        wid = _workspace_id()
        resp = requests.post(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/items/{notebook_id}/move",
            headers=_get_fabric_headers(),
            json={"targetFolderId": folder_id},
        )
        resp.raise_for_status()
        moved = resp.json().get("value", [])
        return json.dumps({
            "status": "success",
            "moved_items": [{"id": i["id"], "name": i["displayName"], "folder_id": i.get("folderId")} for i in moved]
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
    
@mcp.tool()
def run_notebook(notebook_id: str, parameters: dict = None, configuration: dict = None) -> str:
    """
    Trigger an on-demand run of a notebook in Fabric.

    Returns a job_instance_id — pass it to get_run_status to poll for completion.

    Workflow:
      1. Call list_spark_pools to see available compute options
      2. Pass the chosen pool's configuration_hint as the configuration argument

    Args:
        notebook_id:    the notebook GUID from list_notebooks
        parameters:     optional dict of parameters to pass to the notebook
                        format: {"param_name": {"value": "x", "type": "string"}}
        configuration:  Spark session config from list_spark_pools configuration_hint.
                        Examples:
                          {"useStarterPool": True}
                          {"useWorkspacePool": "PoolName"}
                          {"useWorkspacePool": "PoolName", "conf": {"spark.executor.memory": "4g"}}
                          {"useWorkspacePool": "PoolName", "defaultLakehouse": {"name": "...", "id": "...", "workspaceId": "..."}}
                          {"useWorkspacePool": "PoolName", "environment": {"id": "...", "name": "..."}}
    """
    try:
        wid = _workspace_id()
        body = {"executionData": {}}
        if parameters:
            body["executionData"]["parameters"] = parameters
        if configuration:
            body["executionData"]["configuration"] = configuration

        resp = requests.post(
            f"https://api.fabric.microsoft.com/v1/workspaces/{wid}/items/{notebook_id}/jobs/instances?jobType=RunNotebook",
            headers=_get_fabric_headers(),
            json=body,
        )
        resp.raise_for_status()

        location = resp.headers.get("Location", "")
        job_instance_id = location.split("/")[-1] if location else None
        return json.dumps({
            "status": "submitted",
            "job_instance_id": job_instance_id,
            "poll_url": location,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")