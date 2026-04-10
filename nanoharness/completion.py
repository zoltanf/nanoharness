"""Tab completion helpers shared between REPL and TUI."""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG_KEYS, THEME_OPTIONS, TOOL_NAMES  # noqa: F401 — re-exported for callers

COMMANDS = ["/safety", "/workspace", "/think", "/clear", "/todo", "/info", "/code", "/lazygit", "/config", "/pull", "/update", "/help", "/quit", "/exit"]

THINK_OPTIONS = ["on", "off", "once"]
SAFETY_OPTIONS = ["workspace", "confirm", "none"]
_THINK_VALID = ("on", "off", "once", "true", "false", "yes", "no")
_UPDATE_SUBCMDS = ("ollama", "models")
_INFO_SUBCMDS = ("prompt", "context", "tools")


def is_incomplete_command(line: str) -> bool:
    """Return True if the input looks like a command but must not be sent yet.

    Blocked cases:
        "/thi"         partial prefix (ambiguous)
        "/c"           partial prefix (ambiguous)
        "/"            bare slash
        "/foo"         unknown command
        "/think xyz"   recognised command with invalid argument
        "/safety xyz"  recognised command with invalid argument
        "/update xyz"  recognised command with invalid argument

    Allowed (returns False):
        "hello"        plain text — not a command
        "/think"       valid command, no arg (toggles)
        "/think on"    valid command + valid arg
        "/update ollama"  valid subcommand
        "/workspace"   valid, no arg (shows current)
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return False

    parts = stripped.split(maxsplit=1)
    cmd_part = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    first_arg = arg.lower().split()[0] if arg else ""

    # Unknown or partial-prefix command — block it
    if cmd_part not in COMMANDS:
        return True

    # Known command: validate argument for those with a fixed single-word value set.
    # We check the FULL arg string (not just the first word) so that e.g.
    # "/think once blablabla" is blocked — the extra text is not expected.
    if arg:
        arg_lower = arg.lower()
        if cmd_part == "/think":
            return arg_lower not in _THINK_VALID
        if cmd_part == "/safety":
            return arg_lower not in SAFETY_OPTIONS
        if cmd_part == "/update":
            return arg_lower not in _UPDATE_SUBCMDS
        if cmd_part == "/info":
            return arg_lower not in _INFO_SUBCMDS

    return False


def command_send_error(line: str) -> str:
    """Return a short, human-readable reason why this command is blocked from sending.

    Returns an empty string when the input is fine to send (or is not a command).
    Intended for display in the hint line / REPL feedback.
    """
    stripped = line.strip()
    if not stripped.startswith("/") or not is_incomplete_command(stripped):
        return ""

    parts = stripped.split(maxsplit=1)
    cmd_part = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    arg_lower = arg.lower()
    first_arg = arg_lower.split()[0] if arg_lower else ""

    if cmd_part not in COMMANDS:
        matches = [c for c in COMMANDS if c.startswith(cmd_part)]
        if matches:
            return f"Incomplete — did you mean: {', '.join(matches[:4])}?"
        return f"Unknown command '{cmd_part}'. Type /help to see all commands."

    if arg:
        # Distinguish "wrong value" from "valid value + unexpected trailing text"
        def _arg_error(valid: tuple[str, ...], usage: str) -> str:
            if first_arg in valid:
                return f"Unexpected text after '{first_arg}' — usage: {cmd_part} {usage}"
            return f"{cmd_part}: expected {usage}, got '{first_arg}'"

        if cmd_part == "/think":
            return _arg_error(_THINK_VALID, "on | off | once")
        if cmd_part == "/safety":
            return _arg_error(tuple(SAFETY_OPTIONS), " | ".join(SAFETY_OPTIONS))
        if cmd_part == "/update":
            return _arg_error(_UPDATE_SUBCMDS, "ollama | models")
        if cmd_part == "/info":
            return _arg_error(_INFO_SUBCMDS, "prompt | context | tools")

    return "Invalid command syntax."

# Maps each command to (arg_hint, description)
COMMAND_HINTS: dict[str, tuple[str, str]] = {
    "/safety":    ("confirm|workspace|none",    "Set session safety level"),
    "/workspace": ("<dir>",                     "Switch workspace directory"),
    "/think":     ("on|off|once",               "Toggle thinking mode"),
    "/clear":     ("",                          "Clear conversation history"),
    "/todo":      ("[list|clear|add|done|remove]", "Manage task list"),
    "/info":      ("[prompt|context|tools]",      "Show model info, system prompt/context, or available tools"),
    "/code":      ("",                          "Open workspace in VS Code"),
    "/lazygit":   ("",                          "Open lazygit in a new terminal window"),
    "/config":    ("[tools | set KEY VAL]",       "Show/edit config or tool enables"),
    "/pull":      ("[model|all]",                "Pull a model; 'all' pulls every local model"),
    "/update":    ("ollama|models",             "Update Ollama binary or pull all local models"),
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
        # The line is a regular message that may have an embedded /command at the end.
        # Only /think makes sense mid-message, so we restrict hints accordingly.
        parts = stripped.rsplit(None, 1)
        last_token = parts[-1] if parts else ""

        # "text /think <partial_opt>" — last token is the partial option word
        if not last_token.startswith("/") and len(parts) > 1:
            prev = parts[0].rsplit(None, 1)
            if prev[-1].lower() == "/think":
                opts = [o for o in THINK_OPTIONS if o.startswith(last_token.lower())]
                return f"/think {' | '.join(opts)}" if opts else ""

        # "text /" or "text /th..." — last token is the partial command
        if last_token.startswith("/") and len(parts) > 1:
            cmd_lower = last_token.lower()
            if "/think".startswith(cmd_lower):
                return "/think on|off|once  Toggle thinking mode"
            return ""

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
        # /config set <key> or /config tools — show hints
        if cmd_part == "/config":
            sub_parts = arg_part.split()
            first_sub = sub_parts[0] if sub_parts else ""
            # /config tools ...
            if "tools".startswith(first_sub) and first_sub not in ("set", "theme"):
                if len(sub_parts) <= 1:
                    return "/config tools [<tool> [global] [workspace]]  Configure tool access"
                if len(sub_parts) == 2:
                    return "/config tools <tool> on|off|_  (global; _ = keep current)"
                if len(sub_parts) == 3:
                    return f"/config tools <tool> {sub_parts[2]} on|off|inherit|_  (workspace)"
                return ""
            if "theme".startswith(first_sub) and first_sub not in ("set", "tools"):
                if len(sub_parts) <= 1:
                    return "/config theme light|dark|auto  Set UI color theme"
                return ""
            # /config set ...
            if not sub_parts or not "set".startswith(first_sub):
                return "/config tools | theme | set <key> <value>  Show/edit config or tool enables"
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
        # /update with partial subcommand
        if cmd_part == "/update":
            opts = [o for o in _UPDATE_SUBCMDS if o.startswith(arg_part)]
            if opts:
                return f"/update {' | '.join(opts)}"
            return ""
        # /safety with partial arg
        if cmd_part == "/safety" and arg_part:
            opts = [o for o in SAFETY_OPTIONS if o.startswith(arg_part)]
            if opts:
                return f"/safety {' | '.join(opts)}"
            return ""
        # Just show the usage pattern
        return f"{cmd_part} {arg_hint}  {desc}" if desc else f"{cmd_part} {arg_hint}"

    return ""


def abs_dir_matches(partial: str) -> list[str]:
    """Complete directories anywhere on the filesystem (for /workspace command)."""
    import os
    try:
        if not partial:
            home = Path.home()
            return sorted(
                f"~/{e.name}"
                for e in home.iterdir()
                if e.is_dir() and not e.name.startswith(".")
            )
        expanded = os.path.expanduser(partial)
        p = Path(expanded)
        # When the partial already ends with "/" the user has typed a complete
        # directory name and wants to see what's *inside* it — use it as the
        # parent and match all entries (empty prefix).
        if partial.endswith("/") and p.is_dir():
            parent = p
            prefix = ""
        elif p.is_absolute():
            parent = p.parent
            prefix = p.name
        else:
            base = Path.cwd() / partial
            parent = base.parent
            prefix = base.name
        if not parent.is_dir():
            return []
        home_str = str(Path.home())
        matches = []
        prefix_lower = prefix.lower()
        for entry in parent.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name.lower().startswith(prefix_lower):
                full = str(entry)
                completion = "~" + full[len(home_str):] if full.startswith(home_str) else full
                matches.append(completion)
        return sorted(matches)
    except OSError:
        return []


def dir_matches(base: Path, partial: str) -> list[str]:
    """Return directory names matching a partial path relative to base. Dirs only."""
    try:
        if not partial:
            return sorted(
                e.name
                for e in base.iterdir()
                if e.is_dir() and not e.name.startswith(".")
            )

        partial_path = base / partial
        parent = partial_path.parent
        prefix = partial_path.name

        prefix_lower = prefix.lower()
        matches = []
        for entry in parent.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name.lower().startswith(prefix_lower):
                rel = str(entry.relative_to(base))
                matches.append(rel)

        return sorted(matches)
    except OSError:
        return []


def path_matches(workspace: Path, partial: str) -> list[str]:
    """Return file/folder names matching a partial path relative to workspace."""
    try:
        if not partial:
            return sorted(
                e.name
                for e in workspace.iterdir()
                if not e.name.startswith(".")
            )

        partial_path = workspace / partial
        parent = partial_path.parent
        prefix = partial_path.name

        prefix_lower = prefix.lower()
        matches = []
        for entry in parent.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.name.lower().startswith(prefix_lower):
                rel = str(entry.relative_to(workspace))
                matches.append(rel)

        return sorted(matches)
    except OSError:
        return []


def complete_line(workspace: Path, line: str) -> list[str]:
    """Return completions for the full input line (context-aware).

    Handles /workspace <dir>, /think <option>, and delegates to complete_token for rest.
    """
    stripped = line.lstrip()

    # /workspace <partial_dir> — complete any directory on the filesystem
    if stripped.lower().startswith("/workspace "):
        partial = stripped[len("/workspace "):].lstrip()
        return [f"/workspace {m}" for m in abs_dir_matches(partial)]

    # /think <partial_option> — complete on/off/once
    if stripped.lower().startswith("/think "):
        partial = stripped[len("/think "):].lstrip().lower()
        return [f"/think {o}" for o in THINK_OPTIONS if o.startswith(partial)]

    # /update <subcommand> — complete ollama|models
    if stripped.lower().startswith("/update "):
        partial = stripped[len("/update "):].lstrip().lower()
        return [f"/update {o}" for o in _UPDATE_SUBCMDS if o.startswith(partial)]

    # /info <subcommand> — complete prompt|tools
    if stripped.lower().startswith("/info "):
        partial = stripped[len("/info "):].lstrip().lower()
        return [f"/info {o}" for o in _INFO_SUBCMDS if o.startswith(partial)]

    # /config set|tools — complete subcommands, keys, tool names, and values
    if stripped.lower().startswith("/config "):
        rest = stripped[len("/config "):].lstrip()
        trailing = rest != rest.rstrip()  # user typed a space after the last token
        rest_parts = rest.split(maxsplit=3)
        first = rest_parts[0].lower() if rest_parts else ""

        def _config_subcmds(prefix: str) -> list[str]:
            return [f"/config {s}" for s in ("set", "theme", "tools") if s.startswith(prefix)]

        # /config tools [<tool> [global] [workspace]]
        if "tools".startswith(first) and first not in ("set", "theme"):
            if first != "tools" or (len(rest_parts) == 1 and not trailing):
                return _config_subcmds(first)
            # first == "tools"; user has typed a space → complete next token
            if len(rest_parts) == 1:
                return [f"/config tools {n}" for n in TOOL_NAMES]
            tool_partial = rest_parts[1].lower()
            matching_tools = [n for n in TOOL_NAMES if n.startswith(tool_partial)]
            if len(rest_parts) == 2 and not trailing:
                return [f"/config tools {n}" for n in matching_tools]
            tool = rest_parts[1]
            if len(rest_parts) == 2:
                return [f"/config tools {tool} {v}" for v in ("on", "off", "_")]
            g_partial = rest_parts[2].lower()
            if len(rest_parts) == 3 and not trailing:
                return [f"/config tools {tool} {v}" for v in ("on", "off", "_") if v.startswith(g_partial)]
            g_val = rest_parts[2]
            if len(rest_parts) == 3:
                return [f"/config tools {tool} {g_val} {v}" for v in ("on", "off", "inherit")]
            w_partial = rest_parts[3].lower()
            return [f"/config tools {tool} {g_val} {v}" for v in ("on", "off", "inherit") if v.startswith(w_partial)]

        # /config theme [value]
        if "theme".startswith(first) and first not in ("set", "tools"):
            if first != "theme" or (len(rest_parts) == 1 and not trailing):
                return _config_subcmds(first)
            if len(rest_parts) == 1:
                return [f"/config theme {v}" for v in THEME_OPTIONS]
            partial = rest_parts[1].lower()
            return [f"/config theme {v}" for v in THEME_OPTIONS if v.startswith(partial)]

        # /config set <key> [value]
        if not rest_parts or not "set".startswith(first):
            return _config_subcmds("")
        if len(rest_parts) == 1 and not trailing:
            return ["/config set"]
        if len(rest_parts) == 1:
            return [f"/config set {k}" for k in CONFIG_KEYS]
        key_partial = rest_parts[1].lower() if len(rest_parts) >= 2 else ""
        matching_keys = [k for k in CONFIG_KEYS if k.startswith(key_partial)]
        if len(rest_parts) == 2 and not trailing:
            return [f"/config set {k}" for k in matching_keys]
        if len(rest_parts) == 2:
            return [f"/config set {k}" for k in CONFIG_KEYS]
        key = rest_parts[1].lower()
        val_partial = rest_parts[2].lower() if len(rest_parts) > 2 else ""
        if key == "model.thinking":
            opts = [o for o in ("on", "off") if o.startswith(val_partial)]
            return [f"/config set {key} {o}" for o in opts]
        if key == "safety.level":
            opts = [o for o in SAFETY_OPTIONS if o.startswith(val_partial)]
            return [f"/config set {key} {o}" for o in opts]
        return []

    # Fall back to token-based completion on last word.
    # Split at last whitespace to separate the "active token" from any prefix.
    parts = stripped.rsplit(None, 1)
    has_prefix = len(parts) > 1  # there is content before the active token

    # -----------------------------------------------------------------------
    # Embedded /think detection: the user appended /think (or a partial) to a
    # regular message.  Only /think makes sense mid-message; other slash
    # commands are standalone.
    # -----------------------------------------------------------------------

    # Case A: trailing space after "/think" — offer all subcommands.
    # e.g.  "refactor this /think "
    if stripped.endswith(" "):
        last_word = parts[-1].lower() if parts else ""
        if last_word == "/think":
            return ["/think once", "/think on", "/think off"]
        return []

    last_token = parts[-1] if parts else ""

    # Case B: last token is a plain word preceded by "/think"
    # e.g.  "refactor this /think o"  →  parts = ["refactor this /think", "o"]
    if has_prefix and not last_token.startswith("/"):
        prev = parts[0].rsplit(None, 1)
        if prev[-1].lower() == "/think":
            return [
                f"/think {o}"
                for o in ("once", "on", "off")
                if o.startswith(last_token.lower())
            ]
        return complete_token(workspace, last_token)

    # Case C: last token starts with "/" (bare "/" or partial command)
    if last_token.startswith("/"):
        if has_prefix:
            # Embedded after real content — only /think is useful mid-message.
            if "/think".startswith(last_token.lower()):
                return ["/think once", "/think on", "/think off"]
            return []
        # Standalone command at the start of the line.
        return complete_token(workspace, last_token)

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
