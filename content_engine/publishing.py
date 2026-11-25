"""Local export receipts and user-confirmed publication, independent of prose state."""
import datetime as dt
import json

from .documents import DocumentConflict, cover_id, adapt
from .engine import date, digest, iso, now, uid, https_url, text
from .store import dumps


def public_record(row):
    if not row:
        return None
    result = {key: value for key, value in row.items() if key != "snapshot"}
    if "format" in result:
        result["title"] = json.loads(row["snapshot"])["title"]
    return result


class PublicationTracker:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def snapshot(self, article):
        photos = self.engine.photos.list(article["id"])
        document = adapt(article, photos)
        assets = {p["id"]: p for p in photos}
        # Status reads still work if a photo file is missing. No network or AI.
        projection = []
        for block in document["blocks"]:
            if block["type"] == "text":
                projection.append({"type": "text", "text": block["text"]})
            else:
                from .word_export import publication_caption
                asset = assets.get(block["asset_id"])
                caption = publication_caption({**asset, "data": {**asset["data"], "caption": block.get("caption", "")}}) if asset else block.get("caption", "")
                projection.append({"type": "image", "sha256": asset["sha256"] if asset else None, "caption": caption})
        fingerprint = digest({"title": document["title"], "blocks": projection, "cover_sha256": assets.get(cover_id(document), {}).get("sha256")})
        return {"title": document["title"], "revision": article["revision"], "revision_hash": article["revision_hash"], "document_hash": fingerprint, "document": document}

    def prepare_export(self, article_id, body):
        with self.engine.lock:
            article = self.engine.article(article_id)
            if type(body.get("revision")) is not int or body["revision"] != article["revision"]:
                raise DocumentConflict("稿件版本已变化，请保存当前内容后重新导出")
            format = body.get("format", "docx")
            if format not in ("docx", "zip"):
                raise ValueError("导出格式无效")
            snapshot = self.snapshot(article)
            content = self.engine.export_word(article_id, article["revision"], body.get("skip_missing") is True) if format == "docx" else self.engine.export_article(article_id, article["revision"], body.get("skip_missing") is True)
            after = self.engine.article(article_id)
            if after["revision"] != article["revision"] or self.snapshot(after)["document_hash"] != snapshot["document_hash"]:
                raise DocumentConflict("导出期间图文发生变化，请重新导出；未记录错误版本")
            receipt = uid()
            with self.store.db() as db:
                db.execute("INSERT INTO article_exports VALUES(?,?,?,?,?,?,?,?,NULL)", (receipt, article_id, article["revision"], article["revision_hash"], snapshot["document_hash"], dumps(snapshot), format, iso()))
            return content, receipt

    def complete_export(self, article_id, receipt):
        self.engine.article(article_id)
        with self.store.db() as db:
            row = db.execute("SELECT * FROM article_exports WHERE id=? AND article_id=?", (receipt, article_id)).fetchone()
            if not row:
                raise ValueError("导出记录不存在或不属于此稿件")
            if not row["delivered_at"]:
                db.execute("UPDATE article_exports SET delivered_at=? WHERE id=?", (iso(), receipt))
                self.engine.audit(db, "article.exported", article_id, {"export_id": receipt, "revision": row["revision"], "format": row["format"]})
        return self.summary(article_id)

    def active(self, article):
        row = self.store.one("SELECT * FROM article_publications WHERE article_id=? AND state='active'", (article["id"],))
        if row:
            return row
        # A revoked local flag must not reappear from an old schedule.
        if self.store.one("SELECT 1 FROM article_publications WHERE article_id=?", (article["id"],)):
            return None
        legacy = self.store.one("SELECT * FROM schedules WHERE article_id=? AND state='live' ORDER BY updated_at DESC LIMIT 1", (article["id"],))
        if not legacy or self.engine.topic(article["topic_id"])["is_demo"]:
            return None
        revision = self.store.one("SELECT revision,data FROM revisions WHERE article_id=? AND revision_hash=?", (article["id"], legacy["revision_hash"]))
        snapshot = self.snapshot(article) if legacy["revision_hash"] == article["revision_hash"] else {"document_hash": "legacy:" + legacy["revision_hash"]}
        return {"id": "legacy-" + legacy["id"], "article_id": article["id"], "export_id": None, "revision": revision["revision"] if revision else None, "revision_hash": legacy["revision_hash"], "document_hash": snapshot["document_hash"], "snapshot": dumps(snapshot), "title": json.loads(revision["data"])["title"] if revision else article["data"]["title"], "confirmed_at": legacy["updated_at"], "published_at": None, "remote_url": legacy.get("remote_url") or "", "source": "legacy", "state": "active", "version": 0, "updated_at": legacy["updated_at"]}

    def summary(self, article_id, article=None):
        article = article or self.engine.article(article_id)
        latest = self.store.one("SELECT * FROM article_exports WHERE article_id=? AND delivered_at IS NOT NULL ORDER BY created_at DESC,rowid DESC LIMIT 1", (article_id,))
        active = self.active(article)
        current_hash = self.snapshot(article)["document_hash"]
        return {"state": "published" if active else "pending" if latest else "draft", "latest_export": public_record(latest), "publication": public_record(active), "has_local_changes": bool(active and active["document_hash"] != current_hash), "export_is_current": bool(latest and latest["document_hash"] == current_hash), "is_demo": bool(self.engine.topic(article["topic_id"])["is_demo"])}

    def detail(self, article_id):
        article = self.engine.article(article_id)
        summary = self.summary(article_id, article)
        events = self.store.all("SELECT created_at,action,detail FROM audit WHERE object_id=? AND action LIKE 'publication.%' ORDER BY id DESC LIMIT 50", (article_id,))
        for event in events:
            event["detail"] = json.loads(event["detail"])
        return {**summary, "article_id": article_id, "title": article["data"]["title"], "revision": article["revision"], "account": self.engine.account(article["account_id"]), "exports": [public_record(row) for row in self.store.all("SELECT * FROM article_exports WHERE article_id=? AND delivered_at IS NOT NULL ORDER BY created_at DESC,rowid DESC LIMIT 50", (article_id,))], "history": [public_record(row) for row in self.store.all("SELECT * FROM article_publications WHERE article_id=? ORDER BY updated_at DESC,rowid DESC LIMIT 50", (article_id,))], "events": events}

    def change(self, article_id, body):
        action = body.get("action")
        if action not in ("confirm", "update", "revoke"):
            raise ValueError("发布记录操作无效")
        if type(body.get("expected_version", 0)) is not int:
            raise ValueError("发布记录版本无效")
        with self.engine.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            article = self.engine.article(article_id)
            if self.engine.topic(article["topic_id"])["is_demo"]:
                raise ValueError("演示稿不能登记为真实已发布")
            active = self.active(article)
            expected = (body.get("expected_id"), body.get("expected_version", 0))
            actual = (active["id"], active["version"]) if active else (None, 0)
            if action != "revoke":
                link = text(body.get("remote_url", ""), 2000)
                link = https_url(link) if link else ""
                published = body.get("published_at")
                published = iso(date(published)) if published else None
                if published and date(published) > now() + dt.timedelta(minutes=5):
                    raise ValueError("实际发布时间不能是未来；不清楚可以留空")
                if body.get("target") == "published" and active:
                    chosen = {**active, "export_id": active["export_id"]}
                elif body.get("export_id"):
                    row = db.execute("SELECT * FROM article_exports WHERE id=? AND article_id=? AND delivered_at IS NOT NULL", (body["export_id"], article_id)).fetchone()
                    if not row:
                        raise ValueError("请选择本稿件已完成的导出版本")
                    chosen = {**dict(row), "export_id": row["id"], "title": json.loads(row["snapshot"])["title"]}
                else:
                    if type(body.get("revision")) is not int or body["revision"] != article["revision"]:
                        raise DocumentConflict("当前版本已变化，请重新打开确认框选择发布版本；原填写内容未覆盖")
                    snapshot = self.snapshot(article)
                    chosen = {**snapshot, "snapshot": dumps(snapshot), "export_id": None}
                # A retried confirmation is idempotent, even if its first
                # response was lost. It must never create a duplicate record.
                if active and active["document_hash"] == chosen["document_hash"] and active["remote_url"] == link and active["published_at"] == published:
                    return self.summary(article_id)
            if expected != actual:
                raise DocumentConflict("发布记录已被其他窗口修改，请重新打开记录后处理；当前填写内容仍保留")
            if action in ("update", "revoke") and not active:
                raise ValueError("当前没有已发布标记")
            if active and active["source"] == "legacy" and not db.execute("SELECT 1 FROM article_publications WHERE id=?", (active["id"],)).fetchone():
                columns = list(active)
                db.execute("INSERT INTO article_publications(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(active.values()))
            if action == "revoke":
                db.execute("UPDATE article_publications SET state='revoked',version=version+1,updated_at=? WHERE id=?", (iso(), active["id"]))
            elif active and active["document_hash"] == chosen["document_hash"]:
                db.execute("UPDATE article_publications SET remote_url=?,published_at=?,version=version+1,updated_at=? WHERE id=?", (link, published, iso(), active["id"]))
            else:
                if active:
                    db.execute("UPDATE article_publications SET state='superseded',version=version+1,updated_at=? WHERE id=?", (iso(), active["id"]))
                db.execute("INSERT INTO article_publications(id,article_id,export_id,revision,revision_hash,document_hash,snapshot,title,confirmed_at,published_at,remote_url,source,state,version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'manual','active',1,?)", (uid(), article_id, chosen["export_id"], chosen["revision"], chosen["revision_hash"], chosen["document_hash"], chosen["snapshot"], chosen["title"], iso(), published, link, iso()))
            self.engine.audit(db, "publication." + action, article_id, {"previous": public_record(active), "target_revision": chosen["revision"] if action != "revoke" else None, "remote_url": link if action != "revoke" else None, "published_at": published if action != "revoke" else None, "confirmed_by": "user_manual"})
        return self.summary(article_id)
