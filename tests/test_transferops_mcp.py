from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _reload_module():
    module_name = "scripts.transferops_mcp"
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "transferops_mcp.py"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_streamable_http_defaults_to_stateless(monkeypatch):
    monkeypatch.setenv("TRANSFEROPS_MCP_TRANSPORT", "streamable-http")
    monkeypatch.delenv("TRANSFEROPS_MCP_STATELESS_HTTP", raising=False)

    module = _reload_module()

    assert module.TRANSFEROPS_MCP_STATELESS_HTTP is True
    assert module.mcp.settings.stateless_http is True


def test_explicit_stateful_override_is_respected(monkeypatch):
    monkeypatch.setenv("TRANSFEROPS_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("TRANSFEROPS_MCP_STATELESS_HTTP", "false")

    module = _reload_module()

    assert module.TRANSFEROPS_MCP_STATELESS_HTTP is False
    assert module.mcp.settings.stateless_http is False


def test_retry_handoff_tool_calls_retry_endpoint(monkeypatch):
    module = _reload_module()
    calls: list[tuple[str, dict | None]] = []

    def fake_post(path, payload=None):
        calls.append((path, payload))
        return {"ok": True}

    monkeypatch.setattr(module, "_post", fake_post)

    result = module.transferops_retry_handoff(3)

    assert result == {"ok": True}
    assert calls == [("/api/agent/handoffs/3/retry", None)]
