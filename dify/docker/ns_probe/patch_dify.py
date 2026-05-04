"""
Patch Dify source files at Docker build time to integrate ns_probe.

Patches:
  1. app_factory.py  — import ext_ns_probe and add it to the extensions list
  2. workflow_entry.py — register NsProbeLayer on the GraphEngine

Safe to run multiple times (idempotent).
"""

import sys

FACTORY = "/app/api/app_factory.py"
WORKFLOW = "/app/api/core/workflow/workflow_entry.py"


def patch_app_factory():
    with open(FACTORY) as f:
        text = f.read()

    if "ext_ns_probe" in text:
        print(f"  [skip] {FACTORY} already patched")
        return

    # Add import — after ext_mail
    if "ext_mail,\n" in text:
        text = text.replace("ext_mail,\n", "ext_mail,\n        ext_ns_probe,\n", 1)
    else:
        print(f"  [WARN] could not find ext_mail import anchor in {FACTORY}")
        return

    # Add to extensions list — after ext_otel (use ext_request_logging as context
    # to ensure we match the extensions list, not the imports block)
    EXT_LIST_ANCHOR = "ext_otel,\n        ext_request_logging,"
    if EXT_LIST_ANCHOR in text:
        text = text.replace(
            EXT_LIST_ANCHOR,
            "ext_otel,\n        ext_ns_probe,\n        ext_request_logging,",
            1,
        )
    else:
        print(f"  [WARN] could not find ext_otel/ext_request_logging anchor in {FACTORY}")
        return

    with open(FACTORY, "w") as f:
        f.write(text)
    print(f"  [done] {FACTORY}")


def patch_workflow_entry():
    with open(WORKFLOW) as f:
        text = f.read()

    if "ns_probe" in text:
        print(f"  [skip] {WORKFLOW} already patched")
        return

    anchor = "self.graph_engine.layer(ObservabilityLayer())"
    if anchor not in text:
        print(f"  [WARN] ObservabilityLayer anchor not found in {WORKFLOW}")
        return

    patch = """
        # Add ns_probe observability layer if available
        try:
            from extensions.ext_ns_probe import get_ns_probe_layer

            ns_layer = get_ns_probe_layer()
            if ns_layer is not None:
                self.graph_engine.layer(ns_layer)
        except Exception:
            pass"""

    text = text.replace(anchor, anchor + patch, 1)

    with open(WORKFLOW, "w") as f:
        f.write(text)
    print(f"  [done] {WORKFLOW}")


if __name__ == "__main__":
    print("Patching Dify for ns_probe integration …")
    patch_app_factory()
    patch_workflow_entry()
    print("Done.")
