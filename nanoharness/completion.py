"""Tab completion helpers shared between REPL and TUI."""

from __future__ import annotations

from pathlib import Path

COMMANDS = ["/think", "/workspace", "/code", "/lazygit", "/clear", "/config", "/info", "/todo", "/help", "/quit", "/exit"]

THINK_OPTIONS = ["on", "off", "once"]
SAFETY_OPTIONS = ["workspace", "unrestricted", "confirm"]
CONFIG_KEYS = [
    "model.name",
    "model.thinking",
    "model.num_ctx",
    "agent.max_steps",
    "agent.timeout_seconds",
    "agent.max_output_chars",
    "safety.level",
    "ollama.base_url",
]


def is_incomplete_command(line: str) -> bool:
    """Return True if input is a partial command prefix that shouldn't be sent.

    Examples:
        "/thi"       -> True  (partial, matches /think but isn't complete)
        "/think"     -> False (valid command, no required args)
        "/think on"  -> False (valid)
        "/c"         -> True  (ambiguous partial)
        "/clear"     -> False (valid)
        "hello"      -> False (not a command at all)
        "/foo"       -> False (unknown command, let the handler report the error)
        "/"          -> True  (bare slash)
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return False
    cmd_part = stripped.split()[0].lower()
    # Exact match to a known command — not incomplete
    if cmd_part in COMMANDS:
        return False
    # Partial prefix that matches at least one command — incomplete
    if any(c.startswith(cmd_part) for c in COMMANDS):
        return True
    # No match at all (e.g. "/foo") — let it through so handler can show "unknown command"
    return False

# Maps each command to (arg_hint, description)
COMMAND_HINTS: dict[str, tuple[str, str]] = {
    "/think":     ("on|off|once",               "Toggle thinking mode"),
    "/workspace": ("<dir>",                     "Switch workspace directory"),
    "/code":      ("",                          "Open workspace in VS Code"),
    "/lazygit":   ("",                          "Open lazygit in a new terminal window"),
    "/clear":     ("",                          "Clear conversation history"),
    "/config":    ("[set KEY VAL]",             "Show/edit configuration"),
    "/info":      ("",                          "Show model details from Ollama"),
    "/todo":      ("[list|clear|add|done|remove]", "Manage task list"),
    "/help":      ("",                          "Show available commands"),
    "/quit":      ("",                          "Exit NanoHarness"),
    "/exit":      ("",                          "Exit NanoHarness"),
}


def hint_for_input(line: str) -> str:
    """Return inline hint text for the current input.

    Examples:
        "/thi"          -> "/think on|off|once  Toggle thinking mode"
        "/think "       -> "/think on|off|once  Toggle thinking mode"
        "/think o"      -> "/think on|off|once"
        "/w"            -> "/workspace <dir>  Switch workspace directory"
        "/workspace "   -> "/workspace <dir>  Switch workspace directory"
        "hello"         -> ""
    """
    stripped = line.lstrip()
    if not stripped.startswith("/"):
        # Check for a trailing /command token (command embedded in longer text)
        parts = stripped.rsplit(None, 1)
        last_token = parts[-1] if parts else ""
        if last_token.startswith("/") and len(parts) > 1:
            # Delegate hint to the inline command token
            return hint_for_input(last_token)
        return ""

    parts = stripped.split(maxsplit=1)
    cmd_part = parts[0].lower()
    has_space = len(parts) > 1 or (stripped.endswith(" ") and len(parts) == 1)
    arg_part = parts[1].lower() if len(parts) > 1 else ""

    # Typing a command prefix (no space yet): find matching commands
    if not has_space:
        matches = [c for c in COMMANDS if c.startswith(cmd_part)]
        if not matches:
            return ""
        if len(matches) == 1:
            c = matches[0]
            arg_hint, desc = COMMAND_HINTS[c]
            # Show the full command + args as ghost text
            ghost = c
            if arg_hint:
                ghost += " " + arg_hint
            suffix = f"  {desc}" if desc else ""
            return ghost + suffix
        # Multiple matches: show them all
        return "  ".join(matches)

    # Already typed a full command + space: show arg hints
    if cmd_part in COMMAND_HINTS:
        arg_hint, desc = COMMAND_HINTS[cmd_part]
        if not arg_hint:
            return ""
        # /think with a partial arg
        if cmd_part == "/think" and arg_part:
            opts = [o for o in THINK_OPTIONS if o.startswith(arg_part)]
            if opts:
                return f"/think {' | '.join(opts)}"
            return ""
        # /workspace with partial path — don't show hint, real dirs are better
        if cmd_part == "/workspace" and arg_part:
            return ""
        # /config set <key> — show key list or value hint
        if cmd_part == "/config":
            sub_parts = arg_part.split(maxsplit=1)
            if not sub_parts or not "set".startswith(sub_parts[0]):
                return "/config set <key> <value>  Edit configuration"
            if len(sub_parts) == 1 and sub_parts[0] == "set":
                return f"/config set {' | '.join(CONFIG_KEYS)}"
            if len(sub_parts) == 2:
                key = sub_parts[1].strip()
                matching = [k for k in CONFIG_KEYS if k.startswith(key)]
                if len(matching) == 1:
                    k = matching[0]
                    if k == "model.thinking":
                        return f"/config set {k} on|off"
                    if k == "safety.level":
                        return f"/config set {k} {' | '.join(SAFETY_OPTIONS)}"
                    return f"/config set {k} <value>"
                if matching:
                    return "  ".join(matching)
            return ""
        # Just show the usage pattern
        return f"{cmd_part} {arg_hint}  {desc}" if desc else f"{cmd_part} {arg_hint}"

    return ""


def dir_matches(base: Path, partial: str) -> list[str]:
    """Return directory names matching a partial path relative to base. Dirs only."""
    try:
        if not partial:
            return sorted(
                f"{e.name}/"
                for e in base.iterdir()
                if e.is_dir() and not e.name.startswith(".")
            )

        partial_path = base / partial
        parent = partial_path.parent
        prefix = partial_path.name

        if not parent.is_dir():
            return []

        matches = []
        for entry in parent.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name.startswith(prefix):
                rel = str(entry.relative_to(base))
                rel += "/"
                matches.append(rel)

        return sorted(matches)
    except OSError:
        return []


def path_matches(workspace: Path, partial: str) -> list[str]:
    """Return file/folder names matching a partial path relative to workspace."""
    try:
        if not partial:
            return sorted(
                f"{e.name}/" if e.is_dir() else e.name
                for e in workspace.iterdir()
                if not e.name.startswith(".")
            )

        partial_path = workspace / partial
        parent = partial_path.parent
        prefix = partial_path.name

        if not parent.is_dir():
            return []

        matches = []
        for entry in parent.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.name.startswith(prefix):
                rel = str(entry.relative_to(workspace))
                if entry.is_dir():
                    rel += "/"
                matches.append(rel)

        return sorted(matches)
    except OSError:
        return []


def complete_line(workspace: Path, line: str) -> list[str]:
    """Return completions for the full input line (context-aware).

    Handles /workspace <dir>, /think <option>, and delegates to complete_token for rest.
    """
    stripped = line.lstrip()

    # /workspace <partial_dir> — complete directories only
    if stripped.lower().startswith("/workspace "):
        partial = stripped[len("/workspace "):].lstrip()
        return [f"/workspace {m}" for m in dir_matches(workspace, partial)]

    # /think <partial_option> — complete on/off/once
    if stripped.lower().startswith("/think "):
        partial = stripped[len("/think "):].lstrip().lower()
        return [f"/think {o}" for o in THINK_OPTIONS if o.startswith(partial)]

    # /config set <key> [value] — complete keys and enum values
    if stripped.lower().startswith("/config "):
        rest = stripped[len("/config "):].lstrip()
        rest_parts = rest.split(maxsplit=2)
        # Only handle "set" sub-command
        if not rest_parts or not "set".startswith(rest_parts[0].lower()):
            return ["/config set"]
        if len(rest_parts) == 1:
            # Tab after "set" — suggest all keys
            return [f"/config set {k}" for k in CONFIG_KEYS]
        key_partial = rest_parts[1].lower() if len(rest_parts) >= 2 else ""
        matching_keys = [k for k in CONFIG_KEYS if k.startswith(key_partial)]
        if len(rest_parts) == 2:
            # Still completing the key
            return [f"/config set {k}" for k in matching_keys]
        # Completing the value for a known key
        key = rest_parts[1].lower()
        val_partial = rest_parts[2].lower() if len(rest_parts) > 2 else ""
        if key == "model.thinking":
            opts = [o for o in ("on", "off") if o.startswith(val_partial)]
            return [f"/config set {key} {o}" for o in opts]
        if key == "safety.level":
            opts = [o for o in SAFETY_OPTIONS if o.startswith(val_partial)]
            return [f"/config set {key} {o}" for o in opts]
        return []

    # Fall back to token-based completion on last word
    parts = stripped.rsplit(None, 1)
    last_token = parts[-1] if parts else ""
    return complete_token(workspace, last_token)


def complete_token(workspace: Path, text: str) -> list[str]:
    """Return completions for a token (the last word being typed).

    Handles /commands, !shell paths, and bare file paths.
    """
    if text.startswith("/"):
        return [c for c in COMMANDS if c.startswith(text)]

    if text.startswith("!"):
        partial = text[1:]
        return ["!" + m for m in path_matches(workspace, partial)]

    return path_matches(workspace, text)
