import argparse
import hmac
import http.cookies
import json
import mimetypes
import secrets
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Engine, ROOT, text
from .modes import validate_mode
from .store import dumps
from . import __version__
from .documents import DocumentConflict


def make_server(engine, port=8765):
    token_path = engine.data_dir / ".local-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_hex(32), encoding="ascii")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    token = token_path.read_text(encoding="ascii").strip()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ContentWorkbench/0.4"

        def log_message(self, fmt, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def send(self, status, content, content_type="application/json; charset=utf-8", filename=None, cookie=False, extra_headers=None):
            if isinstance(content, (dict, list)):
                content = dumps(content).encode("utf-8")
            elif isinstance(content, str):
                content = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie", f"ce_session={token}; HttpOnly; SameSite=Strict; Path=/")
            if filename:
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
            self.end_headers()
            self.wfile.write(content)

        def guard(self, write=False):
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host", "") not in allowed_hosts:
                raise PermissionError("仅允许本机工作台访问")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in allowed_hosts}:
                raise PermissionError("拒绝跨站请求")
            supplied = self.headers.get("X-App-Token", "")
            if not write and not supplied:
                try:
                    cookies = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                    supplied = cookies["ce_session"].value if "ce_session" in cookies else ""
                except http.cookies.CookieError:
                    supplied = ""
            if not hmac.compare_digest(supplied, token):
                raise PermissionError("本地会话已失效，请重新打开工作台")
            if write and self.headers.get_content_type() != "application/json":
                raise PermissionError("写操作需为 JSON 请求")

        def dispatch_get(self):
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/app.js", "/models.js", "/agent.js", "/writer-skills.js", "/publishing.js", "/editor.js", "/editor.css", "/style.css", "/agent.css", "/health"):
                # Host/Origin validation also applies to the bootstrap page.
                allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
                if self.headers.get("Host") not in allowed:
                    raise PermissionError("无效主机")
                if path == "/health":
                    return self.send(200, {"ok": True, "version": __version__, "deepseek_only": engine.deepseek_only})
                name = "index.html" if path == "/" else path[1:]
                content = (ROOT / "web" / name).read_text(encoding="utf-8").replace("__APP_TOKEN__", token)
                return self.send(200, content, "text/html; charset=utf-8" if path == "/" else "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8", cookie=path == "/")
            self.guard()
            parts = path.strip("/").split("/")
            if path == "/api/state":
                selected = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("account_id", [None])[0]
                if selected:
                    engine.account(selected)
                return self.send(200, engine.state(selected))
            if path == "/api/agent/state":
                selected = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("account_id", [None])[0]
                if selected:
                    engine.account(selected)
                return self.send(200, engine.agent.state(selected))
            if path == "/api/ai":
                selected = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("account_id", ["main"])[0]
                engine.account(selected)
                return self.send(200, engine.models.status(selected))
            if path == "/api/writer-skills":
                mode_id = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("mode_id", ["sports"])[0]
                return self.send(200, engine.writer_factory.state(mode_id))
            if len(parts) == 5 and parts[:3] == ["api", "agent", "runs"] and parts[4] == "external-package":
                import io, zipfile
                folder = engine.agent.external_folder(engine.agent.run(parts[3]))
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for file in folder.rglob("*"):
                        relative = file.relative_to(folder)
                        approved = relative.as_posix() in {"instructions.md", "output.schema.json", "contract.json", "request.json", "skills/evidence-content-writer/SKILL.md", "skills/natural-evidence-editor/SKILL.md", "skills/evidence-fact-review/SKILL.md"}
                        if approved and file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(folder.resolve()) and file.stat().st_size <= 800_000:
                            archive.write(file, file.relative_to(folder).as_posix())
                return self.send(200, buffer.getvalue(), "application/zip", "WorkBuddy任务-" + parts[3][:8] + ".zip")
            if len(parts) == 4 and parts[:3] == ["api", "agent", "runs"]:
                return self.send(200, engine.agent.detail(parts[3]))
            if len(parts) == 3 and parts[:2] == ["api", "articles"]:
                article = engine.article(parts[2])
                photos = engine.photos.list(article["id"])
                from .documents import cover_id as document_cover_id
                cover_id = document_cover_id(engine.document(article["id"]))
                defaults = {"ai": engine.models.selection(article["account_id"]), "content_depth": engine.agent.config(article["account_id"])["content_depth"]}
                from .word_export import publication_caption
                for photo in photos:
                    photo["credit_suffix"] = publication_caption({**photo, "data": {**photo["data"], "caption": "", "photo_role": "event_photo"}})
                return self.send(200, {"article": article, "publication": engine.publications.summary(article["id"], article), "document": engine.document(article["id"]), "suggestions": engine.suggestions(article["id"]), "topic": engine.topic(article["topic_id"]), "account": engine.account(article["account_id"]), "quality": engine.quality(article["id"]), "photos": photos, "cover_photo_id": cover_id, "production_defaults": defaults})
            if len(parts) == 4 and parts[:2] == ["api", "photos"] and parts[3] == "file":
                content, mime = engine.photos.content(parts[2])
                return self.send(200, content, mime)
            if len(parts) == 4 and parts[:2] == ["api", "articles"]:
                if parts[3] == "publication":
                    return self.send(200, engine.publications.detail(parts[2]))
                if parts[3] == "cover":
                    assets = engine.photos.selected(parts[2])
                    content, mime = (engine.photos.cover_bytes(parts[2]), "image/jpeg") if assets else (engine.cover(parts[2]), "image/png")
                    return self.send(200, content, mime)
                if parts[3] == "cover.png":
                    return self.send(200, engine.cover(parts[2]), "image/png")
                if parts[3] == "facts-card.png":
                    return self.send(200, engine.fact_card(parts[2]), "image/png")
                if parts[3] == "export":
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    return self.send(200, engine.export_article(parts[2], int(query["revision"][0]) if "revision" in query else None, query.get("skip_missing") == ["1"]), "application/zip", f"图文交付-{parts[2][:8]}.zip")
                if parts[3] == "toutiao.docx":
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    return self.send(200, engine.export_word(parts[2], int(query["revision"][0]) if "revision" in query else None, query.get("skip_missing") == ["1"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f"头条图文-{parts[2][:8]}.docx")
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "package":
                job = engine.store.one("SELECT * FROM jobs WHERE id=?", (parts[2],))
                if not job:
                    raise ValueError("任务不存在")
                import io, zipfile
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for file in (engine.data_dir / "jobs" / job["id"]).glob("*"):
                        if file.is_file() and file.suffix in (".md", ".json") and file.stat().st_size <= 1_000_000:
                            archive.write(file, file.name)
                return self.send(200, buffer.getvalue(), "application/zip", f"AI任务-{job['id'][:8]}.zip")
            return self.send(404, {"error": "页面不存在"})

        def dispatch_post(self):
            self.guard(True)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError("请求为空或超过 1MB")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求需为 JSON 对象")
            path = urllib.parse.urlsplit(self.path).path
            parts = path.strip("/").split("/")
            if path == "/api/demo":
                result = engine.seed_demo()
            elif path == "/api/accounts":
                result = engine.add_account(body)
            elif path == "/api/topics":
                result = engine.add_topic(body)
            elif path == "/api/sources":
                result = engine.add_source(body)
            elif path == "/api/metrics":
                result = engine.add_metric(body)
            elif path == "/api/pause":
                result = engine.set_paused(body)
            elif path == "/api/agent/discover":
                result = engine.agent.discover(body)
            elif path == "/api/agent/produce-batch":
                from .batch_production import submit
                result = submit(engine.agent, body)
            elif path == "/api/agent/preferences":
                result = engine.agent.preferences(body)
            elif path == "/api/ai/settings":
                result = engine.agent.preferences({"account_id": body.get("account_id", "main"), "ai": body.get("ai", {})})
            elif path == "/api/ai/deepseek-key":
                engine.models.secret.set(body.get("key"))
                result = {"ok": True, **engine.models.secret.status()}
            elif path == "/api/ai/deepseek-test":
                result = engine.models.check_deepseek(body.get("generate") is True, body.get("account_id", "main"))
            elif path == "/api/agent/revise":
                result = engine.agent.create("revise", body)
            elif path == "/api/agent/illustrate":
                result = engine.agent.create("illustrate", body)
            elif path == "/api/agent/learn-style":
                result = engine.agent.create("learn_style", body)
            elif path == "/api/writer-skills/activate":
                result = engine.writer_factory.activate(body.get("mode_id", "sports"), body.get("profile_id"), body.get("expected_id"))
            elif len(parts) == 3 and parts[:2] == ["api", "photos"]:
                result = engine.photos.update(parts[2], body)
            elif path == "/api/agent/mediacrawler":
                result = engine.agent.bridge_settings(body)
            elif path == "/api/agent/mediacrawler/import":
                result = engine.agent.import_signals(body.get("account_id", "main"), body.get("records"), body.get("platform"))
            elif len(parts) == 5 and parts[:3] == ["api", "agent", "candidates"] and parts[4] == "decision":
                result = engine.agent.decide(parts[3], body)
            elif len(parts) == 5 and parts[:3] == ["api", "agent", "runs"] and parts[4] == "cancel":
                result = engine.agent.cancel(parts[3])
            elif len(parts) == 5 and parts[:3] == ["api", "agent", "runs"] and parts[4] == "external-output":
                result = engine.agent.submit_external(parts[3], body)
            elif path == "/api/modes":
                mode = validate_mode(body.get("mode"))
                folder = engine.modes.folder / mode["id"]
                if folder.exists():
                    raise ValueError("模式已存在；本入口仅新增，不覆盖现有配置")
                style = text(body.get("style", "依据事实包写作，保留分析边界。"), 20000)
                folder.mkdir()
                (folder / "mode.json").write_text(dumps(mode), encoding="utf-8")
                (folder / "style.md").write_text(style, encoding="utf-8")
                engine.modes.reload()
                result = {"id": mode["id"]}
            elif len(parts) == 3 and parts[:2] == ["api", "accounts"]:
                result = engine.update_account(parts[2], body)
            elif len(parts) == 3 and parts[:2] == ["api", "topics"]:
                result = engine.update_topic(parts[2], body)
            elif len(parts) == 4 and parts[:2] == ["api", "topics"] and parts[3] == "generate":
                provider = body.get("provider", "file")
                if provider == "agent":
                    topic = engine.topic(parts[2])
                    result = engine.agent.create("produce", {**body, "account_id": topic["account_id"], "topic_id": topic["id"]})
                else:
                    result = engine.create_job(parts[2], provider, body.get("template_id"))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "import":
                result = engine.import_job_result(parts[2], body.get("result"))
            elif len(parts) == 4 and parts[:2] == ["api", "sources"] and parts[3] == "refresh":
                result = engine.refresh_source(parts[2])
            elif len(parts) == 3 and parts[:2] == ["api", "sources"]:
                source = engine.store.one("SELECT * FROM sources WHERE id=?", (parts[2],))
                if not source:
                    raise ValueError("来源不存在")
                with engine.store.db() as db:
                    db.execute("UPDATE sources SET enabled=? WHERE id=?", (int(body.get("enabled") is True), parts[2]))
                result = {"ok": True}
            elif len(parts) == 4 and parts[:2] == ["api", "articles"]:
                action = parts[3]
                if action == "edit":
                    result = engine.edit_article(parts[2], body)
                elif action == "publication":
                    result = engine.publications.change(parts[2], body)
                elif action == "export-complete":
                    result = engine.publications.complete_export(parts[2], body.get("receipt"))
                elif action == "export-download":
                    content, receipt = engine.publications.prepare_export(parts[2], body)
                    format = body.get("format", "docx")
                    mime = "application/zip" if format == "zip" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    return self.send(200, content, mime, f"头条图文-{parts[2][:8]}.{format}", extra_headers={"X-Export-Id": receipt})
                elif action == "document":
                    result = engine.save_document(parts[2], body)
                    result["publication"] = engine.publications.summary(parts[2])
                elif action == "adopt-suggestion":
                    result = engine.apply_suggestion(parts[2], body.get("suggestion_id"), body)
                elif action == "approve":
                    result = engine.approve(parts[2], body)
                elif action == "schedule":
                    result = engine.schedule(parts[2], body)
                else:
                    raise ValueError("稿件动作不存在")
            elif len(parts) == 3 and parts[:2] == ["api", "schedules"]:
                result = engine.confirm_schedule(parts[2], body)
            elif len(parts) == 3 and parts[:2] == ["api", "policies"]:
                result = engine.apply_policy(parts[2], body)
            else:
                return self.send(404, {"error": "接口不存在"})
            return self.send(200, result)

        def handle_request(self, callback):
            try:
                callback()
            except DocumentConflict as exc:
                self.send(409, {"error": str(exc), "code": "revision_conflict"})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self.send(400, {"error": str(exc) or "参数无效"})
            except sqlite3.IntegrityError:
                self.send(409, {"error": "该记录已存在或状态关联无效，请刷新检查"})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                self.send(500, {"error": "本地处理失败，请检查运行日志；未执行任何平台发布"})

        def do_GET(self):
            self.handle_request(self.dispatch_get)

        def do_POST(self):
            self.handle_request(self.dispatch_post)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.engine = engine
    return server
