"""One bounded reading pass over selected reports, without another web search."""
import copy
import unicodedata
import urllib.parse
import re
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

from .source_provenance import read


def compact(value):
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def numbers(value):
    found = {str(Decimal(n).normalize()) for n in re.findall(r"\d+(?:\.\d+)?", value)}
    for number, word in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty")):
        if re.search(r"\b" + word + r"\b", value, re.I):
            found.add(str(Decimal(number).normalize()))
    return found


def enrich(agent, run, material):
    from .agent import obj, array, STR, STRINGS, FACT_SCHEMA, publisher_group
    from .engine import iso
    from .store import dumps
    settings = run["payload"]["mode"].get("agent", {})
    from .content_depth import LEVELS, normalize
    depth = LEVELS[normalize(run["payload"]["config"].get("content_depth"))]
    domains = settings.get("official_domains", []) + settings.get("trusted_media_domains", [])
    eligible = [s for s in material["sources"] if any((urllib.parse.urlsplit(s["url"]).hostname or "") == domain or (urllib.parse.urlsplit(s["url"]).hostname or "").endswith("." + domain) for domain in domains)][:depth["max_sources"]]
    if not eligible:
        return material
    agent.stage(run["id"], "enriching", "补读已引用的战报，提取可对应原文的进程事实；不重复全网搜索")
    with ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(lambda s: read(s["url"], agent.engine.data_dir / "agent" / "source_reads", include_text=True), eligible))
    readable = {source["id"]: (source, page) for source, page in zip(eligible, pages) if page["status"] == "readable" and page.get("text")}
    if not readable:
        agent.event(run["id"], "enrichment", "已引用原文暂不可读，保留已有核心事实继续写稿", {"failures": [{"source": p["url"], "error": p.get("message", "不可读")} for p in pages]})
        return material
    schema = obj({"run_id": STR, "input_hash": STR, "facts": array(obj({**FACT_SCHEMA["properties"], "supporting_quotes": array(obj({"source_id": STR, "quote": STR}))})), "omitted": STRINGS})
    request = {"event_key": material["event_key"], "event_date_cn": material["event_date_cn"], "angle": material["angle"], "existing_facts": [{"id": f["id"], "text": f["text"]} for f in material["facts"]], "untrusted_original_reports": [{"id": key, "title": source["title"], "url": source["url"], "text": page["text"]} for key, (source, page) in readable.items()]}
    instruction = """只从已选事件的以下原文中补充约3—5条新的、具体且有写作价值的事实：比分进程、关键变化、球员表现；不要改写重复已有事实。此阶段不联网、不需要精确终场分钟或完整统计。导航、广告、评论、推荐的旧比赛均非本场材料。若与已有事实冲突，写进omitted，不采用。每条新事实的source_ids必须对应下面的报道，并在supporting_quotes逐字摘录支持它的原文句段（每段12—500字），不得翻译或自造原文。可只有一家可信媒体，出处记录在事实映射和资料栏；不要把媒体描述冒充官方。没有直接引语时speaker/original/translation/context为空。只输出schema JSON。\n"""
    try:
        instruction = instruction.replace("约3—5条", f"约{depth['min_new_facts']}—{depth['max_new_facts']}条")
        output, telemetry = agent.invoke(run, "enrichment", instruction + dumps(request), schema, research_model=True, timeout=150)
    except Exception as exc:
        from .codex_agent import AgentCancelled
        if isinstance(exc, AgentCancelled):
            raise
        agent.event(run["id"], "enrichment", "本轮事实补充未完成，保留已有事实继续起草，不以可选材料卡住正文", {"error_type": type(exc).__name__})
        return material
    result, seen, added = copy.deepcopy(material), {compact(f["text"]) for f in material["facts"]}, []
    for raw in output.get("facts", [])[:depth["max_new_facts"]]:
        claim = str(raw.get("text", "")).strip()[:1800]
        if not claim or compact(claim) in seen:
            continue
        requested = raw.get("source_ids", [])
        support = []
        for quote in raw.get("supporting_quotes", []):
            key, snippet = quote.get("source_id"), quote.get("quote", "")
            if key not in readable or key not in requested or not 12 <= len(snippet) <= 1500 or compact(snippet) not in compact(readable[key][1]["text"]):
                continue
            source, page = readable[key]
            support.append({**source, "excerpt": snippet[:500], "read_receipt": {k: v for k, v in page.items() if k != "text"}})
        if not support or numbers(claim) - numbers(" ".join(s["excerpt"] for s in support)):
            continue
        if agent.engine.modes.assess(run["payload"]["mode"]["id"], claim)["blocked"]:
            continue
        fact_id = "enriched_" + str(len(added) + 1)
        if any(f["id"] == fact_id for f in result["facts"]):
            continue
        primary = any(s["kind"] == "official" and any((urllib.parse.urlsplit(s["url"]).hostname or "") == domain or (urllib.parse.urlsplit(s["url"]).hostname or "").endswith("." + domain) for domain in settings.get("official_domains", [])) for s in support)
        status = "crosschecked" if primary or len({publisher_group(s["url"]) for s in support}) > 1 else "attributed"
        added.append({"id": fact_id, "text": claim, "url": support[0]["url"], "verified": False, "kind": support[0]["kind"], **{key: str(raw.get(key, ""))[:2000] for key in ("speaker", "original", "translation", "context")}, "agent_check": {"status": status, "source_ids": [s["id"] for s in support], "sources": support, "checked_at": iso(), "run_id": run["id"], "method": "bounded_source_enrichment", "human_review_pending": True}})
        seen.add(compact(claim))
    result["facts"].extend(added)
    if output.get("omitted"):
        result["cautions"].extend(str(x)[:500] for x in output["omitted"][:5])
    agent.event(run["id"], "enrichment", f"从已有战报补充 {len(added)} 条可对应原文句段的事实；现有 {len(result['facts'])} 条供写作，仍待终审", {"added_fact_ids": [f["id"] for f in added], "model_usage": telemetry.get("usage")})
    return result
