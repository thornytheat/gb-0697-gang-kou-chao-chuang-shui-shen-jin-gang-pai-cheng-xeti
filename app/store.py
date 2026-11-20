import sqlite3, json, hashlib
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS run_versions (run_id TEXT NOT NULL, revision INTEGER NOT NULL, result TEXT NOT NULL, decision TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(run_id, revision))",
    "CREATE TABLE IF NOT EXISTS occupancy (run_id TEXT NOT NULL, slot TEXT NOT NULL, kind TEXT NOT NULL, direction TEXT NOT NULL, ship TEXT NOT NULL DEFAULT '', start_s TEXT NOT NULL, end_s TEXT NOT NULL, PRIMARY KEY(run_id, slot))",
)


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "runs.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    for statement in SCHEMA:
        db.execute(statement)
    return db


def slots(result):
    """把排程结果投影为航道占班槽位；槽位键确定性生成，重跑不会新增重复占班。"""
    out = []
    for bar in result.get("direction_occupancy", []):
        ident = bar.get("id") or bar.get("label") or ""
        key = f"{bar['kind']}:{bar['direction']}:{bar['start_s']}:{bar['end_s']}:{ident}"
        out.append((key, bar["kind"], bar["direction"], bar.get("id") or "",
                    str(bar["start_s"]), str(bar["end_s"])))
    return out


def sync_occupancy(db, ident, result):
    desired = slots(result)
    for slot, kind, direction, ship, start_s, end_s in desired:
        db.execute("INSERT OR IGNORE INTO occupancy(run_id,slot,kind,direction,ship,start_s,end_s) VALUES(?,?,?,?,?,?,?)",
                   (ident, slot, kind, direction, ship, start_s, end_s))
    keep = {row[0] for row in desired}
    stale = [r["slot"] for r in db.execute("SELECT slot FROM occupancy WHERE run_id=?", (ident,))
             if r["slot"] not in keep]
    for slot in stale:
        db.execute("DELETE FROM occupancy WHERE run_id=? AND slot=?", (ident, slot))


def occupancy(ident):
    with connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT slot,kind,direction,ship,start_s,end_s FROM occupancy WHERE run_id=?", (ident,))]
    rows.sort(key=lambda r: (float(r["start_s"]), r["slot"]))
    return rows


def _snapshot(db, ident, revision, result, decision, created):
    """保存修订快照；已带规则版本的旧结果保持原版本，不冒充当前规则。"""
    payload = dict(result)
    payload.setdefault("rule_version", RULE_VERSION)
    db.execute("INSERT OR REPLACE INTO run_versions(run_id,revision,result,decision,created) VALUES(?,?,?,?,?)",
               (ident, revision, json.dumps(payload, ensure_ascii=False), decision, created))


def save(text, result):
    digest = hashlib.sha256((RULE_VERSION + "\n" + text).encode()).hexdigest()
    ident = digest[:16]
    created = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",
                   (ident, digest, json.dumps(dict(result, rule_version=RULE_VERSION), ensure_ascii=False), created))
        row = db.execute("SELECT result,revision,decision,created FROM runs WHERE id=?", (ident,)).fetchone()
        _snapshot(db, ident, row["revision"], json.loads(row["result"]), row["decision"], row["created"])
        sync_occupancy(db, ident, result)
    return get(ident)


def get(ident):
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise BusinessError("NOT_FOUND", "批次不存在", 404)
    item = dict(row)
    item["result"] = backfill(json.loads(item["result"]))
    item["rule_version"] = item["result"].get("rule_version", "2026.11.18.1")
    return item


def backfill(result):
    """旧版批次缺双向排程字段时补空，保证工作台与证据导出可读。"""
    defaults = {
        "turns": [], "changeovers": [], "direction_occupancy": [], "lane_spans": [],
        "lane_conflicts": [], "conflicts": [],
        "lane": {"extent": None, "bars": []},
    }
    for key, value in defaults.items():
        result.setdefault(key, value)
    if result.get("schedule_cost") is None and any(
            r.get("direction") for r in result.get("assigned", [])):
        result["schedule_cost"] = {"失约": 0, "进段扣停秒": 0, "等待秒": 0, "换向次数": 0}
    return result


def versions(ident):
    with connect() as db:
        if db.execute("SELECT 1 FROM runs WHERE id=?", (ident,)).fetchone() is None:
            raise BusinessError("NOT_FOUND", "批次不存在", 404)
        return [dict(r) for r in db.execute(
            "SELECT revision,decision,created FROM run_versions WHERE run_id=? ORDER BY revision DESC",
            (ident,))]


def history():
    with connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]


def review(ident, revision, decision):
    if not isinstance(decision, str) or decision not in {"通过", "退回"}:
        raise BusinessError("DECISION", "复核决定无效")
    current = get(ident)
    if decision == "通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED", "存在阻断项，请先修正数据", 409)
    created = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        changed = db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",
                             (decision, ident, revision)).rowcount
        if not changed:
            raise BusinessError("STALE", "版本已更新，请刷新", 409)
        _snapshot(db, ident, revision + 1, current["result"], decision, created)
    return get(ident)


def restore(ident, revision):
    """中断或复核退回后，恢复指定旧修订的排程序列。

    只回放旧结果，不重新计算；占班槽位按结果幂等同步，因此恢复多次也不会
    重复生成等待占班。
    """
    if type(revision) is not int:
        raise BusinessError("REVISION", "版本须为整数")
    current = get(ident)
    if revision < 0 or revision >= current["revision"]:
        raise BusinessError("STALE", "仅能恢复早于当前修订的序列", 409)
    with connect() as db:
        row = db.execute("SELECT result,decision FROM run_versions WHERE run_id=? AND revision=?",
                         (ident, revision)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "该修订快照不存在", 404)
        result = json.loads(row["result"])
        created = datetime.now(timezone.utc).isoformat()
        changed = db.execute("UPDATE runs SET result=?,revision=revision+1,decision=? WHERE id=? AND revision=?",
                             (json.dumps(result, ensure_ascii=False), "恢复旧版", ident, current["revision"])).rowcount
        if not changed:
            raise BusinessError("STALE", "版本已更新，请刷新", 409)
        _snapshot(db, ident, current["revision"] + 1, result, "恢复旧版", created)
        sync_occupancy(db, ident, result)
    return get(ident)
