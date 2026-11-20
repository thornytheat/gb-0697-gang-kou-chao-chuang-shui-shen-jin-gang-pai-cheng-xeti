from app.report import report


def overlaps(rows):
    ordered = sorted((r["start_s"], r["end_s"], r["id"]) for r in rows if r["status"] == "SCHEDULED")
    return [(a[2], b[2]) for a, b in zip(ordered, ordered[1:]) if a[1] > b[0]]


def direction_conflicts(bars):
    """窄段同一时刻只能有一个方向；异向或换向清空重叠即为冲突。"""
    return [{"at_s": span["start_s"], "bar_ids": span["bars"],
             "directions": span["directions"]}
            for span in lane_signature(bars) if not span["exclusive"]]


def lane_signature(bars):
    """把时间轴切成互不重叠区间，逐段核对只有一方向占用。"""
    marks = sorted({x for bar in bars for x in (bar["start_s"], bar["end_s"])})
    spans = []
    for a, b in zip(marks, marks[1:]):
        active = [bar for bar in bars if bar["start_s"] < b and bar["end_s"] > a]
        directions = sorted({bar["direction"] for bar in active})
        if not active:
            label = "空闲"
        elif len(directions) == 1:
            label = "换向" if directions[0] == "changeover" else {
                "up": "上行", "down": "下行"}[directions[0]]
        else:
            label = "混用"
        spans.append({"start_s": a, "end_s": b, "directions": directions,
                      "label": label, "exclusive": len(directions) <= 1,
                      "bars": [bar.get("id") or bar.get("label") for bar in active]})
    return spans


def apply(ctx):
    conflicts = overlaps(ctx["assigned"])
    ctx["conflicts"] = conflicts
    bars = ctx.get("direction_occupancy", [])
    lane_conflicts = direction_conflicts(bars)
    ctx["lane_conflicts"] = lane_conflicts
    ctx["lane_spans"] = lane_signature(bars)
    ctx["balance"] = {"input": ctx["input_count"],
                      "accepted": ctx["input_count"] - len(ctx["quarantine"]) - ctx["blank_rows"],
                      "quarantined": len(ctx["quarantine"]), "blank": ctx["blank_rows"]}
    rows = [dict(r, check="CONFLICT" if any(r["id"] in c for c in conflicts) else "CLEAR")
            for r in ctx["assigned"]]
    ctx["checked"] = rows
    return report(rows, 排程冲突=len(conflicts), 航道换向冲突=len(lane_conflicts), 船舶=len(rows))
