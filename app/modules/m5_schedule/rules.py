"""M5 引航排程：单向窄段的双向通行序列。

同一时刻窄段只允许一个方向占用。逐船取最早时段会让上下行在窄段相遇，
或漏掉清空就把下一批放进去，因此这里以“方向编队”为单位排程：

* 每批只能是一个方向的连续船队（船与船首尾相接，不夹空档）；
* 编队边界做有界搜索，比较“继续同向”与“清空换向”的代价，按
  （失约船数, 已进段船被扣停秒数, 总等待秒数, 换向次数, 编队数）取小；
* 换向准备固定为：确认本向全部驶出 → 瞭望复核无对向船 → 引航员换乘 →
  发布对向放行许可；这段清空等待本身占用航道，登记为换向占班；
* 未进段的船在入口侧锚地/编队位等待，不进入窄段长时间停船。

因此赶潮窗的重载上行船可以插到下行编队之前；若先送完下行会让它错失潮窗，
失约数变大，搜索会自动避开这种序列。
"""
from app.modules.m4_channel.rules import subtract
from app.report import report
from app.config import CHANGEOVER_SECONDS

DIRECTIONS = ("up", "down")
LABEL = {"up": "上行", "down": "下行"}
WAIT_UP = "下游待泊锚地"
WAIT_DOWN = "上游待泊锚地"
WAIT_ENTRY = "窄段入口编队位"
WAIT_NONE = "直进窄段"
WAIT_MISS = "无法放行"
NODE_BUDGET = 20000


def first_fit(windows, pilots, duration, occupied):
    candidates = []
    for a, b in windows:
        for p, q in pilots:
            lo, hi = max(a, p), min(b, q)
            if lo < hi:
                for x, y in subtract((lo, hi), occupied):
                    if y - x >= duration:
                        candidates.append((x, x + duration))
    return min(candidates) if candidates else None


def feasible_starts(windows, pilots, duration):
    """可放行时刻的闭区间列表（已扣除航次时长，保证整段航次落在窗口内）。"""
    ranges = []
    for a, b in windows:
        for p, q in pilots:
            lo, hi = max(a, p), min(b, q)
            if hi - lo >= duration:
                ranges.append((lo, hi - duration))
    return ranges


def next_start(ranges, t):
    """不早于 t 的最早可行放行时刻；无解返回 None。"""
    best = None
    for lo, hi in ranges:
        x = max(lo, t)
        if x <= hi and (best is None or x < best):
            best = x
    return best


def build_items(ships, pilots):
    """把船舶申请投影为排程条目。

    earliest/deadline 为潮窗与引航窗口求交、再扣除航次时长后的最早/最晚
    放行时刻，是硬约束；ranges 为空表示没有可用安全窗。
    """
    items = []
    for ship in ships:
        windows = [(w["start_s"], w["end_s"]) for w in ship["_windows"]]
        ranges = feasible_starts(windows, pilots, ship["duration"])
        items.append({
            "id": ship["id"],
            "direction": ship.get("direction") or "up",
            "a": ship["a"], "b": ship["b"],
            "duration": ship["duration"], "line": ship["line"],
            "ranges": ranges,
            "earliest": min((lo for lo, _ in ranges), default=None),
            "deadline": max((hi for _, hi in ranges), default=None),
            "in_section": False,
        })
    return items


def mark_in_section(items):
    """标记开排时已经在窄段内的下行船（内港下行船已离泊）。

    下行船申请开始即离泊；只有最早离泊的那艘可能已压进窄段，且其申请
    时刻不晚于全港最早可放行时刻才算“已在段内”。其余同向船在入口侧
    锚地等待。代价函数对在段船舶的扣停时间计罚，避免让赶潮上行船先走、
    把已进段的下行船长时间扣在段内。
    """
    live = [it for it in items if it["earliest"] is not None]
    if not live:
        return set()
    opening = min(it["earliest"] for it in live)
    candidates = sorted((it for it in live
                         if it["direction"] == "down" and it["a"] <= opening),
                        key=lambda it: (it["a"], it["id"]))
    if not candidates:
        return set()
    lead = candidates[0]
    lead["in_section"] = True
    return {lead["id"]}


def _urgency(item):
    deadline = item["deadline"]
    return (deadline is None, deadline if deadline is not None else 0, item["a"], item["id"])


def _pending(items, pending, direction=None):
    return [it for it in items if it["id"] in pending
            and (direction is None or it["direction"] == direction)]


def _convoy_options(items, pending, direction, start):
    """该方向从 start 起可连续放行的编队前缀。

    每一步选“最早能走”的船，因此早到早开的船不会被后开窗的船堵住；
    编队必须首尾相接，遇到空档即停，空档留给对向换向，不夹在编队中间。
    """
    placed = []
    t = start
    remaining = sorted(_pending(items, pending, direction), key=_urgency)
    while remaining:
        best = None
        for item in remaining:
            begin = next_start(item["ranges"], t)
            if begin is None:
                continue
            key = (begin, _urgency(item))
            if best is None or key < best[0]:
                best = (key, item, begin)
        if best is None:
            break
        _, item, begin = best
        if placed and begin != t:
            break
        placed.append((item, begin))
        t = begin + item["duration"]
        remaining.remove(item)
    return [placed[:n] for n in range(1, len(placed) + 1)]


def _cost(state):
    missed, held, wait, changes, turns = state
    return (missed, held, wait, changes, turns)


def _better(a, b):
    return b is None or _cost(a) < _cost(b)


def _greedy(items, changeover):
    """先构造一个可行上界：每次选最紧迫方向、整队放行。"""
    pending = {it["id"] for it in items if it["ranges"]}
    plan, cursor, current = [], 0, None
    held = wait = changes = turns = 0
    while pending:
        best = None
        for direction in DIRECTIONS:
            if not _pending(items, pending, direction):
                continue
            switching = current is not None and direction != current
            start = cursor + changeover if switching else cursor
            options = _convoy_options(items, pending, direction, start)
            if not options:
                continue
            lead = _urgency(options[0][0][0])
            if best is None or lead < best[0]:
                best = (lead, direction, switching, options[-1])
        if best is None:
            break
        _, direction, switching, batch = best
        t = cursor
        if switching:
            plan.append(("changeover", current, direction, cursor, cursor + changeover))
            t = cursor + changeover
            changes += 1
        turns += 1
        for item, begin in batch:
            plan.append(("ship", direction, item, begin, begin + item["duration"]))
            waited = begin - item["earliest"]
            wait += int(waited)
            if item["in_section"]:
                held += int(waited)
            pending.discard(item["id"])
            t = begin + item["duration"]
        cursor, current = t, direction
    return plan, (len(pending), held, wait, changes, turns)


def search_schedule(items, changeover=CHANGEOVER_SECONDS):
    """有界搜索方向编队序列，返回 (plan, cost)。先取贪心上界再改进。"""
    best_plan, best_cost = _greedy(items, changeover)
    live = {it["id"] for it in items if it["ranges"]}
    memo = {}
    budget = [NODE_BUDGET]

    def dfs(pending, current, cursor, plan, held, wait, changes, turns):
        nonlocal best_plan, best_cost
        if budget[0] <= 0:
            return
        budget[0] -= 1
        state = (0, held, wait, changes, turns) if not pending else None
        if state is not None:
            if _better(state, best_cost):
                best_cost, best_plan = state, list(plan)
            return
        key = (frozenset(pending), current, cursor)
        floor = (held, wait, changes, turns)
        seen = memo.get(key)
        if seen is not None and seen <= floor:
            return
        memo[key] = floor
        if _cost((0,) + floor) >= best_cost:
            return

        moves = []
        for direction in DIRECTIONS:
            if not _pending(items, pending, direction):
                continue
            switching = current is not None and direction != current
            start = cursor + changeover if switching else cursor
            for batch in _convoy_options(items, pending, direction, start):
                moves.append((direction, switching, batch))
        moves.sort(key=lambda m: (_urgency(m[2][0][0]), m[1]))
        for direction, switching, batch in moves:
            remaining = set(pending)
            t = cursor
            next_plan = list(plan)
            if switching:
                next_plan.append(("changeover", current, direction, cursor,
                                  cursor + changeover))
                t = cursor + changeover
            extra_held = extra_wait = 0
            for item, begin in batch:
                next_plan.append(("ship", direction, item, begin,
                                  begin + item["duration"]))
                waited = begin - item["earliest"]
                extra_wait += int(waited)
                if item["in_section"]:
                    extra_held += int(waited)
                remaining.discard(item["id"])
                t = begin + item["duration"]
            dfs(frozenset(remaining), direction, t, next_plan,
                held + extra_held, wait + extra_wait,
                changes + (1 if switching else 0), turns + 1)

    dfs(frozenset(live), None, 0, [], 0, 0, 0, 0)
    return best_plan, best_cost


def _miss(item, reason):
    return {
        "id": item["id"], "direction": item["direction"],
        "direction_label": LABEL[item["direction"]],
        "status": "NO_SAFE_SLOT", "reason": reason,
        "start_s": None, "end_s": None, "line": item["line"], "turn": None,
        "window_open_s": item["earliest"], "window_latest_s": item["deadline"],
        "window_slack_s": None, "wait_s": 0, "wait_position": WAIT_MISS,
        "changeover_before_s": 0, "in_section": item["in_section"],
        "release_note": "无安全潮窗" if reason == "NO_SAFE_WINDOW" else "潮窗已被前序占用",
    }


def _wait_position(direction, index, wait_s, changeover_before, in_section):
    if index > 0:
        return WAIT_ENTRY
    if changeover_before or wait_s > 0:
        if in_section:
            return "窄段内待避区"
        return WAIT_UP if direction == "up" else WAIT_DOWN
    return WAIT_NONE


def _changeover_row(seq, current, chosen, start, changeover):
    return {
        "seq": seq,
        "from": current, "to": chosen,
        "from_label": LABEL[current], "to_label": LABEL[chosen],
        "start_s": start, "end_s": start + changeover,
        "duration_s": changeover, "status": "CLEARED",
        "preparation": [
            f"确认{LABEL[current]}方向全部船舶驶出窄段",
            "确认窄段内无对向船，雷达/瞭望复核",
            "引航员换乘与系缆检查",
            f"发布{LABEL[chosen]}方向放行许可",
        ],
    }


def _lane(occupancy):
    """把占用条投影到百分比时间轴，供工作台画出窄段独占带。"""
    if not occupancy:
        return {"extent": None, "bars": []}
    lo = min(r["start_s"] for r in occupancy)
    hi = max(r["end_s"] for r in occupancy)
    span = hi - lo
    bars = []
    for row in sorted(occupancy, key=lambda r: (r["start_s"], r["end_s"])):
        left = float((row["start_s"] - lo) / span * 100) if span else 0.0
        width = float((row["end_s"] - row["start_s"]) / span * 100) if span else 100.0
        bars.append(dict(row, left_pct=round(left, 4), width_pct=round(width, 4)))
    return {"extent": [lo, hi], "bars": bars}


def render_schedule(plan, changeover=CHANGEOVER_SECONDS):
    """把序列展开为逐船放行单、编队、换向清空与占用条。"""
    assigned, turns, changeovers, occupancy = [], [], [], []
    pending_changeover = None
    turn_no = 0
    members = []
    direction = None

    def close_turn():
        nonlocal turn_no, members, direction
        if not members:
            return
        turn_no += 1
        turns.append({
            "seq": turn_no, "direction": direction,
            "direction_label": LABEL[direction],
            "ship_ids": [m["id"] for m in members],
            "start_s": members[0]["start_s"],
            "end_s": members[-1]["end_s"],
            "changeover_s": members[0]["changeover_before_s"],
            "changeover_from": (changeovers[-1]["from"]
                                if members[0]["changeover_before_s"] else None),
        })
        members = []

    for step in plan:
        if step[0] == "changeover":
            close_turn()
            _, frm, to, start, end = step
            changeovers.append(_changeover_row(len(changeovers) + 1, frm, to,
                                               start, changeover))
            occupancy.append({
                "kind": "CHANGEOVER", "direction": "changeover",
                "start_s": start, "end_s": end,
                "label": f"换向 {LABEL[frm]}→{LABEL[to]}",
            })
            pending_changeover = changeover
            direction = to
            continue
        _, dir_now, item, begin, end = step
        # 方向变化或编队内出现空档都另起一批：空档对应对向换向或候潮。
        if direction != dir_now or (members and begin != members[-1]["end_s"]):
            close_turn()
            direction = dir_now
        changeover_before = pending_changeover or 0
        pending_changeover = None
        wait_s = begin - item["earliest"] if item["earliest"] is not None else 0
        row = {
            "id": item["id"], "direction": dir_now,
            "direction_label": LABEL[dir_now],
            "status": "SCHEDULED",
            "start_s": begin, "end_s": end,
            "line": item["line"], "turn": turn_no + 1,
            "window_open_s": item["earliest"], "window_latest_s": item["deadline"],
            "window_slack_s": (item["deadline"] - begin)
            if item["deadline"] is not None else None,
            "wait_s": wait_s,
            "wait_position": _wait_position(dir_now, len(members), wait_s,
                                            changeover_before, item["in_section"]),
            "changeover_before_s": changeover_before,
            "in_section": item["in_section"],
            "release_note": "换向清空后放行" if changeover_before
            else ("同向编队跟进" if members else "潮窗起点放行"),
        }
        assigned.append(row)
        members.append(row)
        occupancy.append({
            "kind": "PASSAGE", "direction": dir_now, "id": item["id"],
            "start_s": begin, "end_s": end,
            "label": f"{LABEL[dir_now]} {item['id']}",
        })
    close_turn()
    return assigned, turns, changeovers, occupancy


def schedule_passages(ships, pilots, changeover=CHANGEOVER_SECONDS):
    """生成可执行的双向通行序列；未排船给出失约原因。"""
    items = build_items(ships, pilots)
    mark_in_section(items)
    plan, cost = search_schedule(items, changeover)
    assigned, turns, changeovers, occupancy = render_schedule(plan, changeover)
    scheduled = {row["id"] for row in assigned}
    for item in items:
        if item["id"] in scheduled:
            continue
        assigned.append(_miss(item, "NO_SAFE_WINDOW" if not item["ranges"] else "WINDOW_MISSED"))
    return {
        "assigned": assigned,
        "turns": turns,
        "changeovers": changeovers,
        "occupancy": sorted(occupancy, key=lambda r: (r["start_s"], r["end_s"])),
        "lane": _lane(occupancy),
        "cost": {"失约": cost[0], "进段扣停秒": cost[1], "等待秒": cost[2],
                 "换向次数": cost[3]},
    }


plan_passages = schedule_passages


def apply(ctx):
    pilots = [(r["a"], r["b"]) for r in ctx["effective"] if r["kind"] == "pilot"]
    ships = []
    for r in sorted((r for r in ctx["effective"] if r["kind"] == "ship"),
                    key=lambda r: (r["a"], r["id"])):
        ship = dict(r)
        ship["_windows"] = [w for w in ctx["channel_windows"] if w["id"] == r["id"]]
        ships.append(ship)
    result = schedule_passages(ships, pilots)
    # assigned 保持申请顺序（同刻按船号），执行顺序见 turns 与 final。
    order = {r["id"]: index for index, r in enumerate(
        sorted((r for r in ctx["effective"] if r["kind"] == "ship"),
               key=lambda r: (r["a"], r["id"])))}
    ctx["assigned"] = sorted(result["assigned"], key=lambda r: order[r["id"]])
    ctx["turns"] = result["turns"]
    ctx["changeovers"] = result["changeovers"]
    ctx["direction_occupancy"] = result["occupancy"]
    ctx["lane"] = result["lane"]
    ctx["schedule_cost"] = result["cost"]
    ctx["unplaced"] = [r for r in result["assigned"] if r["status"] != "SCHEDULED"]
    return report(result["assigned"],
                  已排=sum(r["status"] == "SCHEDULED" for r in result["assigned"]),
                  待排=len(ctx["unplaced"]),
                  编队=len(result["turns"]),
                  换向=len(result["changeovers"]))
