"""Declarative modes. A new JSON directory works without changing the engine."""
import json
import math
import re
from pathlib import Path

GLOBAL_BLOCKS = ["国际政治", "军事", "近代史", "财经投资", "股票", "证券", "期货", "赌博", "博彩", "赔率", "投注"]
KNOWN_VALIDATORS = {"final_result", "original_quote"}


def validate_mode(mode):
    if not isinstance(mode, dict) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", str(mode.get("id", ""))):
        raise ValueError("模式 id 使用 2–40 位英文小写字母、数字、下划线或连字符")
    for key in ("name", "description", "audience"):
        if not isinstance(mode.get(key), str) or not mode[key].strip():
            raise ValueError(f"模式缺少 {key}")
    if not isinstance(mode.get("version"), int) or isinstance(mode["version"], bool) or mode["version"] < 1:
        raise ValueError("模式 version 必须是正整数")
    for key in ("allowed_keywords", "blocked_keywords", "editorial_rules", "slots"):
        if not isinstance(mode.get(key), list) or not all(isinstance(x, str) for x in mode[key]):
            raise ValueError(f"{key} 必须是字符串列表")
    for slot in mode["slots"]:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", slot):
            raise ValueError("排期时间必须为 HH:MM")
    templates = mode.get("templates", [])
    if not templates or len({x.get("id") for x in templates}) != len(templates):
        raise ValueError("模板不能为空且 id 必须唯一")
    for item in templates:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", str(item.get("id", ""))):
            raise ValueError("模板 id 不合法")
        if not 0 < item.get("min_chars", 0) <= item.get("max_chars", 0) <= 10000:
            raise ValueError("模板字数范围不合法")
        if not 0 < item.get("ttl_hours", 0) <= 720:
            raise ValueError("模板有效期需要在 0–720 小时之间")
        if set(item.get("validators", [])) - KNOWN_VALIDATORS:
            raise ValueError("未知检查器；可用 final_result / original_quote")
        if not isinstance(item.get("outline"), list) or not all(isinstance(x, str) for x in item["outline"]):
            raise ValueError("模板 outline 必须是字符串列表")
    weights = mode.get("scoring_weights", {})
    agent = mode.get("agent", {})
    profiles, scopes = agent.get("topic_profiles", []), agent.get("sport_scopes", [])
    if profiles:
        if not isinstance(profiles, list) or not isinstance(scopes, list) or not scopes:
            raise ValueError("选题模式需同时配置稿型和内容范围")
        profile_ids, scope_ids = set(), set()
        for profile in profiles:
            if not isinstance(profile, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", str(profile.get("id", ""))) or profile["id"] in profile_ids:
                raise ValueError("稿型 id 无效或重复")
            profile_ids.add(profile["id"])
            if not profile.get("name") or not isinstance(profile.get("prompt"), str) or not profile["prompt"].strip() or profile.get("template_id") not in {t["id"] for t in templates}:
                raise ValueError("稿型需有名称、专业提示词和有效模板")
            if type(profile.get("min_facts")) is not int or not 1 <= profile["min_facts"] <= 8:
                raise ValueError("稿型核心事实数应为 1–8")
        for scope in scopes:
            if not isinstance(scope, dict) or not scope.get("id") or scope["id"] in scope_ids or not scope.get("name") or not isinstance(scope.get("keywords"), list) or not all(isinstance(word, str) for word in scope["keywords"]):
                raise ValueError("内容范围配置无效")
            scope_ids.add(scope["id"])
        if agent.get("default_topic_profile") not in profile_ids or agent.get("default_sport_scope") not in scope_ids:
            raise ValueError("默认稿型或内容范围不存在")
    if set(weights) != {"heat", "rise", "freshness", "relevance", "value"} or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in weights.values()) or abs(sum(weights.values()) - 1) > .001:
        raise ValueError("评分权重需要含五个分项，非负且总和为 1")
    return mode


class Modes:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.reload()

    def reload(self):
        loaded = {}
        for path in sorted(self.folder.glob("*/mode.json")):
            if path.parent.name.startswith("_"):
                continue
            mode = validate_mode(json.loads(path.read_text(encoding="utf-8")))
            if path.parent.name != mode["id"] or mode["id"] in loaded:
                raise ValueError("模式目录名与 id 需一致且不能重复")
            style = path.parent / "style.md"
            mode["style"] = style.read_text(encoding="utf-8") if style.exists() else "依据事实包写作，提供明确增量，保持自然表达。"
            loaded[mode["id"]] = mode
        if not loaded:
            raise ValueError("没有可用模式")
        self.items = loaded

    def get(self, mode_id):
        if mode_id not in self.items:
            raise ValueError("模式不存在")
        return self.items[mode_id]

    def template(self, mode_id, template_id):
        for template in self.get(mode_id)["templates"]:
            if template["id"] == template_id:
                return template
        raise ValueError("该模式没有这个模板")

    def assess(self, mode_id, text):
        mode = self.get(mode_id)
        hits = [word for word in GLOBAL_BLOCKS + mode["blocked_keywords"] if word.casefold() in text.casefold()]
        relevant = not mode["allowed_keywords"] or any(word.casefold() in text.casefold() for word in mode["allowed_keywords"])
        return {"blocked": sorted(set(hits)), "relevant": relevant}
