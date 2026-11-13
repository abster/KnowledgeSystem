"""Prompts and schemas for prose fact extraction and row-label verification (PRO-2, TBL-12).

The version string is a hash of the prompt text and schema, so editing either one changes every
cache key (PRO-8): a stale cache entry can never answer for a new prompt.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ATTRIBUTES: tuple[str, ...] = (
    "rate",
    "dividend_rate",
    "apy",
    "apr",
    "min_to_open",
    "min_to_earn",
    "fee",
    "count",
    "address",
    "phone",
    "hours",
    "status",
    "eligibility",
    "prose",
)
QUALIFIER_KEYS: tuple[str, ...] = (
    "term",
    "amount_tier",
    "balance_band",
    "model_year",
    "product",
    "rate_type",
    "loan_type",
)
TEXT_ATTRIBUTES = frozenset({"address", "phone", "hours", "status", "eligibility", "prose"})

EXTRACT_TOOL = "record_facts"
VERIFY_TOOL = "record_verdict"

EXTRACT_SYSTEM = f"""You extract atomic facts from one passage of a credit union's web page.
Return every fact the passage states explicitly. Never infer, calculate or combine values.
For each fact give:
- entity: the product, service, place or thing the fact is about, as the passage names it.
- attribute: exactly one of {", ".join(ATTRIBUTES)}.
- qualifiers: an object using only these keys when the passage states them for this fact:
  {", ".join(QUALIFIER_KEYS)}. Values are short phrases copied from the passage, such as
  "30-year", "$100,000 or less", "2024-2027 model", "purchase", "fixed", "conventional".
- value: the value exactly as the passage writes it (for example "5.39%", "$25", "9:00 a.m.").
- unit: percent, usd, count, months or null.
- quote: the shortest verbatim excerpt of the passage that states the fact.
If the passage holds no fact, return an empty list. Precision matters more than coverage:
when you are unsure, leave the fact out."""

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "attribute": {"type": "string", "enum": list(ATTRIBUTES)},
                    "qualifiers": {
                        "type": "object",
                        "properties": {key: {"type": "string"} for key in QUALIFIER_KEYS},
                        "additionalProperties": False,
                    },
                    "value": {"type": "string"},
                    "unit": {"type": ["string", "null"]},
                    "quote": {"type": "string"},
                },
                "required": ["entity", "attribute", "qualifiers", "value", "unit", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}

VERIFY_SYSTEM = """You check how a table row label was read by a program.
You are given the label text, the other label cells of the row, and the reading: the product
name and the qualifiers (term in months, amount tier, model year, product, rate type, loan
type). Answer correct=true only if the reading matches the label text exactly. If the product
name drops or adds words, or a qualifier is missing, wrong or invented, answer correct=false."""

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"correct": {"type": "boolean"}},
    "required": ["correct"],
    "additionalProperties": False,
}


def prompt_version(label: str, system: str, schema: dict[str, Any]) -> str:
    """``<label>-<8 hex>``: changes whenever the system text or the schema changes."""
    digest = hashlib.sha256(
        f"{system}\x00{json.dumps(schema, sort_keys=True)}".encode()
    ).hexdigest()
    return f"{label}-{digest[:8]}"


EXTRACT_VERSION = prompt_version("prose", EXTRACT_SYSTEM, EXTRACT_SCHEMA)
VERIFY_VERSION = prompt_version("rowlabel", VERIFY_SYSTEM, VERIFY_SCHEMA)
