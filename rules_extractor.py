"""
Kalshi market rules text is free-form and varies market to market. Before
trading a market you need to know, precisely: which station, which data
provider, what threshold counts as a "yes," and what happens on missing data.

Rather than hand-parsing every market's rules PDF, this uses the Anthropic
API to extract those fields into structured JSON, with a local cache so you
never re-pay for the same market twice. This is the one place an LLM
belongs in this system — parsing inconsistent legal text, not deciding
whether to trade.

ALWAYS spot-check a sample of extractions against the actual rules PDF
before trusting this at scale. A parsing mistake here (wrong station, wrong
threshold) silently breaks the whole strategy.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

import anthropic

from config import SETTINGS

EXTRACTION_PROMPT = """You are extracting structured settlement rules from a \
prediction market's contract terms. Read the rules text and return ONLY a \
JSON object (no prose, no markdown fences) with these exact fields:

{
  "station_code": "<the NWS or airport station code, e.g. KAUS, or null if not stated>",
  "settlement_source": "<'NWS' | 'The Weather Company' | 'NOAA CDO' | 'other' | 'unclear'>",
  "measure": "<'precipitation_daily' | 'precipitation_monthly' | 'temperature_high' | 'temperature_low' | 'other'>",
  "threshold_description": "<plain-language threshold, e.g. 'strictly greater than 0 inches' or 'high temperature 85-89F'>",
  "threshold_low_f": "<for temperature_high/temperature_low markets ONLY: numeric lower bound in Fahrenheit for YES to resolve true, or null if there is no lower bound (e.g. 'less than 70F' has no lower bound) or if measure is not a temperature type>",
  "threshold_high_f": "<for temperature_high/temperature_low markets ONLY: numeric upper bound in Fahrenheit for YES to resolve true, or null if there is no upper bound (e.g. 'greater than 85F' has no upper bound) or if measure is not a temperature type>",
  "trace_counts_as_zero": <true | false | null if not stated>,
  "fallback_rule": "<brief description of what happens if primary source has no data, or null>",
  "confidence": "<'high' | 'medium' | 'low' — your confidence this extraction is complete and correct>"
}

Rules text:
---
{rules_text}
---"""

# Bump this whenever MarketRules gains/changes a field that changes what a
# cached extraction means — e.g. adding threshold_low_f/threshold_high_f.
# Cached entries written under an older version get transparently
# re-extracted once (see extract() below) instead of silently running with
# fields that don't exist / are always null, which would otherwise leave a
# temperature model doing nothing without any error to notice.
CACHE_SCHEMA_VERSION = 2


@dataclass
class MarketRules:
    ticker: str
    station_code: Optional[str]
    settlement_source: str
    measure: str
    threshold_description: str
    trace_counts_as_zero: Optional[bool]
    fallback_rule: Optional[str]
    confidence: str
    threshold_low_f: Optional[float] = None
    threshold_high_f: Optional[float] = None


class RulesExtractor:
    def __init__(self, cache_path: str | None = None, api_key: str | None = None):
        self.cache_path = cache_path or SETTINGS.rules_cache_path
        self._client = anthropic.Anthropic(api_key=api_key or SETTINGS.anthropic_api_key)
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_path):
            with open(self.cache_path, "r") as f:
                return json.load(f)
        return {}

    def _save_cache(self) -> None:
        with open(self.cache_path, "w") as f:
            json.dump(self._cache, f, indent=2)

    def extract(self, ticker: str, rules_text: str, force: bool = False) -> MarketRules:
        if not force and ticker in self._cache:
            cached = self._cache[ticker]
            if cached.get("_schema_version") == CACHE_SCHEMA_VERSION:
                return MarketRules(**{k: v for k, v in cached.items() if k != "_schema_version"})
            # Older cache entry from before this field set existed — refresh
            # once rather than running forever with fields that are always
            # null (see CACHE_SCHEMA_VERSION comment above).

        if not rules_text.strip():
            result = MarketRules(
                ticker=ticker, station_code=None, settlement_source="unclear",
                measure="other", threshold_description="no rules text available",
                trace_counts_as_zero=None, fallback_rule=None, confidence="low",
                threshold_low_f=None, threshold_high_f=None,
            )
            self._cache[ticker] = {"_schema_version": CACHE_SCHEMA_VERSION, **asdict(result)}
            self._save_cache()
            return result

        prompt = EXTRACTION_PROMPT.replace("{rules_text}", rules_text[:6000])
        response = self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(block.text for block in response.content if hasattr(block, "text"))
        try:
            parsed = json.loads(raw.strip().strip("`").removeprefix("json").strip())
        except (json.JSONDecodeError, ValueError):
            parsed = {
                "station_code": None, "settlement_source": "unclear", "measure": "other",
                "threshold_description": "extraction failed — parse manually",
                "threshold_low_f": None, "threshold_high_f": None,
                "trace_counts_as_zero": None, "fallback_rule": None, "confidence": "low",
            }

        result = MarketRules(ticker=ticker, **parsed)
        self._cache[ticker] = {"_schema_version": CACHE_SCHEMA_VERSION, **asdict(result)}
        self._save_cache()
        return result
