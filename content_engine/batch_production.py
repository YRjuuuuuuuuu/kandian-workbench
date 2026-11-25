"""User-selected batches enter the existing persistent, bounded production queue."""
import json
import re
from .engine import iso, uid
from .store import dumps


def submit(agent, body):
    account_id = body.get("account_id", "main")
    from .content_depth import normalize
    depth = normalize(body.get("content_depth", agent.config(account_id)["content_depth"]))
    keys = body.get("candidate_ids")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 10 or any(not isinstance(k, str) for k in keys) or len(keys) != len(set(keys)):
        raise ValueError("每批选择 1–10 个不同选题")
    batch_id = body.get("batch_id") or uid()
    if not isinstance(batch_id, str) or not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise ValueError("批次标识无效")
    candidates = [agent.candidate(key) for key in keys]
    if any(c["account_id"] != account_id for c in candidates):
        raise ValueError("一批只能生产当前账号的选题")
    with agent.batch_lock:
        existing = agent.store.one("SELECT value FROM settings WHERE key=?", ("batch:" + batch_id,))
        if existing:
            result = json.loads(existing["value"])
            if result["account_id"] != account_id or result["candidate_ids"] != keys:
                raise ValueError("批次标识已用于其他请求")
            return result
        result = {"batch_id": batch_id, "account_id": account_id, "content_depth": depth, "candidate_ids": keys, "created_at": iso(), "submitted": [], "skipped": [], "errors": []}
        for candidate in candidates:
            try:
                if candidate["article_id"]:
                    result["skipped"].append({"candidate_id": candidate["id"], "article_id": candidate["article_id"], "reason": "已有稿件"})
                    continue
                run = agent.decide(candidate["id"], {"decision": "select", "ai": body.get("ai", {}), "content_depth": depth})
                result["submitted"].append({"candidate_id": candidate["id"], "run_id": run["id"], "existing": run.get("existing", False)})
            except ValueError as exc:
                result["errors"].append({"candidate_id": candidate["id"], "reason": str(exc)})
        with agent.store.db() as db:
            db.execute("INSERT INTO settings VALUES(?,?)", ("batch:" + batch_id, dumps(result)))
        return result


def recent(agent, account_id=None):
    output = []
    for row in agent.store.all("SELECT value FROM settings WHERE key LIKE 'batch:%'"):
        batch = json.loads(row["value"])
        if account_id and batch["account_id"] != account_id:
            continue
        progress = []
        for item in batch["submitted"]:
            run = agent.store.one("SELECT id,state,stage,result,error FROM agent_runs WHERE id=?", (item["run_id"],))
            if run:
                result = json.loads(run.pop("result")) if run["result"] else {}
                progress.append({**run, "article_id": result.get("article_id")})
        output.append({**batch, "progress": progress})
    return sorted(output, key=lambda x: x["created_at"], reverse=True)[:15]
