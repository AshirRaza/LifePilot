"""Automated knowledge-base freshness maintenance (Phase 9C.5)."""

from lifepilot.maintenance.change_classify import ChangeClass, classify_content_change
from lifepilot.maintenance.pipeline import MaintenanceReport, run_knowledge_maintenance

__all__ = [
    "ChangeClass",
    "MaintenanceReport",
    "classify_content_change",
    "run_knowledge_maintenance",
]
