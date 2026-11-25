import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_agent
from content_engine.agent import RESEARCH_SCHEMA
from content_engine.ai import DeepSeekProvider
from content_engine.deepseek_research import research_result
from content_engine.engine import iso


class StandaloneTests(unittest.TestCase):
    setUp = test_agent.AgentTests.setUp
    tearDown = test_agent.AgentTests.tearDown

    def test_standalone_converts_preferences_but_rejects_old_execution_routes(self):
        old = self.agent.config("main")["ai"]
        self.engine.deepseek_only = True
        new = self.agent.config("main")["ai"]
        self.assertTrue(all(route["provider"] == "deepseek" for route in new["stage_routes"].values()))
        self.assertIsInstance(self.engine.models.adapter(new, stage="image_review"), DeepSeekProvider)
        with self.assertRaisesRegex(ValueError, "旧版模型"):
            self.engine.models.adapter(old, stage="writing")
        with self.assertRaisesRegex(ValueError, "旧版模型"):
            self.agent.preferences({"ai": {"stage_routes": old["stage_routes"]}})

    def test_legacy_external_wait_is_ended_after_switch_to_standalone(self):
        run_id = self.agent.discover({})["id"]
        with self.engine.store.db() as database:
            database.execute("UPDATE agent_runs SET state='waiting_external' WHERE id=?", (run_id,))
        self.engine.deepseek_only = True
        self.agent.tick(execute=False, schedule=False)
        run = self.agent.run(run_id)
        self.assertEqual(run["state"], "failed")
        self.assertIn("重建", run["error"])

    def topic(self):
        return self.engine.add_topic({"title": "篮球赛制观察", "template_id": "rules",
            "facts": [{"id": "f1", "text": "篮球赛制分小组赛和淘汰赛。", "url": "https://www.nba.com/news/example", "verified": True}]})["id"]

    def test_manual_topic_runs_shared_pipeline_and_repairs_invalid_fact_id(self):
        self.engine.deepseek_only = True
        with patch.object(self.engine.models.secret, "get", return_value="unit-test-key"):
            topic_id = self.topic()
            created = self.agent.create("produce", {"topic_id": topic_id})
            self.assertEqual(self.agent.create("produce", {"topic_id": topic_id})["id"], created["id"])
            def produce(run, stem, prompt, schema, **kwargs):
                checkpoint = self.engine.store.one("SELECT data FROM agent_checkpoints WHERE run_id=?", (run["id"],))
                job_id = json.loads(checkpoint["data"])["job_id"]
                job = self.engine.store.one("SELECT input_hash FROM jobs WHERE id=?", (job_id,))
                return {"job_id": job_id, "input_hash": job["input_hash"], "title": "篮球赛制观察",
                    "paragraphs": [{"text": "篮球赛制分小组赛和淘汰赛。", "kind": "fact",
                                    "claim_ids": ["wrong" if stem == "writing" else "f1"]}],
                    "unresolved": [], "notes": [], "image_queries": []}, {}
            with patch.object(self.agent, "invoke", side_effect=produce) as invoke:
                self.agent.tick(schedule=False)
        run = self.agent.run(created["id"])
        self.assertEqual(run["state"], "needs_review", run["error"])
        self.assertIn("writing_fix", [call.args[1] for call in invoke.call_args_list])
        self.assertEqual(self.engine.article(run["result"]["article_id"])["topic_id"], topic_id)

    def test_bad_candidate_does_not_discard_valid_siblings(self):
        candidate = {"scope_id": "basketball", "title": "合成测试", "event_key": "test", "summary": "summary",
            "angle": "angle", "why_now": "today", "template_id": "analysis", "event_at": iso(),
            "event_end_at": None, "event_date_cn": "2026-11-25", "time_precision": "date", "event_final": True,
            "sources": [], "facts": [{"id": "f1", "text": "测试", "source_ids": ["s1"], "speaker": "",
            "original": "", "translation": "", "context": ""}], "cautions": [], "value_score": 60}
        malformed = copy.deepcopy(candidate)
        malformed["facts"][0]["source_ids"] = "s1"
        result, rejected = research_result({"run_id": "run", "input_hash": "hash", "summary": "",
            "queries": [], "candidates": [malformed, candidate], "unavailable": []}, RESEARCH_SCHEMA, [])
        self.assertEqual(result["candidates"], [candidate])
        self.assertIn("source_ids", rejected[0]["reason"])

    def test_research_length_recovery_reuses_sources_with_same_model_and_budget(self):
        def completion(reason, message):
            return {"model": "deepseek-flash", "choices": [{"finish_reason": reason, "message": message}]}
        provider = DeepSeekProvider(Mock(get=Mock(return_value="example-key")), {"deepseek_model": "deepseek-flash", "deepseek_max_tokens": 4096})
        source = "https://www.nba.com/news/example"
        responses = [
            completion("tool_calls", {"tool_calls": [{"id": "t1", "type": "function", "function": {
                "name": "search_web", "arguments": '{"query":"NBA"}'}}]}),
            {"results": [{"url": source, "content": "公开资料。" * 100}]},
            completion("length", {"content": "discarded fragment"}),
            completion("stop", {"content": '{"run_id":"r","input_hash":"h","summary":"","queries":[],"candidates":[],"unavailable":[]}'})
        ]
        with patch("content_engine.ai.deepseek_request", side_effect=responses) as request:
            _, telemetry = provider.run(self.folder, "discovery", "research", RESEARCH_SCHEMA,
                                        lambda *args: None, lambda: False, search=True)
        final = request.call_args.args[2]
        self.assertNotIn("tools", final)
        self.assertEqual(final["model"], "deepseek-flash")
        self.assertEqual(final["max_tokens"], 4096)
        self.assertEqual(telemetry["observed_urls"], [source])
        self.assertEqual(request.call_count, 4)
