"""
ns_probe observability extension for Dify.

Registers a GraphEngineLayer that captures per-node spans for every workflow
execution, mirroring the detail shown in Dify's built-in TRACING tab (node
title, type, inputs, outputs, duration, tokens, status).

The layer creates:
  - A root span "workflow.run" for the entire workflow execution
  - Child spans for each node (named by node title, e.g. "AUTH_ZUS", "GENERATE_FORM")
  - Attributes: node.type, node.id, input, output, tokens, cost, status

Activation
----------
The extension is silently skipped when ``ns_probe`` is not installed.
No env vars needed — endpoint auto-detected (host.docker.internal inside Docker).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dify_app import DifyApp

logger = logging.getLogger(__name__)

_NS_PROBE_AVAILABLE = False
_tracer = None  # module-level tracer, set in init_app
_buffer = None  # module-level buffer for manual span push

try:
    from ns_probe import configure, force_flush, get_tracer
    from ns_probe.span import Span, SpanKind, StatusCode

    _NS_PROBE_AVAILABLE = True
except ImportError:
    pass


def is_enabled() -> bool:
    """Return False when ns_probe is missing so the extension is skipped."""
    return _NS_PROBE_AVAILABLE


def _get_collector_endpoint() -> str:
    """Return the collector endpoint, auto-detecting Docker environments."""
    explicit = os.getenv("NS_PROBE_ENDPOINT")
    if explicit:
        return explicit
    if os.path.exists("/.dockerenv"):
        return "http://host.docker.internal:4318/v1/traces"
    return "http://localhost:4318/v1/traces"


def init_app(app: DifyApp) -> None:
    """Configure ns_probe (no instrument_all — we use a layer instead)."""
    global _tracer, _buffer
    endpoint = _get_collector_endpoint()
    configure(
        service_name=os.getenv("NS_PROBE_SERVICE_NAME", "dify-api"),
        endpoint=endpoint,
        environment=os.getenv("NS_PROBE_ENVIRONMENT", app.config.get("DEPLOY_ENV", "development")),
    )
    # Do NOT call instrument_all() — it creates noisy db.connect spans.
    _tracer = get_tracer("dify")
    # Access the internal buffer for manual span submission
    _buffer = _tracer._buffer
    logger.info("ns_probe extension initialised — traces → %s", endpoint)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TRUNCATE = 4000


def _safe_json(obj: Any) -> str:
    """Safely serialize to JSON, truncated."""
    try:
        from graphon.file import File
        from graphon.variables import Segment
        from pydantic import BaseModel

        def _convert(v: Any) -> Any:
            if v is None or isinstance(v, (bool, int, float, str)):
                return v
            if isinstance(v, Segment):
                return _convert(v.value)
            if isinstance(v, File):
                return v.to_dict()
            if isinstance(v, BaseModel):
                return _convert(v.model_dump(mode="json"))
            if isinstance(v, dict):
                return {k: _convert(val) for k, val in v.items()}
            if isinstance(v, (list, tuple)):
                return [_convert(i) for i in v]
            return str(v)

        s = json.dumps(_convert(obj), ensure_ascii=False)
        return s[:_TRUNCATE] if len(s) > _TRUNCATE else s
    except Exception:
        try:
            s = repr(obj)
            return s[:_TRUNCATE] if len(s) > _TRUNCATE else s
        except Exception:
            return "<unserializable>"


# ---------------------------------------------------------------------------
# NsProbeLayer — a GraphEngineLayer for per-node tracing
# ---------------------------------------------------------------------------
def get_ns_probe_layer():
    """Factory to create the NsProbeLayer. Returns None if ns_probe unavailable."""
    if not _NS_PROBE_AVAILABLE or _tracer is None:
        logger.warning("ns_probe layer skipped: available=%s, tracer=%s, buffer=%s", _NS_PROBE_AVAILABLE, _tracer, _buffer)
        return None

    # Support both package names: "graphon" (local dev) and "dify_graph" (Docker image)
    try:
        from graphon.graph_engine.layers import GraphEngineLayer
        from graphon.graph_events import GraphNodeEventBase, NodeRunFailedEvent, NodeRunSucceededEvent
        from graphon.nodes.base.node import Node
    except ImportError:
        from dify_graph.graph_engine.layers.base import GraphEngineLayer  # type: ignore[no-redef]
        from dify_graph.graph_events import GraphNodeEventBase, NodeRunFailedEvent, NodeRunSucceededEvent  # type: ignore[no-redef]
        from dify_graph.nodes.base.node import Node  # type: ignore[no-redef]

    class NsProbeLayer(GraphEngineLayer):
        """
        GraphEngineLayer that creates ns_probe spans for each workflow node.

        Creates a root "workflow.run" span and child spans per node, with full
        input/output/token/status attributes — matching what Dify shows in TRACING.
        """

        def __init__(self) -> None:
            super().__init__()
            self._root_span: Span | None = None
            self._node_spans: dict[str, Span] = {}
            self._workflow_input: str = ""
            self._workflow_output: str = ""

        def on_graph_start(self) -> None:
            """Create the root workflow span."""
            self._root_span = _tracer.start_span_no_context(
                name="workflow.run",
                kind=SpanKind.SERVER,
                attributes={"agent.name": "dify"},
            )
            self._node_spans.clear()
            logger.info("ns_probe layer: workflow.run span started (trace_id=%s)", self._root_span.trace_id)

        def on_node_run_start(self, node: Node) -> None:
            """Create a child span for this node."""
            if self._root_span is None:
                return

            execution_id = node.execution_id
            if not execution_id:
                return

            span = _tracer.start_span_no_context(
                name=node.title,
                kind=SpanKind.INTERNAL,
                trace_id=self._root_span.trace_id,
                parent_span_id=self._root_span.span_id,
                attributes={
                    "node.id": node.id,
                    "node.type": str(node.node_type),
                    "node.title": node.title,
                    "agent.name": "dify",
                },
            )
            self._node_spans[execution_id] = span
            logger.info("ns_probe layer: node span started '%s' (type=%s)", node.title, node.node_type)

        def on_node_run_end(
            self,
            node: Node,
            error: Exception | None,
            result_event: GraphNodeEventBase | None = None,
        ) -> None:
            """End the node span with result attributes."""
            execution_id = node.execution_id
            if not execution_id:
                return

            span = self._node_spans.pop(execution_id, None)
            if span is None:
                return

            # Set inputs/outputs/metadata from result
            if result_event and result_event.node_run_result:
                nr = result_event.node_run_result

                # -- Input/Output (trace explorer looks for traceloop.entity.input/output) --
                if nr.inputs:
                    input_json = _safe_json(nr.inputs)
                    span.set_attribute("traceloop.entity.input", json.dumps({"inputs": nr.inputs}, default=str)[:_TRUNCATE])
                    span.set_attribute("input", input_json)
                    # Capture start node input as workflow-level input
                    if str(node.node_type) == "start":
                        self._workflow_input = json.dumps({"inputs": {"input": input_json}}, default=str)[:_TRUNCATE]
                if nr.outputs:
                    output_json = _safe_json(nr.outputs)
                    span.set_attribute("traceloop.entity.output", json.dumps({"outputs": nr.outputs}, default=str)[:_TRUNCATE])
                    span.set_attribute("output", output_json)
                    # Capture end node output as workflow-level output
                    if str(node.node_type) == "end":
                        self._workflow_output = json.dumps({"outputs": {"output": output_json}}, default=str)[:_TRUNCATE]
                if nr.process_data:
                    span.set_attribute("process_data", _safe_json(nr.process_data))

                # -- LLM-specific attributes (for Generate_Form and other LLM nodes) --
                process_data = nr.process_data or {}
                outputs = nr.outputs or {}

                model_name = process_data.get("model_name", "") if isinstance(process_data, dict) else ""
                model_provider = process_data.get("model_provider", "") if isinstance(process_data, dict) else ""

                if model_name:
                    span.set_attribute("gen_ai.request.model", str(model_name))
                    span.set_attribute("gen_ai.response.model", str(model_name))
                if model_provider:
                    span.set_attribute("gen_ai.system", str(model_provider))

                # Token usage — use gen_ai.usage.* keys (triggers processor extraction)
                usage_data = (process_data.get("usage") if isinstance(process_data, dict) else None) or {}
                if not usage_data and isinstance(outputs, dict):
                    usage_data = outputs.get("usage") or {}

                prompt_tokens = 0
                completion_tokens = 0
                total_tokens = 0

                if usage_data and isinstance(usage_data, dict):
                    prompt_tokens = int(usage_data.get("prompt_tokens", 0) or 0)
                    completion_tokens = int(usage_data.get("completion_tokens", 0) or 0)
                    total_tokens = int(usage_data.get("total_tokens", 0) or 0)
                elif nr.llm_usage:
                    prompt_tokens = nr.llm_usage.prompt_tokens or 0
                    completion_tokens = nr.llm_usage.completion_tokens or 0
                    total_tokens = nr.llm_usage.total_tokens or 0

                if total_tokens > 0:
                    span.set_attribute("gen_ai.usage.input_tokens", str(prompt_tokens))
                    span.set_attribute("gen_ai.usage.output_tokens", str(completion_tokens))
                    span.set_attribute("llm.usage.total_tokens", str(total_tokens))
                    span.set_attribute("llm.usage.prompt_tokens", str(prompt_tokens))
                    span.set_attribute("llm.usage.completion_tokens", str(completion_tokens))

                # Cost
                if nr.llm_usage and nr.llm_usage.total_price:
                    span.set_attribute("cost", str(float(nr.llm_usage.total_price)))

                # Finish reason
                if isinstance(outputs, dict):
                    finish_reason = outputs.get("finish_reason", "")
                    if finish_reason:
                        span.set_attribute("gen_ai.completion.0.finish_reason", str(finish_reason))

                    # LLM text output as completion
                    text_output = outputs.get("text", "")
                    if text_output:
                        span.set_attribute("gen_ai.completion.0.content", str(text_output)[:_TRUNCATE])

                # Prompt messages for LLM nodes
                if isinstance(process_data, dict):
                    prompts = process_data.get("prompts", [])
                    if prompts and isinstance(prompts, list):
                        for i, prompt in enumerate(prompts):
                            if isinstance(prompt, dict):
                                role = prompt.get("role", "")
                                text = prompt.get("text", "")
                                if role:
                                    span.set_attribute(f"gen_ai.prompt.{i}.role", str(role))
                                if text:
                                    span.set_attribute(f"gen_ai.prompt.{i}.content", str(text)[:_TRUNCATE])

                # Status from result
                span.set_attribute("node.status", str(nr.status))
                if nr.error:
                    span.set_attribute("error.message", nr.error)

            # Determine success from event type
            if error:
                span.set_status(StatusCode.ERROR, str(error))
            elif isinstance(result_event, NodeRunFailedEvent):
                span.set_status(StatusCode.ERROR, getattr(result_event, "error", "unknown"))
            else:
                span.set_status(StatusCode.OK)

            span.end()
            if _buffer is not None:
                _buffer.push(span)

        def on_event(self, event) -> None:
            """Not used — node lifecycle covered by on_node_run_start/end."""
            pass

        def on_graph_end(self, error: Exception | None) -> None:
            """End the root workflow span and flush."""
            logger.info("ns_probe layer: graph ended (error=%s, orphan_nodes=%d)", error, len(self._node_spans))
            # End any orphaned node spans
            for exec_id, span in self._node_spans.items():
                span.set_status(StatusCode.ERROR, "graph ended before node completed")
                span.end()
                if _buffer is not None:
                    _buffer.push(span)
            self._node_spans.clear()

            # Set workflow-level input/output on root span for trace list display
            if self._root_span:
                if self._workflow_input:
                    self._root_span.set_attribute("traceloop.entity.input", self._workflow_input)
                if self._workflow_output:
                    self._root_span.set_attribute("traceloop.entity.output", self._workflow_output)
                if error:
                    self._root_span.set_status(StatusCode.ERROR, str(error))
                else:
                    self._root_span.set_status(StatusCode.OK)
                self._root_span.end()
                if _buffer is not None:
                    _buffer.push(self._root_span)
                self._root_span = None

            # Flush all spans to collector
            force_flush()

    return NsProbeLayer()
