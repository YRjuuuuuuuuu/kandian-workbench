"""Independent adapter for authorized 公开热榜 JSON/CSV exports.

Does not bundle or execute 公开热榜, read cookies or retain author identities.
"""
import csv
import datetime as dt
import io
import ipaddress
import json
import math
import re
import urllib.parse

PLATFORMS = {"weibo", "bilibili", "douyin", "xhs", "kuaishou", "zhihu", "tieba", "other"}


def public_url(value):
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("素材链接需为公开 HTTPS 地址")
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith((".local", ".internal")) or "." not in hostname:
        raise ValueError("素材链接不能指向本机或私有网络")
    try:
        if not ipaddress.ip_address(hostname).is_global:
            raise ValueError("素材链接不能指向私有地址")
    except ValueError as exc:
        if "私有地址" in str(exc):
            raise
    # Strip tracking/security tokens; video/detail identifiers belong in the path.
    query = ""
    if hostname == "s.weibo.com":
        query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if k in ("q", "cate")])
    return urllib.parse.urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", query, ""))[:2000]


def timestamp(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}(?:\.\d+)?", str(value)):
            number = float(value)
            if not math.isfinite(number):
                return None
            if number > 10**11:
                number /= 1000
            return dt.datetime.fromtimestamp(number, dt.timezone.utc).isoformat()
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def count(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        string = str(value).strip().replace(",", "")
        multiplier = 10000 if string.endswith("万") else 1
        number = float(string.rstrip("万")) * multiplier
        return int(number) if math.isfinite(number) and 0 <= number < 10**14 else None
    except (ValueError, TypeError):
        return None


def normalize_records(records, platform="other"):
    if platform not in PLATFORMS:
        raise ValueError("不支持的平台标识")
    if isinstance(records, dict):
        records = records.get("data", records.get("contents", records.get("items", [])))
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("导出数据需为列表，单次最多 1000 条")
    output, seen = [], set()
    for record in records:
        if not isinstance(record, dict) or "comment_id" in record:
            continue
        link = next((record.get(key) for key in ("url", "note_url", "video_url", "content_url", "aweme_url") if record.get(key)), "")
        if not link:
            continue
        try:
            url = public_url(link)
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        title = str(record.get("title") or record.get("desc") or record.get("content") or "").strip()[:250]
        if not title:
            continue
        metrics, raw_metrics, approximate = {}, {}, []
        for key, alternatives in {"likes": ("liked_count", "digg_count", "like_count", "video_like_count"), "comments": ("comments_count", "comment_count", "video_comment_count"), "shares": ("share_count", "shared_count", "video_share_count"), "views": ("view_count", "play_count", "video_play_count"), "collects": ("collected_count", "collect_count", "video_favorite_count")}.items():
            metrics[key] = next((count(record[name]) for name in alternatives if name in record), None)
            original = next((record[name] for name in alternatives if name in record), None)
            raw_metrics[key] = str(original)[:40] if original is not None else None
            if isinstance(original, str) and original.strip().endswith("万"):
                approximate.append(key)
        raw_time = next((record[key] for key in ("publish_time", "create_time", "created_at", "pubdate", "time") if record.get(key)), None)
        image_values = record.get("image_urls") or record.get("image_list") or ([record.get("cover") or record.get("pic")] if record.get("cover") or record.get("pic") else [])
        images = []
        if isinstance(image_values, list):
            for value in image_values[:5]:
                value = value.get("url") if isinstance(value, dict) else value
                if isinstance(value, str) and value.startswith("//"):
                    value = "https:" + value
                try:
                    images.append(public_url(value))
                except ValueError:
                    pass
        value = {"title": title, "summary": str(record.get("desc") or record.get("content") or "")[:800], "url": url, "platform": platform, "event_at": timestamp(raw_time), "metrics": metrics, "metrics_raw": raw_metrics, "approximate_metrics": approximate, "image_urls": images, "kind": "social_signal"}
        if record.get("source_kind") == "hotlist":
            value.update({"source_kind": "hotlist", "rank": record.get("rank") if isinstance(record.get("rank"), int) else None, "list_position": record.get("list_position"), "heat_raw": str(record.get("heat_raw") or "")[:50], "board_url": public_url(record.get("board_url")), "board_category": str(record.get("board_category", ""))[:60], "observed_at": timestamp(record.get("observed_at")), "retrieval_method": "public_browser_dom"})
        output.append(value)
    return output


def read_export(path, platform):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("导出文件需为普通文件且不超过 2MB")
    content = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        rows = list(csv.DictReader(io.StringIO(content)))
    elif path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    elif path.suffix.lower() == ".json":
        rows = json.loads(content)
    else:
        raise ValueError("仅支持 JSON、JSONL、CSV 内容导出")
    return normalize_records(rows, platform)
