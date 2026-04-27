from __future__ import annotations

import enum


class AccountStatus(str, enum.Enum):
    new = "new"
    idle = "idle"
    busy = "busy"
    paused = "paused"
    limited = "limited"
    unauthorized = "unauthorized"
    subscription_expired = "subscription_expired"
    banned = "banned"
    dead = "dead"

class TaskStatus(str, enum.Enum):
    pending = "pending"
    assigned = "assigned"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    timeout = "timeout"
