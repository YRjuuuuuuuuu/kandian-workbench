import copy
import datetime as dt
import io
import json
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_agent as agent_fixture
import test_engine as engine_fixture
from content_engine import timeliness
from content_engine.agent import RESEARCH_SCHEMA
from content_engine.engine import CN, iso, now
from content_engine.photos import PhotoManager, photo_query, image_identity
from content_engine.sources import public_addresses


class QueueTests(unittest.TestCase):
    setUp = agent_fixture.AgentTests.setUp
    tearDown = agent_fixture.AgentTests.tearDown
    discover = agent_fixture.AgentTests.discover
    produce = agent_fixture.AgentTests.produce

    def test_two_real_worker_paths_overlap_and_third_waits(self):
        runs = [self.agent.discover({"account_id": "main", "focus": f"篮球方向 {i}"}) for i in range(3)]
        first, second = self.agent.claim_next(), self.agent.claim_next()
        self.assertIsNone(self.agent.claim_next())
        barrier = threading.Barrier(3)
        release = threading.Event()
        self.provider.hook = lambda: (barrier.wait(timeout=10), release.wait(timeout=10))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.agent.execute_run, first)
            b = pool.submit(self.agent.execute_run, second)
            try:
                barrier.wait(timeout=10)
                self.assertEqual(len(self.engine.store.all("SELECT id FROM agent_runs WHERE state='running'")), 2)
                self.assertEqual(len(self.engine.store.all("SELECT id FROM agent_runs WHERE state='queued'")), 1)
            finally:
                release.set()
            a.result(timeout=10)
            b.result(timeout=10)
        self.assertNotIn(self.agent.run(first["id"])["state"], ("failed", "running"))
        self.assertNotIn(self.agent.run(second["id"])["state"], ("failed", "running"))
        self.assertEqual(self.agent.claim_next()["id"], next(r["id"] for r in runs if r["id"] not in (first["id"], second["id"])))

    def test_per_account_limit_one_keeps_other_work_queued(self):
        self.agent.preferences({"account_id": "main", "max_concurrency": 1})
        self.agent.discover({"account_id": "main", "focus": "NBA 篮球"})
        self.agent.discover({"account_id": "main", "focus": "足球赛后"})
        self.assertIsNotNone(self.agent.claim_next())
        self.assertIsNone(self.agent.claim_next())

    def test_production_priority_and_duplicate_selection(self):
        found = self.discover()
        pending = self.agent.discover({"account_id": "main", "focus": "另一篮球方向"})
        cid = found["result"]["candidate_ids"][0]
        chosen = self.agent.decide(cid, {"decision": "select"})
        self.assertEqual(chosen["id"], self.agent.decide(cid, {"decision": "select"})["id"])
        self.assertEqual(self.agent.claim_next()["id"], chosen["id"])
        self.assertEqual(self.agent.run(pending["id"])["state"], "queued")

    def test_yesterday_candidate_is_rejected_when_selected_or_dequeued(self):
        found = self.discover()
        cid = found["result"]["candidate_ids"][0]
        chosen = self.agent.decide(cid, {"decision": "select"})
        tomorrow = now() + dt.timedelta(days=1)
        with patch("content_engine.agent.now", return_value=tomorrow):
            self.agent.tick(schedule=False)
        self.assertEqual(self.agent.run(chosen["id"])["state"], "failed")
        self.assertIn("当天", self.agent.run(chosen["id"])["error"])
        with patch("content_engine.agent.now", return_value=tomorrow):
            with self.assertRaises(ValueError):
                self.agent.decide(cid, {"decision": "select"})

    def test_fast_production_truthfully_reuses_fresh_research(self):
        self.agent.preferences({"account_id": "main", "production_mode": "fast"})
        found = self.discover()
        cid = found["result"]["candidate_ids"][0]
        candidate = self.agent.candidate(cid)
        fact = candidate["data"]["facts"][0]
        candidate["data"]["facts"] += [{**copy.deepcopy(fact), "id": "f2"}, {**copy.deepcopy(fact), "id": "f3"}]
        with self.engine.store.db() as db:
            db.execute("UPDATE agent_candidates SET data=? WHERE id=?", (json.dumps(candidate["data"]), cid))
        chosen = self.agent.decide(cid, {"decision": "select"})
        self.agent.tick(schedule=False)
        result = self.agent.run(chosen["id"])
        self.assertEqual(result["state"], "needs_review", result["error"])
        self.assertEqual([c["stem"] for c in self.provider.calls], ["discovery", "writing", "expansion"])
        self.assertEqual(result["result"]["telemetry"]["web_searches"], 0)
        self.assertIn("不重复调用核查模型", result["result"]["review"]["summary"])

    def test_research_purpose_enables_without_commercial_records(self):
        self.agent.bridge_settings({"account_id": "main", "enabled": True, "purpose": "learning_research"})
        self.assertTrue(self.agent.bridge_config("main")["enabled"])
        self.assertEqual(self.agent.bridge_config("main")["purpose"], "learning_research")


class PhotoAndDayTests(unittest.TestCase):
    setUp = engine_fixture.EngineTests.setUp
    tearDown = engine_fixture.EngineTests.tearDown
    make_topic = engine_fixture.EngineTests.make_topic
    result = engine_fixture.EngineTests.result
    make_article = engine_fixture.EngineTests.make_article
    approve = engine_fixture.EngineTests.approve

    def test_bilingual_photo_search_keeps_one_entity_and_sports_context(self):
        query = photo_query("安东尼·爱德华兹 Anthony Edwards 明尼苏达森林狼 Minnesota Timberwolves", "basketball")
        self.assertIn('"Anthony Edwards"', query)
        self.assertIn("basketball", query)
        self.assertNotIn("Minnesota", query)
        self.assertNotIn("爱德华兹", query)

    def test_cdn_crops_are_one_image_and_same_named_actor_is_filtered(self):
        a = "https://cdn.example.org/crop-one?url=https%3A%2F%2Fimages.example.org%2Fathlete.jpg"
        b = "https://cdn.example.org/crop-two?url=https%3A%2F%2Fimages.example.org%2Fathlete.jpg"
        self.assertEqual(image_identity(a), image_identity(b))
        def page(title, category):
            return {"title": title, "categories": [{"title": category}], "imageinfo": [{"url": "https://images.example.org/photo.jpg", "descriptionurl": "https://commons.wikimedia.org/wiki/File:Test", "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}}}]}
        response = {"query": {"pages": {"1": page("Anthony Edwards 2018", "Category:Actors"), "2": page("Anthony Edwards Georgia", "Category:Basketball players")}}}
        manager = PhotoManager(self.engine, fetcher=lambda *args, **kwargs: (json.dumps(response).encode(), "application/json", "https://commons.wikimedia.org"))
        candidates = manager.commons_images('"Anthony Edwards" basketball', "basketball")
        self.assertEqual(len(candidates), 1)
        self.assertIn("Georgia", candidates[0]["source_title"])

    def save_photo(self, article_id):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (640, 360), (80, 120, 100)).save(buffer, "PNG")
        manager = PhotoManager(self.engine, fetcher=lambda url, **kw: (buffer.getvalue(), "image/png", url))
        self.engine.photos = manager
        with patch("content_engine.sources.public_addresses", return_value=(None, "example.org", ["93.184.216.34"])):
            return manager.save(article_id, {"source_url": "https://example.org/report", "image_url": "https://example.org/photo.png", "caption": "合成图片测试（资料图）", "credit": "测试署名", "license": "CC BY 4.0", "photo_role": "file_photo", "reuse_verified": True}, position=1)

    def test_photo_bytes_inline_export_and_approval_invalidation(self):
        aid = self.make_article()
        asset_id = self.save_photo(aid)
        self.approve(aid)
        with zipfile.ZipFile(io.BytesIO(self.engine.export_article(aid))) as bundle:
            metadata = json.loads(bundle.read("manifest.json"))
            path = metadata["photos"][0]["path"]
            self.assertTrue(bundle.read(path).startswith(b"\x89PNG"))
            self.assertIn(path, bundle.read("article.html").decode())
            self.assertIn(path, bundle.read("article.md").decode())
            self.assertEqual(metadata["cover_path"], "cover.png")
            self.assertTrue(bundle.read("cover.png").startswith(b"\x89PNG"))
            self.assertIn("article.docx", bundle.namelist())
        self.engine.photos.update(asset_id, {"caption": "更新说明", "selected": True, "position": 2})
        self.assertIn("资料图", self.engine.photos.asset(asset_id)["data"]["caption"])
        self.assertIsNone(self.engine.article(aid)["approval_hash"])
        self.assertTrue(self.engine.export_article(aid).startswith(b"PK"))

    def test_tampered_photo_is_not_exported(self):
        aid = self.make_article()
        asset_id = self.save_photo(aid)
        self.approve(aid)
        asset = self.engine.photos.asset(asset_id)
        (self.engine.data_dir / "photos" / asset["local_name"]).write_bytes(b"tampered")
        self.assertFalse(self.engine.quality(aid)["approved"])
        with self.assertRaises(ValueError):
            self.engine.export_article(aid)

    def test_day_gate_applies_to_approval_export_and_schedule(self):
        aid = self.make_article()
        self.approve(aid)
        tomorrow = now() + dt.timedelta(days=1)
        with patch("content_engine.engine.now", return_value=tomorrow):
            self.assertTrue(any("当天" in x or "过期" in x for x in self.engine.quality(aid)["warnings"]))
            self.assertTrue(self.engine.export_article(aid).startswith(b"PK"))
        today = now().astimezone(CN)
        midnight = dt.datetime.combine(today.date()+dt.timedelta(days=1), dt.time(0, 1), CN)
        with self.assertRaises(ValueError):
            self.engine.schedule(aid, {"due_at": iso(midnight)})

    def test_overseas_time_uses_beijing_completion_day_and_rejects_conflict(self):
        mode = self.engine.modes.get("sports")
        current = dt.datetime(2026, 10, 27, 9, tzinfo=CN)
        data = {"template_id": "result", "event_at": "2026-10-26T19:00:00-04:00", "event_end_at": "2026-10-26T20:30:00-04:00", "event_date_cn": "2026-10-27"}
        self.assertIsNone(timeliness.error(mode, data, current))
        self.assertEqual(timeliness.deadline(mode, data, current+dt.timedelta(days=2), current), dt.datetime(2026, 10, 28, tzinfo=CN))
        self.assertIn("冲突", timeliness.error(mode, {**data, "event_date_cn": "2026-10-26"}, current))

    def test_private_images_stay_blocked_and_vpn_range_resolves_to_public(self):
        with self.assertRaises(ValueError):
            public_addresses("https://127.0.0.1/image.png")
        with patch("content_engine.sources.socket.getaddrinfo", return_value=[(None,None,None,None,("198.18.0.1",443))]), patch("content_engine.network.resolve_fake_ip", return_value=("93.184.216.34",)):
            self.assertEqual(public_addresses("https://example.org/image.png")[2], ["93.184.216.34"])
        with patch("content_engine.sources.socket.getaddrinfo", return_value=[(None,None,None,None,("10.0.0.1",443))]):
            with self.assertRaises(ValueError):
                public_addresses("https://example.org/image.png")
