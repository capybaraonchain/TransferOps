<p align="center">
  <img src="assets/transferops-abstract-banner.png" alt="Abstract TransferOps systems banner" width="100%">
</p>

<h1 align="center">TransferOps</h1>

<p align="center">
  <strong>Local-first operations control for policy-governed transfer queues.</strong>
  <br>
  Disk budgets, lifecycle obligations, manual approvals, audit trails, and agent-friendly APIs in one dashboard.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.116-009688?style=for-the-badge&logo=fastapi&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-local--first-003B57?style=for-the-badge&logo=sqlite&logoColor=white">
  <img alt="Agent API" src="https://img.shields.io/badge/Agent_API-ready-111827?style=for-the-badge">
</p>

<p align="center">
  <a href="#features">Features</a>
  ·
  <a href="#architecture">Architecture</a>
  ·
  <a href="#quick-start">Quick Start</a>
  ·
  <a href="#api-surface">API</a>
  ·
  <a href="#portfolio-notes">Portfolio Notes</a>
</p>

---

## Overview

TransferOps is a finished local-first FastAPI application for running a small operations desk around queued transfers. It evaluates incoming candidates, enforces disk and retention policies, records every decision, and gives operators a dense authenticated dashboard for daily review.

It is intentionally boring where infrastructure should be boring:

- local SQLite persistence by default
- explicit dry-run mode for safe demos
- runtime-editable settings with masked secrets
- auditable admission decisions
- separate automatic and manual lanes
- agent API and MCP bridge for tool-assisted operations

## Features

<table>
  <tr>
    <td width="50%">
      <h3>Policy Engine</h3>
      <p>Scores candidates against disk pressure, retention obligations, source confidence, historical learning, and lane-specific thresholds.</p>
    </td>
    <td width="50%">
      <h3>Operations Dashboard</h3>
      <p>Authenticated FastAPI/Jinja dashboard with live refresh, budget meters, runtime settings, health checks, audit tables, and manual request workflows.</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <h3>Lifecycle Accounting</h3>
      <p>Tracks active items from candidate intake through managed retention, safe state, retirement eligibility, and error handling.</p>
    </td>
    <td width="50%">
      <h3>Agent-Ready API</h3>
      <p>Bearer-token protected endpoints plus an MCP bridge for local agents that need inspection, planning, and controlled execution tools.</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <h3>Budget Guardrails</h3>
      <p>Separate automatic and manual budgets, high-water marks, free-disk checks, unresolved-work caps, and emergency posture reporting.</p>
    </td>
    <td width="50%">
      <h3>Adapter Boundary</h3>
      <p>Provider and executor integrations stay behind explicit service modules so the policy core remains inspectable and testable.</p>
    </td>
  </tr>
</table>

## Architecture

```mermaid
flowchart LR
  A["Source adapters"] --> B["Candidate normalizer"]
  B --> C["Policy scoring engine"]
  C --> D{"Admit?"}
  D -->|yes| E["Transfer executor"]
  D -->|no| F["Decision audit log"]
  E --> G["Lifecycle reconciler"]
  G --> H["Learning buckets"]
  G --> I["Dashboard state"]
  I --> J["Operator dashboard"]
  I --> K["Agent API / MCP tools"]
```

The codebase is organized around explicit boundaries:

```text
app/
  main.py                 FastAPI routes, scheduler jobs, dashboard rendering
  config.py               typed runtime settings
  models.py               SQLAlchemy persistence models
  services/
    controller.py         admission, reconciliation, audit events
    scoring.py            policy score calculation
    lifecycle.py          managed-state transitions
    settings.py           runtime setting overrides and masking
    integrations.py       health checks and upstream API boundaries
    manual_requests.py    operator-initiated request workflow
scripts/
  transferops_mcp.py      local MCP bridge around the agent API
tests/
  test_api.py             API and workflow coverage
  test_controller.py      policy engine and lifecycle coverage
```

## Quick Start

TransferOps uses `uv` for Python dependency management.

```powershell
uv sync --python 3.12
Copy-Item .env.example .env
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open the dashboard:

```text
http://127.0.0.1:8000
```

Default demo credentials:

```text
username: admin
password: change-me
```

The app starts in `dry_run` mode by default, so it is safe to inspect without connecting a real executor.

## Configuration

Most settings can be changed from the dashboard and are persisted as runtime overrides. Secrets are masked in API responses and can be rotated without rewriting source files.

Common `.env` values:

```env
TRANSFEROPS_APP_NAME=TransferOps
TRANSFEROPS_ENV=development
TRANSFEROPS_DATABASE_URL=sqlite:///./transferops.db
TRANSFEROPS_DRY_RUN=true
TRANSFEROPS_DASHBOARD_USERNAME=admin
TRANSFEROPS_DASHBOARD_PASSWORD=change-me
TRANSFEROPS_AGENT_API_TOKEN=transferops-local-agent
TRANSFEROPS_QBIT_CATEGORY=transferops.managed
TRANSFEROPS_QBIT_TAG=transferops.managed
TRANSFEROPS_QBIT_SAVE_PATH=C:\TransferOps\managed
TRANSFEROPS_HOST_DISK_CHECK_PATH=C:\TransferOps\managed
```

## API Surface

TransferOps exposes both dashboard endpoints and JSON APIs:

| Area | Endpoint |
| --- | --- |
| Dashboard | `GET /` |
| Status | `GET /api/status` |
| Decisions | `GET /api/decisions` |
| Managed transfers | `GET /api/torrents` |
| Runtime settings | `GET /api/settings`, `PUT /api/settings` |
| Manual requests | `POST /api/agent/requests` |
| Candidate planning | `GET /api/agent/requests/{id}/candidates` |
| Controlled execution | `POST /api/agent/requests/{id}/select-candidate` |
| Agent overview | `GET /api/agent/overview` |

Agent routes require:

```http
Authorization: Bearer transferops-local-agent
```

## Development

```powershell
uv run pytest
uv run ruff check .
```

The test suite uses fake executor and integration clients so core policy behavior can be validated without external services.

## Portfolio Notes

TransferOps demonstrates:

- production-style FastAPI route organization
- typed settings and secret masking
- SQLAlchemy models and migrations-on-startup compatibility handling
- scheduler jobs for reconciliation and learning
- dense operational UI design without a frontend framework
- agent-tool bridge design for controlled local automation
- practical test coverage across policy, API, lifecycle, and integration boundaries

This public snapshot is designed as a portfolio-safe showcase of the architecture and product thinking behind a real local operations tool. The default configuration and examples use generic providers and dry-run behavior.

## License

MIT
