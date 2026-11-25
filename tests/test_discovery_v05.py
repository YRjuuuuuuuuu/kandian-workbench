import copy
import datetime as dt
import json
from unittest.mock import Mock, patch
import unittest

import test_agent as fixture
from content_engine.engine import iso, now, CN
from content_engine.topic_profiles import resolve
from content_engine.media_bridge import normalize_records, public_url


class DiscoveryUpgradeTests(unittest.TestCase):
    setUp = fixture.AgentTests.setUp
    tearDown = fixture.AgentTests.tearDown
    discover = fixture.AgentTests.discover

    def raw(self):
        run = self.discover()
        data = copy.deepcopy(self.agent.candidate(run["result"]["candidate_ids"][0])["data"])
        data["value_score"] = data["value"]
        for fact in data["facts"]:
            fact["source_ids"] = fact["agent_check"]["source_ids"]
        return run, data, {"web_searches": 2}

    def test_date_only_finished_score_unlocks_draft_but_not_approval(self):
        run, raw, telemetry = self.raw()
        raw.update(template_id="result", time_precision="date", event_end_at=None, event_date_cn=now().astimezone(CN).date().isoformat(), event_at=iso(dt.datetime.combine(now().astimezone(CN).date(), dt.time(), CN)))
        raw["sources"] = [{**raw["sources"][0], "url": "https://www.cbssports.com/nba/news/test-report", "kind": "media"}]
        raw["facts"][0]["source_ids"] = ["s1"]
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, {"web_searches": 2, "observed_urls": [raw["sources"][0]["url"]]})
        self.assertFalse(rejected)
        self.assertTrue(values[0]["agent_ready"])
        self.assertEqual(values[0]["facts"][0]["agent_check"]["status"], "attributed")
        self.assertFalse(values[0]["facts"][0]["verified"])

    def test_extra_unobserved_link_does_not_discard_supported_match(self):
        run, raw, telemetry = self.raw()
        raw["sources"].append({**raw["sources"][0], "id": "unused", "url": "https://example.net/guessed"})
        telemetry["observed_urls"] = [s["url"].replace("www.", "").rstrip("/") for s in raw["sources"][:2]]
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, telemetry)
        self.assertFalse(rejected)
        self.assertEqual(len(values[0]["sources"]), 2)
        self.assertTrue(any("unused" in warning for warning in values[0]["cautions"]))

    def test_no_supported_links_still_rejects_not_fabricates(self):
        run, raw, telemetry = self.raw()
        telemetry["observed_urls"] = ["https://example.net/another-report"]
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, telemetry)
        self.assertFalse(values)
        self.assertTrue(rejected)

    def test_internal_search_refs_get_bounded_original_url_receipt(self):
        run, raw, telemetry = self.raw()
        telemetry.update(provider="codex", observed_urls=["https://example.net/another-report"])
        with patch("content_engine.source_provenance.read", side_effect=lambda url, folder: {"status": "readable", "url": url, "method": "test_boundary_only"}) as reader:
            values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, telemetry)
        self.assertFalse(rejected)
        self.assertTrue(values[0]["agent_ready"])
        self.assertEqual(reader.call_count, 1)
        self.assertIn("read_receipt", values[0]["sources"][0])

    def test_failed_url_read_does_not_accept_unobserved_sources(self):
        run, raw, telemetry = self.raw()
        telemetry.update(provider="codex", observed_urls=[])
        with patch("content_engine.source_provenance.read", side_effect=lambda url, folder: {"status": "unavailable", "url": url}):
            values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, telemetry)
        self.assertFalse(values)
        self.assertTrue(rejected)

    def test_attributed_facts_flow_through_draft_with_source_label_and_no_approval(self):
        original = self.provider.run
        def with_attribution(*args, **kwargs):
            result, telemetry = original(*args, **kwargs)
            if "candidates" in result:
                for candidate in result["candidates"]:
                    candidate["sources"] = [{**candidate["sources"][0], "kind": "media", "publisher": "CBS Sports", "url": "https://www.cbssports.com/nba/news/test-report"}]
                    for fact in candidate["facts"]:
                        fact["source_ids"] = ["s1"]
            elif "paragraphs" in result:
                result["paragraphs"][0]["text"] = "据CBS Sports报道，" + result["paragraphs"][0]["text"]
            return result, telemetry
        self.provider.run = with_attribution
        self.agent.preferences({"production_mode": "fast"})
        found = self.discover()
        created = self.agent.decide(found["result"]["candidate_ids"][0], {"decision": "select"})
        self.agent.tick(schedule=False)
        completed = self.agent.run(created["id"])
        self.assertEqual(completed["state"], "needs_review", completed["error"])
        article_id = completed["result"]["article_id"]
        self.assertFalse(self.engine.quality(article_id)["errors"])
        self.assertIsNone(self.engine.article(article_id)["approval_hash"])
        article = self.engine.article(article_id)
        body = copy.deepcopy(article["data"])
        body["paragraphs"][0]["text"] = body["paragraphs"][0]["text"].replace("据CBS Sports报道，", "")
        self.engine.edit_article(article_id, {**body, "base_revision": article["revision"]})
        self.assertFalse(any("报道来源" in error for error in self.engine.quality(article_id)["warnings"]))

    def test_profile_scope_snapshot_and_same_focus_different_types(self):
        one = self.agent.discover({"topic_profile_id": "match_analysis", "sport_scope_id": "basketball"})
        two = self.agent.discover({"topic_profile_id": "team_analysis", "sport_scope_id": "basketball"})
        self.assertNotEqual(one["id"], two["id"])
        profile = self.agent.run(one["id"])["payload"]["profile"]
        self.assertEqual(profile["template_id"], "analysis")
        self.agent.preferences({"topic_profile_id": "official_quote", "sport_scope_id": "football"})
        self.assertEqual(self.agent.run(one["id"])["payload"]["profile"], profile)
        with self.assertRaises(ValueError):
            self.agent.discover({"topic_profile_id": "invented"})

    def test_hotlist_preserves_rank_heat_and_query_but_not_fake_event_time(self):
        records = [{"title": "库里三分", "url": "https://s.weibo.com/weibo?q=%23库里%23&Refer=top", "source_kind": "hotlist", "board_url": "https://s.weibo.com/top/summary?cate=sport", "board_category": "微博体育热搜", "rank": 3, "heat_raw": "136518", "observed_at": iso(), "uid": "discarded"}]
        output = normalize_records(records, "weibo")[0]
        self.assertEqual(output["rank"], 3)
        self.assertEqual(output["heat_raw"], "136518")
        self.assertIsNone(output["event_at"])
        self.assertNotIn("uid", output)
        self.assertNotEqual(output["url"], public_url("https://s.weibo.com/weibo?q=另一个话题"))

    def test_cached_hotlists_re_filter_for_current_scope_without_browser(self):
        cache = self.engine.data_dir / "crawler" / "hotlists-cache.json"
        cache.parent.mkdir(parents=True)
        records = [{"title": "库里进8个球砍24分", "url": "https://www.douyin.com/hot/101", "platform": "douyin"}, {"title": "凯恩金球奖", "url": "https://www.douyin.com/hot/102", "platform": "douyin"}, {"title": "NBA竞彩公推", "url": "https://www.douyin.com/hot/103", "platform": "douyin"}]
        cache.write_text(json.dumps({"status": "completed", "records": records, "boards": [{"board": "抖音热点榜", "count": 3}]}), encoding="utf-8")
        from content_engine.crawler_runtime import HotlistRuntime
        runtime = HotlistRuntime(self.engine)
        runtime.status = Mock(return_value={"installed": True, "browser_ready": True, "browser_cache": "unused", "python": "unused", "repo": "unused"})
        result = runtime.collect("main", "", self.engine.modes.get("sports"), profile=resolve(self.engine.modes.get("sports"), "result_brief", "basketball"))
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.agent.recent_signals("main")[0]["title"], records[0]["title"])

    def test_checked_event_date_is_not_an_unsupported_number(self):
        completed = fixture.AgentTests.produce(self)
        article_id = completed["result"]["article_id"]
        article = self.engine.article(article_id)
        body = copy.deepcopy(article["data"])
        day = now().astimezone(CN)
        body["paragraphs"][0]["text"] = f"北京时间{day.month}月{day.day}日，" + body["paragraphs"][0]["text"]
        self.engine.edit_article(article_id, {**body, "base_revision": article["revision"]})
        self.assertFalse(self.engine.quality(article_id)["errors"])
        article = self.engine.article(article_id)
        body["paragraphs"][0]["text"] += "未知统计9999。"
        self.engine.edit_article(article_id, {**body, "base_revision": article["revision"]})
        self.assertTrue(any("数字" in error for error in self.engine.quality(article_id)["suggestions"]))

    def test_task_identity_is_bound_in_the_model_schema(self):
        captured = {}
        original = self.provider.run
        def capture(*args, **kwargs):
            captured["schema"] = args[3]
            return original(*args, **kwargs)
        self.provider.run = capture
        run = self.discover()
        properties = captured["schema"]["properties"]
        self.assertEqual(properties["input_hash"]["const"], run["input_hash"])
        self.assertEqual(properties["run_id"]["const"], run["id"])
        from content_engine.ai import validate_schema
        with self.assertRaisesRegex(ValueError, "身份"):
            validate_schema(run["input_hash"][:-16], properties["input_hash"])

    def test_source_enrichment_requires_exact_quotes_and_supported_numbers(self):
        from content_engine.material_enrichment import enrich
        run, raw, _ = self.raw()
        source = {**raw["sources"][0], "kind": "media", "publisher": "CBS Sports", "url": "https://www.cbssports.com/nba/news/test-report"}
        raw["sources"] = [source]
        quote = "At halftime the home team led 66-63. After three quarters the visitors led 91-85."
        basic = {"source_ids": ["s1"], "speaker": "", "original": "", "translation": "", "context": "", "supporting_quotes": [{"source_id": "s1", "quote": quote}]}
        facts = [{**basic, "id": "new", "text": "半场主队66比63领先，三节结束客队91比85领先。"}, {**basic, "id": "invented_number", "text": "主队得到9999分。"}, {**basic, "id": "invented_quote", "text": "客队91比85领先。", "supporting_quotes": [{"source_id": "s1", "quote": "This sentence was never in the original report."}]}]
        with patch("content_engine.material_enrichment.read", return_value={"status": "readable", "url": source["url"], "text": quote}), patch.object(self.agent, "invoke", return_value=({"facts": facts, "omitted": []}, {})):
            result = enrich(self.agent, run, raw)
        self.assertEqual(len(result["facts"]), len(raw["facts"]) + 1)
        self.assertEqual(result["facts"][-1]["agent_check"]["status"], "attributed")
        self.assertFalse(result["facts"][-1]["verified"])

    def test_source_enrichment_failure_keeps_usable_core_facts(self):
        from content_engine.material_enrichment import enrich
        run, raw, _ = self.raw()
        with patch("content_engine.material_enrichment.read", return_value={"status": "unavailable", "url": raw["sources"][0]["url"]}), patch.object(self.agent, "invoke") as invoke:
            result = enrich(self.agent, run, raw)
        self.assertEqual(result, raw)
        invoke.assert_not_called()

    def test_player_stat_alone_does_not_unlock_result_article(self):
        run, raw, telemetry = self.raw()
        raw["template_id"] = "result"
        raw["facts"][0]["text"] = "球员得到47分、4个篮板。"
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, telemetry)
        self.assertFalse(rejected)
        self.assertFalse(values[0]["agent_ready"])

    def test_related_feed_is_not_a_permalink_for_another_match(self):
        run, raw, telemetry = self.raw()
        raw["sources"][0]["excerpt"] = "页面同列其他比赛的球员快讯，不是本报道。"
        values, rejected = self.agent.validate_candidates(run, {"candidates": [raw]}, telemetry)
        self.assertFalse(rejected)
        self.assertEqual([s["id"] for s in values[0]["sources"]], ["s2"])
        self.assertFalse(values[0]["agent_ready"])
