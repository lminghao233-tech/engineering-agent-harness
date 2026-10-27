import math


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def summarize(tasks, selected):
    resolved = sum(t["official_resolved"] == 1 for t in tasks)
    evaluated = sum(t["official_resolved"] is not None for t in tasks)
    return {
        "selected": selected, "submitted": len(tasks), "official_evaluated": evaluated,
        "official_resolved": resolved if evaluated else None,
        "end_to_end_resolved_rate": resolved / selected if selected and evaluated else None,
        "conditional_resolved_rate": resolved / evaluated if evaluated else None,
        "independent_tests_passed": sum(t["state"] in {"AWAITING_REVIEW", "APPROVED", "REJECTED"} for t in tasks),
        "errors": sum(t["state"] in {"FAILED", "BLOCKED"} for t in tasks),
        "unresolved": sum(t["official_resolved"] == 0 for t in tasks),
        "not_run": selected - sum(t["calls"] > 0 for t in tasks),
        "p50_seconds": percentile([t["elapsed"] for t in tasks if t["calls"]], .5),
        "p95_seconds": percentile([t["elapsed"] for t in tasks if t["calls"]], .95),
        "sample_size_timing": sum(t["calls"] > 0 for t in tasks), "cost": None,
        "uncertainty": "Exploratory frozen subset; no claim of statistically significant superiority. Unevaluated tasks are not successes.",
    }
