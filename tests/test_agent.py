import copy
import datetime as dt
import io
import json
import shutil
import unittest
import uuid
import zipfile
import threading
import urllib.request
from unittest.mock import patch, Mock

from content_engine.agent import RESEARCH_SCHEMA, REVIEW_SCHEMA
from content_engine.engine import Engine, ROOT, CN, now, iso
from content_engine.media_bridge import normalize_records, public_url
from content_engine.server import make_server


class ScriptedProvider:
    """A deterministic model boundary. Product tests never spend quota or publish."""
    def __init__(self, engine):
        self.engine = engine
        self.calls = []
        self.no_search = False
        self.bad_identity = False
        self.insufficient = False
        self.invalid_source = False
        self.hook = None

    def run(self, folder, stem, prompt, schema, on_event, should_stop, search=False, timeout=420):
        self.calls.append({"stem": stem, "search": search, "prompt": prompt})
        row = self.engine.store.one("SELECT * FROM agent_runs WHERE id=?", (folder.name,))
        payload = json.loads(row["payload"])
        identity = {"run_id": row["id"], "input_hash": "wrong" if self.bad_identity else row["input_hash"]}
        telemetry = {"web_searches": 0 if self.no_search or not search else 2, "usage": {"output_tokens": 120}, "elapsed_seconds": .1}
        on_event("search" if search else "provider", "测试边界中的合成执行记录", telemetry)
        if "candidates" in schema.get("properties", {}):
            key = payload.get("selected", {}).get("event_key", "synthetic-match-" + now().date().isoformat())
            sources = [{"id": "s1", "url": "https://www.nba.com/game/synthetic-test-report", "title": "合成比赛报告", "publisher": "合成官方源", "kind": "official", "published_at": iso(), "excerpt": "甲队以98比94取胜，获得12个进攻篮板。"}, {"id": "s2", "url": "https://example.org/synthetic-news-report", "title": "合成独立报道", "publisher": "另一合成来源", "kind": "media", "published_at": iso(), "excerpt": "甲队98比94取胜，进攻篮板12个。"}]
            if self.insufficient:
                sources = [dict(sources[0], kind="media", url="https://example.org/synthetic-news-report")]
            candidate = {"scope_id":"basketball", "title": "合成测试：甲队篮球赛后分析", "event_key": key, "summary": "仅供本地测试的合成比赛材料", "angle": "区分比赛结果与篮板数据可能提供的解释", "why_now": "合成测试事件在有效期内", "template_id": "analysis", "event_at": iso(), "event_end_at": None, "event_date_cn": now().astimezone(CN).date().isoformat(), "time_precision": "minute", "event_final": True, "sources": sources, "facts": [{"id": "f1", "text": "甲队以98比94取胜，获得12个进攻篮板。", "source_ids": ["missing"] if self.invalid_source else [source["id"] for source in sources], "speaker": "", "original": "", "translation": "", "context": ""}], "cautions": ["全部为合成材料，不可发布"], "value_score": 70}
            result = {**identity, "summary": "合成测试简报", "queries": ["合成测试查询"], "candidates": [candidate], "unavailable": []}
        elif "issues" in schema.get("properties", {}):
            result = {**identity, "summary": "合成编辑复核完成", "issues": []}
        else:
            if stem == "revision":
                job_id, input_hash = row["id"], row["input_hash"]
            else:
                job = self.engine.store.one("SELECT j.* FROM jobs j JOIN agent_candidates c ON c.topic_id=j.topic_id WHERE c.id=? ORDER BY j.created_at DESC LIMIT 1", (payload["candidate_id"],))
                job_id, input_hash = job["id"], job["input_hash"]
            result = {"job_id": job_id, "input_hash": input_hash, "title": "甲队篮球比赛的观察边界", "paragraphs": [{"text": "这是测试中的合成体育材料。甲队以98比94取胜，记录中包含12个进攻篮板。材料提供了结果和一项统计，但不包含完整回合资料。", "kind": "fact", "claim_ids": ["f1"]}, {"text": "这些统计为讨论比赛提供了入口，也限定了讨论的边界。进攻篮板可能带来额外进攻机会，然而仅凭篮板统计无法说明每次进攻如何结束。文章应把能够观察的结果与作者的解释分开。缺少回合证据时，应保留其他解释，而不把某个战术判断写成确定发生的过程。", "kind": "analysis", "claim_ids": []}], "unresolved": [], "notes": [], "image_queries": []}
            if stem == "revision":
                result["title"] = "甲队比赛还需要哪些证据"
        if self.hook:
            self.hook()
        return result, telemetry


class AgentTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "tests" / ".tmp"
        base.mkdir(exist_ok=True)
        self.folder = (base / ("agent-" + uuid.uuid4().hex)).resolve()
        self.folder.mkdir()
        shutil.copytree(ROOT / "modes", self.folder / "modes")
        self.engine = Engine(self.folder / "data", self.folder / "modes")
        self.engine.modes.get("sports")["agent"]["require_photos"] = False  # Model boundary tests; real media gates are tested separately.
        self.agent = self.engine.agent
        self.provider = ScriptedProvider(self.engine)
        self.agent.provider = self.provider
        self.agent.preferences({"account_id": "main", "daily_limit": 50, "production_mode": "thorough"})
        self.agent.crawler.collect = Mock(return_value={"status":"unit_test", "imported":0})
        self.agent.enrich_material = Mock(side_effect=lambda run, material: material)
        self.engine.photos.discover = Mock(return_value={"asset_ids":[],"failures":[],"photos":[]})

    def tearDown(self):
        self.engine.stop.set()
        if not self.folder.is_relative_to((ROOT / "tests" / ".tmp").resolve()):
            raise RuntimeError("Refusing cleanup outside tests")
        shutil.rmtree(self.folder)

    def discover(self):
        created = self.agent.discover({"account_id": "main"})
        self.agent.tick(schedule=False)
        return self.agent.run(created["id"])

    def produce(self):
        run = self.discover()
        chosen = self.agent.decide(run["result"]["candidate_ids"][0], {"decision": "select"})
        self.agent.tick(schedule=False)
        return self.agent.run(chosen["id"])

    def approve(self, article_id):
        article = self.engine.article(article_id)
        return self.engine.approve(article_id, {"account_id": "main", "revision_hash": article["revision_hash"], "facts_ack": True, "rights_ack": True, "ai_ack": True})

    def test_discovery_waits_for_human_selection(self):
        run = self.discover()
        self.assertEqual(run["state"], "awaiting_selection")
        self.assertEqual(self.engine.store.all("SELECT * FROM articles"), [])
        self.assertEqual(self.engine.store.all("SELECT * FROM jobs"), [])
        candidate = self.agent.candidate(run["result"]["candidate_ids"][0])
        self.assertFalse(candidate["data"]["facts"][0]["verified"])
        self.assertEqual(candidate["data"]["facts"][0]["agent_check"]["status"], "crosschecked")
        self.assertTrue(self.provider.calls[0]["search"])

    def test_selected_topic_researches_writes_reviews_and_makes_images(self):
        run = self.produce()
        self.assertEqual(run["state"], "needs_review", run["error"])
        article = self.engine.article(run["result"]["article_id"])
        self.assertIsNone(article["approval_hash"])
        self.assertEqual(self.engine.quality(article["id"])["errors"], [])
        self.assertEqual([call["stem"] for call in self.provider.calls], ["discovery", "writing", "repair"])
        self.assertTrue(self.engine.fact_card(article["id"]).startswith(b"\x89PNG"))
        self.assertEqual(self.engine.store.all("SELECT * FROM schedules"), [])
        self.assertTrue(self.engine.export_article(article["id"]).startswith(b"PK"))
        self.approve(article["id"])
        with zipfile.ZipFile(io.BytesIO(self.engine.export_article(article["id"]))) as bundle:
            self.assertIn("article.docx", bundle.namelist())

    def test_wrong_identity_or_absent_search_is_not_accepted(self):
        self.provider.bad_identity = True
        run = self.discover()
        self.assertEqual(run["state"], "failed")
        self.assertIn("身份", run["error"])
        self.provider.bad_identity = False
        self.provider.no_search = True
        run = self.discover()
        self.assertEqual(run["state"], "failed")
        self.assertIn("联网", run["error"])
        self.assertEqual(self.engine.store.all("SELECT * FROM agent_candidates"), [])

    def test_invalid_source_mapping_is_visible_as_filtered_candidate(self):
        self.provider.invalid_source = True
        run = self.discover()
        self.assertEqual(run["state"], "no_candidates")
        self.assertIn("来源", run["result"]["rejected"][0]["reason"])

    def test_one_unverified_media_source_does_not_unlock_writing(self):
        self.provider.insufficient = True
        run = self.discover()
        cid = run["result"]["candidate_ids"][0]
        self.assertFalse(self.agent.candidate(cid)["data"]["agent_ready"])
        production = self.agent.decide(cid, {"decision": "select"})
        self.agent.tick(schedule=False)
        final = self.agent.run(production["id"])
        self.assertEqual(final["state"], "failed")
        self.assertIn("证据", final["error"])
        self.assertFalse(self.engine.store.all("SELECT * FROM articles"))

    def test_active_run_dedup_skip_feedback_and_cancel(self):
        queued = self.agent.discover({"account_id": "main"})
        self.assertEqual(self.agent.discover({"account_id": "main"})["id"], queued["id"])
        second = self.agent.discover({"account_id": "main", "focus":"另一篮球研究方向"})
        self.assertNotEqual(second["id"], queued["id"])
        self.agent.cancel(second["id"])
        self.agent.cancel(queued["id"])
        self.assertEqual(self.agent.run(queued["id"])["state"], "cancelled")
        run = self.discover()
        cid = run["result"]["candidate_ids"][0]
        self.agent.decide(cid, {"decision": "skip", "reason": "不熟悉这个项目"})
        self.discover()
        self.assertIn("不熟悉这个项目", self.provider.calls[-1]["prompt"])

    def test_revision_checks_original_version_and_invalidates_approval(self):
        production = self.produce()
        article_id = production["result"]["article_id"]
        self.approve(article_id)
        article = self.engine.article(article_id)
        with self.assertRaisesRegex(ValueError, "变化"):
            self.agent.create("revise", {"account_id": "main", "article_id": article_id, "base_revision": 0, "feedback": "改写标题"})
        created = self.agent.create("revise", {"account_id": "main", "article_id": article_id, "base_revision": article["revision"], "feedback": "标题改为一个具体问题"})
        self.agent.tick(schedule=False)
        self.assertEqual(self.agent.run(created["id"])["state"], "needs_review")
        updated = self.engine.article(article_id)
        self.assertEqual(updated, article)
        suggestion = self.engine.suggestions(article_id)[0]
        self.engine.apply_suggestion(article_id, suggestion["id"], {"base_revision":article["revision"]})
        updated = self.engine.article(article_id)
        self.assertEqual(updated["revision"], article["revision"] + 1)
        self.assertIsNone(updated["approval_hash"])

    def test_editor_notes_do_not_impersonate_approval_or_block_valid_draft(self):
        production = self.produce()
        article_id = production["result"]["article_id"]
        article = self.engine.article(article_id)
        self.engine.edit_article(article_id, {**article["data"], "base_revision": article["revision"], "notes": ["仍需用户核对原文并终审", "没有完整录像，不作具体回合判断"]})
        quality = self.engine.quality(article_id)
        self.assertEqual(quality["errors"], [])
        self.assertFalse(quality["approved"])
        self.assertTrue(any("编辑说明" in value for value in quality["warnings"]))
        updated = self.engine.article(article_id)
        with self.assertRaisesRegex(ValueError, "确认"):
            self.engine.approve(article_id, {"account_id": "main", "revision_hash": updated["revision_hash"]})

    def test_mode_change_during_research_rejects_result(self):
        def change_mode():
            self.engine.modes.items["sports"] = {**self.engine.modes.items["sports"], "version": 99}
        self.provider.hook = change_mode
        run = self.discover()
        self.assertEqual(run["state"], "failed")
        self.assertIn("模式", run["error"])
        self.assertFalse(self.engine.store.all("SELECT * FROM agent_candidates"))

    def test_date_precision_keeps_date_and_uses_explicit_interval_deadline(self):
        run = self.discover()
        source = self.agent.candidate(run["result"]["candidate_ids"][0])["data"]
        raw = {key: source[key] for key in ("title", "event_key", "summary", "angle", "why_now", "template_id", "event_at", "time_precision", "event_final", "sources", "cautions")}
        raw["value_score"] = source["value"]
        raw["facts"] = [{"id": "f1", "text": source["facts"][0]["text"], "source_ids": ["s1", "s2"], "speaker": "", "original": "", "translation": "", "context": ""}]
        anchor = now() - dt.timedelta(hours=50)
        raw["event_at"] = iso(anchor)
        raw["time_precision"] = "date"
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, {"web_searches": 1})
        self.assertFalse(values)
        self.assertIn("当天", rejected[0]["reason"])
        raw["event_at"] = iso()
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, {"web_searches": 1})
        self.assertFalse(rejected)
        self.assertEqual(dt.datetime.fromisoformat(values[0]["expires_at"]).astimezone(CN).time(), dt.time())
        raw["template_id"] = "result"
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, {"web_searches": 1})
        self.assertFalse(rejected)
        self.assertTrue(values[0]["agent_ready"])
        self.assertIsNone(values[0]["event_end_at"])

    def test_scheduled_discovery_does_not_repeat_or_backfill(self):
        slot = now().astimezone(CN).strftime("%H:%M")
        self.agent.preferences({"account_id": "main", "auto_discover": True, "slots": [slot]})
        self.agent.tick(execute=False)
        self.agent.tick(execute=False)
        runs = self.engine.store.all("SELECT * FROM agent_runs")
        self.assertEqual(len(runs), 1)
        self.agent.cancel(runs[0]["id"])
        self.agent.tick(execute=False)
        self.assertEqual(len(self.engine.store.all("SELECT * FROM agent_runs")), 1)
        self.engine.set_paused({"paused": True})
        with self.assertRaisesRegex(ValueError, "暂停"):
            self.agent.discover({"account_id": "main"})

    def test_paused_account_does_not_block_another_account(self):
        first = self.agent.discover({"account_id": "main"})
        self.engine.update_account("main", {"paused": True})
        other = self.engine.add_account({"name": "另一个测试账号", "mode_id": "sports"})["id"]
        second = self.agent.discover({"account_id": other})
        self.agent.tick(schedule=False)
        self.assertEqual(self.agent.run(first["id"])["state"], "queued")
        self.assertEqual(self.agent.run(second["id"])["state"], "awaiting_selection")

    def test_agent_http_contract_from_discovery_to_review(self):
        server = make_server(self.engine, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        token = (self.engine.data_dir / ".local-token").read_text(encoding="ascii").strip()
        def request(path, body=None):
            headers = {"X-App-Token": token}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            with urllib.request.urlopen(urllib.request.Request(endpoint + path, data=data, headers=headers), timeout=10) as response:
                return json.loads(response.read())
        try:
            created = request("/api/agent/discover", {"account_id": "main"})
            self.agent.tick(schedule=False)
            state = request("/api/agent/state?account_id=main")
            self.assertEqual(state["runs"][0]["state"], "awaiting_selection")
            detail = request("/api/agent/runs/" + created["id"])
            self.assertTrue(detail["events"])
            cid = state["candidates"][0]["id"]
            production = request("/api/agent/candidates/" + cid + "/decision", {"decision": "select"})
            self.agent.tick(schedule=False)
            finished = request("/api/agent/runs/" + production["id"])
            self.assertEqual(finished["run"]["state"], "needs_review")
            self.assertTrue(finished["run"]["result"]["article_id"])
        finally:
            server.shutdown()
            server.server_close()

    def test_mediacrawler_requires_recorded_rights_and_drops_identities(self):
        record = {"title": "甲队篮球赛后分析", "note_url": "https://weibo.com/123/abc?token=secret", "content": "比赛结果线索", "liked_count": "1.2万", "comments_count": 40, "user_id": "private-author-id", "nickname": "private-name", "cookie": "private-cookie"}
        self.agent.bridge_settings({"account_id":"main","enabled":False})
        with self.assertRaisesRegex(ValueError, "未启用"):
            self.agent.import_signals("main", [record], "weibo")
        with self.assertRaisesRegex(ValueError, "书面"):
            self.agent.bridge_settings({"account_id": "main", "enabled": True, "purpose":"commercial"})
        self.agent.bridge_settings({"account_id": "main", "enabled": True, "purpose":"learning_research", "authorization_note": "合成测试中的研究记录", "terms_note": "合成测试中的平台获取使用依据，不读取真实平台", "permission_confirmed": True, "platform": "weibo"})
        with patch("content_engine.agent.iso", return_value="2026-10-26T00:00:00+00:00"):
            self.agent.import_signals("main", [record])
        with patch("content_engine.agent.iso", return_value="2026-10-26T01:00:00+00:00"):
            self.agent.import_signals("main", [{**record, "liked_count": 15000}])
        signals = self.agent.recent_signals("main")
        self.assertEqual(signals[0]["metrics"]["likes"], 15000)
        self.assertEqual(signals[0]["previous_metrics"]["likes"], 12000)
        self.assertIsNone(signals[0]["event_at"])
        stored = json.dumps(self.engine.store.all("SELECT * FROM agent_signals"))
        self.assertNotIn("private-author-id", stored)
        self.assertNotIn("private-cookie", stored)
        self.assertNotIn("token=secret", stored)
        self.assertEqual(normalize_records([{**record, "comment_id": "ignore"}], "weibo"), [])
        with self.assertRaises(ValueError):
            public_url("https://127.0.0.1/secret")


if __name__ == "__main__":
    unittest.main()
