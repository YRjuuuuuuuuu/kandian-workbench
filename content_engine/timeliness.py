"""Mode-configured event-day policy. Publication time never substitutes event time."""
import datetime as dt
import re

CN = dt.timezone(dt.timedelta(hours=8))


def has_result_fact(facts):
    """A final flag plus player statistics is not a supported match result."""
    pattern = r"\d+\s*(?:比|[-–:])\s*\d+|击败|战胜|获胜|晋级|夺冠|落败|失利|\b(?:defeats?|beats?|wins?|won)\b"
    return any((fact.get("verified") or fact.get("agent_check", {}).get("status") in ("crosschecked", "attributed")) and re.search(pattern, fact.get("text", ""), re.I) for fact in facts)


def parse(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=CN) if parsed.tzinfo is None else parsed


def applies(mode, data):
    policy = mode.get("agent", {}).get("timeliness", {})
    return policy.get("same_day_events") is True and data.get("template_id") in policy.get("event_templates", [])


def event_day(data):
    if data.get("event_end_at"):
        return parse(data["event_end_at"]).astimezone(CN).date()
    explicit = data.get("event_date_cn")
    if explicit:
        return dt.date.fromisoformat(explicit)
    return parse(data["event_at"]).astimezone(CN).date()


def error(mode, data, current):
    if not applies(mode, data):
        return None
    try:
        day = event_day(data)
    except (ValueError, TypeError, KeyError):
        return "赛事北京时间日期无效，不能判断当天时效"
    today = current.astimezone(CN).date()
    if data.get("event_date_cn") and data.get("event_end_at") and dt.date.fromisoformat(data["event_date_cn"]) != day:
        return "赛事北京时间日期与完赛时间冲突，请核对原始赛程"
    if day != today:
        return f"当天赛事规则：赛事北京时间日期为 {day}，当前为 {today}；昨天赛事不能作为今天的赛果报道"
    if data.get("event_end_at") and parse(data["event_end_at"]) > current:
        return "比赛尚未正式结束，不能生成最终赛果"
    return None


def deadline(mode, data, proposed, current):
    if not applies(mode, data):
        return proposed
    day = event_day(data)
    midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), CN)
    return min(proposed, midnight)
