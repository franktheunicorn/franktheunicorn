"""Pinned Apache Magpie integration.

Apache Magpie (https://github.com/apache/magpie) is the ASF's agent-assisted
maintainership framework: markdown skills for coding agents plus small Python
tools. frank borrows from it rather than depending on it, and every borrowed
piece is tied to one exact upstream commit recorded in ``magpie-pin.yaml`` next
to this file. See :mod:`franktheunicorn.magpie.pin` for the policy and
``docs/magpie-integration.md`` for what is borrowed and why.
"""

from franktheunicorn.magpie.pin import (
    PIN_PATH,
    VENDORED_DIR,
    MagpiePin,
    PinnedTool,
    VendoredFile,
    load_pin,
    vendored_text,
    verify_vendored,
)

__all__ = [
    "PIN_PATH",
    "VENDORED_DIR",
    "MagpiePin",
    "PinnedTool",
    "VendoredFile",
    "load_pin",
    "vendored_text",
    "verify_vendored",
]
