ARG BASE_IMAGE=langgenius/dify-api:1.13.3
FROM ${BASE_IMAGE}

USER root

# --- Install ns_probe SDK ---
COPY ns_probe-0.1.0-py3-none-any.whl /tmp/
RUN python -m zipfile -e /tmp/ns_probe-0.1.0-py3-none-any.whl \
        /app/api/.venv/lib/python3.12/site-packages/ \
    && rm /tmp/ns_probe-0.1.0-py3-none-any.whl

# --- Add ns_probe extension ---
COPY api/extensions/ext_ns_probe.py /app/api/extensions/ext_ns_probe.py

# --- Patch workflow_entry.py and app_factory.py ---
COPY docker/ns_probe/patch_dify.py /tmp/patch_dify.py
RUN python /tmp/patch_dify.py && rm /tmp/patch_dify.py

# Switch back to the original non-root user
USER dify
