"""Deterministic, persistence-agnostic sync domain.

The package deliberately has no dependency on FastAPI, SQLAlchemy, Redis, or a
specific job runner.  The API process and workers integrate it through the
protocols in :mod:`app.sync.ports`.
"""

from .engine import SyncOrchestrator
from .models import (
    ActionKind,
    ActionStatus,
    EventPlanStatus,
    MatchMode,
    RunStatus,
    SyncPlan,
)
from .planner import SyncPlanner
from .serialization import sync_plan_from_dict

__all__ = [
    "ActionKind",
    "ActionStatus",
    "EventPlanStatus",
    "MatchMode",
    "RunStatus",
    "SyncOrchestrator",
    "SyncPlan",
    "SyncPlanner",
    "sync_plan_from_dict",
]
