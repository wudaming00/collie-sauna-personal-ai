"""Shared domain contract for the Collie local node and a person-level cloud node.

This module deliberately contains no storage or transport code.  It names the objects that have
to mean the same thing on every surface, while allowing Collie to use SQLite locally and a cloud
runtime to use a different physical store.

The important separation is semantic:

* a ``task`` is a person's commitment;
* a ``session`` is one attempt by an agent to do work;
* a ``mission`` is a durable execution campaign;
* an ``automation`` is a trigger definition whose fires create sessions;
* a ``memory_claim`` is durable knowledge with evidence, not a chat summary.

``EntitySpec`` is also the single allow-list used by Personal State delta sync.  Keeping SQL table
and wire names here prevents a remote payload from choosing a table or column.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PERSONAL_DELTA_FORMAT", "PERSONAL_CORE_SCHEMA_VERSION", "EntitySpec",
    "ENTITY_SPECS", "ENTITY_BY_TYPE", "COMMITMENT_STATUSES", "SESSION_STATUSES",
    "AUTOMATION_STATUSES", "MEMORY_CARDS",
]


PERSONAL_DELTA_FORMAT = "collie-personal-delta/2"
PERSONAL_CORE_SCHEMA_VERSION = 1

# These state machines intentionally do not overlap.  A Dashboard card in Review is a Session;
# it does not silently turn the person's Task into a different kind of object.
COMMITMENT_STATUSES = ("inbox", "open", "next", "doing", "blocked", "waiting", "done", "dropped")
SESSION_STATUSES = (
    "queued", "working", "needs_you", "review", "done", "junk", "failed", "cancelled",
)
AUTOMATION_STATUSES = ("active", "paused", "disabled")

# Sauna-compatible views.  The source of truth remains typed Memory claims; these are projections.
MEMORY_CARDS = (
    "user_preferences", "rules", "user_profile", "your_tools", "assistant_identity",
    "user_relationships", "recent_activity",
)


@dataclass(frozen=True)
class EntitySpec:
    entity_type: str
    table: str
    key: str
    columns: tuple[str, ...]
    category: str
    merge_policy: str = "review_on_divergence"


ENTITY_SPECS = (
    EntitySpec("project", "projects", "id",
               ("id", "name", "kind", "status", "summary", "path", "created_at", "updated_at"),
               "projects"),
    EntitySpec("goal", "goals", "id",
               ("id", "title", "status", "project_id", "due_at", "summary", "created_at", "updated_at"),
               "goals"),
    EntitySpec("task", "tasks", "id",
               ("id", "title", "status", "project_id", "goal_id", "kind", "due_at", "order_key",
                "source", "notes", "done_at", "created_at", "updated_at"),
               "tasks"),
    EntitySpec("event", "events", "id",
               ("id", "title", "start_at", "end_at", "all_day", "kind", "location", "project_id",
                "goal_id", "external_ref", "notes", "created_at", "updated_at"),
               "calendar"),
    EntitySpec("note", "notes", "id",
               ("id", "title", "body", "project_id", "goal_id", "source", "pinned", "created_at",
                "updated_at"),
               "notes", "preserve_both"),
    EntitySpec("person", "people", "id",
               ("id", "name", "role", "org", "project_id", "notes", "created_at"),
               "relationships"),
    EntitySpec("journal", "journal", "day",
               ("day", "happened_json", "decisions_json", "open_loops_json", "next_json", "narrative",
                "source", "generated_at"),
               "journal", "preserve_both"),
    EntitySpec("workflow", "workflows", "id",
               ("id", "name", "trigger", "steps_json", "status", "observations", "confidence", "source",
                "accepted", "rejected", "last_used_at", "created_at", "updated_at"),
               "workflows"),
    EntitySpec("suggestion", "suggestions", "id",
               ("id", "kind", "title", "body", "task_id", "workflow_id", "goal_id", "action_json",
                "status", "confidence", "source", "created_at", "resolved_at"),
               "workflows"),
    # ``cloud_task`` is retained as a compatibility object while the Session/ExecutionRequest
    # model is introduced.  New code should not confuse it with a person's commitment Task.
    EntitySpec("execution_request", "cloud_tasks", "id",
               ("id", "title", "runtime", "status", "scheduled_for", "deliver_at", "detail_json",
                "result", "created_at", "updated_at"),
               "agent_activity"),
    EntitySpec("device", "devices", "device_id",
               ("device_id", "name", "platform", "kind", "this_device", "runtime_json", "last_seen"),
               "preferences"),
)

ENTITY_BY_TYPE = {spec.entity_type: spec for spec in ENTITY_SPECS}
