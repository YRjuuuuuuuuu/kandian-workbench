"""Bounded original-page receipts when CLI exposes only internal search refs."""
import hashlib
import json
import time
from html.parser import HTMLParser

from .public_fetch import fetch


class PageReceipt(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.in_title = False
        self.ignore = 0
        self.words = []
        self.article_depth = 0
        self.article_words = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "article":
            self.article_depth += 1
        if tag in ("script", "style"):
            self.ignore += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta" and values.get("property") == "og:title":
            self.title = values.get("content", "")[:300]

    def handle_endtag(self, tag):
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        if tag in ("script", "style"):
            self.ignore = max(0, self.ignore - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, value):
        if self.in_title and not self.title:
            self.title = value.strip()[:300]
        if not self.ignore and value.strip():
            self.words.append(value.strip())
            if self.article_depth:
                self.article_words.append(value.strip())


def read(url, folder, include_text=False):
    """Confirms accessible original URL, never certifies the model's claims."""
    key = hashlib.sha256(url.encode()).hexdigest()
    cache = folder / (key + (".material.json" if include_text else ".json"))
    if cache.is_file() and time.time() - cache.stat().st_mtime < 600:
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        body, mime, final_url = fetch(url, limit=2_000_000, timeout=4, accept="text/html")
        if "html" not in mime:
            raise ValueError("来源不是可读报道页面")
        parser = PageReceipt()
        parser.feed(body.decode("utf-8", errors="replace"))
        plain = " ".join(parser.words)
        if not parser.title or len(plain) < 250 or any(word in parser.title.lower() for word in ("access denied", "captcha", "just a moment", "verify you", "robot check", "sign in", "log in", "登录")):
            raise ValueError("页面是访问提示或未取得正文")
        result = {"status": "readable", "url": url, "final_url": final_url, "title": parser.title, "sha256": hashlib.sha256(body).hexdigest(), "read_at": time.time(), "method": "bounded_public_http_read", "note": "原文可读凭据，仅证明读取；事实仍需归因与终审"}
        from .photos import OpenGraphImages
        images = OpenGraphImages()
        images.feed(body.decode("utf-8", errors="replace"))
        result["report_images"] = {"urls": list(dict.fromkeys(images.images))[:10], "title": images.title or parser.title, "rights": images.rights}
        if include_text:
            result["text"] = "\n".join(parser.article_words or parser.words)[:16000]
        folder.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result
    except Exception as exc:
        message = str(exc)[:180] if isinstance(exc, ValueError) else "网络、证书或本地运行权限不可用"
        return {"status": "unavailable", "url": url, "error_type": type(exc).__name__, "message": message}
