"""Mode-neutral writing depth, saved with each production request."""
import math

DEFAULT = "rich"
LEVELS = {
    "compact": {"name": "精简", "min_new_facts": 2, "max_new_facts": 3, "max_sources": 2, "paragraphs": "约四至七段", "focus": "尽快交代核心事实，解释一个最值得关注的变化。"},
    "standard": {"name": "标准", "min_new_facts": 4, "max_new_facts": 6, "max_sources": 3, "paragraphs": "约七至十一段", "focus": "写清事实进程、关键细节与两至三个有依据的解释，给出具体的阅读增量。"},
    "rich": {"name": "丰富", "min_new_facts": 6, "max_new_facts": 9, "max_sources": 4, "paragraphs": "约十至十五段", "focus": "围绕主问题展开进程、人物或队伍作用、已有材料支持的背景和多角度解释；比较其他可能解释，收束到具体看点。"},
    "deep": {"name": "深度", "min_new_facts": 9, "max_new_facts": 14, "max_sources": 6, "paragraphs": "约十四至二十段", "focus": "围绕一个主问题形成完整论证：关键转折、主体作用、可核对的背景或对比、另一种解释及下一步看点；配合有依据的数据图表，避免流水账和重复凑字。"},
}


def normalize(value=None):
    value = DEFAULT if value is None else value
    if not isinstance(value, str) or value not in LEVELS:
        raise ValueError("内容丰富程度请选择精简、标准、丰富或深度")
    return value


def plan(template, value=None, fact_count=0):
    level = normalize(value)
    low, high = template["min_chars"], template["max_chars"]
    rounded = lambda n: int(math.ceil(n / 50) * 50)
    if level == "standard":
        low = max(650, rounded(low * 1.25))
        high = max(low + 400, rounded(high * 1.25))
    elif level == "rich":
        low = max(1000, rounded(low * 1.6))
        high = max(low + 500, rounded(high * 1.65))
    elif level == "deep":
        low = max(1800, rounded(low * 2.2))
        high = max(low + 900, rounded(high * 2.4))
    high = min(6000, high)
    low = min(low, high)
    return {"level": level, **LEVELS[level], "target_min_chars": low, "target_max_chars": high, "available_facts": fact_count, "policy": "目标篇幅按稿型动态计算。优先展开已有事实及其关系；缺少的统计、背景和引语不得补编。同一意思不重复，空话不算丰富。材料不足时缩短并在notes说明，读者正文不塞审核提醒。"}
