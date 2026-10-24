"""`telegram-reader call`: run one tool from a script, without an MCP client.

The server answers in stateless JSON mode, so a call is a single POST with no
session to set up. Values go out as strings and the server converts them to the
types its tools declare. Argument names are checked against the tool list first,
because the server ignores names it does not know: a misspelled `since=` would
quietly read the whole history.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
# Telethon waits out only short flood limits and downloads run in the
# background, so a healthy call returns well within this.
TIMEOUT_SECONDS = 120


class CallError(Exception):
    """The server could not be reached or refused the request."""


def rpc(url: str, method: str, params: dict | None = None) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    # The endpoint is loopback. A proxy from the environment would carry the
    # request off the host unless no_proxy happens to list 127.0.0.1.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, body, HEADERS), timeout=TIMEOUT_SECONDS) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise CallError(f"{url} answered HTTP {exc.code}") from None
    except OSError as exc:
        raise CallError(f"cannot reach {url}: {getattr(exc, 'reason', exc)}") from None
    if "error" in reply:
        raise CallError(reply["error"].get("message") or "the server refused the request")
    return reply["result"]


def arguments(tool: dict, pairs: list[str]) -> dict:
    known = tool["inputSchema"].get("properties", {})
    args = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise CallError(f"expected key=value, got {pair!r}")
        if key not in known:
            raise CallError(f"{tool['name']} has no argument {key!r}; it takes {', '.join(known) or 'none'}")
        args[key] = value
    return args


def call(url: str, name: str, pairs: list[str]) -> tuple[str, bool]:
    """Return the tool's JSON text and whether it is an error. Images are left out."""
    tools = {tool["name"]: tool for tool in rpc(url, "tools/list")["tools"]}
    if name not in tools:
        raise CallError(f"no tool {name!r}; the server has {', '.join(tools)}")
    result = rpc(url, "tools/call", {"name": name, "arguments": arguments(tools[name], pairs)})
    text = "\n".join(part["text"] for part in result["content"] if part["type"] == "text")
    return text, bool(result.get("isError"))


def run_call(url: str, name: str, pairs: list[str]) -> int:
    try:
        text, failed = call(url, name, pairs)
    except CallError as exc:
        print(f"telegram-reader call: {exc}", file=sys.stderr)
        return 1
    print(text, file=sys.stderr if failed else sys.stdout)
    return 1 if failed else 0
