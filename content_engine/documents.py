"""Canonical ordered article document shared by editor, preview and exports."""
import copy
import html
import re
import uuid

from .word_export import publication_caption


class DocumentConflict(ValueError):
    pass


def block_id(article_id, kind, key):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{article_id}/{kind}/{key}").hex


def plain_caption(value):
    value = str(value or "").strip()
    if value.startswith(("原报道配图", "来源报道配图", "相关公开内容封面", "来源配图")) or any(
        word in value for word in ("终审核对", "终审时核对", "转载依据未核实", "发布前核对", "非经核验")
    ):
        return ""
    return value


def adapt(article, photos):
    """Pure legacy projection: reading an old article never rewrites it."""
    if article["data"].get("document"):
        return copy.deepcopy(article["data"]["document"])
    blocks = []
    paragraphs = article["data"]["paragraphs"]
    used = [p for p in photos if p["selected"]]
    for index, paragraph in enumerate(paragraphs, 1):
        blocks.append({"id": block_id(article["id"], "text", index), "type": "text", **paragraph})
        for asset in used:
            if min(max(1, asset["position"]), len(paragraphs)) == index:
                blocks.append({"id": block_id(article["id"], "image", asset["id"]), "type": "image", "asset_id": asset["id"], "caption": plain_caption(asset["data"].get("caption"))})
    manual = next((p["id"] for p in used if p["data"].get("use_as_cover")), None)
    if not manual and used:
        # Previous versions could choose a landscape cover by aspect ratio.
        # Preserve that historical choice instead of silently changing it.
        chosen = max(used, key=lambda a: -abs(a["data"].get("width", 1) / max(1, a["data"].get("height", 1)) - 1.5) + min(a["data"].get("width", 1), 1600) / 10000)
        if chosen["id"] != used[0]["id"]:
            manual = chosen["id"]
    return {"version": 1, "title": article["data"]["title"], "blocks": blocks, "cover": {"mode": "manual" if manual else "auto", "asset_id": manual}}


def normalize(document, photos, previous=None):
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("图文文档版本无效")
    title = document.get("title")
    if not isinstance(title, str) or len(title) > 200:
        raise ValueError("标题需为不超过 200 字的文本")
    items = document.get("blocks")
    if not isinstance(items, list) or not 1 <= len(items) <= 150:
        raise ValueError("正文需要 1–150 个图文模块")
    assets = {p["id"]: p for p in photos}
    old = {p["id"]: p for p in (previous or {}).get("blocks", [])}
    result, ids, image_ids = [], set(), set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", item["id"]) or item["id"] in ids:
            raise ValueError("模块标识无效或重复")
        ids.add(item["id"])
        block = {"id": item["id"], "type": item.get("type")}
        if block["type"] == "text":
            value = item.get("text")
            if not isinstance(value, str) or len(value) > 10000:
                raise ValueError("文字模块需为不超过 10000 字的文本")
            # Only unchanged text retains its old internal mapping. Manual prose
            # does not become a verified fact merely by retaining a block ID.
            source = old.get(item["id"], {})
            unchanged = source.get("text") == value
            block.update(text=value, kind=source.get("kind", "analysis") if unchanged else "analysis", claim_ids=source.get("claim_ids", []) if unchanged else [])
        elif block["type"] == "image":
            asset_id, caption = item.get("asset_id"), item.get("caption", "")
            if asset_id not in assets or asset_id in image_ids:
                raise ValueError("图片不属于此稿件或同一图片重复插入")
            if not isinstance(caption, str) or len(caption) > 2000:
                raise ValueError("图注需为不超过 2000 字的文本")
            image_ids.add(asset_id)
            block.update(asset_id=asset_id, caption=plain_caption(caption))
        else:
            raise ValueError("不支持的正文模块")
        result.append(block)
    if sum(len(b.get("text", "")) for b in result) > 50000:
        raise ValueError("正文超过 50000 字")
    cover = document.get("cover", {"mode": "auto"})
    if not isinstance(cover, dict) or cover.get("mode") not in ("auto", "manual"):
        raise ValueError("封面模式无效")
    warning = None
    if cover["mode"] == "manual" and cover.get("asset_id") not in image_ids:
        warning = "指定封面已从正文移除，已恢复首图封面"
        cover = {"mode": "auto"}
    return {"version": 1, "title": title, "blocks": result, "cover": {"mode": cover["mode"], "asset_id": cover.get("asset_id") if cover["mode"] == "manual" else None}}, warning


def cover_id(document):
    images = [b["asset_id"] for b in document["blocks"] if b["type"] == "image"]
    chosen = document["cover"].get("asset_id") if document["cover"]["mode"] == "manual" else None
    return chosen if chosen in images else next(iter(images), None)


def reader_blocks(document, photos):
    assets = {p["id"]: p for p in photos}
    result = []
    for block in document["blocks"]:
        if block["type"] == "text":
            result.append({"id": block["id"], "type": "text", "text": block["text"]})
        else:
            asset = assets.get(block["asset_id"])
            if asset is None:
                raise ValueError("正文中的图片素材已丢失，请移除或替换后导出")
            asset = {**asset, "data": {**asset["data"], "caption": plain_caption(block.get("caption"))}}
            result.append({**block, "caption": publication_caption(asset), "asset": asset})
    return result


def html_body(blocks, photo_paths):
    output = []
    for block in blocks:
        if block["type"] == "text":
            output.append("<p>" + html.escape(block["text"]).replace("\n", "<br>") + "</p>")
        else:
            output.append("<figure><img style='max-width:100%;height:auto' src='" + html.escape(photo_paths[block["asset_id"]], quote=True) + "' alt='文章配图'>" + ("<figcaption>" + html.escape(block["caption"]) + "</figcaption>" if block["caption"] else "") + "</figure>")
    return "".join(output)
