"""The pull queue: which todo issues should start next, and where they can go.

Derived, never stored. A hand-maintained "next up" list rots the moment an agent
files an issue, and this board is largely agent-fed; recomputing from status,
blockers and WIP headroom means the queue cannot drift from the data it claims to
describe.

The queueing act is already in the schema and needs no new state: `backlog` means
nobody has decided, `todo` means decided but not started. Promoting an issue to
todo is what puts it in line. This module only orders that line and says how much
of it can actually move.

Lives in core, next to `filter_from_params`, for the same reason that does: the
digest, the CLI, the HTTP adapter and the board must not each carry their own
notion of what is next, or they will disagree with each other in front of a user.
"""

from .query import IssueFilter

#: A lane is an epic plus its own headroom. Issues with no parent queue under
#: this sentinel -- they are their own work, not a container's.
NO_EPIC = "_none"


def _sort_key(issue):
    """Priority first, then the nearest commitment, then age.

    due_date sorts before created_at so that dating something is a way to pull it
    forward without inventing a rank column. Undated issues sort after dated ones
    at the same priority rather than jumping the queue on age alone.
    """
    return (
        issue.priority,
        issue.due_date or "9999-99-99",
        issue.created_at or "",
        issue.key,
    )


def container_keys(ctx):
    """Keys that other issues hang off.

    A container reads as in_progress only because its children are, so counting
    it against the WIP limit overstates the work in flight -- the same correction
    the digest applies. Closed children still make their parent a container, so
    the scan includes them.
    """
    return {
        i.parent_key
        for i in ctx.list_issues(IssueFilter(has_parent=True, include_closed=True))
        if i.parent_key
    }


def next_up(ctx, wip_limit=None):
    """What can start next, per lane, and what is stopping the rest.

    Returns three lists plus per-lane accounting:

      ready    -- todo, no open blocker, ordered; `pullable` marks the ones that
                  fit inside their lane's remaining WIP
      blocked  -- todo but held by an open blocker, with the blockers named. This
                  is the useful half of a queue: it says which single blocker
                  would unjam the most work.
      starved  -- lanes with headroom and nothing ready, plus their best backlog
                  candidate, so an idle lane is visible rather than merely empty.
    """
    limit = ctx.config.wip_limit if wip_limit is None else wip_limit
    containers = container_keys(ctx)

    open_issues = list(ctx.list_issues(IssueFilter()))
    lane_of = lambda i: i.parent_key or NO_EPIC
    titles = {i.key: i.title for i in open_issues}

    # WIP per lane, containers excluded.
    in_progress = {}
    for i in open_issues:
        if i.status == "in_progress" and i.key not in containers:
            in_progress.setdefault(lane_of(i), []).append(i.key)

    # Blocker lookup is one call per candidate, so restrict it to the pool that
    # could actually move: a blocked backlog item is not this function's problem.
    candidates = [i for i in open_issues if i.status == "todo" and i.key not in containers]

    ready, blocked = [], []
    for issue in sorted(candidates, key=_sort_key):
        blockers = [
            {"key": b.to_key, "title": b.to_title, "status": b.to_status}
            for b in ctx.blockers(issue.key)
        ]
        row = {**issue.to_dict(), "lane": lane_of(issue)}
        if blockers:
            blocked.append({**row, "blocked_by": blockers})
        else:
            ready.append(row)

    # Mark how many of the ready items each lane can actually take. Everything
    # past a lane's headroom stays in the list -- knowing the queue is backed up
    # matters as much as knowing what is at the front of it.
    lanes, taken = {}, {}
    for lane in {*in_progress, *(r["lane"] for r in ready)}:
        used = len(in_progress.get(lane, []))
        lanes[lane] = {
            "lane": lane,
            "epic": titles.get(lane) if lane != NO_EPIC else None,
            "in_progress": used,
            "limit": limit,
            "headroom": max(0, limit - used),
        }
        taken[lane] = 0
    for r in ready:
        lane = lanes[r["lane"]]
        r["pullable"] = taken[r["lane"]] < lane["headroom"]
        if r["pullable"]:
            taken[r["lane"]] += 1

    # A lane with room and an empty queue: name its best backlog candidate so the
    # gap is actionable rather than just an absence.
    ready_lanes = {r["lane"] for r in ready}
    starved = []
    for lane, info in sorted(lanes.items()):
        if not info["headroom"] or lane in ready_lanes:
            continue
        backlog = sorted(
            (i for i in open_issues
             if lane_of(i) == lane and i.status == "backlog" and i.key not in containers),
            key=_sort_key)
        starved.append({
            **info,
            "backlog_count": len(backlog),
            "candidate": backlog[0].to_dict() if backlog else None,
        })

    return {
        "ready": ready,
        "blocked": blocked,
        "starved": starved,
        "lanes": [lanes[k] for k in sorted(lanes)],
        "limit": limit,
        "pullable_now": sum(1 for r in ready if r["pullable"]),
    }
