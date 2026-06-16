# claude-fabric-mcp

MCP server connecting Claude Desktop to a Microsoft Fabric lakehouse via Azure CLI auth.

## Setup

1. Clone the repo
2. `python3 -m venv venv && source venv/bin/activate`
3. `pip install "mcp[cli]" azure-identity pyodbc python-dotenv`
4. Copy `.env.example` → `.env` and fill in your Fabric server and database
5. `az login` with your work account
6. Configure Claude Desktop — see below

## Claude Desktop config

```json
{
  "mcpServers": {
    "fabric-lakehouse": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "cwd": "/absolute/path/to/claude-fabric-mcp"
    }
  }
}
```

## Blocklist

Edit `blocklist.json` to prevent Claude from querying sensitive tables.
Format: `"schema.table"` entries under `"blocked_tables"`.