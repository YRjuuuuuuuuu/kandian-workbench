"""Actual sports-photo discovery, provenance, local previews and inline placement.

An accessible image is a research asset, not an automatic copyright grant.
"""
import hashlib
import html
import io
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from .public_fetch import fetch
from .store import dumps


class OpenGraphImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.title = ""
        self.rights = ""
        self.article_depth = 0
        self.figure_depth = 0
        self.jsonld = False
        self.jsonld_parts = []
        self.details = {}
        self.containers = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        key = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content", "")
        marker = " ".join([values.get("class", ""), values.get("id", ""), values.get("role", "")]).lower()
        if tag in ("div", "section", "article", "figure"):
            self.containers.append((tag, bool(re.search(r"related|recommend|advert|footer|sidebar|navigation", marker)), bool(re.search(r"article[-_]?content|content[-_]?article|detail[-_]?content|text[-_]?body|rich[-_]?media", marker))))
        if tag == "article":
            self.article_depth += 1
        if tag == "figure":
            self.figure_depth += 1
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self.jsonld, self.jsonld_parts = True, []
        if tag == "img" and not any(skip for _, skip, _ in self.containers) and (self.article_depth or self.figure_depth or any(content for _, _, content in self.containers)):
            raw = values.get("data-src") or values.get("data-original") or values.get("data-lazy-src") or values.get("src")
            srcset = values.get("data-srcset") or values.get("srcset", "")
            if srcset:
                choices = [part.strip().split() for part in srcset.split(",") if part.strip()]
                choices = [p for p in choices if p[0].startswith(("https://", "//"))]
                if choices:
                    raw = choices[-1][0]
            if raw:
                self.images.append(raw)
                self.details[raw] = {"alt": values.get("alt", "")[:350], "title": values.get("title", "")[:350], "context": "article_body"}
        if tag == "meta" and key in ("og:image", "og:image:url", "twitter:image", "twitter:image:src") and content:
            self.images.append(content)
        elif tag == "meta" and key in ("og:title", "twitter:title"):
            self.title = content[:250]
        elif tag == "meta" and key in ("copyright", "dc.rights", "rights"):
            self.rights = content[:500]

    def handle_data(self, data):
        if self.jsonld:
            self.jsonld_parts.append(data)

    def handle_endtag(self, tag):
        if tag in ("div", "section", "article", "figure"):
            for i in range(len(self.containers)-1, -1, -1):
                if self.containers[i][0] == tag:
                    self.containers = self.containers[:i]
                    break
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        if tag == "figure":
            self.figure_depth = max(0, self.figure_depth - 1)
        if tag == "script" and self.jsonld:
            try:
                self._structured(json.loads("".join(self.jsonld_parts)))
            except (ValueError, TypeError):
                pass
            self.jsonld = False

    def _structured(self, value):
        if isinstance(value, list):
            for item in value:
                self._structured(item)
        elif isinstance(value, dict):
            if any(name in str(value.get("@type", "")) for name in ("NewsArticle", "Article", "BlogPosting")):
                images = value.get("image", [])
                for image in images if isinstance(images, list) else [images]:
                    url = (image.get("url") or image.get("contentUrl")) if isinstance(image, dict) else image
                    if isinstance(url, str):
                        self.images.append(url)
                self.title = self.title or str(value.get("headline", ""))[:250]
            if "@graph" in value:
                self._structured(value["@graph"])


def plain(value, maximum=500):
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).strip()[:maximum]


def photo_query(value, context=""):
    # A bilingual instruction is not an entity search: Commons ANDs every
    # word, so Chinese + English + team names commonly yields zero images.
    latin = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[-’'][A-Za-zÀ-ÖØ-öø-ÿ]+)*", str(value))
    if len(latin) >= 2:
        query = '"' + " ".join(latin[:2]) + '"'
    else:
        query = str(value).strip()[:100]
    return query + (" " + context if context else "")


def image_identity(url):
    parsed = urllib.parse.urlsplit(url)
    nested = dict(urllib.parse.parse_qsl(parsed.query)).get("url", "")
    # Two different CDN crops of the same original are one photo.
    if nested.startswith("https://"):
        return image_identity(nested)
    path = re.sub(r"/\d{2,5}x\d{2,5}/", "/", parsed.path)
    query = [(k,v) for k,v in urllib.parse.parse_qsl(parsed.query) if not k.lower().startswith("utm_") and k.lower() not in ("width","height","w","h","quality")]
    return urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,path,urllib.parse.urlencode(query),""))


def unsuitable_background(value):
    return bool(re.search(r"church|pope|papal|archimadrid|ofrenda|politic|military|protest|president|white house|biden|trump|parliament|secretary|minister|diplomat|embassy|nato|governor|senator|教堂|教皇|政治|军事|抗议|总统|外交|部长", value, re.I))


def placeholder_image(url):
    return bool(re.search(r"logo|sprite|avatar|placeholder|favicon|default[-_].*image|generic[-_].*image",urllib.parse.urlsplit(url).path,re.I))


class PhotoManager:
    def __init__(self, engine, fetcher=None):
        self.engine = engine
        self.store = engine.store
        self.fetch = fetcher or fetch

    def list(self, article_id):
        self.engine.article(article_id)
        rows = self.store.all("SELECT * FROM photo_assets WHERE article_id=? ORDER BY position,created_at", (article_id,))
        for row in rows:
            row["data"] = json.loads(row["data"])
            row["preview_url"] = "/api/photos/" + row["id"] + "/file"
        return [row for row in rows if not row["data"].get("rejected_reason")]

    def selected(self, article_id):
        assets = self.list(article_id)
        document = self.engine.article(article_id)["data"].get("document")
        if document:
            by_id = {p["id"]: p for p in assets}
            return [by_id[b["asset_id"]] for b in document["blocks"] if b["type"] == "image" and b["asset_id"] in by_id]
        return [asset for asset in assets if asset["selected"]]

    def cover_asset(self, article_id):
        assets = self.selected(article_id)
        if not assets:
            raise ValueError("缺少封面照片，请先让 Agent 补图或选择合适的图片")
        document = self.engine.article(article_id)["data"].get("document")
        if document:
            from .documents import cover_id
            return next((p for p in assets if p["id"] == cover_id(document)), assets[0])
        explicit = next((a for a in assets if a["data"].get("use_as_cover")), None)
        return explicit or max(assets, key=lambda a: -abs(a["data"]["width"] / a["data"]["height"] - 1.5) + min(a["data"]["width"], 1600) / 10000)

    def cover_bytes(self, article_id):
        from PIL import Image, ImageOps
        asset = self.cover_asset(article_id)
        original, _ = self.content(asset["id"])
        with Image.open(io.BytesIO(original)) as picture:
            picture = ImageOps.exif_transpose(picture).convert("RGBA")
            white = Image.new("RGBA", picture.size, "white")
            white.alpha_composite(picture)
            picture = white.convert("RGB")
            picture.thumbnail((1200, 800), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (1200, 800), (247, 247, 245))
            canvas.paste(picture, ((1200-picture.width)//2, (800-picture.height)//2))
            output = io.BytesIO()
            canvas.save(output, "JPEG", quality=92)
        return output.getvalue()

    def asset(self, asset_id):
        result = self.store.one("SELECT * FROM photo_assets WHERE id=?", (asset_id,))
        if not result:
            raise ValueError("图片素材不存在")
        result["data"] = json.loads(result["data"])
        return result

    def content(self, asset_id):
        asset = self.asset(asset_id)
        path = (self.engine.data_dir / "photos" / asset["local_name"]).resolve()
        if path.parent != (self.engine.data_dir / "photos").resolve() or not path.is_file():
            raise ValueError("图片文件缺失")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise ValueError("图片内容变化，请重新检索并审定")
        return data, asset["mime"]

    def invalidate(self, article_id):
        from .engine import iso
        with self.store.db() as db:
            db.execute("UPDATE articles SET approval_hash=NULL,status=CASE WHEN status IN ('live','submitted','platform_scheduled') THEN 'correction_required' ELSE 'needs_review' END,updated_at=? WHERE id=?", (iso(), article_id))
            db.execute("UPDATE schedules SET state='needs_review',updated_at=? WHERE article_id=? AND state IN ('scheduled','ready')", (iso(), article_id))

    def save(self, article_id, candidate, position=1, selected=True):
        from PIL import Image
        from .engine import iso, uid
        from .sources import public_addresses
        image_url = candidate["image_url"]
        # No localhost/file/signed-in browser resources can enter this downloader.
        public_addresses(image_url)
        old = self.store.one("SELECT id,data FROM photo_assets WHERE article_id=? AND image_url=?", (article_id, image_url))
        if old and not json.loads(old["data"]).get("rejected_reason"):
            return old["id"]
        content, _, resolved = self.fetch(image_url, limit=8_000_000, accept="image/jpeg,image/png,image/webp")
        with Image.open(io.BytesIO(content)) as picture:
            width, height, form = picture.width, picture.height, picture.format
            if form not in ("JPEG", "PNG", "WEBP") or width < 400 or height < 220 or width * height > 36_000_000:
                raise ValueError("图片尺寸／类型不适合作为赛事图文素材")
            picture.verify()
        suffix, mime = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}[form]
        sha = hashlib.sha256(content).hexdigest()
        folder = self.engine.data_dir / "photos"
        folder.mkdir(exist_ok=True)
        filename = sha + "." + suffix
        (folder / filename).write_bytes(content)
        metadata = {"caption": plain(candidate.get("caption") or "来源报道配图，发布前核对赛事与人物"), "credit": plain(candidate.get("credit") or "署名信息见原始出处"), "license": plain(candidate.get("license") or "公开可见，转载依据未核实"), "license_url": candidate.get("license_url", ""), "source_title": plain(candidate.get("source_title")), "source_kind": candidate.get("source_kind", "public_report"), "photo_role": candidate.get("photo_role", "source_report"), "width": width, "height": height, "retrieved_at": iso(), "resolved_image_url": resolved, "reuse_verified": candidate.get("reuse_verified") is True}
        metadata["matched_subject"] = plain(candidate.get("matched_subject", ""))
        for key in ("relevance_score", "relevance_reason", "visual_hash", "visual_review", "scene", "subject_names"):
            if key in candidate:
                metadata[key] = candidate[key]
        asset_id = old["id"] if old else uid()
        with self.engine.lock:
            current = self.engine.article(article_id)
            if current["status"] in ("live", "submitted", "platform_scheduled", "correction_required"):
                raise ValueError("稿件已经提交平台，本次未替换图片")
            with self.store.db() as db:
                db.execute("INSERT OR REPLACE INTO photo_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)", (asset_id, article_id, candidate["source_url"], image_url, filename, sha, mime, dumps(metadata), int(selected), position, iso()))
            self.invalidate(article_id)
        return asset_id

    def save_generated(self, article_id, content, metadata, position=2):
        from .engine import iso, uid
        from PIL import Image
        with Image.open(io.BytesIO(content)) as picture:
            width, height = picture.size
            if picture.format != "PNG" or width * height > 12_000_000:
                raise ValueError("生成的图表格式无效")
        sha = hashlib.sha256(content).hexdigest()
        image_url = "generated:chart:" + sha
        old = self.store.one("SELECT id FROM photo_assets WHERE article_id=? AND image_url=?", (article_id, image_url))
        if old:
            return old["id"]
        folder = self.engine.data_dir / "photos"
        folder.mkdir(exist_ok=True)
        (folder / (sha + ".png")).write_bytes(content)
        aid = uid()
        data = {**metadata, "width": width, "height": height, "source_kind": "generated_chart", "photo_role": "data_chart", "license": "原创数据图表", "reuse_verified": True, "credit": "", "retrieved_at": iso()}
        with self.store.db() as db:
            db.execute("INSERT INTO photo_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)", (aid, article_id, metadata.get("source_url", ""), image_url, sha + ".png", sha, "image/png", dumps(data), 0, position, iso()))
        return aid

    def update(self, asset_id, body):
        with self.engine.lock:
            return self._update(asset_id, body)

    def _update(self, asset_id, body):
        asset = self.asset(asset_id)
        article = self.engine.article(asset["article_id"])
        if article["status"] in ("live", "submitted", "platform_scheduled", "correction_required"):
            raise ValueError("已提交平台的图片保留历史，请在平台处理更正")
        metadata = asset["data"]
        for key in ("caption", "credit"):
            if key in body:
                metadata[key] = plain(body[key])
        position = body.get("position", asset["position"])
        if not isinstance(position, int) or not 1 <= position <= max(1, len(article["data"]["paragraphs"])):
            raise ValueError("插图位置需为正文已有段落编号")
        if metadata.get("photo_role") == "file_photo" and "资料图" not in metadata["caption"]:
            metadata["caption"] += "（资料图）"
        with self.store.db() as db:
            if body.get("use_as_cover") is True:
                for other in db.execute("SELECT id,data FROM photo_assets WHERE article_id=?", (article["id"],)).fetchall():
                    data = json.loads(other["data"])
                    data["use_as_cover"] = other["id"] == asset_id
                    db.execute("UPDATE photo_assets SET data=? WHERE id=?", (dumps(data), other["id"]))
                metadata["use_as_cover"] = True
            selected = body.get("use_as_cover") is True or body.get("selected", bool(asset["selected"])) is True
            db.execute("UPDATE photo_assets SET data=?,selected=?,position=? WHERE id=?", (dumps(metadata), int(selected), position, asset_id))
        self.invalidate(article["id"])
        return {"ok": True}

    def report_images(self, source_url):
        parser = OpenGraphImages()
        final = source_url
        import time
        key = hashlib.sha256(source_url.encode()).hexdigest()
        cached = None
        for suffix in (".material.json", ".json"):
            path = self.engine.data_dir / "agent" / "source_reads" / (key + suffix)
            if path.is_file() and time.time() - path.stat().st_mtime < 600:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if "report_images" in saved:
                    cached, final = saved["report_images"], saved.get("final_url", source_url)
                    break
        if cached is not None:
            parser.images, parser.title, parser.rights = cached["urls"], cached["title"], cached["rights"]
        else:
            payload, kind, final = self.fetch(source_url, limit=2_000_000, accept="text/html")
            if "html" not in kind:
                return []
            parser.feed(payload.decode("utf-8", errors="replace"))
        result = []
        for raw in dict.fromkeys(parser.images):
            url = urllib.parse.urljoin(final, raw)
            if not url.startswith("https://") or placeholder_image(url):
                continue
            result.append({"image_url": url, "source_url": source_url, "source_title": parser.title, "caption": "原报道配图（赛事、人物及拍摄时间请在终审核对）", "credit": parser.rights or "来源：" + (urllib.parse.urlsplit(source_url).hostname or "原报道"), "source_kind": "public_report", "photo_role": "source_report", "license": parser.rights or "公开可见；转载依据未核实", "reuse_verified": False})
            result[-1].update(parser.details.get(raw, {}))
        return result[:12]

    def commons_images(self, query, context=""):
        params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 24, "prop": "imageinfo|categories", "cllimit": 30, "iiprop": "url|extmetadata|size", "iiurlwidth": 1200}
        url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
        content, _, _ = self.fetch(url, limit=3_000_000, accept="application/json")
        pages = json.loads(content).get("query", {}).get("pages", {})
        output = []
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            metadata = info.get("extmetadata", {})
            background = page.get("title", "") + " " + plain(metadata.get("ImageDescription", {}).get("value")) + " " + plain(metadata.get("Artist", {}).get("value")) + " " + " ".join(c.get("title", "") for c in page.get("categories", []))
            if unsuitable_background(background):
                continue
            if context and re.search(r"Category:(?:Actors|Film actors|Singers)|Category:.*actors", background, re.I) and not re.search(r"basketball|soccer|tennis|nba", background, re.I):
                continue
            subject = re.search(r'"([^"]+)"', query)
            if subject and not all(word in background.casefold().replace("_", " ") for word in subject[1].casefold().split()):
                continue
            context_words = {"soccer": ("soccer", "football"), "table tennis": ("table tennis", "乒乓")}.get(context, (context,))
            from .image_subjects import title_matches
            exact_subject = bool(subject and title_matches(page.get("title", ""), {"name": subject[1]}))
            if context and not exact_subject and not any(word in background.casefold().replace("_", " ") for word in context_words):
                continue
            license = metadata.get("LicenseShortName", {}).get("value", "")
            if not any(value in license.lower() for value in ("cc by", "cc-by", "public domain", "cc0")):
                continue
            if not info.get("url") or not info.get("descriptionurl"):
                continue
            output.append({"image_url": info.get("thumburl") or info["url"], "source_url": info["descriptionurl"], "source_title": page.get("title", ""), "matched_subject":subject[1] if subject else query, "caption": plain(metadata.get("ObjectName", {}).get("value") or page.get("title", "")) + "（资料图，并非本场比赛现场）", "credit": plain(metadata.get("Artist", {}).get("value")), "license": license, "license_url": metadata.get("LicenseUrl", {}).get("value", ""), "source_kind": "wikimedia_commons", "photo_role": "file_photo", "reuse_verified": True})
        return output

    def discover(self, article_id, queries=None, on_event=None, run=None):
        from .image_pipeline import discover
        return discover(self, article_id, queries, on_event, run)

    def _legacy_discover(self, article_id, queries=None, on_event=None):
        article = self.engine.article(article_id)
        if article["status"] in ("live", "submitted", "platform_scheduled", "correction_required"):
            raise ValueError("已提交平台的图文不自动替换素材")
        topic = self.engine.topic(article["topic_id"])
        on_event = on_event or (lambda *args: None)
        primary = (queries or article["data"].get("image_queries", []) or [""])[0]
        team_terms = r"celtics|lakers|warriors|nets|arsenal|united|city|town|fc\b|club|team|nba\b"
        primary_person = bool(len(re.findall(r"[A-Za-z]+", primary)) >= 2 and not re.search(team_terms, primary, re.I))
        existing_seen, changed = set(), False
        for asset in self.list(article_id):
            identity = image_identity(asset["image_url"])
            background = asset["data"].get("source_title", "") + " " + asset["data"].get("caption", "") + " " + asset["data"].get("matched_subject", "")
            reason = "同一原图的不同尺寸，避免重复配图" if identity in existing_seen else None
            if placeholder_image(asset["image_url"]):
                reason = "来源站点的默认图或品牌占位图，不是赛事照片"
            if asset["data"].get("source_kind") == "wikimedia_commons":
                subject = re.search(r'"([^"]+)"', photo_query(primary))
                if unsuitable_background(background) or primary_person and subject and not all(w in background.casefold().replace("_"," ") for w in subject[1].casefold().split()):
                    reason = "资料图与当前主体不符或包含模式排除的活动内容"
            existing_seen.add(identity)
            if reason:
                metadata = {**asset["data"], "rejected_reason":reason}
                with self.store.db() as db:
                    db.execute("UPDATE photo_assets SET selected=0,data=? WHERE id=?",(dumps(metadata),asset["id"]))
                changed = True
        if changed:
            self.invalidate(article_id)
        sources = list(dict.fromkeys([source["url"] for source in topic["data"].get("agent_origin", {}).get("sources", [])] + [fact["url"] for fact in topic["data"]["facts"]]))
        proposals, failures, ids = [], [], []
        already_selected = len(self.selected(article_id))
        # Only use platform covers when that exact post is cited in this topic.
        for row in self.store.all("SELECT url,data FROM agent_signals WHERE account_id=?", (article["account_id"],)):
            if row["url"] not in sources:
                continue
            signal = json.loads(row["data"])
            for url in signal.get("image_urls", [])[:2]:
                proposals.append({"image_url": url, "source_url": row["url"], "source_title": signal.get("title", ""), "caption": "相关公开内容封面（非经核验的本场现场照）", "credit": "来源：相关公开内容页面", "source_kind": "mediacrawler_public_cover", "photo_role": "platform_cover", "reuse_verified": False})
        # First use actual images from the reports that support this event.
        def read_report(source):
            try:
                return source, self.report_images(source), None
            except Exception as exc:
                return source, [], str(exc)[:250]
        with ThreadPoolExecutor(max_workers=3) as pool:
            for source, images, error in pool.map(read_report, sources[:6]):
                proposals.extend(images)
                on_event("photo", "读取原报道配图：" + (urllib.parse.urlsplit(source).hostname or "来源"), {})
                if error:
                    failures.append({"source": source, "error": error})
        keywords = queries or article["data"].get("image_queries", [])
        if not keywords:
            keywords = [article["data"]["title"]]
        scope = topic["data"].get("agent_origin", {}).get("profile", {}).get("scope", {}).get("id")
        if not scope:
            corpus = article["data"]["title"] + " " + " ".join(keywords)
            scope = next((s["id"] for s in self.engine.modes.get(topic["mode_id"]).get("agent", {}).get("sport_scopes", []) if any(k.casefold() in corpus.casefold() for k in s["keywords"])), None)
        context = {"basketball": "basketball", "football": "soccer", "tennis": "tennis", "racket": "table tennis" if re.search(r"乒乓|WTT|table tennis", article["data"]["title"] + " ".join(keywords), re.I) else "badminton"}.get(scope, "")
        # A current athlete story must not fall back to a different historical
        # player merely because the old photograph mentions the same team.
        if primary_person:
            keywords = keywords[:1]
        # Commons contributes licensed background photos, never fake live scenes.
        for keyword in dict.fromkeys(photo_query(value, context) for value in keywords[:2]):
            try:
                on_event("photo", "检索资料图库：" + keyword, {})
                proposals.extend(self.commons_images(keyword, context))
            except Exception as exc:
                failures.append({"source": "Wikimedia Commons", "error": str(exc)[:250]})
        seen = {image_identity(p["image_url"]) for p in self.list(article_id)}
        for candidate in proposals:
            identity = image_identity(candidate["image_url"])
            if identity in seen:
                continue
            seen.add(identity)
            try:
                position = 1 if not ids else max(1, len(article["data"]["paragraphs"]) // 2)
                ids.append(self.save(article_id, candidate, position=position, selected=already_selected + len(ids) < 2))
                if len(ids) >= 4:
                    break
            except Exception as exc:
                failures.append({"source": candidate["source_url"], "error": str(exc)[:250]})
        on_event("photo", f"取得 {len(ids)} 张真实图片候选；保留来源、署名与使用范围记录", {"failures": failures})
        return {"asset_ids": ids, "failures": failures, "photos": self.list(article_id)}
