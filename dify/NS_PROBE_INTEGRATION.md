# ns_probe Integration with Dify

How ns_probe was integrated into Dify to send workflow traces to ns_observe.

---

## What it does

Every time a Dify workflow runs, ns_probe captures:

- **1 root span** (`workflow.run`) covering the entire workflow
- **1 child span per node** (e.g. `User_Input`, `Auth_Zus`, `Generate_Form`, `Output`)
- **Per-node attributes**: input, output, status, duration
- **LLM-specific attributes**: model name, provider, prompt/completion tokens, cost, prompts, completions
- **Workflow-level input/output** shown in the trace list view

Traces flow: **Dify → ns_collector (:4318) → Kafka → ns_processor → ClickHouse → ns_api → Trace Explorer (:8085)**

---

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Dify API    │     │ Dify Worker  │     │  ns_observe  │
│ (Gunicorn)   │     │ (Celery)     │     │  stack       │
│              │     │              │     │              │
│ ext_ns_probe │     │ ext_ns_probe │────▶│ ns_collector │
│              │     │  ↑ runs here │     │ :4318        │
└──────────────┘     └──────────────┘     └──────────────┘
```

Key point: **Workflows execute on the Celery worker container**, not the API container. Both containers need ns_probe installed.

---

## Files changed (3 files)

### 1. `api/extensions/ext_ns_probe.py` (new file)

The core integration. A Dify extension that:

- Configures ns_probe with auto-detected collector endpoint
- Provides `NsProbeLayer` — a `GraphEngineLayer` that hooks into workflow execution
- Creates spans with proper attributes for ns_observe's trace explorer

**How it hooks in:**

```
GraphEngine runs workflow
  → on_graph_start()    → creates root "workflow.run" span
  → on_node_run_start() → creates child span per node
  → on_node_run_end()   → sets attributes (input, output, tokens, model) and ends span
  → on_graph_end()      → sets workflow input/output on root span, flushes to collector
```

**Attribute mapping** (what ns_observe expects):

| Trace Explorer field | Span attribute |
|---|---|
| INPUT | `traceloop.entity.input` |
| OUTPUT | `traceloop.entity.output` |
| Input Tokens | `gen_ai.usage.input_tokens` |
| Output Tokens | `gen_ai.usage.output_tokens` |
| Total Tokens | `llm.usage.total_tokens` |
| Model | `gen_ai.request.model` |
| Provider | `gen_ai.system` |

### 2. `api/app_factory.py` (2 lines added)

```python
# In the imports block:
ext_ns_probe,

# In the extensions list (after ext_otel):
ext_ns_probe,
```

This makes Dify call `ext_ns_probe.init_app()` at startup, which configures the ns_probe tracer.

### 3. `api/core/workflow/workflow_entry.py` (6 lines added)

```python
# After ObservabilityLayer registration:
try:
    from extensions.ext_ns_probe import get_ns_probe_layer
    ns_layer = get_ns_probe_layer()
    if ns_layer is not None:
        self.graph_engine.layer(ns_layer)
except Exception:
    pass
```

This registers the `NsProbeLayer` on each workflow's GraphEngine so it receives node lifecycle events.

---

## How it's deployed (Docker)

A custom Docker image is built on top of the official Dify image. Three files handle this:

### `docker/ns_probe/Dockerfile`

```dockerfile
FROM langgenius/dify-api:1.13.3
USER root
# Install ns_probe wheel into the venv
COPY ns_probe-0.1.0-py3-none-any.whl /tmp/
RUN python -m zipfile -e /tmp/ns_probe-0.1.0-py3-none-any.whl \
    /app/api/.venv/lib/python3.12/site-packages/ && rm /tmp/ns_probe-0.1.0-py3-none-any.whl
# Add the extension file
COPY api/extensions/ext_ns_probe.py /app/api/extensions/ext_ns_probe.py
# Patch app_factory.py and workflow_entry.py
COPY docker/ns_probe/patch_dify.py /tmp/patch_dify.py
RUN python /tmp/patch_dify.py && rm /tmp/patch_dify.py
USER dify
```

### `docker/ns_probe/patch_dify.py`

A build-time Python script that inserts the ns_probe import/registration into `app_factory.py` and `workflow_entry.py`. Idempotent — safe to run multiple times.

### `docker/docker-compose.override.yaml`

```yaml
services:
  api:
    build:
      context: ..
      dockerfile: docker/ns_probe/Dockerfile
    image: dify-api-ns-probe:1.13.3
  worker:
    build:
      context: ..
      dockerfile: docker/ns_probe/Dockerfile
    image: dify-api-ns-probe:1.13.3
```

Docker Compose automatically merges this with `docker-compose.yaml`. Both `api` and `worker` use the custom image.

---

## Commands

### Build and start

```bash
cd dify/docker
docker compose build api      # builds dify-api-ns-probe:1.13.3
docker compose up -d api worker
```

### Rebuild after changing ext_ns_probe.py

```bash
cd dify/docker
docker compose build api
docker compose up -d api worker
```

### Upgrade Dify version

Update the version in 2 places:
1. `docker/ns_probe/Dockerfile` → `BASE_IMAGE` arg
2. `docker/docker-compose.override.yaml` → `BASE_IMAGE` arg and `image` tag

Then rebuild.

### Check it's working

```bash
# Verify ns_probe is installed
docker exec docker-worker-1 python -c "import ns_probe; print('ok')"

# Verify patches applied
docker exec docker-worker-1 grep ns_probe /app/api/app_factory.py
docker exec docker-worker-1 grep ns_probe /app/api/core/workflow/workflow_entry.py

# After running a workflow, check traces
curl -s 'http://localhost:8085/api/traces?limit=1' | python -m json.tool
```

---

## How it stays persistent

Previously, files were injected at runtime via `docker cp` — lost on every `docker compose up`. Now:

- The Dockerfile **bakes** ns_probe into a custom image (`dify-api-ns-probe:1.13.3`)
- The `docker-compose.override.yaml` tells Compose to use that image
- Running `docker compose up` always uses the custom image
- `docker compose build` rebuilds it when you change ext_ns_probe.py

---

## Graceful degradation

If ns_probe is not installed (e.g. the standard Dify image), the extension is silently skipped:
- `ext_ns_probe.is_enabled()` returns `False` → `init_app()` is skipped
- `get_ns_probe_layer()` returns `None` → no layer registered
- Zero impact on workflow execution

---

## Endpoint auto-detection

No env vars needed. The extension detects the environment:
- Inside Docker (`/.dockerenv` exists) → `http://host.docker.internal:4318/v1/traces`
- Local dev → `http://localhost:4318/v1/traces`
- Override with `NS_PROBE_ENDPOINT` env var if needed
