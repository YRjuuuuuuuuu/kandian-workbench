import copy
import datetime as dt
import io
import json
import unittest
import zipfile
import xml.etree.ElementTree as ET
from unittest.mock import patch

import test_agent as fixture
import test_upgrade as upgrade_fixture
from content_engine import discovery_portfolio as portfolio
from content_engine import batch_production
from content_engine.ai import normalize, merge_selection
from content_engine.engine import CN, iso, now
from content_engine.photos import OpenGraphImages


class PortfolioBatchTests(unittest.TestCase):
    setUp = fixture.AgentTests.setUp
    tearDown = fixture.AgentTests.tearDown
    discover = fixture.AgentTests.discover

    def test_scheduled_plan_ignores_manual_brief_and_keeps_all_enabled_types(self):
        self.agent.preferences({"account_id": "main", "topic_profile_id": "result_brief", "sport_scope_id": "basketball", "auto_discover": True})
        local = dt.datetime.combine(now().astimezone(CN).date(), dt.time(6, 31), CN)
        with patch("content_engine.agent.now", return_value=local):
            self.agent.tick(execute=False)
            self.agent.tick(execute=False)
        runs = self.engine.store.all("SELECT payload FROM agent_runs WHERE schedule_key IS NOT NULL")
        self.assertEqual(len(runs), 1)
        plan = json.loads(runs[0]["payload"])["profile"]
        self.assertEqual(plan["id"], "adaptive")
        self.assertIn("athlete", {p["template_id"] for p in plan["profiles"]})
        self.assertIn("team", {p["template_id"] for p in plan["profiles"]})
        self.assertGreaterEqual(len(plan["scopes"]), 3)
        manual = self.agent.discover({"account_id": "main"})
        self.assertEqual(self.agent.run(manual["id"])["payload"]["profile"]["id"], "result_brief")

    def test_adaptive_candidate_uses_own_ttl_and_diversity(self):
        mode = self.engine.modes.get("sports")
        config = self.agent.config("main")
        plan = portfolio.portfolio(mode, config)
        athlete = portfolio.for_candidate(mode, plan, {"template_id": "athlete", "title": "NBA 球员表现", "summary": ""})
        self.assertEqual(athlete["lookback_hours"], 72)
        self.assertEqual(athlete["scope"]["id"], "basketball")
        def c(score, template, scope):
            return {"editorial_score": score, "template_id": template, "profile": {"scope": {"id": scope}}}
        mixed = portfolio.diversify([c(80,"result","basketball"),c(79,"result","basketball"),c(76,"athlete","tennis")], 2)
        self.assertEqual([x["template_id"] for x in mixed], ["result", "athlete"])
        with self.assertRaises(ValueError):
            self.agent.preferences({"account_id": "main", "schedule_profile_ids": []})
        self.assertEqual(portfolio.for_candidate(mode,plan,{"template_id":"athlete","title":"未识别的运动员","scope_id":"racket"})["scope"]["id"],"racket")
        self.assertEqual(portfolio.for_candidate(mode,plan,{"template_id":"athlete","title":"未识别的运动员"})["scope"]["id"],"all")

    def test_model_routes_and_legacy_discovery_effort_remain_independent(self):
        legacy = normalize({"reasoning_effort": "high"})
        legacy = merge_selection(legacy, {"reasoning_effort": "medium"})
        production = self.engine.models.adapter(legacy)
        discovery = self.engine.models.adapter(legacy, search=True)
        self.assertEqual((production.model, production.effort), ("gpt-6.1-terra", "medium"))
        self.assertEqual(discovery.effort, "high")

    def test_citations_are_added_from_claims_without_changing_facts_or_approval(self):
        from content_engine.attribution import complete
        original = {"paragraphs":[{"kind":"analysis","claim_ids":["f1"],"text":"篮板可能带来额外机会。"}],"notes":[]}
        facts = [{"id":"f1","agent_check":{"status":"attributed","sources":[{"publisher":"Sky Sports"}]}}]
        updated = complete(original, facts)
        self.assertEqual(updated,original)
        self.assertEqual(updated["paragraphs"][0]["claim_ids"],["f1"])
        self.assertNotIn("Sky Sports",original["paragraphs"][0]["text"])
        self.assertEqual(complete(updated,facts)["paragraphs"],updated["paragraphs"])

    def test_batch_is_idempotent_and_does_not_lose_partial_quota_errors(self):
        found = self.discover()
        first = self.agent.candidate(found["result"]["candidate_ids"][0])
        second = copy.deepcopy(first)
        second["id"] = "b"*32
        second["data"]["event_key"] += "-second"
        with self.engine.store.db() as db:
            db.execute("INSERT INTO agent_candidates VALUES(?,?,?,?,?,?,?,?,?,?)", (second["id"], second["run_id"], second["account_id"], second["data"]["event_key"], json.dumps(second["data"]), second["decision"], second["topic_id"], second["article_id"], second["created_at"], second["updated_at"]))
        self.agent.preferences({"account_id": "main", "daily_limit": 2})
        request = {"account_id": "main", "batch_id": "a"*32, "candidate_ids": [first["id"], second["id"]]}
        result = batch_production.submit(self.agent, request)
        self.assertEqual(len(result["submitted"]), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(batch_production.submit(self.agent, request), result)
        self.assertEqual(len(batch_production.recent(self.agent, "main")[0]["progress"]), 1)
        with self.assertRaises(ValueError):
            batch_production.submit(self.agent, {**request, "account_id":"demo-sports"})


class MediaDocumentTests(unittest.TestCase):
    setUp = upgrade_fixture.PhotoAndDayTests.setUp
    tearDown = upgrade_fixture.PhotoAndDayTests.tearDown
    make_topic = upgrade_fixture.PhotoAndDayTests.make_topic
    result = upgrade_fixture.PhotoAndDayTests.result
    make_article = upgrade_fixture.PhotoAndDayTests.make_article
    approve = upgrade_fixture.PhotoAndDayTests.approve
    save_photo = upgrade_fixture.PhotoAndDayTests.save_photo

    def test_report_supports_inline_and_jsonld_not_only_og(self):
        parser = OpenGraphImages()
        parser.feed('<article><img data-src="https://images.example.org/live.jpg"></article><script type="application/ld+json">{"@type":"NewsArticle","headline":"体育报道","image":{"url":"https://images.example.org/other.jpg"}}</script>')
        self.assertEqual(len(parser.images), 2)
        self.assertEqual(parser.title, "体育报道")

    def test_photo_relevance_rejects_diplomacy_and_resize_duplicates(self):
        from content_engine.photos import unsuitable_background, image_identity, placeholder_image
        self.assertTrue(unsuitable_background("Secretary Kerry presents Celtics basketball at NATO"))
        self.assertTrue(placeholder_image("https://sportsfly.cbsistatic.com/images/default-article-image-large.png"))
        self.assertEqual(image_identity("https://example.org/26/10/1600x900/football.jpg?20261027"),image_identity("https://example.org/26/10/2048x1152/football.jpg?20261027"))

    def test_report_image_cache_avoids_second_network_read_and_brand_images(self):
        import hashlib
        from content_engine.photos import PhotoManager
        url="https://example.org/match-report"
        path=self.engine.data_dir/'agent'/'source_reads'
        path.mkdir(parents=True,exist_ok=True)
        (path/(hashlib.sha256(url.encode()).hexdigest()+'.material.json')).write_text(json.dumps({'final_url':url,'report_images':{'urls':['https://example.org/default-article-image-large.png','https://example.org/match-photo.jpg'],'title':'赛事报道','rights':''}}),encoding='utf-8')
        from unittest.mock import Mock
        fetch=Mock(side_effect=RuntimeError('must reuse original receipt'))
        photos=PhotoManager(self.engine,fetcher=fetch).report_images(url)
        self.assertEqual([p['image_url'] for p in photos],['https://example.org/match-photo.jpg'])
        fetch.assert_not_called()

    def test_revalidated_subject_can_restore_previously_rejected_asset(self):
        aid=self.make_article()
        asset=self.save_photo(aid)
        saved=self.engine.photos.asset(asset)
        with self.engine.store.db() as db:
            db.execute('UPDATE photo_assets SET data=?,selected=0 WHERE id=?',(json.dumps({**saved['data'],'rejected_reason':'old matching rule'}),asset))
        with patch('content_engine.sources.public_addresses',return_value=(None,'example.org',['93.184.216.34'])):
            restored=self.engine.photos.save(aid,{'source_url':saved['source_url'],'image_url':saved['image_url'],'matched_subject':'Jayson Tatum','source_kind':'wikimedia_commons'})
        self.assertEqual(restored,asset)
        restored=self.engine.photos.asset(asset)
        self.assertEqual(restored['data']['matched_subject'],'Jayson Tatum')
        self.assertNotIn('rejected_reason',restored['data'])
        self.assertTrue(restored['selected'])

    def test_cover_is_selected_and_docx_embeds_photos_with_no_external_dependency(self):
        aid = self.make_article()
        asset = self.save_photo(aid)
        self.engine.photos.update(asset, {"selected": False, "use_as_cover": True})
        self.assertEqual(self.engine.photos.cover_asset(aid)["id"], asset)
        self.assertTrue(self.engine.photos.asset(asset)["selected"])
        from PIL import Image
        with Image.open(io.BytesIO(self.engine.photos.cover_bytes(aid))) as image:
            self.assertEqual(image.size, (1200,800))
        self.approve(aid)
        with zipfile.ZipFile(io.BytesIO(self.engine.export_word(aid))) as doc:
            for file in doc.namelist():
                if file.endswith((".xml", ".rels")):
                    ET.fromstring(doc.read(file))
            media = [n for n in doc.namelist() if n.startswith("word/media/")]
            self.assertEqual(len(media), 1)
            with Image.open(io.BytesIO(doc.read(media[0]))) as image:
                self.assertEqual(image.size, (640,360))
            rels = doc.read("word/_rels/document.xml.rels").decode()
            self.assertNotIn('TargetMode="External"', rels)
            text = doc.read("word/document.xml").decode()
            self.assertIn("资料图", text)
            self.assertNotIn("facts-card.png", text)

    def test_missing_photos_blocks_final_review_but_not_pre_photo_text_check(self):
        aid = self.make_article()
        topic_id = self.engine.article(aid)["topic_id"]
        topic = self.engine.topic(topic_id)
        topic["data"]["agent_origin"] = {"test":True}
        with self.engine.store.db() as db:
            db.execute("UPDATE topics SET data=? WHERE id=?", (json.dumps(topic["data"]),topic_id))
        before = self.engine.quality(aid, media=False)["suggestions"]
        final = self.engine.quality(aid)["suggestions"]
        self.assertGreater(len(final), len(before))


if __name__ == "__main__":
    unittest.main()
