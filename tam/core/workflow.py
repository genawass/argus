"""The status transition matrix and its guards.

Kept in its own module because it is the part of the system most likely to be
edited, and the part where a silent mistake is most expensive: a wrong entry
here lets the board drift into states nothing else expects.
"""

from ..errors import TransitionError
from ..models import CLOSED_STATUSES

TRANSITIONS = {
    "backlog": {"todo", "cancelled"},
    "todo": {"in_progress", "blocked", "backlog", "cancelled"},
    "in_progress": {"review", "blocked", "done", "todo", "cancelled"},
    "blocked": {"in_progress", "todo", "cancelled"},
    "review": {"done", "in_progress", "blocked"},
    "done": {"in_progress"},          # reopen
    "cancelled": {"backlog"},         # revive
}


def allowed_from(status):
    return sorted(TRANSITIONS.get(status, set()))


def check_transition(old, new):
    """Raise unless `old -> new` is a legal move. Same-status is a no-op elsewhere."""
    if old == new:
        return
    if new not in TRANSITIONS.get(old, set()):
        raise TransitionError(
            f"cannot move {old} -> {new}; allowed from {old}: "
            f"{', '.join(allowed_from(old)) or '(none)'}",
            from_status=old,
            to_status=new,
            allowed=allowed_from(old),
        )


def closes(status):
    return status in CLOSED_STATUSES
