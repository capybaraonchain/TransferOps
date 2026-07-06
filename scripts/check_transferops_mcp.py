from __future__ import annotations

import argparse

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def _main(url: str, call_overview: bool) -> None:
    async with streamable_http_client(url) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"session_id={get_session_id()}")
            print(f"tool_count={len(tools.tools)}")
            has_overview = any(tool.name == "transferops_overview" for tool in tools.tools)
            print(f"has_transferops_overview={has_overview}")
            if call_overview:
                result = await session.call_tool("transferops_overview")
                print(f"overview_content_items={len(result.content)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the local transferops MCP bridge.")
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--skip-overview", action="store_true")
    args = parser.parse_args()
    anyio.run(_main, args.url, not args.skip_overview)


if __name__ == "__main__":
    main()
