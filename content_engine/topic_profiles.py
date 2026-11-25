"""Mode-owned editorial briefs; the UI selects a type instead of writing prompts."""
import copy


def writing_mode(mode):
    """Pass writing policy, not every discovery profile and keyword dictionary."""
    keys = ("id", "name", "version", "description", "audience", "style", "editorial_rules", "blocked_keywords")
    return {key: copy.deepcopy(mode[key]) for key in keys if key in mode}


def resolve(mode, profile_id=None, scope_id=None):
    agent = mode.get("agent", {})
    profiles = agent.get("topic_profiles", [])
    scopes = agent.get("sport_scopes", [])
    if not profiles:
        return {"id": "custom", "name": mode["name"], "prompt": agent.get("focus", mode["description"]), "template_id": mode["templates"][0]["id"], "min_facts": 1, "max_queries": 5, "scope": {"id": "all", "name": mode["name"], "keywords": agent.get("crawler_keywords", [])}}
    profile_id = profile_id or agent.get("default_topic_profile") or profiles[0]["id"]
    scope_id = scope_id or agent.get("default_sport_scope") or scopes[0]["id"]
    profile = next((p for p in profiles if p["id"] == profile_id), None)
    scope = next((s for s in scopes if s["id"] == scope_id), None)
    if not profile or not scope:
        raise ValueError("选题类型或体育项目不存在")
    return {**copy.deepcopy(profile), "scope": copy.deepcopy(scope)}
