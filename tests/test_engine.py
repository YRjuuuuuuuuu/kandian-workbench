import copy
import datetime as dt
import io
import json
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
import uuid
from pathlib import Path
from unittest.mock import patch

from content_engine.engine import Engine, ROOT, CN, digest, iso, now
from content_engine.modes import validate_mode
from content_engine.server import make_server
from content_engine.sources import parse_feed, public_addresses
from content_engine.store import dumps


class EngineTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "tests" / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.folder = (temp_root / ("case-" + uuid.uuid4().hex)).resolve()
        self.folder.mkdir()
        self.assertTrue(self.folder.is_relative_to(temp_root.resolve()))
        shutil.copytree(ROOT / "modes", self.folder / "modes")
        self.engine = Engine(self.folder / "data", self.folder / "modes")

    def tearDown(self):
        self.engine.stop.set()
        if not self.folder.is_relative_to((ROOT / "tests" / ".tmp").resolve()):
            raise RuntimeError("Refusing cleanup outside the test workspace")
        shutil.rmtree(self.folder)

    def make_topic(self, label="甲", template="result", account_id="main"):
        return self.engine.add_topic({"account_id": account_id, "event_key": label+template, "title": label+"队篮球赛后分析", "summary": "体育比赛事实与分析", "event_final": True, "template_id": template, "facts": [{"id": "f1", "text": label+"队以98比94赢得比赛，获得12个进攻篮板。", "url": "https://example.org/match", "verified": True}]})["id"]

    def result(self, topic_id, label="甲"):
        job = self.engine.create_job(topic_id)
        j = self.engine.store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
        data = {"job_id": job["id"], "input_hash": j["input_hash"], "title": label+"队赢在比赛细节", "paragraphs": [{"text": label+"队以98比94赢下篮球比赛。比赛数据记录了12个进攻篮板，这为讨论二次进攻提供了事实基础。", "kind": "fact", "claim_ids": ["f1"]}, {"text": (label+"队的表现应放在比赛背景中理解。没有回合资料，就不能把一项数据当作胜负的全部解释。需要区分可观察的统计与作者对比赛的判断。")*3, "kind": "analysis", "claim_ids": []}], "unresolved": []}
        return job, data

    def make_article(self, label="甲", template="result"):
        topic = self.make_topic(label, template)
        job, data = self.result(topic, label)
        return self.engine.import_job_result(job["id"], data)["article_id"]

    def approve(self, article_id):
        article = self.engine.article(article_id)
        return self.engine.approve(article_id, {"account_id": article["account_id"], "revision_hash": article["revision_hash"], "facts_ack": True, "rights_ack": True, "ai_ack": True})

    def schedule(self, article_id):
        expires = dt.datetime.fromisoformat(self.engine.topic(self.engine.article(article_id)["topic_id"])["data"]["expires_at"])
        due = min(now()+dt.timedelta(minutes=20),now()+(expires-now())/2)
        return self.engine.schedule(article_id, {"due_at": iso(due)})["id"]

    def live(self, article_id):
        sid = self.schedule(article_id)
        return self.engine.confirm_schedule(sid, {"account_id": "main", "confirmed": True, "state": "live", "proof_note": "单元测试中的合成后台回执，不访问真实平台", "remote_url": "https://www.toutiao.com/article/100001/"})

    def test_demo_refresh_after_expiry(self):
        sample = self.engine.seed_demo()
        self.approve(sample["article_id"])
        revision = self.engine.article(sample["article_id"])["revision"]
        later = now() + dt.timedelta(hours=7)
        with patch("content_engine.engine.now", return_value=later):
            renewed = self.engine.seed_demo()
            self.assertEqual(renewed["article_id"], sample["article_id"])
            self.assertGreater(self.engine.article(renewed["article_id"])["revision"], revision)
            quality = self.engine.quality(renewed["article_id"])
            self.assertFalse(quality["approved"])
            self.assertEqual(quality["errors"], [])
            self.approve(renewed["article_id"])
            self.assertTrue(self.engine.export_article(renewed["article_id"]).startswith(b"PK"))

    def test_pause_prevents_manual_source_refresh(self):
        source = self.engine.add_source({"account_id": "main", "mode_id": "sports", "name": "合成来源配置", "url": "https://example.org/rss", "rights_note": "测试中的合成许可记录，不读取网络", "permission_confirmed": True})
        self.engine.set_paused({"paused": True})
        with patch("content_engine.engine.fetch_feed") as fetch:
            with self.assertRaisesRegex(ValueError, "暂停"):
                self.engine.refresh_source(source["id"])
            fetch.assert_not_called()

    def test_generic_mode_without_engine_change(self):
        mode = json.loads((ROOT / "modes/_template/mode.example.json").read_text(encoding="utf-8"))
        validate_mode(mode)
        target = self.folder / "modes" / mode["id"]
        target.mkdir()
        (target / "mode.json").write_text(dumps(mode), encoding="utf-8")
        self.engine.modes.reload()
        account_id = self.engine.add_account({"name": "工具账号", "mode_id": "technology"})["id"]
        topic_id = self.engine.add_topic({"account_id": account_id, "title": "电脑工具实测", "template_id": "explainer", "facts": [{"text": "此工具支持图片检查。", "url": "https://example.org/tool", "verified": True}]})["id"]
        self.assertEqual(self.engine.topic(topic_id)["mode_id"], "technology")
        self.assertFalse(self.engine.evidence_errors(self.engine.topic(topic_id), "explainer"))
        self.assertTrue(self.engine.modes.assess("technology", "工具与股票投资")["blocked"])

    def test_blocked_unverified_and_nonfinal_rejected(self):
        tid = self.make_topic()
        original = self.engine.topic(tid)["data"]
        for changes in ({"summary": "体育赔率分析"}, {"event_final": False}, {"facts": [{**original["facts"][0], "verified": False}]}):
            self.engine.update_topic(tid, {**original, **changes})
            with self.assertRaises(ValueError):
                self.engine.create_job(tid)

    def test_job_identity_and_claims(self):
        tid = self.make_topic()
        job, data = self.result(tid)
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.engine.import_job_result(job["id"], {**data, "input_hash": "wrong"})
        wrong = copy.deepcopy(data)
        wrong["paragraphs"][0]["claim_ids"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.engine.import_job_result(job["id"], wrong)

    def test_approval_export_and_original_cover(self):
        aid = self.make_article()
        self.approve(aid)
        output = self.engine.export_article(aid)
        with zipfile.ZipFile(io.BytesIO(output)) as z:
            self.assertEqual(z.read("cover.png")[:8], b"\x89PNG\r\n\x1a\n")
            self.assertIn("article.html", z.namelist())
            self.assertTrue(json.loads(z.read("manifest.json"))["ai_label"])
            self.assertNotIn(b"<script", z.read("article.html"))

    def test_facts_change_revokes_approval_and_schedule(self):
        aid = self.make_article()
        self.approve(aid)
        sid = self.schedule(aid)
        article = self.engine.article(aid)
        td = self.engine.topic(article["topic_id"])["data"]
        td["facts"][0]["text"] += "官方统计已更正。"
        self.engine.update_topic(article["topic_id"], td)
        self.assertFalse(self.engine.quality(aid)["approved"])
        self.assertEqual(self.engine.store.one("SELECT state FROM schedules WHERE id=?", (sid,))["state"], "needs_review")
        self.assertTrue(self.engine.export_article(aid).startswith(b"PK"))

    def test_edit_optimistic_lock_and_approval_invalidation(self):
        aid = self.make_article()
        self.approve(aid)
        old = self.engine.article(aid)
        with self.assertRaisesRegex(ValueError, "版本"):
            self.engine.edit_article(aid, {**old["data"], "base_revision": 0})
        self.engine.edit_article(aid, {**old["data"], "base_revision": 1, "title": "篮球赛后的一点分析"})
        self.assertFalse(self.engine.quality(aid)["approved"])
        with self.assertRaisesRegex(ValueError, "版本"):
            self.engine.approve(aid, {"revision_hash": old["revision_hash"]})

    def test_unknown_numbers_fail_quality(self):
        tid = self.make_topic()
        job, data = self.result(tid)
        data["paragraphs"][0]["text"] += "最后得分是123。"
        aid = self.engine.import_job_result(job["id"], data)["article_id"]
        self.assertTrue(any("未支持的数字" in error for error in self.engine.quality(aid)["suggestions"]))

    def test_schedule_dedup_and_reschedule_cancelled(self):
        aid = self.make_article()
        self.approve(aid)
        sid = self.schedule(aid)
        with self.assertRaises(ValueError):
            self.schedule(aid)
        self.engine.confirm_schedule(sid, {"state": "cancelled"})
        self.assertEqual(self.schedule(aid), sid)
        self.engine.tick(False, False)
        self.assertEqual(self.engine.store.one("SELECT state FROM schedules WHERE id=?", (sid,))["state"], "scheduled")

    def test_due_only_reminds_and_expired_never_publishes(self):
        aid = self.make_article()
        self.approve(aid)
        sid = self.schedule(aid)
        with self.engine.store.db() as db:
            db.execute("UPDATE schedules SET due_at=? WHERE id=?", (iso(now()-dt.timedelta(minutes=1)), sid))
        self.engine.tick(False, False)
        self.assertEqual(self.engine.store.one("SELECT state FROM schedules WHERE id=?", (sid,))["state"], "ready")
        tid = self.engine.article(aid)["topic_id"]
        data = self.engine.topic(tid)["data"]
        data["expires_at"] = iso(now()-dt.timedelta(seconds=1))
        with self.engine.store.db() as db:
            db.execute("UPDATE topics SET data=? WHERE id=?", (dumps(data), tid))
        self.engine.tick(False, False)
        self.assertEqual(self.engine.store.one("SELECT state FROM schedules WHERE id=?", (sid,))["state"], "expired")

    def test_demo_never_becomes_real_post_or_revenue(self):
        aid = self.engine.seed_demo()["article_id"]
        self.approve(aid)
        sid = self.schedule(aid)
        with self.assertRaisesRegex(ValueError, "演示"):
            self.engine.confirm_schedule(sid, {"state": "live", "account_id": "demo", "confirmed": True, "proof_note": "测试中的模拟记录", "remote_url": "https://www.toutiao.com/article/100001/"})
        with self.assertRaisesRegex(ValueError, "演示"):
            self.engine.add_metric({"article_id": aid})
        self.assertIsNone(self.engine.analytics()["summary"]["revenue_cents"])

    def test_cumulative_metrics_use_latest_keep_null(self):
        aid = self.make_article()
        self.approve(aid)
        self.live(aid)
        base = {"article_id": aid, "horizon": "24h", "confirmed": True, "provenance": "合成单元测试数据，不是真实收益", "revenue_cents": None}
        self.engine.add_metric({**base, "reads": 100, "observed_at": iso(now()-dt.timedelta(hours=1))})
        self.engine.add_metric({**base, "reads": 200, "observed_at": iso()})
        summary = self.engine.analytics()["summary"]
        self.assertEqual(summary["reads"], 200)
        self.assertIsNone(summary["revenue_cents"])
        self.assertEqual(summary["articles"], 1)

    def test_rss_atom_and_entity_rejection(self):
        rss = b'<rss><channel><item><title>Result</title><link>https://example.org/a</link><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'
        self.assertEqual(len(parse_feed(rss)), 1)
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Result</title><link href="https://example.org/a"/><updated>2026-10-26T10:00:00Z</updated></entry></feed>'
        self.assertEqual(parse_feed(atom)[0]["url"], "https://example.org/a")
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE x [<!ENTITY y "z">]><rss/>')

    def test_private_source_addresses_rejected(self):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]):
            with self.assertRaisesRegex(ValueError, "私有网络"):
                public_addresses("https://example.org/feed")
        with self.assertRaises(ValueError):
            public_addresses("http://example.org/feed")

    def test_learning_uses_account_groups_and_supports_rollback(self):
        # Only isolated database fixtures; nothing is posted or counted in the delivered app.
        for template, reads in (("result", 100), ("analysis", 240)):
            for index in range(5):
                aid = self.make_article(f"{template}{index}", template)
                with self.engine.store.db() as db:
                    db.execute("INSERT INTO metrics VALUES(?,?,?,?,?,?)", (f"{template}-{index}", aid, iso(), "24h", dumps({"reads": reads, "impressions": None, "revenue_cents": None, "minutes": None}), 0))
        other = self.engine.add_account({"name": "另一账号", "mode_id": "sports"})["id"]
        self.assertEqual(self.engine.analytics(other)["summary"]["articles"], 0)
        suggestion = self.engine.analytics("main")["suggestions"][0]
        self.assertEqual(suggestion["template_id"], "analysis")
        self.engine.apply_policy("main", {"template_id": "analysis"})
        self.assertEqual(self.engine.policy("main")["template_boosts"]["analysis"], 5)
        self.engine.apply_policy("main", {"template_id": "analysis"})
        self.assertEqual(self.engine.policy("main")["template_boosts"]["analysis"], 10)
        self.engine.apply_policy("main", {"rollback_version": 1})
        self.assertEqual(self.engine.policy("main")["template_boosts"]["analysis"], 5)

    def test_http_write_guards_and_real_bootstrap(self):
        server = make_server(self.engine, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base) as response:
                html = response.read().decode()
            token = (self.engine.data_dir / ".local-token").read_text().strip()
            self.assertIn(token, html)
            headers = {"X-App-Token": token, "Content-Type": "application/json"}
            request = urllib.request.Request(base+"/api/pause", data=b'{"paused":true}', headers={**headers,"Origin":"https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(request)
            self.assertEqual(failure.exception.code, 403)
            self.assertFalse(self.engine.is_paused())
            good = urllib.request.Request(base+"/api/pause", data=b'{"paused":true}', headers=headers)
            self.assertTrue(json.loads(urllib.request.urlopen(good).read())["ok"])
            self.assertTrue(self.engine.is_paused())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
