"""Drive the massive-mcp server directly over stdio JSON-RPC.

Bypasses any MCP client. Lets us see *exactly* what an agent sees from each tool.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

SERVER_CMD = ["node", "/tmp/mcp-unpack/dist/index.js"]


class MCPClient:
    def __init__(self, cmd: list[str], env: dict[str, str] | None = None):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
            text=True,
            bufsize=1,
        )
        self._id = 0

    def _send(self, method: str, params: dict | None = None, *, notification: bool = False) -> Any:
        if notification:
            req = {"jsonrpc": "2.0", "method": method}
        else:
            self._id += 1
            req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        line = json.dumps(req) + "\n"
        assert self.proc.stdin
        self.proc.stdin.write(line)
        self.proc.stdin.flush()
        if notification:
            return None
        return self._read_response(self._id)

    def _read_response(self, expected_id: int) -> Any:
        assert self.proc.stdout
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == expected_id:
                if "error" in msg:
                    raise RuntimeError(f"MCP error: {msg['error']}")
                return msg.get("result")

    def initialize(self) -> dict:
        result = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-probe", "version": "0.1"},
        })
        # Required handshake
        self._send("notifications/initialized", notification=True)
        return result

    def list_tools(self) -> dict:
        return self._send("tools/list")

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self._send("tools/call", {"name": name, "arguments": arguments or {}})

    def stderr(self) -> str:
        # non-blocking peek isn't easy; close and read at end
        return ""

    def close(self) -> None:
        try:
            self.proc.stdin.close() if self.proc.stdin else None
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def measure_call(client: MCPClient, name: str, args: dict, label: str = "") -> dict:
    t0 = time.time()
    try:
        out = client.call_tool(name, args)
        elapsed = time.time() - t0
        text_content = ""
        for c in out.get("content", []):
            if c.get("type") == "text":
                text_content += c.get("text", "")
        return {
            "label": label or name,
            "tool": name,
            "args": args,
            "elapsed_s": round(elapsed, 1),
            "is_error": out.get("isError", False),
            "text_bytes": len(text_content),
            "text_excerpt": text_content[:1000],
            "structured": out.get("structuredContent"),
            "raw_keys": list(out.keys()),
        }
    except Exception as e:
        return {
            "label": label or name,
            "tool": name,
            "args": args,
            "elapsed_s": round(time.time() - t0, 1),
            "exception": f"{type(e).__name__}: {e}",
        }


def main() -> int:
    token = os.environ.get("MASSIVE_TOKEN")
    if not token:
        print("Set MASSIVE_TOKEN", file=sys.stderr)
        return 1

    client = MCPClient(SERVER_CMD, env={"MASSIVE_TOKEN": token})
    try:
        info = client.initialize()
        print("=== INITIALIZE ===")
        print(json.dumps(info, indent=2)[:600])

        print("\n=== TOOLS LIST ===")
        tools = client.list_tools()
        for t in tools.get("tools", []):
            print(f"\n• {t['name']}")
            print(f"  desc: {t.get('description', '')}")
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            req = schema.get("required", [])
            for pname, pinfo in props.items():
                marker = "*" if pname in req else " "
                ptype = pinfo.get("type") or pinfo.get("enum") or "?"
                pdesc = pinfo.get("description", "")
                print(f"   {marker} {pname}: {ptype} — {pdesc}")
        print(json.dumps(tools, indent=2), file=open("/tmp/mcp-tools.json", "w"))

        # Now run the test matrix.
        results = []

        # Test 1: account_status (free, fastest)
        results.append(measure_call(client, "account_status", {}, "T1: account_status"))

        # Test 2: ai_chat_completion — cold hallucination check
        results.append(measure_call(
            client, "ai_chat_completion",
            {"prompt": "What is the joinmassive Web Render API?", "model": "chatgpt"},
            "T2: ai_chat_completion (chatgpt, hallucination-prone topic)",
        ))

        # Test 3: ai_chat_completion — perplexity, same question (compare sources/structure)
        results.append(measure_call(
            client, "ai_chat_completion",
            {"prompt": "What is the joinmassive Web Render API?", "model": "perplexity"},
            "T3: ai_chat_completion (perplexity, same question)",
        ))

        # Test 4: web_search — the tool I didn't know existed
        results.append(measure_call(
            client, "web_search",
            {"query": "Browserbase YC company founders", "max_results": 5},
            "T4: web_search",
        ))

        # Test 5: web_fetch — markdown of a real page
        results.append(measure_call(
            client, "web_fetch",
            {"url": "https://www.ycombinator.com/companies/browserbase", "format": "markdown"},
            "T5: web_fetch (markdown)",
        ))

        print("\n=== TEST RESULTS ===")
        for r in results:
            print(f"\n--- {r['label']} ---")
            print(f"  args:        {r.get('args')}")
            print(f"  elapsed:     {r.get('elapsed_s')}s")
            if "exception" in r:
                print(f"  EXCEPTION:   {r['exception']}")
                continue
            print(f"  is_error:    {r.get('is_error')}")
            print(f"  text_bytes:  {r.get('text_bytes')} ({r['text_bytes']/1024:.1f} KB)")
            sc = r.get("structured")
            if sc is not None:
                if isinstance(sc, dict):
                    print(f"  structured:  keys={list(sc.keys())}")
                else:
                    print(f"  structured:  {type(sc).__name__}")
            else:
                print(f"  structured:  (none)")
            print(f"  text head:   {r['text_excerpt'][:300]!r}")

        with open("/tmp/mcp-test-results.json", "w") as f:
            json.dump(results, f, indent=2)
        print("\n=> wrote /tmp/mcp-test-results.json (full payloads)")

    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
