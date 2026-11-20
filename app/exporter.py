import csv, io, json


def evidence_csv(run):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["run_id", "sha256", "rule_version", "kind", "line", "reason", "evidence"])
    summary = {
        "balance": run["result"]["balance"],
        "decision": run["decision"],
        "revision": run["revision"],
        "schedule_cost": run["result"].get("schedule_cost"),
        "turns": len(run["result"].get("turns", [])),
        "changeovers": len(run["result"].get("changeovers", [])),
    }
    writer.writerow([run["id"], run["digest"], run["rule_version"], "manifest", "", "",
                     json.dumps(summary, ensure_ascii=False)])
    for turn in run["result"].get("turns", []):
        writer.writerow([run["id"], run["digest"], run["rule_version"], "turn",
                         turn.get("seq", ""), turn.get("direction_label", ""),
                         json.dumps(turn, ensure_ascii=False)])
    for row in run["result"].get("changeovers", []):
        writer.writerow([run["id"], run["digest"], run["rule_version"], "changeover",
                         row.get("seq", ""), row.get("status", ""),
                         json.dumps(row, ensure_ascii=False)])
    for row in run["result"].get("direction_occupancy", []):
        writer.writerow([run["id"], run["digest"], run["rule_version"], "occupancy",
                         row.get("id", ""), row.get("direction", ""),
                         json.dumps(row, ensure_ascii=False)])
    for row in run["result"].get("final", []):
        writer.writerow([run["id"], run["digest"], run["rule_version"], "result",
                         row.get("line", ""), row.get("status", ""),
                         json.dumps(row, ensure_ascii=False)])
    for row in run["result"].get("quarantine", []):
        writer.writerow([run["id"], run["digest"], run["rule_version"], "quarantine",
                         row.get("line", ""), row["reason"], json.dumps(row, ensure_ascii=False)])
    return "﻿" + out.getvalue()
