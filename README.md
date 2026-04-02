# mdl-train-mcp

An MCP server for monitoring and managing training jobs on [Modal](https://modal.com). Built for LLMs that need to check on long-running GPU training without drowning in log output.

## Why?

Training logs on Modal can be tens of thousands of lines — weight loading bars, Omniverse init spam, 8 ranks of identical output. Dumping all of that into an LLM context is wasteful and often hits resource limits.

This server gives you **browsable logs**: start with a summary, then drill into what matters.

| Tool | What it does |
|---|---|
| `list_apps` | List running, deployed, and recent apps with filtering |
| `get_logs` | Browse logs with summary/window/grep modes |
| `stop_app` | Stop a running app |

## The `get_logs` workflow

Instead of returning a giant blob, `get_logs` has three modes:

**1. Summary (default)** — returns line count, first/last 10 lines, and any errors with line numbers. Small response, always works.

```
get_logs(app_id="ap-xxx")
→ {total_lines: 30000, errors: [{line: 847, text: "CUDA error: ..."}], head: [...], tail: [...]}
```

**2. Window** — read a specific range. Like scrolling through a file.

```
get_logs(app_id="ap-xxx", window_start=840, window_size=30)
→ 30 lines around the error
```

**3. Grep** — search with regex and context lines. Like `grep -C`.

```
get_logs(app_id="ap-xxx", grep="Error|Traceback", grep_context=15)
→ all errors with 15 lines of surrounding context
```

**Landmarks** — pass `landmark_patterns` in summary mode to get a table of contents:

```
get_logs(app_id="ap-xxx", landmark_patterns=["Iteration \\d+", "success_rate", "checkpoint"])
→ landmarks: [{line: 200, text: "Iteration 1/3000"}, {line: 5000, text: "success_rate: 0.95"}, ...]
```

Landmark sampling is fair across patterns — one pattern won't dominate.

## Features

- **Progress bar collapsing** — tqdm bars, HF weight loading, and downloads are collapsed to their latest update (50 progress lines → 1 showing current state)
- **Auto-retry on resource limits** — if Modal's API rejects a large `tail`, automatically retries with smaller values and tells you what happened
- **Error deduplication** — 10,000 identical `[Error]` lines become a handful of unique entries
- **Case-sensitive error detection** — won't false-positive on metric names like `rot_align_error`

## Setup

### 1. Install

```bash
# Using uv (recommended)
uv pip install mdl-train-mcp

# Or from source
git clone https://github.com/JoshuaSP/mdl-train-mcp
cd mdl-train-mcp
uv venv && uv pip install -e .
```

### 2. Configure Modal

Make sure you have the [Modal CLI](https://modal.com/docs/guide/cli) installed and authenticated:

```bash
pip install modal
modal setup
```

### 3. Add to Claude Code

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "mdl": {
      "command": "mdl-train-mcp",
      "env": {
        "MODAL_PROFILE": "your-profile"
      }
    }
  }
}
```

Or from source:

```json
{
  "mcpServers": {
    "mdl": {
      "command": "uv",
      "args": ["--directory", "/path/to/mdl-train-mcp", "run", "mdl-train-mcp"],
      "env": {
        "MODAL_BIN": "/path/to/modal",
        "MODAL_PROFILE": "your-profile"
      }
    }
  }
}
```

### Environment variables

| Variable | Description | Default |
|---|---|---|
| `MODAL_BIN` | Path to modal CLI binary | `modal` |
| `MODAL_PROFILE` | Modal profile to use | (default profile) |

## Tools reference

### list_apps

```
list_apps(state?: string, name_contains?: string)
```

Filter by state (`"running"`, `"deployed"`, `"stopped"`, `"ephemeral"`) or name substring.

### get_logs

```
get_logs(
  app_id: string,
  tail?: number,              # log entries to fetch (default 500, max 5000)
  since?: string,             # "1h", "30m", "2d", or ISO datetime
  until?: string,
  source?: string,            # "stdout", "stderr", "system"
  window_start?: number,      # line number for window mode
  window_size?: number,       # lines to return (default 50, max 200)
  grep?: string,              # regex search (case-insensitive)
  grep_context?: number,      # context lines around matches (max 30)
  landmark_patterns?: string[] # regex patterns for summary landmarks
)
```

### stop_app

```
stop_app(app_id: string)
```

Irreversible — terminates the app and all its containers.

## License

MIT
