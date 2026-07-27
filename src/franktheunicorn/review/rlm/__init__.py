"""Recursive Language Model (RLM) review engine (v1.5, opt-in).

Decomposes a large PR into bounded leaf reviews and aggregates them, instead
of stuffing one oversized prompt. See ``backend.RLMBackend`` (provider
``rlm``) for the review-pipeline entry point and ``engine.RLMEngine`` for the
orchestration core.
"""

from __future__ import annotations

from franktheunicorn.review.rlm.aggregate import aggregate_review, interest_label_from_findings
from franktheunicorn.review.rlm.broker import ModelBroker
from franktheunicorn.review.rlm.budget import RLMBudget, estimate_tokens
from franktheunicorn.review.rlm.decompose import RLMNode, fits_single_call, partition
from franktheunicorn.review.rlm.engine import RLMEngine
from franktheunicorn.review.rlm.protocol import BrokerClient
from franktheunicorn.review.rlm.server import BrokerServer

__all__ = [
    "BrokerClient",
    "BrokerServer",
    "ModelBroker",
    "RLMBudget",
    "RLMEngine",
    "RLMNode",
    "aggregate_review",
    "estimate_tokens",
    "fits_single_call",
    "interest_label_from_findings",
    "partition",
]
