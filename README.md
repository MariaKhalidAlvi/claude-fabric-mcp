# claude-fabric-mcp

MCP server connecting Claude to a Microsoft Fabric lakehouse via Azure CLI auth.

Supports both **Claude Desktop** and **Claude Code** (CLI).

## Prerequisites

- Python 3.9+
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) (`az`)
- [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)

## Setup

1. Clone the repo
   ```bash
   git clone https://github.com/MariaKhalidAlvi/claude-fabric-mcp.git
   cd claude-fabric-mcp
   ```

2. Create and activate a virtual environment
   ```bash
   python3 -m venv venv
   source venv/bin/activate        # Mac/Linux
   venv\Scripts\activate           # Windows
   ```

3. Install dependencies
   ```bash
   pip install "mcp[cli]" azure-identity pyodbc python-dotenv requests
   ```

4. Configure environment variables
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and fill in your values:
   ```
   FABRIC_SERVER=your-server.datawarehouse.fabric.microsoft.com
   FABRIC_DATABASE=lh_silver
   FABRIC_WORKSPACE_ID=your-workspace-guid
   ```
   - **FABRIC_SERVER** — found in the Fabric portal under your lakehouse SQL endpoint
   - **FABRIC_DATABASE** — your lakehouse name (default: `lh_silver`)
   - **FABRIC_WORKSPACE_ID** — the GUID in your Fabric portal URL: `app.fabric.microsoft.com/groups/<GUID>/...`

5. Log in with Azure CLI
   ```bash
   az login
   ```
   Use your work account that has access to the Fabric workspace.

---

## Claude Desktop setup

### 1. Find the config file

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

### 2. Add the MCP server

Open the config file and add the `fabric-lakehouse` entry under `mcpServers`. Replace the paths with your actual absolute paths:

**Mac example:**
```json
{
  "mcpServers": {
    "fabric-lakehouse": {
      "command": "/Users/yourname/claude-fabric-mcp/venv/bin/python",
      "args": ["/Users/yourname/claude-fabric-mcp/server.py"],
      "cwd": "/Users/yourname/claude-fabric-mcp"
    }
  }
}
```

**Windows example:**
```json
{
  "mcpServers": {
    "fabric-lakehouse": {
      "command": "C:\\Users\\yourname\\claude-fabric-mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\yourname\\claude-fabric-mcp\\server.py"],
      "cwd": "C:\\Users\\yourname\\claude-fabric-mcp"
    }
  }
}
```

### 3. Restart Claude Desktop

Quit and reopen Claude Desktop. You should see the `fabric-lakehouse` tools available (hammer icon).

---

## Claude Code (CLI) setup

Add the server to your project or global MCP config:

```bash
claude mcp add fabric-lakehouse \
  /absolute/path/to/venv/bin/python \
  /absolute/path/to/claude-fabric-mcp/server.py
```

Or add it manually to `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "fabric-lakehouse": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["/absolute/path/to/claude-fabric-mcp/server.py"]
    }
  }
}
```

---

## Available tools

### SQL tools (read-only)
| Tool | Description |
|------|-------------|
| `list_tables` | List all tables and views in the lakehouse |
| `describe_table` | Get columns, types, and nullability for a table |
| `run_query` | Run a SELECT query (capped at 500 rows) |
| `get_sample_rows` | Fetch a few sample rows from a table |

### Notebook tools (Fabric REST API)
| Tool | Description |
|------|-------------|
| `list_notebooks` | List all notebooks in the workspace |
| `get_notebook` | Download notebook content as `.ipynb` |
| `update_notebook` | Push updated notebook content back to Fabric |
| `create_notebook` | Create a new notebook in the workspace |
| `run_notebook` | Trigger an on-demand notebook run |
| `get_run_status` | Poll the status of a notebook run |
| `cancel_run` | Cancel a running notebook job |
| `list_folders` | List workspace folders |
| `move_notebook_to_folder` | Move a notebook into a folder |

---

## Blocklist

Edit `blocklist.json` to prevent Claude from querying sensitive tables:

```json
{
  "blocked_tables": ["dbo.etl_audit_log", "sensitive_table"]
}
```

Entries can be `"schema.table"` or just `"table"`. This is a convenience guard — enforce real access control via SQL endpoint permissions in the Fabric portal.
