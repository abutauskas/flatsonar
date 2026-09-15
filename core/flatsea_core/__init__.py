"""Shared pieces of Flatsea: manifest parsing, sandbox risk scoring, license checks."""

from .manifest import Manifest, ManifestSource, parse_manifest, parse_manifest_text
from .metadata import metadata_app_id, metadata_to_finish_args
from .risk import RiskLevel, RiskReport, score_finish_args
from .spdx import is_open_source

__all__ = [
    "Manifest",
    "ManifestSource",
    "RiskLevel",
    "RiskReport",
    "is_open_source",
    "metadata_app_id",
    "metadata_to_finish_args",
    "parse_manifest",
    "parse_manifest_text",
    "score_finish_args",
]
