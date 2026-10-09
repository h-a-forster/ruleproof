"""``doctor/mcp-drift``: MCP servers configured differently per agent, and inline secrets."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from ruleproof.doctor._common import (
    Finding,
    RepoInfo,
    clip,
    ev,
    read_text,
    shannon_entropy,
    strip_jsonc,
)

JSON_CONFIGS: tuple[tuple[str, str, bool], ...] = (
    (".mcp.json", "mcpServers", False),
    (".cursor/mcp.json", "mcpServers", False),
    (".vscode/mcp.json", "servers", True),
    (".gemini/settings.json", "mcpServers", True),
)
"""(path, key holding the servers, comments allowed)."""
CODEX_CONFIG = ".codex/config.toml"

SECRET_PREFIXES: tuple[str, ...] = (
    "sk-",
    "sk_live_",
    "rk_live_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "AKIA",
    "AIza",
    "glpat-",
)
_REFERENCE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*|\{env:[^}]*\}|%[A-Za-z_]\w*%")
_SECRET_KEY = re.compile(
    r"key|token|secret|passw|pwd|auth|credential|private|bearer|cookie|session", re.IGNORECASE
)
_PLACEHOLDER = re.compile(
    r"your|example|placeholder|changeme|replace|xxxx|<|>|\.\.\.", re.IGNORECASE
)
_WORKSPACE_VARS = re.compile(
    r"^\$\{(?:workspaceFolder|workspaceRoot|CLAUDE_PROJECT_DIR|PWD)\}[/\\]?|^\./"
)


@dataclass(slots=True)
class Server:
    name: str
    file: str  # repo-relative config path
    line: int | None
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def signature(self) -> dict[str, object]:
        command = self.command
        args = list(self.args)
        if command:
            command = _exe(command)
            if command == "cmd" and args[:1] and args[0].lower() in ("/c", "/k") and len(args) > 1:
                command, args = _exe(args[1]), args[2:]
            if command in ("npx", "bunx", "pnpx"):
                args = [a for a in args if a not in ("-y", "--yes")]
        return {
            "command": command,
            "args": tuple(_WORKSPACE_VARS.sub("", a) for a in args),
            "url": self.url.rstrip("/") if self.url else None,
        }


def _exe(command: str) -> str:
    base = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return re.sub(r"\.(?:exe|cmd|bat)$", "", base)


def check_mcp(info: RepoInfo) -> list[Finding]:
    out: list[Finding] = []
    servers: list[Server] = []
    for rel, key, jsonc in JSON_CONFIGS:
        path = info.repo / rel
        if not info.is_file(path):
            continue
        text = read_text(path) or ""
        try:
            data = json.loads(strip_jsonc(text) if jsonc else text)
        except json.JSONDecodeError as exc:
            out.append(_invalid(rel, "JSON", exc.lineno, exc.colno, exc.msg, text))
            continue
        block = data.get(key) if isinstance(data, dict) else None
        if isinstance(block, dict):
            servers.extend(_from_mapping(block, rel, text, toml=False))
    path = info.repo / CODEX_CONFIG
    if info.is_file(path):
        text = read_text(path) or ""
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            m = re.search(r"line (\d+), column (\d+)", str(exc))
            line, col = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
            msg = re.sub(r"\s*\(at line.*\)$", "", str(exc))
            out.append(_invalid(CODEX_CONFIG, "TOML", line, col, msg, text))
        else:
            block = data.get("mcp_servers")
            if isinstance(block, dict):
                servers.extend(_from_mapping(block, CODEX_CONFIG, text, toml=True))
    for server in servers:
        out.extend(_secrets(server, info))
    out.extend(_drift(servers))
    return out


def _invalid(rel: str, kind: str, line: int, col: int, msg: str, text: str) -> Finding:
    lines = text.splitlines()
    excerpt = clip(lines[line - 1]) if 0 < line <= len(lines) else None
    return Finding(
        "mcp-drift",
        "warning",
        f"{rel} is not valid {kind} (line {line}, column {col}: {msg}); "
        "its MCP servers are ignored",
        [ev(f"invalid {kind}", rel, line, excerpt)],
    )


def _strings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def _from_mapping(block: dict[str, Any], rel: str, text: str, toml: bool) -> list[Server]:
    out: list[Server] = []
    lines = text.splitlines()
    for name, entry in block.items():
        if not isinstance(entry, dict):
            continue
        if toml:
            pattern = rf"^\s*\[mcp_servers\.(?:\"{re.escape(name)}\"|{re.escape(name)})[\].]"
        else:
            pattern = rf"\"{re.escape(name)}\"\s*:"
        line = next((i + 1 for i, ln in enumerate(lines) if re.search(pattern, ln)), None)
        args = entry.get("args")
        url = entry.get("url") or entry.get("httpUrl") or entry.get("serverUrl")
        headers = _strings(entry.get("headers")) | _strings(entry.get("http_headers"))
        out.append(
            Server(
                name=str(name),
                file=rel,
                line=line,
                command=entry.get("command") if isinstance(entry.get("command"), str) else None,
                args=tuple(str(a) for a in args) if isinstance(args, list) else (),
                url=url if isinstance(url, str) else None,
                env=_strings(entry.get("env")),
                headers=headers,
            )
        )
    return out


def redact_secret(key: str, value: str) -> str | None:
    """A redacted form of ``value`` when it looks like a literal secret, else None."""
    v = value.strip()
    if not v or _REFERENCE.search(v):
        return None
    v = re.sub(r"^(?:bearer|token|basic)\s+", "", v, flags=re.IGNORECASE)
    if any(c.isspace() for c in v):
        return None
    for prefix in SECRET_PREFIXES:
        if v.startswith(prefix) and len(v) >= len(prefix) + 12:
            return f"{prefix}…({len(v)} chars)"
    if (
        _SECRET_KEY.search(key)
        and len(v) >= 20
        and not _PLACEHOLDER.search(v)
        and not re.search(r"://|^[./~]|^[A-Za-z]:[\\/]|\\", v)
        and re.search(r"\d", v)
        and re.search(r"[A-Za-z]", v)
        and shannon_entropy(v) >= 3.5
    ):
        return f"…({len(v)} chars)"
    return None


def _secrets(server: Server, info: RepoInfo) -> list[Finding]:
    found: list[tuple[str, str]] = []
    for kind, values in (("env", server.env), ("headers", server.headers)):
        for key, value in values.items():
            redacted = redact_secret(key, value)
            if redacted:
                found.append((f"{kind}.{key}", redacted))
    for n, arg in enumerate(server.args):
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if any(value.startswith(p) for p in SECRET_PREFIXES):
            redacted = redact_secret("", value)
            if redacted:
                found.append((f"args[{n}]", redacted))
    if server.url:
        for key, value in re.findall(r"[?&]([^=&#]+)=([^&#]+)", server.url):
            redacted = redact_secret(key, value)
            if redacted:
                found.append((f"url query {key}", redacted))
    lines = (read_text(info.repo / server.file) or "").splitlines()
    out: list[Finding] = []
    for where, redacted in found:
        field_name = where.split(".", 1)[-1].split("[", 1)[0].split(" ")[-1]
        line = next(
            (
                i + 1
                for i, ln in enumerate(lines)
                if i + 1 >= (server.line or 1) and field_name in ln
            ),
            server.line,
        )
        out.append(
            Finding(
                "mcp-drift",
                "error",
                f"{server.file} has a literal secret in MCP server '{server.name}' {where} "
                f"({redacted}); reference an environment variable instead",
                [ev("literal secret", server.file, line, f"{server.name}: {where} = {redacted}")],
            )
        )
    return out


def _display(value: object) -> str:
    if isinstance(value, tuple):
        value = " ".join(_safe(str(v)) for v in value)
    elif isinstance(value, str):
        value = _safe(value)
    return f"`{clip(str(value), 50)}`" if value else "(none)"


def _safe(token: str) -> str:
    if "?" in token and "://" in token:
        token = token.split("?", 1)[0] + "?…"
    return redact_secret("", token) or token


def _drift(servers: list[Server]) -> list[Finding]:
    by_name: dict[str, list[Server]] = {}
    for s in servers:
        by_name.setdefault(s.name, []).append(s)
    out: list[Finding] = []
    for name, entries in sorted(by_name.items()):
        files = {s.file for s in entries}
        if len(files) < 2:
            continue
        signatures = [s.signature() for s in entries]
        if all(sig == signatures[0] for sig in signatures):
            continue
        first = entries[0]
        other = next(s for s, sig in zip(entries, signatures, strict=True) if sig != signatures[0])
        a, b = first.signature(), other.signature()
        diffs = [
            f"{k} {_display(a[k])} vs {_display(b[k])}"
            for k in ("command", "args", "url")
            if a[k] != b[k]
        ]
        out.append(
            Finding(
                "mcp-drift",
                "warning",
                f"MCP server '{name}' differs between {first.file} and {other.file}: "
                + "; ".join(diffs),
                [ev(f"'{name}' as configured here", s.file, s.line) for s in entries],
            )
        )
    return out
