"""Shared pieces of Flatsea: manifest parsing, sandbox risk scoring, license checks."""

from .manifest import Manifest, ManifestSource, parse_manifest, parse_manifest_text
from .risk import RiskLevel, RiskReport, score_finish_args
from .spdx import is_open_source

__all__ = [
    "Manifest",
    "ManifestSource",
    "RiskLevel",
    "RiskReport",
    "is_open_source",
    "parse_manifest",
    "parse_manifest_text",
    "score_finish_args",
]
