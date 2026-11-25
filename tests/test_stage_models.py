import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_agent as fixtures
from content_engine.ai import DeepSeekProvider, WorkBuddyProvider, normalize, stage_name
from content_engine.codex_agent import AgentCancelled, CodexAgentProvider


def all_deepseek():
    ai = normalize({"production_provider": "deepseek"})
    ai["stage_routes"] = {stage: {"provider": "deepseek", "model": "deepseek-flash", "effort": "medium", "max_tokens": 4096} for stage in ai["stage_routes"]}
    return ai


SIMPLE = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def completion(message, finish="stop"):
    return {"model": "deepseek-flash", "choices": [{"message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class StageModelsTests(unittest.TestCase):
    setUp = fixtures.AgentTests.setUp
    tearDown = fixtures.AgentTests.tearDown

    def test_all_five_stages_and_enrichment_can_use_deepseek(self):
        ai = all_deepseek()
        for stage in ai["stage_routes"]:
            adapter = self.engine.models.adapter(ai, search=stage in ("discovery", "research"), stage=stage)
            self.assertIsInstance(adapter, DeepSeekProvider)
        self.assertEqual(stage_name("enrichment"), "research")
        self.assertEqual(stage_name("repair"), "polish")
        self.assertEqual(stage_name("revision"), "revision")
        self.agent.preferences({"account_id": "main", "ai": ai})
        runtime = self.agent.state("main")["runtime"]
        self.assertEqual(runtime["ai"]["provider"], "deepseek")
        self.assertEqual(runtime["ai"]["model"], "deepseek-flash")
        self.assertEqual(runtime["models"]["research_provider"], "deepseek")

    def test_mixed_routes_select_actual_stage_model_and_effort(self):
        ai = all_deepseek()
        ai["stage_routes"]["research"].update(provider="codex", model="gpt-6.1-sol", effort="low")
        ai["stage_routes"]["revision"].update(provider="workbuddy", model="桌面模型说明")
        research = self.engine.models.adapter(ai, search=True, stage="research")
        self.assertIsInstance(research, CodexAgentProvider)
        self.assertEqual((research.model, research.effort), ("gpt-6.1-sol", "low"))
        self.assertIsInstance(self.engine.models.adapter(ai, stage="writing"), DeepSeekProvider)
        self.assertIsInstance(self.engine.models.adapter(ai, stage="revision"), WorkBuddyProvider)

    def test_new_tasks_freeze_routes_and_historical_tasks_keep_mixed_route(self):
        legacy = normalize({"production_provider": "deepseek"})
        self.assertEqual(legacy["stage_routes"]["research"]["provider"], "codex")
        with patch.object(self.engine.models.secret, "status", return_value={"configured": True}):
            self.agent.preferences({"account_id": "main", "ai": all_deepseek()})
            first = self.agent.discover({"account_id": "main"})
            changed = all_deepseek()
            changed["stage_routes"]["discovery"].update(provider="codex", model="gpt-6.1-sol")
            self.agent.preferences({"account_id": "main", "ai": changed})
        self.assertEqual(self.agent.run(first["id"])["payload"]["config"]["ai"]["stage_routes"]["discovery"]["provider"], "deepseek")
        self.assertTrue(self.agent.detail(first["id"])["run"]["ai_routes_explicit"])
        with self.assertRaisesRegex(ValueError, "刷新工作台"):
            self.agent.preferences({"account_id": "main", "ai": {"production_provider": "deepseek"}})
        self.assertEqual(self.agent.config("main")["ai"]["stage_routes"], changed["stage_routes"])

    def test_missing_key_rejects_deepseek_discovery_before_admission(self):
        self.agent.preferences({"account_id": "main", "ai": all_deepseek()})
        with patch.object(self.engine.models.secret, "status", return_value={"configured": False}), self.assertRaisesRegex(ValueError, "没有创建任务"):
            self.agent.discover({"account_id": "main"})
        self.assertFalse(self.engine.store.all("SELECT id FROM agent_runs"))

    def test_enrichment_invoke_uses_research_route_without_hidden_codex(self):
        with patch.object(self.engine.models.secret, "status", return_value={"configured": True}):
            created = self.agent.discover({"account_id": "main", "ai": all_deepseek()})
        run = self.agent.claim_next()
        self.agent.provider = None
        identity = {"run_id": run["id"], "input_hash": run["input_hash"]}
        schema = {"type": "object", "properties": {k: {"type": "string"} for k in identity}, "required": list(identity), "additionalProperties": False}
        with patch.object(self.engine.models.secret, "get", return_value="synthetic-key"), patch("content_engine.ai.deepseek_request", return_value=completion({"content": json.dumps(identity)})), patch("content_engine.ai.CodexAgentProvider", side_effect=AssertionError("hidden Codex")):
            self.agent.invoke(run, "enrichment", "合成测试", schema, research_model=True)
        route = self.engine.store.one("SELECT data FROM agent_events WHERE run_id=? AND kind='route'", (created["id"],))
        self.assertEqual(json.loads(route["data"])["stage_key"], "research")
        self.assertEqual(json.loads(route["data"])["provider"], "deepseek")

    def test_routes_reject_missing_stage_unavailable_provider_and_bad_model(self):
        invalid = []
        ai = all_deepseek(); del ai["stage_routes"]["research"]; invalid.append(ai)
        ai = all_deepseek(); ai["stage_routes"]["research"]["provider"] = "workbuddy"; invalid.append(ai)
        ai = all_deepseek(); ai["stage_routes"]["writing"]["model"] = "gpt; echo"; invalid.append(ai)
        for ai in invalid:
            with self.assertRaises(ValueError): normalize(ai)

    def test_research_uses_real_search_payload_and_aggregates_usage(self):
        provider = DeepSeekProvider(Mock(get=Mock(return_value="synthetic-key")), all_deepseek())
        calls = [{"id": "search1", "type": "function", "function": {"name": "search_web", "arguments": '{"query":"synthetic sports test"}'}}]
        results = [completion({"content": None, "tool_calls": calls}, "tool_calls"), {"results": [{"url": "https://example.org/sports-test", "title": "Synthetic", "content": "Synthetic public research material. " * 20}]}, completion({"content": '{"ok":true}'})]
        events = []
        with patch("content_engine.ai.deepseek_request", side_effect=results) as request:
            value, info = provider.run(self.folder, "discovery", "合成测试 JSON", SIMPLE, lambda *e: events.append(e), lambda: False, search=True)
        self.assertTrue(value["ok"])
        self.assertEqual(request.call_args_list[1].args[1:3], ("/tools/search", {"query": "synthetic sports test", "count": 4, "need_content": True}))
        self.assertEqual(info["web_searches"], 1)
        self.assertEqual(info["observed_urls"], ["https://example.org/sports-test"])
        self.assertEqual(info["usage"], {"input_tokens": 20, "output_tokens": 10})
        self.assertEqual(info["reported_model"], "deepseek-flash")

    def test_summary_only_search_is_not_recorded_as_read_original(self):
        provider = DeepSeekProvider(Mock(get=Mock(return_value="synthetic-key")), all_deepseek())
        calls = [{"id": "s", "type": "function", "function": {"name": "search_web", "arguments": '{"query":"synthetic test"}'}}]
        with patch("content_engine.ai.deepseek_request", side_effect=[completion({"content": None, "tool_calls": calls}, "tool_calls"), {"results": [{"url": "https://example.org/test", "summary": "摘要" * 500, "content": ""}]}, completion({"content": '{"ok":true}'})]):
            _, info = provider.run(self.folder, "discovery", "JSON", SIMPLE, lambda *a: None, lambda: False, search=True)
        self.assertEqual(info["observed_urls"], [])

    def test_auth_error_never_calls_another_provider(self):
        provider = DeepSeekProvider(Mock(get=Mock(return_value="synthetic-key")), all_deepseek())
        with patch("content_engine.ai.deepseek_request", side_effect=ValueError("DeepSeek HTTP 402：余额不足")), patch("content_engine.ai.CodexAgentProvider", side_effect=AssertionError("hidden fallback")), self.assertRaisesRegex(ValueError, "402"):
            provider.run(self.folder, "discovery", "JSON", SIMPLE, lambda *a: None, lambda: False, search=True)

    def test_cancelled_research_does_not_call_api(self):
        provider = DeepSeekProvider(Mock(get=Mock(return_value="synthetic-key")), all_deepseek())
        with patch("content_engine.ai.deepseek_request") as request, self.assertRaises(AgentCancelled):
            provider.run(self.folder, "discovery", "JSON", SIMPLE, lambda *a: None, lambda: True, search=True)
        request.assert_not_called()

    def test_schema_auxiliary_fields_are_removed_without_inventing_required_data(self):
        from content_engine.deepseek_research import schema_fields
        ignored=[]
        value=schema_fields({'ok':True,'debug_source':'auxiliary'},SIMPLE,ignored)
        self.assertEqual(value,{'ok':True})
        self.assertEqual(ignored,['result.debug_source'])
        self.assertEqual(schema_fields({'other':1},SIMPLE,[]),{})

    def test_first_search_cannot_silently_use_previous_year(self):
        provider=DeepSeekProvider(Mock(get=Mock(return_value='synthetic-key')),all_deepseek())
        calls=[{'id':'s','type':'function','function':{'name':'search_web','arguments':'{"query":"NBA November 5 2025 scores"}'}}]
        with patch('content_engine.ai.deepseek_request',side_effect=[completion({'content':None,'tool_calls':calls},'tool_calls'),completion({'content':'{"ok":true}'})]) as request:
            _,info=provider.run(self.folder,'discovery','request: {"beijing_now":"2026-11-05T15:00:00+08:00"}',SIMPLE,lambda *a:None,lambda:False,search=True)
        self.assertEqual(info['web_searches'],0)
        self.assertEqual(len(request.call_args_list),2)
        self.assertIn('2026',info['tool_calls'][0]['error'])


if __name__ == "__main__":
    unittest.main()
