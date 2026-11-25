"""One research task covers changing scopes and article types."""
import copy
from .topic_profiles import resolve


def defaults(mode):
    agent = mode.get("agent", {})
    return {"schedule_adaptive": True, "schedule_profile_ids": [p["id"] for p in agent.get("topic_profiles", [])], "schedule_scope_ids": [s["id"] for s in agent.get("sport_scopes", []) if s["id"] != "all"]}


def validate(mode, config):
    for key, collection in (("schedule_profile_ids", "topic_profiles"), ("schedule_scope_ids", "sport_scopes")):
        allowed = {p["id"] for p in mode.get("agent", {}).get(collection, [])}
        values = config.get(key, [])
        if allowed and (not isinstance(values, list) or not values or len(values) != len(set(values)) or set(values) - allowed):
            raise ValueError("请选择有效且不重复的定时选题范围与稿件类型")


def portfolio(mode, config, primary_scope=None):
    validate(mode, config)
    agent = mode.get("agent", {})
    if not agent.get("topic_profiles") or not agent.get("sport_scopes"):
        return resolve(mode)
    scopes = [copy.deepcopy(s) for s in agent["sport_scopes"] if s["id"] in config["schedule_scope_ids"]]
    scopes.sort(key=lambda s: s["id"] != primary_scope)
    profiles = [copy.deepcopy(p) for p in agent["topic_profiles"] if p["id"] in config["schedule_profile_ids"]]
    return {"id": "adaptive", "name": "智能组合选题", "template_id": profiles[0]["template_id"], "min_facts": 1, "lookback_hours": max(p.get("lookback_hours", 12) for p in profiles), "max_queries": 5, "scope": {"id": "all", "name": "多方向 · " + "／".join(s["name"] for s in scopes), "keywords": list(dict.fromkeys(k for s in scopes for k in s["keywords"]))}, "scopes": scopes, "profiles": profiles, "prompt": "根据北京时间、真实热榜与赛程动态选稿。优先高关注赛事和人物；没有今日热门赛果时，转向近期运动员表现、球队观察、赛事看点或规则解读，不强行寻找冷门联赛凑快报。NBA尚未开赛不等于没有题可写。赛果限北京当天完赛；观察解读可用近期材料并标日期。尽量覆盖不同方向和稿型，同一事件只保留一个最合适角度，不为凑类别降低证据要求。"}


def for_candidate(mode, plan, raw):
    if plan.get("id") != "adaptive":
        return plan
    profile = next((p for p in plan["profiles"] if p["template_id"] == raw.get("template_id")), None)
    if not profile:
        raise ValueError("候选稿型不在本轮智能组合范围内")
    corpus = " ".join([str(raw.get("title", "")), str(raw.get("summary", "")), *[str(f.get("text", "")) for f in raw.get("facts", [])], *[str(s.get("title", "")) for s in raw.get("sources", [])]])
    scope = next((s for s in plan["scopes"] if s["id"] == raw.get("scope_id")), None)
    if raw.get("scope_id") and not scope:
        raise ValueError("候选运动项目不在本轮组合范围内")
    if not scope:
        scores = [(sum(len(k) for k in s["keywords"] if k.casefold() in corpus.casefold()), s) for s in plan["scopes"]]
        score, inferred = max(scores, key=lambda x: x[0])
        scope = inferred if score else plan["scope"]
    return {**copy.deepcopy(profile), "scope": copy.deepcopy(scope)}


def diversify(candidates, maximum):
    remaining, chosen = list(candidates), []
    while remaining and len(chosen) < maximum:
        scopes = {c["profile"]["scope"]["id"] for c in chosen}
        types = {c["template_id"] for c in chosen}
        best = max(remaining, key=lambda c: c["editorial_score"] + (6 if c["template_id"] not in types else 0) + (4 if c["profile"]["scope"]["id"] not in scopes else 0))
        chosen.append(best)
        remaining.remove(best)
    return chosen
