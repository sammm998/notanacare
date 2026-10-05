"""Group structured interventions into visits.

Rules (per care recipient, interventions sorted by preferred time):

* Interventions are merged into the current visit while
  - their preferred times are within ``MAX_ANCHOR_GAP`` minutes of the visit anchor,
  - the intersection of all start windows stays non-empty,
  - the visit stays within ``MAX_INTERVENTIONS`` / ``MAX_DURATION``.
* Visit duration is the **sum** of intervention durations -- interventions are
  performed sequentially, nothing is assumed to overlap.
* Timing constraints of an intervention apply to the start of the visit that
  contains it (window = intersection of the interventions' windows).
* A visit requires two employees if any intervention requires double staffing.
* Skills are required of every employee on the visit; delegations are required
  of the lead employee (see ``Visit.required_delegations``).
"""

from __future__ import annotations

from .domain import CareRecipient, Intervention, TimingKind, Visit, VisitStatus

MAX_ANCHOR_GAP = 45
MAX_INTERVENTIONS = 12
MAX_DURATION = 130


def _make_visit(vid: str, recipient: CareRecipient, group: list[Intervention]) -> Visit:
    earliest = max(i.earliest_start for i in group)
    latest = min(i.latest_start for i in group)
    hard = [i for i in group if i.timing == TimingKind.HARD]
    pref_source = hard or group
    preferred = round(sum(i.preferred_time for i in pref_source) / len(pref_source))
    preferred = max(earliest, min(latest, preferred))
    return Visit(
        id=vid,
        recipient_id=recipient.id,
        intervention_ids=[i.id for i in group],
        total_duration_minutes=sum(i.duration_minutes for i in group),
        location_id=recipient.location_id,
        earliest_start=earliest,
        latest_start=latest,
        preferred_start=preferred,
        timing=TimingKind.HARD if hard else TimingKind.SOFT,
        required_skills=sorted({s for i in group for s in i.required_skills}),
        required_delegations=sorted({d for i in group for d in i.required_delegations}),
        required_employee_count=2 if any(i.requires_double_staffing for i in group) else 1,
        priority=max(i.priority for i in group),
        continuity_preference=recipient.continuity_weight,
        status=VisitStatus.UNPLANNED,
        slot=str(group[0].source.get("slot", "")),
    )


def group_interventions(items: list[Intervention]) -> list[list[Intervention]]:
    items = sorted(items, key=lambda i: (i.preferred_time, i.id))
    groups: list[list[Intervention]] = []
    cur: list[Intervention] = []
    lo, hi, anchor = 0, 24 * 60, 0
    for it in items:
        if cur:
            nlo, nhi = max(lo, it.earliest_start), min(hi, it.latest_start)
            duration = sum(i.duration_minutes for i in cur) + it.duration_minutes
            if (
                abs(it.preferred_time - anchor) <= MAX_ANCHOR_GAP
                and nlo <= nhi
                and len(cur) < MAX_INTERVENTIONS
                and duration <= MAX_DURATION
            ):
                cur.append(it)
                lo, hi = nlo, nhi
                continue
            groups.append(cur)
        cur = [it]
        lo, hi, anchor = it.earliest_start, it.latest_start, it.preferred_time
    if cur:
        groups.append(cur)
    return groups


def build_visits(
    recipients: dict[str, CareRecipient], interventions: dict[str, Intervention]
) -> dict[str, Visit]:
    by_recipient: dict[str, list[Intervention]] = {}
    for it in interventions.values():
        by_recipient.setdefault(it.recipient_id, []).append(it)
    visits: dict[str, Visit] = {}
    n = 0
    for rid in sorted(by_recipient):
        for group in group_interventions(by_recipient[rid]):
            n += 1
            vid = f"V-{n:04d}"
            visits[vid] = _make_visit(vid, recipients[rid], group)
    return visits
