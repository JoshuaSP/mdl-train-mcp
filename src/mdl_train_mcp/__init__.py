"""
Minimal Modal MCP server — 3 tools, REST-style.

Tools:
  list_apps  — what's running/deployed/recent (GET /apps)
  get_logs   — browsable logs with summary, windows, and grep (GET /apps/:id/logs)
  stop_app   — stop a running app (DELETE /apps/:id)
"""

import asyncio
import json
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "mdl — Modal App Monitor",
    instructions="""\
This is the Modal (mdl) MCP server for monitoring apps running on Modal.

Use these tools when the user asks about:
- Modal apps or jobs
- Checking what's running on Modal, reading Modal logs, or stopping Modal apps

Workflow: list_apps → get_logs (summary first, then window/grep to drill in) → stop_app if needed.
""",
)

MODAL_BIN = os.environ.get("MODAL_BIN", "modal")
MODAL_PROFILE = os.environ.get("MODAL_PROFILE", "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _modal_env() -> dict[str, str]:
    env = {**os.environ}
    if MODAL_PROFILE:
        env["MODAL_PROFILE"] = MODAL_PROFILE
    return env


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07", "", s)


def _is_progress_bar(line: str) -> bool:
    """Detect progress bar lines."""
    if "%" in line and ("|" in line or "█" in line or "░" in line):
        return True
    if re.match(r"\s*\d+%\|", line):
        return True
    if re.search(r"Loading.*shards?.*\d+/\d+", line):
        return True
    if re.search(r"Downloading.*\d+\.\d+[kMG]", line):
        return True
    return False


def _progress_bar_key(line: str) -> str | None:
    """Extract a stable key for a progress bar so we can collapse updates.
    Returns None if we can't determine a key (treat as unique)."""
    # tqdm: "  5%|██ | 1/20 [..." → key is everything after the last |
    # but that changes. Better: strip the dynamic parts.
    # For "Loading checkpoint shards: 2/4" → "Loading checkpoint shards"
    m = re.match(r"(Loading.*shards?)", line)
    if m:
        return m.group(1)
    # For "Downloading model-00001.safetensors" → "Downloading model-00001.safetensors"
    m = re.match(r"(Downloading\s+\S+)", line)
    if m:
        return m.group(1)
    # For tqdm bars, key on description prefix + total count
    # "  5%|██| 1/20 [00:03<00:57]" → key is "tqdm:20" (the total)
    # "desc:  5%|██| 1/20" → key is "desc:20"
    m = re.match(r"(.*?)\s*\d+%\|", line)
    prefix = m.group(1).strip() if m else ""
    # Extract the "/N" total from "X/N"
    total_m = re.search(r"\d+/(\d+)", line)
    total = total_m.group(1) if total_m else ""
    if total:
        return f"{prefix}:{total}" if prefix else f"tqdm:{total}"
    if prefix:
        return prefix
    # Generic: "X/Y" pattern like "3/4" — key on text before it
    m = re.match(r"(.*?)\d+/\d+", line)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # Last resort for bare progress bars — collapse all unknown bars together
    return "_progress"


async def _run_modal(*args: str, timeout: int = 30) -> tuple[str, str, int]:
    """Run a modal CLI command and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        MODAL_BIN, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_modal_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "", "Command timed out", 1
    return stdout.decode(), stderr.decode(), proc.returncode


class _FetchResult:
    def __init__(self, lines: list[str], requested_tail: int, actual_tail: int, retried: bool):
        self.lines = lines
        self.requested_tail = requested_tail
        self.actual_tail = actual_tail
        self.retried = retried


async def _fetch_log_lines(app_id: str, tail: int = 500, since: str | None = None,
                           until: str | None = None, source: str | None = None,
                           timestamps: bool = False) -> _FetchResult | str:
    """Fetch and clean log lines from Modal. Returns _FetchResult or error string.
    Auto-retries with smaller tail if Modal's API hits resource limits."""

    async def _try_fetch(t: int) -> tuple[str, str, int]:
        cmd = [MODAL_BIN, "app", "logs", app_id, "--tail", str(t)]
        if since:
            cmd.extend(["--since", since])
        if until:
            cmd.extend(["--until", until])
        if source:
            cmd.extend(["--source", source])
        if timestamps:
            cmd.append("--timestamps")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_modal_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "", "timeout", 1
        return stdout.decode(), stderr.decode(), proc.returncode

    # Try requested tail, auto-retry with smaller values on resource limit
    actual_tail = tail
    retried = False
    for attempt_tail in [tail, tail // 2, tail // 5, 50]:
        if attempt_tail < 10:
            break
        stdout, stderr, rc = await _try_fetch(attempt_tail)
        if rc == 0:
            actual_tail = attempt_tail
            break
        err = _strip_ansi(stderr).strip()
        if "resource limit" in err.lower():
            retried = True
            continue  # retry with smaller tail
        return f"modal app logs failed: {err}"
    else:
        return "Modal logs API resource limit — even tail=50 failed. Try using since='30m' to narrow the time range."

    if rc != 0:
        err = _strip_ansi(stderr).strip()
        return f"modal app logs failed: {err}"

    raw_lines = stdout.split("\n")

    # First pass: clean lines, collapse progress bars (keep last update per bar)
    lines = []
    progress_bars: dict[str, int] = {}  # key → index in lines
    for line in raw_lines:
        cleaned = _strip_ansi(line).rstrip()
        if not cleaned:
            continue
        if cleaned.startswith(("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")):
            continue
        if _is_progress_bar(cleaned):
            key = _progress_bar_key(cleaned)
            if key and key in progress_bars:
                # Replace the previous update for this bar
                lines[progress_bars[key]] = cleaned
                continue
            elif key:
                progress_bars[key] = len(lines)
        lines.append(cleaned)

    # Second pass: deduplicate consecutive identical lines
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    return _FetchResult(deduped, requested_tail=tail, actual_tail=actual_tail, retried=retried)


# Universal error/warning pattern — things any log consumer would want to know about
_ERROR_PATTERN = re.compile(
    r"\bError\b|\bERROR\b|Exception|Traceback|FATAL|PANIC|\bkilled\b|"
    r"\bOOM\b|segfault|CUDA error|RuntimeError|KeyError|ValueError|"
    r"TypeError|AssertionError|out of memory|signal \d+|exit code [1-9]|"
    r"\[Error\]|\[error\]",
)


def _build_summary(lines: list[str], head_n: int = 10, tail_n: int = 10,
                    landmark_patterns: list[str] | None = None) -> dict[str, Any]:
    """Build a navigable summary of log lines."""
    # Find error/warning lines — deduplicate similar messages
    errors = []
    seen_error_prefixes: set[str] = set()
    for i, line in enumerate(lines):
        if _ERROR_PATTERN.search(line):
            # Deduplicate by first 80 chars (catches repeated identical errors)
            prefix = line[:80]
            if prefix not in seen_error_prefixes:
                seen_error_prefixes.add(prefix)
                errors.append({"line": i, "text": line[:200]})

    # Find caller-specified landmarks — per-pattern to ensure diversity
    MAX_LANDMARKS = 30
    landmarks = []
    if landmark_patterns:
        per_pattern: dict[str, list[dict]] = {}
        for pat in landmark_patterns:
            compiled = re.compile(pat, re.IGNORECASE)
            matches = []
            for i, line in enumerate(lines):
                if compiled.search(line):
                    matches.append({"line": i, "text": line[:200], "pattern": pat})
            per_pattern[pat] = matches

        # Allocate slots evenly across patterns, then fill
        total_matches = sum(len(m) for m in per_pattern.values())
        if total_matches <= MAX_LANDMARKS:
            for matches in per_pattern.values():
                landmarks.extend(matches)
        else:
            # Fair share per pattern: at least 2 (first + last), rest distributed
            slots_per = max(2, MAX_LANDMARKS // len(landmark_patterns))
            for pat, matches in per_pattern.items():
                if len(matches) <= slots_per:
                    landmarks.extend(matches)
                else:
                    # Keep first, last, and evenly spaced middle
                    stride = (len(matches) - 1) / (slots_per - 1)
                    landmarks.extend(matches[int(i * stride)] for i in range(slots_per))

        landmarks.sort(key=lambda x: x["line"])

    result: dict[str, Any] = {
        "total_lines": len(lines),
        "head": [{"line": i, "text": lines[i][:200]} for i in range(min(head_n, len(lines)))],
        "tail": [{"line": i, "text": lines[i][:200]} for i in range(max(0, len(lines) - tail_n), len(lines))],
        "errors": errors[:50],
        "error_count": len(errors),
    }
    if landmarks:
        result["landmarks"] = landmarks
        result["landmark_total"] = sum(len(m) for m in per_pattern.values())

    return result


# ---------------------------------------------------------------------------
# Tool 1: list_apps
# ---------------------------------------------------------------------------

LIST_APPS_DESC = """\
List Modal apps — running, deployed, and recently stopped.

Filtering (all optional):
  - state: filter by state — "running", "deployed", "stopped". \
"running" matches ANY live app (including "ephemeral (detached)" from --detach jobs).
  - live: if true, return only apps with at least one running task. \
This is the most reliable "is anything burning GPU right now?" filter.
  - name_contains: substring match on app description/name (case-insensitive)

Returns: app_id, description, state, running task count, created_at, stopped_at.
"""


@mcp.tool(description=LIST_APPS_DESC)
async def list_apps(
    state: str | None = None,
    live: bool = False,
    name_contains: str | None = None,
) -> str:
    stdout, stderr, rc = await _run_modal("app", "list", "--json")
    if rc != 0:
        return json.dumps({"error": f"modal app list failed: {stderr}"})

    apps = json.loads(stdout)

    normalized = []
    for a in apps:
        normalized.append({
            "app_id": a["App ID"],
            "description": a["Description"],
            "state": a["State"],
            "tasks": int(a.get("Tasks", "0") or "0"),
            "created_at": a["Created at"],
            "stopped_at": a.get("Stopped at"),
        })

    if live:
        normalized = [a for a in normalized if a["tasks"] > 0]
    if state:
        state_lower = state.lower()
        # "running" should match ephemeral/detached apps — they're running
        if state_lower == "running":
            normalized = [a for a in normalized if
                          a["state"].lower() in ("running",) or
                          "ephemeral" in a["state"].lower() or
                          a["tasks"] > 0]
        else:
            normalized = [a for a in normalized if state_lower in a["state"].lower()]
    if name_contains:
        name_lower = name_contains.lower()
        normalized = [a for a in normalized if name_lower in a["description"].lower()]

    return json.dumps({"count": len(normalized), "apps": normalized}, default=str)


# ---------------------------------------------------------------------------
# Tool 2: get_logs
# ---------------------------------------------------------------------------

GET_LOGS_DESC = """\
Browse logs for a Modal app. Designed for iterative exploration — start with a \
summary, then zoom into specific regions.

**Modes (pick one):**

1. **Summary (default)**: Returns total line count, first/last 10 lines, and any \
error/exception lines with their line numbers. Use this FIRST to understand the \
log structure, then zoom in with window or grep.

2. **Window**: Set window_start and window_size to read a specific range of lines. \
Like scrolling through a file. Use after summary tells you where to look.

3. **Grep**: Set grep to a regex pattern to find matching lines. Set grep_context \
to include surrounding lines (like grep -C). Use for "find all errors", \
"search for X", etc.

**Workflow:**
  1. get_logs(app_id="ap-xxx") → summary: 4200 lines, errors at lines 847, 1203
  2. get_logs(app_id="ap-xxx", window_start=840, window_size=30) → see error context
  3. get_logs(app_id="ap-xxx", grep="reward|success", grep_context=2) → find metrics

**Args:**
  - app_id: Modal app ID or name
  - tail: how many recent log entries to fetch from Modal (default 500, max 5000). \
This is the raw fetch size — summary/window/grep operate on these fetched lines. \
Increase if you need to search further back in time; use "since" for time-based ranges.
  - since/until: time range filter (e.g. "1h", "30m", ISO datetime)
  - source: "stdout", "stderr", or "system"
  - window_start: starting line number for window mode
  - window_size: number of lines to return in window mode (default 50, max 200)
  - grep: regex pattern for search mode (case-insensitive)
  - grep_context: lines of context around each grep match (default 0, max 30)
  - landmark_patterns: list of regex patterns to highlight in summary mode. \
These create a "table of contents" showing where matching lines appear. \
Examples: ["Iteration \\\\d+", "reward", "checkpoint", "saving"]
  - timestamps: if true, prefix each log line with its timestamp. Useful for \
correlating events across ranks or measuring time between log entries.
"""


@mcp.tool(description=GET_LOGS_DESC)
async def get_logs(
    app_id: str,
    tail: int = 500,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    window_start: int | None = None,
    window_size: int = 50,
    grep: str | None = None,
    grep_context: int = 0,
    landmark_patterns: list[str] | None = None,
    timestamps: bool = False,
) -> str:
    tail = min(tail, 5000)
    window_size = min(window_size, 200)
    grep_context = min(grep_context, 30)

    fetch = await _fetch_log_lines(app_id, tail=tail, since=since, until=until, source=source, timestamps=timestamps)
    if isinstance(fetch, str):
        return json.dumps({"error": fetch})
    lines = fetch.lines

    # Base metadata included in all responses
    meta: dict[str, Any] = {"app_id": app_id}
    if fetch.retried:
        meta["note"] = (
            f"Modal API resource limit hit at tail={fetch.requested_tail}, "
            f"succeeded with tail={fetch.actual_tail}. Use 'since' for time-based "
            f"filtering to see more logs."
        )

    # Mode: grep
    if grep:
        pattern = re.compile(grep, re.IGNORECASE)
        if grep_context > 0:
            match_indices = {i for i, line in enumerate(lines) if pattern.search(line)}
            include = set()
            for idx in match_indices:
                for j in range(max(0, idx - grep_context), min(len(lines), idx + grep_context + 1)):
                    include.add(j)
            result_lines = [{"line": i, "text": lines[i]} for i in sorted(include)]
        else:
            result_lines = [{"line": i, "text": lines[i]} for i in range(len(lines)) if pattern.search(lines[i])]

        return json.dumps({
            **meta,
            "mode": "grep",
            "grep": grep,
            "grep_context": grep_context,
            "total_lines": len(lines),
            "match_count": sum(1 for i, l in enumerate(lines) if pattern.search(l)),
            "results": result_lines[:500],
        })

    # Mode: window
    if window_start is not None:
        window_start = max(0, min(window_start, len(lines)))
        window_end = min(window_start + window_size, len(lines))
        window_lines = [{"line": i, "text": lines[i]} for i in range(window_start, window_end)]

        return json.dumps({
            **meta,
            "mode": "window",
            "total_lines": len(lines),
            "window_start": window_start,
            "window_end": window_end,
            "lines": window_lines,
        })

    # Mode: summary (default)
    summary = _build_summary(lines, landmark_patterns=landmark_patterns)
    return json.dumps({
        **meta,
        "mode": "summary",
        **summary,
    })


# ---------------------------------------------------------------------------
# Tool 3: stop_app
# ---------------------------------------------------------------------------

STOP_APP_DESC = """\
Stop one or more running Modal apps.

Args:
  - app_ids: list of Modal app IDs (e.g. ["ap-abc123", "ap-def456"]) or app names

Returns per-app status. This is irreversible — apps and their containers will be terminated.
"""


@mcp.tool(description=STOP_APP_DESC)
async def stop_app(
    app_ids: list[str],
) -> str:
    results = []
    for app_id in app_ids:
        stdout, stderr, rc = await _run_modal("app", "stop", app_id)
        if rc != 0:
            results.append({"app_id": app_id, "status": "error", "error": _strip_ansi(stderr).strip()})
        else:
            results.append({"app_id": app_id, "status": "stopped"})
    return json.dumps({"results": results})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
