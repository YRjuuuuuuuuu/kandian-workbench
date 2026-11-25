import datetime as dt
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import threading
import urllib.parse
import uuid
import zipfile
from pathlib import Path

from .modes import Modes
from .sources import fetch_feed, parse_feed
from .store import Store, dumps
from . import __version__
from . import timeliness

UTC = dt.timezone.utc
CN = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]


def now():
    return dt.datetime.now(UTC)


def iso(value=None):
    return (value or now()).astimezone(UTC).isoformat(timespec="seconds")


def date(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (result.replace(tzinfo=CN) if result.tzinfo is None else result).astimezone(UTC)
    except (ValueError, TypeError):
        raise ValueError("日期无效，请使用 ISO 日期时间；未带时区按北京时间解释")


def uid():
    return uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def text(value, limit=10000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"文本无效或超过 {limit} 字")
    return value.strip()


def https_url(value):
    value = text(value, 2000)
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
    except ValueError:
        raise ValueError("来源链接必须是有效的 HTTPS 地址")
    return value


ARTICLE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "job_id": {"type": "string"},
        "input_hash": {"type": "string"},
        "title": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"text": {"type": "string"}, "kind": {"type": "string", "enum": ["fact", "analysis"]}, "claim_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["text", "kind", "claim_ids"]}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
        "image_queries": {"type": "array", "items": {"type": "string"}}
    }, "required": ["job_id", "input_hash", "title", "paragraphs", "unresolved", "notes", "image_queries"]
}


class Engine:
    def __init__(self, data_dir=None, modes_dir=None):
        self.data_dir = Path(data_dir or ROOT / "data").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.deepseek_only = (ROOT / "standalone.deepseek").is_file() or (self.data_dir / "standalone.deepseek").is_file()
        self.store = Store(self.data_dir / "workbench.sqlite3")
        self.modes = Modes(modes_dir or ROOT / "modes")
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.initialize()
        from .ai import AIModels
        self.models = AIModels(self)
        from .agent import EditorialAgent
        self.agent = EditorialAgent(self)
        from .photos import PhotoManager
        self.photos = PhotoManager(self)
        from .writer_factory import WriterFactory
        self.writer_factory = WriterFactory(self)
        from .publishing import PublicationTracker
        self.publications = PublicationTracker(self)

    def initialize(self):
        with self.store.db() as db:
            if not db.execute("SELECT 1 FROM accounts").fetchone():
                db.execute("INSERT INTO accounts(id,name,mode_id) VALUES(?,?,?)", ("main", "默认账号", "sports" if "sports" in self.modes.items else next(iter(self.modes.items))))
            db.execute("INSERT OR IGNORE INTO settings VALUES('paused','false')")

    def audit(self, db, action, object_id, detail):
        db.execute("INSERT INTO audit(created_at,action,object_id,detail) VALUES(?,?,?,?)", (iso(), action, object_id, dumps(detail)))

    def account(self, account_id):
        result = self.store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not result:
            raise ValueError("账号不存在")
        return result

    def topic(self, topic_id):
        result = self.store.one("SELECT * FROM topics WHERE id=?", (topic_id,))
        if not result:
            raise ValueError("选题不存在")
        result["data"] = json.loads(result["data"])
        return result

    def article(self, article_id):
        result = self.store.one("SELECT * FROM articles WHERE id=?", (article_id,))
        if not result:
            raise ValueError("稿件不存在")
        result["data"] = json.loads(result["data"])
        return result

    def material_hash(self, topic, template_id):
        return digest({"topic": topic["data"], "version": topic["version"], "account": topic["account_id"], "mode": self.modes.get(topic["mode_id"]), "template": template_id})

    def add_account(self, body):
        mode_id = body.get("mode_id")
        self.modes.get(mode_id)
        name = text(body.get("name"), 60)
        limit = body.get("daily_limit", 2)
        if not name or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("填写账号名和 1–10 的单日计划上限")
        account_id = uid()
        with self.store.db() as db:
            db.execute("INSERT INTO accounts(id,name,mode_id,daily_limit) VALUES(?,?,?,?)", (account_id, name, mode_id, limit))
        return {"id": account_id}

    def update_account(self, account_id, body):
        account = self.account(account_id)
        name = text(body.get("name", account["name"]), 60)
        limit = body.get("daily_limit", account["daily_limit"])
        if not name or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("账号名称或上限无效")
        with self.store.db() as db:
            db.execute("UPDATE accounts SET name=?,paused=?,daily_limit=? WHERE id=?", (name, int(bool(body.get("paused", account["paused"]))), limit, account_id))
        return {"ok": True}

    def policy(self, account_id):
        row = self.store.one("SELECT * FROM policies WHERE account_id=? AND active=1 ORDER BY version DESC LIMIT 1", (account_id,))
        return {"version": row["version"], **json.loads(row["data"])} if row else {"version": 0, "template_boosts": {}}

    def topic_score(self, topic):
        data = topic["data"]
        assessment = self.modes.assess(topic["mode_id"], data["title"] + " " + data["summary"])
        age = max(0, (now() - date(data["event_at"])).total_seconds() / 3600)
        scores = {"heat": data.get("heat", 0), "rise": data.get("rise") or 0, "freshness": max(0, 100 - age * 100 / 48), "relevance": 100 if assessment["relevant"] else 0, "value": data.get("value", 50)}
        rank = sum(self.modes.get(topic["mode_id"])["scoring_weights"][key] * value for key, value in scores.items())
        boosts = self.policy(topic["account_id"])["template_boosts"]
        rank += boosts.get(data["template_id"], 0)
        return {"score": round(rank, 1), "components": scores, "assessment": assessment, "expired": date(data["expires_at"]) <= now() or (not topic["is_demo"] and bool(timeliness.error(self.modes.get(topic["mode_id"]), data, now()))), "rise_unknown": data.get("rise") is None}

    def normalize_topic(self, body, account, previous=None):
        mode_id = account["mode_id"]
        template_id = body.get("template_id") or self.modes.get(mode_id)["templates"][0]["id"]
        template = self.modes.template(mode_id, template_id)
        title = text(body.get("title"), 180)
        summary = text(body.get("summary", ""), 10000)
        if not title:
            raise ValueError("选题标题不能为空")
        event_at = date(body.get("event_at") or iso())
        expires_at = date(body["expires_at"]) if body.get("expires_at") else event_at + dt.timedelta(hours=template["ttl_hours"])
        if expires_at <= event_at:
            raise ValueError("失效时间必须晚于事件时间")
        facts = body.get("facts", [])
        if not isinstance(facts, list) or len(facts) > 60:
            raise ValueError("事实应为列表，最多 60 项")
        normalized = []
        ids = set()
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                raise ValueError("事实条目需为对象")
            fact_id = text(fact.get("id", f"f{index+1}"), 40)
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", fact_id) or fact_id in ids:
                raise ValueError("事实 id 需唯一且仅含英文、数字、下划线、连字符")
            ids.add(fact_id)
            normalized.append({"id": fact_id, "text": text(fact.get("text"), 4000), "url": https_url(fact.get("url", "")), "verified": fact.get("verified") is True, "kind": text(fact.get("kind", "official"), 40), "speaker": text(fact.get("speaker", ""), 120), "original": text(fact.get("original", ""), 6000), "translation": text(fact.get("translation", ""), 6000), "context": text(fact.get("context", ""), 2000), "checked_at": iso(date(fact["checked_at"])) if fact.get("checked_at") else None})
            if not normalized[-1]["text"]:
                raise ValueError("事实文本不能为空")
            if normalized[-1]["verified"] and not normalized[-1]["checked_at"]:
                normalized[-1]["checked_at"] = iso()
            if isinstance(fact.get("agent_check"), dict):
                check = fact["agent_check"]
                research = self.store.one("SELECT account_id FROM agent_runs WHERE id=?", (check.get("run_id", ""),))
                if research and research["account_id"] == account["id"] and check.get("method") in ("native_web_search_primary_or_independent", "bounded_source_enrichment"):
                    normalized[-1]["agent_check"] = check
        scores = {}
        for key in ("heat", "rise", "value"):
            value = body.get(key, None if key == "rise" else 50)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError("热度、增速、价值的编辑评分需为 0–100；未知增速使用 null")
            scores[key] = value
        result = {"title": title, "summary": summary, "template_id": template_id, "event_at": iso(event_at), "expires_at": iso(expires_at), "event_final": body.get("event_final") is True, "facts": normalized, "url": https_url(body["url"]) if body.get("url") else "", "source_name": text(body.get("source_name", "手动导入"), 120), **scores}
        if isinstance(body.get("agent_origin"), dict):
            result["agent_origin"] = body["agent_origin"]
        precision = body.get("time_precision", (previous or {}).get("data", {}).get("time_precision", "minute"))
        result["time_precision"] = precision if precision in ("minute", "date") else "minute"
        if body.get("event_end_at"):
            result["event_end_at"] = iso(date(body["event_end_at"]))
        result["event_date_cn"] = text(body.get("event_date_cn") or timeliness.event_day(result).isoformat(), 10)
        day_error = timeliness.error(self.modes.get(mode_id), result, now())
        if day_error and not account["is_demo"]:
            raise ValueError(day_error)
        if not account["is_demo"]:
            result["expires_at"] = iso(timeliness.deadline(self.modes.get(mode_id), result, expires_at, now()))
        return result

    def add_topic(self, body):
        account = self.account(body.get("account_id", "main"))
        data = self.normalize_topic(body, account)
        event_key = text(body.get("event_key") or digest({"title": data["title"], "day": data["event_at"][:10]})[:24], 120)
        topic_id = uid()
        with self.store.db() as db:
            duplicate = db.execute("SELECT id FROM topics WHERE account_id=? AND event_key=?", (account["id"], event_key)).fetchone()
            if duplicate:
                return {"id": duplicate["id"], "duplicate": True}
            db.execute("INSERT INTO topics(id,account_id,mode_id,event_key,data,is_demo,created_at) VALUES(?,?,?,?,?,?,?)", (topic_id, account["id"], account["mode_id"], event_key, dumps(data), account["is_demo"], iso()))
            self.audit(db, "topic.import", topic_id, {"mode": account["mode_id"]})
        return {"id": topic_id}

    def update_topic(self, topic_id, body):
        with self.lock:
            old = self.topic(topic_id)
            data = self.normalize_topic(body, self.account(old["account_id"]), old)
            with self.store.db() as db:
                db.execute("UPDATE topics SET data=?,version=version+1 WHERE id=?", (dumps(data), topic_id))
                db.execute("UPDATE articles SET approval_hash=NULL,status=CASE WHEN status IN ('live','submitted','platform_scheduled') THEN 'correction_required' ELSE 'needs_review' END WHERE topic_id=?", (topic_id,))
                db.execute("UPDATE schedules SET state='needs_review',updated_at=? WHERE article_id IN (SELECT id FROM articles WHERE topic_id=?) AND state IN ('scheduled','ready')", (iso(), topic_id))
                self.audit(db, "topic.correct", topic_id, {"previous_version": old["version"]})
        return {"ok": True}

    def add_source(self, body):
        mode_id = body.get("mode_id", "sports")
        self.modes.get(mode_id)
        account = self.account(body.get("account_id", "main"))
        if account["mode_id"] != mode_id:
            raise ValueError("来源与账号模式需一致")
        name = text(body.get("name"), 80)
        url = https_url(body.get("url", ""))
        note = text(body.get("rights_note", ""), 2000)
        interval = body.get("interval_minutes", 30)
        if not name or len(note) < 8 or body.get("permission_confirmed") is not True:
            raise ValueError("请记录来源获取与使用依据，并明确确认已核实适用权限")
        if not isinstance(interval, int) or not 15 <= interval <= 1440:
            raise ValueError("RSS 间隔为 15–1440 分钟，仍需符合来源限额")
        source_id = uid()
        with self.store.db() as db:
            db.execute("INSERT INTO sources(id,name,mode_id,url,rights_note,enabled,interval_minutes) VALUES(?,?,?,?,?,?,?)", (source_id, name, mode_id, url, dumps({"note": note, "account_id": account["id"]}), 1, interval))
        return {"id": source_id}

    def refresh_source(self, source_id):
        if self.is_paused():
            raise ValueError("全局任务已暂停，恢复后才能读取来源")
        source = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not source or not source["enabled"]:
            raise ValueError("来源未启用")
        if self.account(json.loads(source["rights_note"])["account_id"])["paused"]:
            raise ValueError("该来源所属账号已暂停")
        if source["last_check"] and (now() - date(source["last_check"])).total_seconds() < source["interval_minutes"] * 60:
            raise ValueError("来源仍在读取间隔内，请稍后再试")
        # Claim the interval before network I/O to prevent concurrent polling.
        with self.lock, self.store.db() as db:
            changed = db.execute("UPDATE sources SET last_check=? WHERE id=? AND last_check IS ?", (iso(), source_id, source["last_check"])).rowcount
            if not changed:
                raise ValueError("来源正在读取")
        try:
            body, etag, modified = fetch_feed(source["url"], source["etag"], source["modified"])
            imported = 0
            for item in parse_feed(body) if body else []:
                if not item["event_at"]:
                    continue  # No invented event date for undated feed entries.
                result = self.add_topic({"account_id": json.loads(source["rights_note"])["account_id"], "event_key": digest(item["url"])[:32], "title": item["title"], "summary": item["summary"], "event_at": item["event_at"], "source_name": source["name"], "url": item["url"], "heat": 0, "rise": None, "facts": []})
                imported += not result.get("duplicate", False)
            with self.store.db() as db:
                db.execute("UPDATE sources SET last_error=NULL,etag=?,modified=? WHERE id=?", (etag, modified, source_id))
            return {"imported": imported, "note": "RSS 只创建候选，不视为已核实事实；热度暂无数据"}
        except Exception as exc:
            with self.store.db() as db:
                db.execute("UPDATE sources SET enabled=0,last_error=? WHERE id=?", (str(exc)[:500], source_id))
            raise ValueError(f"读取失败，来源已暂停：{exc}")

    def evidence_errors(self, topic, template_id):
        data = topic["data"]
        errors = []
        if not topic["is_demo"]:
            day_error = timeliness.error(self.modes.get(topic["mode_id"]), data, now())
            if day_error:
                errors.append(day_error)
        assessment = self.modes.assess(topic["mode_id"], " ".join([data["title"], data["summary"], *[f["text"] for f in data["facts"]]]))
        if assessment["blocked"]:
            errors.append("命中排除项：" + "、".join(assessment["blocked"]))
        if not assessment["relevant"]:
            errors.append("没有匹配本模式题材，请调整选题或选择合适账号")
        if date(data["expires_at"]) <= now():
            errors.append("选题已超过有效期")
        if not data["facts"] or any(not f["verified"] and f.get("agent_check", {}).get("status") not in ("crosschecked", "attributed") for f in data["facts"]):
            errors.append("需有一手／独立证据、可信媒体原文归因或人工核实，才能起草")
        template = self.modes.template(topic["mode_id"], template_id)
        if "final_result" in template["validators"] and not data["event_final"]:
            errors.append("赛后快读需要确认比赛正式结束")
        if "original_quote" in template["validators"] and not any(f["original"] and f["speaker"] and f["context"] and f["translation"] for f in data["facts"]):
            errors.append("原话解读需要说话人、原文、译文与上下文")
        return errors

    def create_job(self, topic_id, provider="file", template_id=None, execution=None, content_depth=None):
        with self.lock:
            topic = self.topic(topic_id)
            account = self.account(topic["account_id"])
            template_id = template_id or topic["data"]["template_id"]
            if account["paused"] or self.is_paused():
                raise ValueError("账号或全局任务已暂停")
            errors = self.evidence_errors(topic, template_id)
            if errors:
                raise ValueError("；".join(errors))
            if provider == "codex" and self.deepseek_only:
                raise ValueError("独立版请使用 DeepSeek 生产或通用文件任务")
            if provider not in ("file", "codex"):
                raise ValueError("支持文件任务包或 Codex CLI")
            day_start = dt.datetime.combine(now().astimezone(CN).date(), dt.time(), CN)
            count = self.store.one("SELECT COUNT(*) AS n FROM jobs WHERE created_at>=?", (iso(day_start),))["n"]
            if count >= 20 and execution is None:
                raise ValueError("已达到每日 20 个生成任务上限，请控制额度")
            job_id = uid()
            mode = self.modes.get(topic["mode_id"])
            from .topic_profiles import writing_mode
            payload = {"job_id": job_id, "schema_version": 1, "topic_id": topic_id, "account_id": topic["account_id"], "mode": writing_mode(mode), "template": self.modes.template(topic["mode_id"], template_id), "evidence": topic["data"], "is_demo": bool(topic["is_demo"])}
            from .content_depth import plan
            depth = content_depth if content_depth is not None else self.agent.config(topic["account_id"])["content_depth"]
            payload["writing_policy"] = plan(payload["template"], depth, len(topic["data"]["facts"]))
            payload["template"] = {**payload["template"], "min_chars": payload["writing_policy"]["target_min_chars"], "max_chars": payload["writing_policy"]["target_max_chars"]}
            from .ai import normalize, skills_for
            payload["ai"] = normalize(execution or self.models.selection(topic["account_id"]))
            payload["shared_skills"] = skills_for("writing")
            payload["writer_skill"] = self.writer_factory.snapshot(topic["mode_id"])
            if payload["writer_skill"]:
                payload["shared_skills"]["toutiao-"+topic["mode_id"]+"-writer"] = payload["writer_skill"]["skill_markdown"]
            input_hash = self.material_hash(topic, template_id)
            payload["input_hash"] = input_hash
            folder = self.data_dir / "jobs" / job_id
            folder.mkdir(parents=True)
            (folder / "request.json").write_text(dumps(payload), encoding="utf-8")
            (folder / "evidence.json").write_text(dumps(topic["data"]), encoding="utf-8")
            (folder / "output.schema.json").write_text(dumps(ARTICLE_SCHEMA), encoding="utf-8")
            instructions = (ROOT / "prompts" / "writer.md").read_text(encoding="utf-8") + "\n\n## 本账号编辑风格\n" + mode["style"] + "\n\n## 输出格式\n参照 output.schema.json；将最终 JSON 写到 output.json。输出不得包含 Markdown 代码围栏。"
            (folder / "instructions.md").write_text(instructions, encoding="utf-8")
            (folder / "output.example.json").write_text(dumps({"job_id": job_id, "input_hash": input_hash, "title": "填写准确标题", "paragraphs": [{"text": "填写有证据的正文段落", "kind": "fact", "claim_ids": [topic["data"]["facts"][0]["id"]]}], "unresolved": [], "notes": [], "image_queries": []}), encoding="utf-8")
            with self.store.db() as db:
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id, topic_id, template_id, provider, "waiting_result" if provider == "file" else "queued", input_hash, dumps(payload), None, None, iso(), iso()))
                self.audit(db, "job.create", job_id, {"provider": provider})
            return {"id": job_id, "folder": str(folder)}

    def validate_result(self, body, topic):
        from .ai import SchemaError
        if not isinstance(body, dict):
            raise ValueError("输出必须为 JSON 对象")
        title = text(body.get("title"), 180)
        paragraphs = body.get("paragraphs")
        if not title or not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 50:
            raise ValueError("稿件需要标题与 1–50 段正文")
        ids = {f["id"] for f in topic["data"]["facts"]}
        normalized = []
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict) or paragraph.get("kind") not in ("fact", "analysis"):
                raise ValueError("每段需指定 kind=fact 或 analysis")
            claims = paragraph.get("claim_ids")
            if not isinstance(claims, list) or not all(isinstance(x, str) for x in claims) or set(claims) - ids:
                raise SchemaError("段落引用了不存在的事实 id")
            if paragraph["kind"] == "fact" and not claims:
                raise SchemaError("事实段必须关联已核实的 claim_ids")
            paragraph_text = text(paragraph.get("text"), 6000)
            if not paragraph_text:
                raise ValueError("正文段落不能为空")
            normalized.append({"text": paragraph_text, "kind": paragraph["kind"], "claim_ids": claims})
        unresolved = body.get("unresolved", [])
        if not isinstance(unresolved, list) or not all(isinstance(x, str) for x in unresolved):
            raise ValueError("unresolved 必须为字符串列表")
        notes = body.get("notes", [])
        if not isinstance(notes, list) or len(notes) > 30 or not all(isinstance(value, str) and len(value) <= 2000 for value in notes):
            raise ValueError("notes 必须为编辑说明列表，最多 30 条")
        queries = body.get("image_queries", [])
        if not isinstance(queries, list) or len(queries) > 6 or not all(isinstance(value, str) and len(value) <= 150 for value in queries):
            raise ValueError("image_queries 应为最多 6 条配图检索关键词")
        if sum(len(p["text"]) for p in normalized) > 20000:
            raise ValueError("正文超过 20000 字上限")
        return {"title": title, "paragraphs": normalized, "unresolved": unresolved, "notes": notes, "image_queries": queries}

    def import_job_result(self, job_id, body=None):
        with self.lock:
            job = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job or job["state"] not in ("waiting_result", "running", "failed"):
                raise ValueError("任务不存在或已完成")
            topic = self.topic(job["topic_id"])
            if job["input_hash"] != self.material_hash(topic, job["template_id"]):
                raise ValueError("事实包或模式已经变化，请建立新任务")
            if self.evidence_errors(topic, job["template_id"]):
                raise ValueError("事实已过期或不再满足检查")
            if body is None:
                path = self.data_dir / "jobs" / job_id / "output.json"
                if not path.is_file() or path.stat().st_size > 500000:
                    raise ValueError("未找到有效 output.json，或文件超过大小上限")
                body = json.loads(path.read_text(encoding="utf-8-sig"))
            if body.get("job_id") != job_id or body.get("input_hash") != job["input_hash"]:
                raise ValueError("输出任务 id 或输入哈希不匹配，拒绝串稿")
            data = self.validate_result(body, topic)
            policy = json.loads(job["payload"]).get("writing_policy")
            if policy:
                data["writing_policy"] = policy
            writer = json.loads(job["payload"]).get("writer_skill")
            if writer:
                data["writer_skill"] = {k:v for k,v in writer.items() if k != "skill_markdown"}
            article_id = uid()
            revision_hash = digest({"data": data, "material": job["input_hash"]})
            with self.store.db() as db:
                db.execute("INSERT INTO articles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (article_id, job["topic_id"], topic["account_id"], job["template_id"], 1, dumps(data), job["input_hash"], revision_hash, None, "needs_review", iso(), iso()))
                db.execute("INSERT INTO revisions(article_id,revision,data,revision_hash,created_at) VALUES(?,?,?,?,?)", (article_id, 1, dumps(data), revision_hash, iso()))
                db.execute("UPDATE jobs SET state='succeeded',result=?,error=NULL,updated_at=? WHERE id=?", (article_id, iso(), job_id))
                self.audit(db, "article.generated", article_id, {"job": job_id})
            return {"article_id": article_id}

    def run_codex(self, job):
        from .ai import normalize
        if self.deepseek_only:
            raise ValueError("此旧任务请使用 DeepSeek 重新创建")
        folder = self.data_dir / "jobs" / job["id"]
        payload = json.loads(job["payload"])
        selection = normalize(payload.get("ai"))
        prompt = (folder / "instructions.md").read_text(encoding="utf-8") + "\n共享技能与事实包：\n" + dumps(payload)
        output, telemetry = self.models.adapter(selection, stage="writing").run(folder, "output", prompt, ARTICLE_SCHEMA, lambda *args: None, self.stop.is_set, timeout=300)
        (folder / "output.json").write_text(dumps(output), encoding="utf-8")
        return self.import_job_result(job["id"], output)

    def edit_article(self, article_id, body):
        with self.lock:
            old = self.article(article_id)
            if body.get("base_revision") != old["revision"]:
                from .documents import DocumentConflict
                raise DocumentConflict("稿件版本已变化；当前编辑已保留，请比较另一个窗口的版本")
            if old["status"] in ("live", "submitted", "platform_scheduled", "correction_required"):
                raise ValueError("已提交平台的稿件保留历史；请在平台处理更正，本地另建跟进稿")
            topic = self.topic(old["topic_id"])
            data = self.validate_result({**old["data"], **body}, topic)
            if "content_depth" in body:
                from .content_depth import plan
                data["writing_policy"] = plan(self.modes.template(topic["mode_id"], old["template_id"]), body["content_depth"], len(topic["data"]["facts"]))
            elif old["data"].get("writing_policy"):
                data["writing_policy"] = old["data"]["writing_policy"]
            if old["data"].get("writer_skill"):
                data["writer_skill"] = old["data"]["writer_skill"]
            # Model/legacy text edits retain image order and captions. Images are
            # never reselected or silently discarded when prose changes.
            if old["data"].get("document"):
                from .documents import adapt, block_id
                document = adapt(old, self.photos.list(article_id))
                texts = iter(data["paragraphs"])
                blocks = []
                for block in document["blocks"]:
                    if block["type"] == "image":
                        blocks.append(block)
                    else:
                        paragraph = next(texts, None)
                        if paragraph:
                            blocks.append({**block, **paragraph})
                blocks.extend({"id": uid(), "type": "text", **p} for p in texts)
                data["document"] = {**document, "title": data["title"], "blocks": blocks}
            material = self.material_hash(topic, old["template_id"])
            revision = old["revision"] + 1
            revision_hash = digest({"data": data, "material": material})
            with self.store.db() as db:
                db.execute("UPDATE articles SET data=?,revision=?,material_hash=?,revision_hash=?,approval_hash=NULL,status='needs_review',updated_at=? WHERE id=?", (dumps(data), revision, material, revision_hash, iso(), article_id))
                db.execute("INSERT INTO revisions(article_id,revision,data,revision_hash,created_at) VALUES(?,?,?,?,?)", (article_id, revision, dumps(data), revision_hash, iso()))
                db.execute("UPDATE schedules SET state='needs_review',updated_at=? WHERE article_id=? AND state IN ('scheduled','ready')", (iso(), article_id))
                self.audit(db, "article.edit", article_id, {"revision": revision})
        return {"ok": True}

    def document(self, article_id):
        from .documents import adapt
        return adapt(self.article(article_id), self.photos.list(article_id))

    def save_document(self, article_id, body):
        from .documents import DocumentConflict, normalize, cover_id
        with self.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self.article(article_id)
            if type(body.get("base_revision")) is not int or body["base_revision"] != old["revision"]:
                raise DocumentConflict("另一窗口已保存新版本，当前编辑仍在本地。请比较版本后决定如何保存")
            document, warning = normalize(body.get("document"), self.photos.list(article_id), self.document(article_id))
            data = {**old["data"], "title": document["title"], "document": document,
                    "paragraphs": [{k: b[k] for k in ("text", "kind", "claim_ids")} for b in document["blocks"] if b["type"] == "text"]}
            revision = old["revision"] + 1
            revision_hash = digest({"data": data, "material": old["material_hash"]})
            # Preserve historical remote state; editing locally never claims a
            # remote update or changes published content.
            status = old["status"] if old["status"] in ("live", "submitted", "platform_scheduled", "correction_required") else "needs_review"
            db.execute("UPDATE articles SET data=?,revision=?,revision_hash=?,approval_hash=NULL,status=?,updated_at=? WHERE id=?", (dumps(data), revision, revision_hash, status, iso(), article_id))
            db.execute("INSERT INTO revisions(article_id,revision,data,revision_hash,created_at) VALUES(?,?,?,?,?)", (article_id, revision, dumps(data), revision_hash, iso()))
            chosen, position, placements = cover_id(document), 0, {}
            for block in document["blocks"]:
                if block["type"] == "text":
                    position += 1
                else:
                    placements[block["asset_id"]] = (max(1, position), block["caption"])
            for asset in self.photos.list(article_id):
                info = {**asset["data"], "use_as_cover": document["cover"]["mode"] == "manual" and asset["id"] == chosen}
                if asset["id"] in placements:
                    info["caption"] = placements[asset["id"]][1]
                db.execute("UPDATE photo_assets SET selected=?,position=?,data=? WHERE id=?", (int(asset["id"] in placements), placements.get(asset["id"], (asset["position"],))[0], dumps(info), asset["id"]))
            db.execute("UPDATE schedules SET state='needs_review',updated_at=? WHERE article_id=? AND state IN ('scheduled','ready')", (iso(), article_id))
            self.audit(db, "document.save", article_id, {"revision": revision, "blocks": len(document["blocks"])})
        return {"ok": True, "revision": revision, "revision_hash": revision_hash, "document": document, "warning": warning}

    def suggest_revision(self, article_id, run_id, base_revision, output, policy):
        data = self.validate_result(output, self.topic(self.article(article_id)["topic_id"]))
        data["writing_policy"] = policy
        suggestion_id = uid()
        with self.store.db() as db:
            db.execute("INSERT INTO article_suggestions VALUES(?,?,?,?,?,?,?)", (suggestion_id, article_id, run_id, base_revision, dumps(data), "pending", iso()))
        return suggestion_id

    def suggestions(self, article_id):
        return [{**row, "data": json.loads(row["data"])} for row in self.store.all("SELECT * FROM article_suggestions WHERE article_id=? AND state='pending' ORDER BY created_at DESC", (article_id,))]

    def apply_suggestion(self, article_id, suggestion_id, body):
        from .documents import DocumentConflict
        with self.lock:
            suggestion = self.store.one("SELECT * FROM article_suggestions WHERE id=? AND article_id=? AND state='pending'", (suggestion_id, article_id))
            if not suggestion:
                raise ValueError("改稿建议不存在或已处理")
            article = self.article(article_id)
            if body.get("base_revision") != article["revision"] or suggestion["base_revision"] != article["revision"]:
                raise DocumentConflict("建议生成后正文已有修改。请复制需要的内容手动合并，未覆盖当前稿件")
            data = json.loads(suggestion["data"])
            result = self.edit_article(article_id, {**data, "base_revision": article["revision"], "content_depth": data.get("writing_policy", {}).get("level", "standard")})
            with self.store.db() as db:
                db.execute("UPDATE article_suggestions SET state='adopted' WHERE id=?", (suggestion_id,))
            return result

    def cover(self, article_id):
        from .images import cover_png
        article = self.article(article_id)
        topic = self.topic(article["topic_id"])
        return cover_png(article["data"]["title"], self.modes.get(topic["mode_id"])["name"], date(topic["data"]["event_at"]).astimezone(CN).strftime("%Y.%m.%d"), bool(topic["is_demo"]))

    def fact_card(self, article_id):
        from .images import fact_card_png
        article = self.article(article_id)
        topic = self.topic(article["topic_id"])
        return fact_card_png(article["data"]["title"], self.modes.get(topic["mode_id"])["name"], tuple(fact["text"] for fact in topic["data"]["facts"][:3]), date(topic["data"]["event_at"]).astimezone(CN).strftime("%Y.%m.%d"), bool(topic["is_demo"]))

    def approval_signature(self, article):
        assets = self.photos.selected(article["id"])
        for asset in assets:
            self.photos.content(asset["id"])
        return digest({"revision": article["revision_hash"], "account_id": article["account_id"], "cover_sha256": hashlib.sha256(self.photos.cover_bytes(article["id"]) if assets else self.cover(article["id"])).hexdigest(), "fact_card_sha256": hashlib.sha256(self.fact_card(article["id"])).hexdigest(), "photos": [{"id": asset["id"], "sha256": asset["sha256"], "position": asset["position"], "data": asset["data"]} for asset in assets], "ai_label": True, "publisher": "manual_native_schedule"})

    def quality(self, article_id, media=True):
        import difflib
        article = self.article(article_id)
        topic = self.topic(article["topic_id"])
        data = article["data"]
        errors = self.evidence_errors(topic, article["template_id"])
        warnings = []
        body = "\n\n".join(p["text"] for p in data["paragraphs"])
        if not 2 <= len(data["title"]) <= 30:
            errors.append("当前头条标题应为 2–30 字")
        if data["unresolved"]:
            errors.append("存在未解决事项：" + "；".join(data["unresolved"]))
        if article["material_hash"] != self.material_hash(topic, article["template_id"]):
            errors.append("事实包或模式已变更，需重新生成或编辑并复核")
        hits = self.modes.assess(topic["mode_id"], data["title"] + " " + body)["blocked"]
        if hits:
            errors.append("正文／标题命中排除项：" + "、".join(hits))
        facts = topic["data"]["facts"]
        facts_by_id = {fact["id"]: fact for fact in facts}
        def numbers(value):
            from decimal import Decimal
            return {format(Decimal(n).normalize(), "f") for n in re.findall(r"\d+(?:\.\d+)?", value)}
        known_numbers = numbers(" ".join(f["text"] + " " + f["original"] + " " + f["translation"] for f in facts))
        # A checked event date is evidence too. Midnight placeholders and local
        # publication/expiry timestamps are deliberately not number sources.
        if topic["data"].get("event_date_cn"):
            known_numbers |= numbers(topic["data"]["event_date_cn"])
        unknown_numbers = numbers(data["title"] + " " + body) - known_numbers
        # Rounded thresholds such as 40+ describe 42/45 without asserting an
        # additional exact statistic. Never format Decimal(40) as 4E+1.
        unknown_numbers = {n for n in unknown_numbers if not (
            re.search(r"(?:突破|超过|至少|达到)\s*" + re.escape(n) + r"|" + re.escape(n) + r"(?:分)?(?:以上|[+＋])", data["title"] + body)
            and any(float(n) <= float(k) < float(n) + 10 for k in known_numbers))}
        if unknown_numbers:
            errors.append("存在事实包未支持的数字；请核实并补入事实条目：" + "、".join(sorted(unknown_numbers)))
        for paragraph in data["paragraphs"]:
            if paragraph["kind"] == "fact" and (not paragraph["claim_ids"] or any(key not in facts_by_id for key in paragraph["claim_ids"])):
                errors.append("事实段缺少有效来源映射")
        if not any(p["kind"] == "analysis" for p in data["paragraphs"]):
            errors.append("需补充明确的分析或解释增量，不能只拼接事实")
        if any(word in data["title"] for word in ("全网炸锅", "全网疯传", "集体怒斥", "所有人都", "震惊")):
            errors.append("标题存在缺乏依据的夸大表达")
        if any(word in body for word in ("我在现场", "我亲眼", "我采访", "我看完整场")):
            errors.append("稿件声称亲历；本工作流不提供此类经历证明，请删除或转为可验证叙述")
        if len({p["text"] for p in data["paragraphs"]}) < len(data["paragraphs"]):
            errors.append("存在重复段落")
        template = self.modes.template(topic["mode_id"], article["template_id"])
        if len(body) < 120:
            errors.append("正文过短，尚不足以作为可发布图文")
        else:
            policy = data.get("writing_policy", {})
            low, high = policy.get("target_min_chars", template["min_chars"]), policy.get("target_max_chars", template["max_chars"])
            if not low <= len(body) <= high:
                warnings.append(f"正文 {len(body)} 字，{policy.get('name', '模板')}档建议 {low}–{high} 字；按材料展开，不补编事实或凑字数")
        for other in self.store.all("SELECT id,data FROM articles WHERE id<>? ORDER BY created_at DESC LIMIT 150", (article_id,)):
            other_body = "\n\n".join(p["text"] for p in json.loads(other["data"])["paragraphs"])
            similarity = difflib.SequenceMatcher(None, body[:10000], other_body[:10000], autojunk=False).ratio()
            if similarity >= .88:
                errors.append("与工作台另一稿件高度相似，请合并或增加实质新信息")
                break
        if not all(f["kind"] == "official" for f in facts):
            warnings.append("包含非官方来源，请特别检查转载归属及事实交叉核对")
        if topic["is_demo"]:
            warnings.append("合成演示：导出包带演示标识，不计入真实发布／收益")
        if any(not fact["verified"] and fact.get("agent_check", {}).get("status") in ("crosschecked", "attributed") for fact in facts):
            warnings.append("部分事实为 Agent 初步核对或媒体原文归因，尚未人工终审；请核对原始出处后再审定")
        if topic["data"].get("time_precision") == "date":
            warnings.append("事件只核实到日期；不要把零点当作发生时刻。失效时间是日期区间上界推导的编辑保鲜上限，发布前仍需判断时效")
        warnings.append("自动检查不能证明全部事实真实或保证平台推荐；审定时仍需检查原始材料")
        warnings.extend("编辑说明：" + note for note in data.get("notes", []))
        if topic["data"].get("agent_origin"):
            assets = self.photos.selected(article_id)
            if not assets:
                warnings.append("尚未选入真实赛事照片；当前只有图卡，可在配图素材区重新检索或补充素材")
                if media and self.modes.get(topic["mode_id"]).get("agent", {}).get("require_photos", True):
                    errors.append("缺少合适的封面与配图，请让 Agent 补图并选择图片后再审定")
            if any(not asset["data"].get("reuse_verified") for asset in assets):
                warnings.append("部分配图为公开报道的学习参考素材，公开可见不等于已获转载许可；审定时需核对本次使用依据")
        try:
            signature = self.approval_signature(article)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
            signature = None
        suggestions = list(dict.fromkeys(errors))
        return {"errors": [], "suggestions": suggestions, "warnings": suggestions + warnings, "characters": len(body), "approved": bool(signature and article["approval_hash"] == signature ), "signature": signature}

    def approve(self, article_id, body):
        with self.lock:
            article = self.article(article_id)
            if body.get("revision_hash") != article["revision_hash"]:
                raise ValueError("稿件版本已变化，请重新审阅")
            if body.get("account_id") != article["account_id"] or not all(body.get(key) is True for key in ("facts_ack", "rights_ack", "ai_ack")):
                raise ValueError("需要核对目标账号、事实和素材权利，并确认使用适用 AI 声明")
            quality = self.quality(article_id)
            if quality["errors"]:
                raise ValueError("；".join(quality["errors"]))
            with self.store.db() as db:
                db.execute("UPDATE articles SET approval_hash=?,status='approved',updated_at=? WHERE id=?", (quality["signature"], iso(), article_id))
                self.audit(db, "article.approve", article_id, {"revision_hash": article["revision_hash"], "account_id": article["account_id"], "ai_label": True})
        return {"ok": True}

    def schedule(self, article_id, body):
        with self.lock:
            article = self.article(article_id)
            account = self.account(article["account_id"])
            topic = self.topic(article["topic_id"])
            if account["paused"] or self.is_paused():
                raise ValueError("账号或全局排期已暂停")
            if not self.quality(article_id)["approved"]:
                raise ValueError("只有当前版本已审定且未过期的稿件才能加入排期")
            due = date(body.get("due_at"))
            if due < now() - dt.timedelta(minutes=1) or due >= date(topic["data"]["expires_at"]):
                raise ValueError("排期必须在当前之后且在选题失效之前")
            if not topic["is_demo"] and timeliness.applies(self.modes.get(topic["mode_id"]), topic["data"]) and due.astimezone(CN).date() != timeliness.event_day(topic["data"]):
                raise ValueError("赛事报道只能安排在北京时间赛事当天，不能排到明天")
            day = due.astimezone(CN).date()
            start = dt.datetime.combine(day, dt.time(), CN)
            with self.store.db() as db:
                duplicates = db.execute("SELECT s.id FROM schedules s JOIN articles a ON s.article_id=a.id WHERE a.topic_id=? AND a.template_id=? AND s.state NOT IN ('cancelled','expired','needs_review')", (article["topic_id"], article["template_id"])).fetchone()
                if duplicates:
                    raise ValueError("同一事件角度已有排期或发布记录，不能重复安排")
                count = db.execute("SELECT COUNT(*) FROM schedules WHERE account_id=? AND due_at>=? AND due_at<? AND state NOT IN ('cancelled','expired','needs_review')", (account["id"], iso(start), iso(start+dt.timedelta(days=1)))).fetchone()[0]
                if count >= account["daily_limit"]:
                    raise ValueError("已达到该账号单日计划上限")
                schedule_id = uid()
                old = db.execute("SELECT id FROM schedules WHERE article_id=? AND revision_hash=?", (article_id, article["revision_hash"])).fetchone()
                if old:
                    schedule_id = old["id"]
                    db.execute("UPDATE schedules SET due_at=?,state='scheduled',updated_at=? WHERE id=?", (iso(due), iso(), schedule_id))
                else:
                    db.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?)", (schedule_id, article_id, account["id"], article["revision_hash"], iso(due), "scheduled", None, None, iso(), iso()))
                db.execute("UPDATE articles SET status='scheduled' WHERE id=?", (article_id,))
                self.audit(db, "schedule.add", schedule_id, {"due_at": iso(due), "method": "manual_native_schedule"})
            return {"id": schedule_id}

    def confirm_schedule(self, schedule_id, body):
        with self.lock:
            schedule = self.store.one("SELECT * FROM schedules WHERE id=?", (schedule_id,))
            if not schedule:
                raise ValueError("排期不存在")
            state = body.get("state")
            if state == "cancelled":
                if schedule["state"] in ("live", "platform_scheduled", "submitted"):
                    raise ValueError("平台上的作品不能在本地撤回；请先到平台处理后记录")
                with self.store.db() as db:
                    db.execute("UPDATE schedules SET state='cancelled',updated_at=? WHERE id=?", (iso(), schedule_id))
                return {"ok": True}
            if state not in ("platform_scheduled", "submitted", "live", "unknown"):
                raise ValueError("发布状态无效")
            article = self.article(schedule["article_id"])
            topic = self.topic(article["topic_id"])
            if body.get("account_id") != schedule["account_id"] or body.get("confirmed") is not True:
                raise ValueError("请明确确认已查看该账号的原生后台状态")
            if schedule["revision_hash"] != article["revision_hash"] or schedule["state"] in ("cancelled", "needs_review"):
                raise ValueError("稿件版本已变化或排期已取消，请重新核对")
            if topic["is_demo"]:
                raise ValueError("演示样例不能登记为真实平台发布；可体验导出和本地排期")
            proof = text(body.get("proof_note", ""), 2000)
            if len(proof) < 4:
                raise ValueError("请记录后台状态依据，例如核对时间与作品标识")
            url = text(body.get("remote_url", ""), 2000)
            if url:
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "www.toutiao.com" or parsed.username or parsed.password or not re.fullmatch(r"/(?:article|w)/[0-9]{6,}/?", parsed.path):
                    raise ValueError("作品链接需为 https://www.toutiao.com/article/数字/ 或 /w/数字/")
            if state == "live" and not url:
                raise ValueError("记录已公开需提供真实头条作品链接")
            with self.store.db() as db:
                db.execute("UPDATE schedules SET state=?,remote_url=?,proof_note=?,updated_at=? WHERE id=?", (state, url or None, proof, iso(), schedule_id))
                db.execute("UPDATE articles SET status=?,updated_at=? WHERE id=?", (state, iso(), article["id"]))
                self.audit(db, "publish.manual_confirmation", schedule_id, {"state": state, "proof": proof})
        return {"ok": True}

    def export_article(self, article_id, expected_revision=None, skip_missing=False):
        from .documents import reader_blocks, html_body, cover_id, DocumentConflict
        from .word_export import build
        with self.lock:
            article = self.article(article_id)
            if expected_revision is not None and article["revision"] != expected_revision:
                raise DocumentConflict("导出前稿件发生变化，请保存当前内容后重新导出")
            document = self.document(article_id)
            if not document["title"].strip() or not any(b.get("text", "").strip() for b in document["blocks"]):
                raise ValueError("请先填写标题和正文再导出")
            photos = self.photos.list(article_id)
            blocks = reader_blocks(document, photos)
            files, paths, credits, skipped = {}, {}, [], []
            available = []
            for block in blocks:
                if block["type"] == "text":
                    available.append(block)
                    continue
                asset = block["asset"]
                try:
                    content = self.photos.content(asset["id"])[0]
                except (ValueError, OSError):
                    if not skip_missing:
                        raise ValueError("正文图片无法读取，请替换图片后重试；也可明确选择跳过缺失图片导出") from None
                    skipped.append(asset["id"])
                    continue
                name = f"photos/{len(paths)+1:02d}-{asset['id'][:12]}." + asset["local_name"].rsplit(".", 1)[-1]
                files[name], paths[asset["id"]] = content, name
                credits.append({"path": name, "asset_id": asset["id"], "sha256": asset["sha256"], "source_url": asset["source_url"], "image_url": asset["image_url"], **asset["data"]})
                available.append(block)
            topic, account = self.topic(article["topic_id"]), self.account(article["account_id"])
            title = document["title"]
            word = build(title, [], [], self.photos.content, draft=bool(topic["is_demo"]), blocks=available)
            prefix = "【合成演示，请勿当作新闻发布】\n\n" if topic["is_demo"] else ""
            markdown = prefix + "# " + title + "\n\n" + "\n\n".join(b["text"] if b["type"] == "text" else "![文章配图](" + paths[b["asset_id"]] + ")" + ("\n\n" + b["caption"] if b["caption"] else "") for b in available)
            page = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>" + html.escape(title) + "</title><main style='max-width:680px;margin:40px auto;font:18px/1.8 sans-serif'>" + ("<p>" + html.escape(prefix) + "</p>" if prefix else "") + "<h1>" + html.escape(title) + "</h1>" + html_body(available, paths) + "</main></html>"
            manifest = {"article_id": article_id, "revision": article["revision"], "revision_hash": article["revision_hash"], "account_id": account["id"], "account": account["name"], "is_demo": bool(topic["is_demo"]), "method": "manual_document_import", "exported_at": iso(), "cover_asset_id": cover_id(document), "skipped_images": skipped, "toutiao_document": "article.docx", "photos": credits, "ai_label": True}
            chosen = cover_id(document)
            # The independently selected cover is delivered separately, never
            # duplicated inside the reader-facing body.
            cover = next((b["asset"] for b in available if b["type"] == "image" and b["asset_id"] == chosen), None)
            manifest["cover_path"] = "cover." + cover["local_name"].rsplit(".", 1)[-1] if cover else "cover.png"
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
                for name, content in {"article.md": markdown, "article.html": page, "article.docx": word, "manifest.json": dumps(manifest), "evidence.json": dumps(topic["data"]), "图片来源与署名.json": dumps(credits), "标题.txt": title, "导入说明.txt": "在头条图文编辑器导入 article.docx，检查正文、封面及平台要求的声明。素材出处和内部记录在本包单独保存，不插入正文。\n"}.items():
                    z.writestr(name, content)
                for name, content in files.items():
                    z.writestr(name, content)
                if cover:
                    z.writestr("cover." + cover["local_name"].rsplit(".", 1)[-1], self.photos.content(cover["id"])[0])
                elif not skipped:
                    z.writestr("cover.png", self.cover(article_id))
            return output.getvalue()

    def export_word(self, article_id, expected_revision=None, skip_missing=False):
        with zipfile.ZipFile(io.BytesIO(self.export_article(article_id, expected_revision, skip_missing))) as bundle:
            return bundle.read("article.docx")

    def add_metric(self, body):
        article = self.article(body.get("article_id"))
        topic = self.topic(article["topic_id"])
        if topic["is_demo"]:
            raise ValueError("不导入演示收益；统计只收录真实作品")
        if not self.store.one("SELECT 1 FROM schedules WHERE article_id=? AND state='live'", (article["id"],)):
            raise ValueError("先登记真实已公开作品，再录入数据")
        observed = date(body.get("observed_at") or iso())
        if observed > now() + dt.timedelta(minutes=5):
            raise ValueError("统计时间不能在未来")
        horizon = body.get("horizon", "24h")
        if horizon not in ("1h", "24h", "72h", "7d"):
            raise ValueError("统计口径时点应为 1h、24h、72h、7d")
        data = {}
        for key in ("impressions", "reads", "monetized_reads", "likes", "comments", "collects", "revenue_cents", "minutes"):
            value = body.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10**12):
                raise ValueError("指标需为非负整数；金额用分，缺失用 null")
            data[key] = value
        if data["monetized_reads"] is not None and data["reads"] is not None and data["monetized_reads"] > data["reads"]:
            raise ValueError("获利阅读不应高于同口径总阅读")
        data["provenance"] = text(body.get("provenance", ""), 2000)
        if len(data["provenance"]) < 4 or body.get("confirmed") is not True:
            raise ValueError("请确认这些数据来自真实后台并记录来源／口径")
        metric_id = uid()
        with self.store.db() as db:
            db.execute("INSERT INTO metrics VALUES(?,?,?,?,?,?)", (metric_id, article["id"], iso(observed), horizon, dumps(data), 0))
        return {"id": metric_id}

    def analytics(self, account_id=None):
        accounts = {a["id"]: a for a in self.store.all("SELECT * FROM accounts")}
        latest, groups = {}, {}
        for metric in self.store.all("SELECT m.*,a.account_id,a.template_id,a.created_at article_created FROM metrics m JOIN articles a ON m.article_id=a.id WHERE m.is_demo=0 ORDER BY m.observed_at"):
            if account_id and metric["account_id"] != account_id:
                continue
            metric["data"] = json.loads(metric["data"])
            latest[metric["article_id"]] = metric
            if metric["horizon"] == "24h":
                groups[(metric["account_id"], metric["article_id"])] = metric
        summary = {key: None for key in ("reads", "impressions", "revenue_cents", "minutes")}
        for key in summary:
            available = [row["data"][key] for row in latest.values() if row["data"][key] is not None]
            summary[key] = sum(available) if available else None
        summary["articles"] = len(latest)
        summary["revenue_coverage"] = sum(row["data"]["revenue_cents"] is not None for row in latest.values())
        by_template = {}
        for row in groups.values():
            key = (row["account_id"], row["template_id"])
            by_template.setdefault(key, []).append(row)
        comparisons = []
        for (account, template), rows in by_template.items():
            reads = [row["data"]["reads"] for row in rows if row["data"]["reads"] is not None]
            comparisons.append({"account_id": account, "account_name": accounts[account]["name"], "template_id": template, "count": len(rows), "median_reads_24h": statistics.median(reads) if reads else None})
        suggestions = []
        for account in accounts.values():
            if account_id and account_id != account["id"]:
                continue
            comparable = [c for c in comparisons if c["account_id"] == account["id"] and c["count"] >= 5 and c["median_reads_24h"] is not None]
            if len(comparable) >= 2:
                best = max(comparable, key=lambda x: x["median_reads_24h"])
                suggestions.append({"account_id": account["id"], "template_id": best["template_id"], "delta": 5, "reason": "同账号、同为 24h 累计快照，该模板阅读中位数较高；仍可能受赛事热度影响，仅建议小幅试验", "sample_count": best["count"]})
        return {"summary": summary, "comparisons": comparisons, "suggestions": suggestions, "note": "每篇只取最新累计快照；不相加不同采样时点。收入为后台已报告金额，未扣税费／工具支出，不等于实际到账或净利润。未提供的指标保留为空。", "sample_note": "每个模板不足 5 篇同账号 24h 样本时仅观察，不提出自动调整。"}

    def apply_policy(self, account_id, body):
        account = self.account(account_id)
        current = self.policy(account_id)
        if body.get("rollback_version") is not None:
            old = self.store.one("SELECT * FROM policies WHERE account_id=? AND version=?", (account_id, body["rollback_version"]))
            if not old:
                raise ValueError("该策略版本不存在")
            data = json.loads(old["data"])
        else:
            template_id = body.get("template_id")
            self.modes.template(account["mode_id"], template_id)
            matching = next((s for s in self.analytics(account_id)["suggestions"] if s["template_id"] == template_id), None)
            if not matching:
                raise ValueError("没有足够真实样本支持此调整")
            boosts = dict(current["template_boosts"])
            boosts[template_id] = min(10, boosts.get(template_id, 0) + 5)
            data = {"template_boosts": boosts, "reason": matching["reason"]}
        with self.store.db() as db:
            db.execute("UPDATE policies SET active=0 WHERE account_id=?", (account_id,))
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM policies WHERE account_id=?", (account_id,)).fetchone()[0]
            db.execute("INSERT INTO policies VALUES(?,?,?,?,?,?)", (uid(), account_id, version, dumps(data), 1, iso()))
            self.audit(db, "policy.change", account_id, {"version": version})
        return {"version": version}

    def is_paused(self):
        return json.loads(self.store.one("SELECT value FROM settings WHERE key='paused'")["value"])

    def set_paused(self, body):
        with self.store.db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='paused'", (dumps(body.get("paused") is True),))
        return {"ok": True}

    def tick(self, run_network=True, run_ai=True):
        with self.store.db() as db:
            db.execute("UPDATE jobs SET state='failed',error='任务超过恢复时限，可能因进程中断；请核对 output.json 后导入或重建任务',updated_at=? WHERE provider<>'agent' AND state='running' AND updated_at<?", (iso(), iso(now()-dt.timedelta(minutes=10))))
        with self.lock:
            for schedule in self.store.all("SELECT * FROM schedules WHERE state IN ('scheduled','ready')"):
                article = self.article(schedule["article_id"])
                topic = self.topic(article["topic_id"])
                state = None
                if date(topic["data"]["expires_at"]) <= now():
                    state = "expired"
                elif schedule["revision_hash"] != article["revision_hash"] or not self.quality(article["id"])["approved"]:
                    state = "needs_review"
                elif not self.is_paused() and not self.account(schedule["account_id"])["paused"] and date(schedule["due_at"]) <= now():
                    state = "ready"
                if state and state != schedule["state"]:
                    with self.store.db() as db:
                        db.execute("UPDATE schedules SET state=?,updated_at=? WHERE id=?", (state, iso(), schedule["id"]))
        if self.is_paused():
            return
        if run_network:
            for source in self.store.all("SELECT * FROM sources WHERE enabled=1"):
                if not source["last_check"] or (now()-date(source["last_check"])).total_seconds() >= source["interval_minutes"]*60:
                    try:
                        self.refresh_source(source["id"])
                    except ValueError:
                        pass
        if run_ai:
            job = self.store.one("SELECT j.* FROM jobs j JOIN topics t ON t.id=j.topic_id JOIN accounts a ON a.id=t.account_id WHERE j.state='queued' AND a.paused=0 ORDER BY j.created_at LIMIT 1")
            if job:
                with self.store.db() as db:
                    changed = db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=? AND state='queued'", (iso(), job["id"])).rowcount
                if changed:
                    try:
                        self.run_codex(job)
                    except Exception as exc:
                        with self.store.db() as db:
                            db.execute("UPDATE jobs SET state='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:1000], iso(), job["id"]))

    def start_worker(self):
        if self.worker:
            return
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception:
                    # Avoid crashing the queue; surface operational failures in audit.
                    with self.store.db() as db:
                        self.audit(db, "worker.error", "worker", {"message": "任务循环失败，请检查本地日志与数据状态"})
                self.stop.wait(20)
        self.worker = threading.Thread(target=loop, daemon=True)
        self.worker.start()
        self.agent.start()

    def state(self, analytics_account=None):
        topics = self.store.all("SELECT * FROM topics ORDER BY created_at DESC LIMIT 300")
        for topic in topics:
            topic["data"] = json.loads(topic["data"])
            topic.update(self.topic_score(topic))
        articles = self.store.all("SELECT * FROM articles ORDER BY updated_at DESC LIMIT 150")
        for article in articles:
            article["data"] = json.loads(article["data"])
            article["publication"] = self.publications.summary(article["id"], article)
        jobs = self.store.all("SELECT id,topic_id,template_id,provider,state,result,error,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT 100")
        for job in jobs:
            job["folder"] = str(self.data_dir / "jobs" / job["id"])
        policies = self.store.all("SELECT * FROM policies ORDER BY version DESC")
        for policy in policies:
            policy["data"] = json.loads(policy["data"])
        return {"version": __version__, "time": iso(), "modes": list(self.modes.items.values()), "accounts": self.store.all("SELECT * FROM accounts"), "sources": self.store.all("SELECT * FROM sources"), "topics": topics, "articles": articles, "jobs": jobs, "schedules": self.store.all("SELECT * FROM schedules ORDER BY due_at DESC LIMIT 200"), "policies": policies, "analytics": self.analytics(analytics_account), "paused": self.is_paused(), "deepseek_only": self.deepseek_only, "codex_available": not self.deepseek_only and bool(shutil.which("codex")), "publisher": "manual_native_schedule"}

    def seed_demo(self):
        if "sports" not in self.modes.items:
            raise ValueError("演示需要体育模式")
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id,name,mode_id,is_demo) VALUES('demo','体育演示账号','sports',1)")
        result = self.add_topic({"account_id": "demo", "event_key": "synthetic-result-demo", "title": "演示：海湾队赛后分析", "summary": "合成体育样例，用于体验素材、核查、审稿与导出，不是真实赛事。", "event_at": iso(), "event_final": True, "facts": [{"id": "f1", "text": "合成演示：海湾队以98比94战胜山城队。", "url": "https://example.com/synthetic-match", "verified": True}, {"id": "f2", "text": "合成演示：海湾队获得12个进攻篮板，山城队为6个。", "url": "https://example.com/synthetic-match", "verified": True}], "heat": 0, "rise": None, "value": 50})
        existing = self.store.one("SELECT id FROM articles WHERE topic_id=?", (result["id"],))
        if existing:
            old_topic = self.topic(result["id"])
            old_article = self.article(existing["id"])
            if date(old_topic["data"]["expires_at"]) <= now() or old_article["material_hash"] != self.material_hash(old_topic, old_article["template_id"]):
                # Renew only the synthetic example. Real events must never be
                # made fresh by changing their occurrence time.
                refreshed = dict(old_topic["data"], event_at=iso(), expires_at=iso(now() + dt.timedelta(hours=6)))
                self.update_topic(result["id"], refreshed)
                article = self.article(existing["id"])
                self.edit_article(article["id"], dict(article["data"], base_revision=article["revision"]))
            return {"topic_id": result["id"], "article_id": existing["id"], "existing": True}
        job = self.create_job(result["id"], "file")
        j = self.store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
        output = {"job_id": job["id"], "input_hash": j["input_hash"], "title": "演示：海湾队赢在哪里", "paragraphs": [
            {"text": "这是一份合成体育演示，不是真实新闻。样例中，海湾队以98比94战胜山城队。这些数据只用于展示工作台如何核对事实和制作图文，不对应任何真实球队或比赛。", "kind": "fact", "claim_ids": ["f1"]},
            {"text": "样例统计显示，海湾队获得12个进攻篮板，山城队为6个。这个差异提示了一个值得分析的方向：海湾队可能争取到了更多二次进攻机会。但单凭这一项数据，还不能解释全部比赛过程。", "kind": "fact", "claim_ids": ["f2"]},
            {"text": "评价比赛需要把结果和解释分开。进攻篮板较多可能与拼抢、阵容和投篮情况有关；没有回合资料，就不能进一步声称某个球员改变了比赛。正式写作时，应补充对应的真实证据，并检查统计口径。这个样例保留了分析边界，也展示了如何避免把推测写成已经发生的事实。", "kind": "analysis", "claim_ids": []}
        ], "unresolved": []}
        imported = self.import_job_result(job["id"], output)
        return {"topic_id": result["id"], **imported}
