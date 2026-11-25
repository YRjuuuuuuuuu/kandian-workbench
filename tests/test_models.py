import copy
import datetime as dt
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import Mock, patch

import test_agent as fixtures
from content_engine.ai import DEFAULTS, DeepSeekProvider, ExternalPending, SecretKey, WorkBuddyProvider, deepseek_request, normalize, validate_schema
from content_engine.codex_agent import CodexAgentProvider, restricted_config
from content_engine.engine import iso, now
from content_engine.server import make_server

SIMPLE = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


class ModelsTests(unittest.TestCase):
    setUp = fixtures.AgentTests.setUp
    tearDown = fixtures.AgentTests.tearDown
    discover = fixtures.AgentTests.discover

    def waiting(self):
        self.agent.preferences({"account_id": "main", "production_mode": "fast"})
        self.agent.provider = None
        self.engine.models.adapter = lambda selection, search=False, stage=None: self.provider if search else WorkBuddyProvider(selection)
        found = self.discover()
        cid = found["result"]["candidate_ids"][0]
        run = self.agent.decide(cid, {"decision": "select", "ai": {"production_provider": "workbuddy"}})
        self.agent.tick(schedule=False)
        result = self.agent.run(run["id"])
        self.assertEqual(result["state"], "waiting_external", result["error"])
        return result

    def external_result(self, run):
        expected = run["result"]["external"]["expected_identity"]
        return {**expected, "title": "合成篮球比赛的分析边界", "paragraphs": [{"text": "这是合成测试材料，不是真实比赛新闻。甲队以98比94取胜，获得12个进攻篮板。材料提供了赛果和统计，但没有逐回合的完整录像证据。", "kind": "fact", "claim_ids": ["f1"]}, {"text": "进攻篮板可能带来额外进攻机会，文章需要区分观察到的统计与作者的解释。缺乏回合证据时，不能把一项数据写成比赛获胜的唯一原因。保留其他解释，才有助于读者理解分析的适用范围。", "kind": "analysis", "claim_ids": []}], "unresolved": [], "notes": ["合成测试，不可发布"], "image_queries": []}

    def test_default_and_task_snapshot_ignore_later_setting_changes(self):
        selection = self.agent.config("main")["ai"]
        self.assertEqual(selection["codex_model"], "gpt-6.1-terra")
        self.assertEqual(selection["reasoning_effort"], "medium")
        self.assertEqual(selection["research_reasoning_effort"], "high")
        run = self.agent.discover({"account_id": "main"})
        self.agent.preferences({"account_id": "main", "ai": {"codex_model": "gpt-6-astra", "reasoning_effort": "high"}})
        self.assertEqual(self.agent.run(run["id"])["payload"]["config"]["ai"], selection)
        self.assertEqual(self.agent.config("main")["ai"]["codex_model"], "gpt-6-astra")

    def test_legacy_model_history_is_not_relabelled_with_new_default(self):
        created = self.agent.discover({"account_id": "main"})
        run = self.agent.run(created["id"])
        del run["payload"]["config"]["ai"]
        with self.engine.store.db() as db:
            db.execute("UPDATE agent_runs SET payload=? WHERE id=?", (json.dumps(run["payload"]), run["id"]))
        self.assertIsNone(self.agent.state("main")["runs"][0]["ai"])
        self.assertIsNone(self.agent.detail(run["id"])["run"]["ai"])

    def test_routing_separates_research_from_production(self):
        selected = normalize({"production_provider": "deepseek"})
        self.assertIsInstance(self.engine.models.adapter(selected), DeepSeekProvider)
        research = self.engine.models.adapter(selected, search=True)
        self.assertIsInstance(research, CodexAgentProvider)
        self.assertEqual(research.model, "gpt-6.1-terra")
        self.assertIn('model_reasoning_effort="low"', restricted_config("low"))
        self.assertIsInstance(self.engine.models.adapter(normalize({"production_provider": "workbuddy"})), WorkBuddyProvider)

    def test_configuration_rejects_command_injection_and_unknown_provider(self):
        for value in ({"codex_model": "gpt; echo bad"}, {"production_provider": "unknown"}, {"deepseek_max_tokens": True}):
            with self.assertRaises(ValueError):
                normalize(value)
        self.assertNotIn("api_key", normalize({"api_key": "not-persisted"}))

    def test_deepseek_json_usage_and_bounded_request(self):
        secret = Mock()
        secret.get.return_value = "test-key-never-log"
        provider = DeepSeekProvider(secret, normalize({"production_provider": "deepseek"}))
        response = {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 30, "completion_tokens": 7, "prompt_cache_hit_tokens": 10}}
        with patch("content_engine.ai.deepseek_request", return_value=response) as call:
            result, telemetry = provider.run(self.folder, "writing", "合成测试 JSON", SIMPLE, lambda *a: None, lambda: False)
        self.assertTrue(result["ok"])
        self.assertEqual(telemetry["usage"], {"input_tokens": 30, "output_tokens": 7, "cached_input_tokens": 10})
        body = call.call_args.args[2]
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_deepseek_missing_key_never_falls_back(self):
        secret = Mock()
        secret.get.return_value = ""
        provider = DeepSeekProvider(secret, normalize())
        with patch("content_engine.ai.deepseek_request") as call, self.assertRaisesRegex(ValueError, "Key"):
            provider.run(self.folder, "writing", "JSON", SIMPLE, lambda *a: None, lambda: False)
        call.assert_not_called()

    def test_missing_key_stops_before_admission_or_research_cost(self):
        with patch.object(self.engine.models.secret, "status", return_value={"configured": False}), self.assertRaisesRegex(ValueError, "没有创建任务"):
            self.agent.create("produce", {"account_id": "main", "ai": {"production_provider": "deepseek"}})
        self.assertFalse(self.engine.store.all("SELECT id FROM agent_runs"))

    def test_deepseek_truncated_or_schema_invalid_is_rejected(self):
        provider = DeepSeekProvider(Mock(get=Mock(return_value="test-key")), normalize())
        for choice in ({"finish_reason": "length", "message": {"content": '{"ok":true}'}}, {"finish_reason": "stop", "message": {"content": '{"ok":"true"}'}}):
            with patch("content_engine.ai.deepseek_request", return_value={"choices": [choice]}), self.assertRaises(ValueError):
                provider.run(self.folder, "writing", "JSON", SIMPLE, lambda *a: None, lambda: False)

    def test_http_errors_redact_response_and_key(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.deepseek.com/v1/models", 401, "do not echo test-secret", {}, io.BytesIO(b'test-secret'))
        with patch("content_engine.ai.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "HTTP 401") as caught:
                deepseek_request("test-secret", "/models")
        self.assertNotIn("test-secret", str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_key_encrypted_and_not_returned_in_status(self):
        secret = SecretKey(self.folder)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            secret.set("synthetic-key-only-for-test")
            self.assertNotIn(b"synthetic-key", secret.path.read_bytes())
            self.assertEqual(secret.get(), "synthetic-key-only-for-test")
            self.assertNotIn("synthetic-key", json.dumps(secret.status()))

    def test_workbuddy_resume_reuses_job_and_does_not_research_twice(self):
        run = self.waiting()
        self.assertEqual(self.engine.store.one("SELECT COUNT(*) n FROM jobs")["n"], 1)
        folder = self.agent.external_folder(run)
        self.assertTrue((folder / "skills" / "evidence-content-writer" / "SKILL.md").is_file())
        self.agent.submit_external(run["id"], {"result": self.external_result(run)})
        self.agent.tick(schedule=False)
        result = self.agent.run(run["id"])
        self.assertEqual(result["state"], "needs_review", result["error"])
        self.assertEqual(self.engine.store.one("SELECT COUNT(*) n FROM jobs")["n"], 1)
        self.assertEqual([x["stem"] for x in self.provider.calls], ["discovery"])
        self.assertIsNone(self.engine.article(result["result"]["article_id"])["approval_hash"])

    def test_bad_workbuddy_identity_stays_waiting(self):
        run = self.waiting()
        output = self.external_result(run)
        output["input_hash"] = "wrong"
        with self.assertRaisesRegex(ValueError, "身份"):
            self.agent.submit_external(run["id"], {"result": output})
        self.assertEqual(self.agent.run(run["id"])["state"], "waiting_external")
        self.assertFalse(self.engine.store.all("SELECT * FROM articles"))

    def test_file_watch_resumes_and_waiting_does_not_take_worker_slot(self):
        run = self.waiting()
        other = self.agent.discover({"account_id": "main", "focus": "另一篮球测试方向"})
        self.assertEqual(self.agent.claim_next()["id"], other["id"])
        self.agent.cancel(other["id"])
        (self.agent.external_folder(run) / "output.json").write_text(json.dumps(self.external_result(run)), encoding="utf-8")
        self.agent.tick(schedule=False)
        self.assertEqual(self.agent.run(run["id"])["state"], "needs_review")

    def test_waiting_result_expires_next_day_and_cancels(self):
        run = self.waiting()
        with patch("content_engine.agent.now", return_value=now()+dt.timedelta(days=1)):
            self.agent.tick(execute=False, schedule=False)
        self.assertEqual(self.agent.run(run["id"])["state"], "failed")
        self.assertIn("跨日", self.agent.run(run["id"])["error"])

    def test_cancel_waiting_releases_candidate(self):
        run = self.waiting()
        self.agent.cancel(run["id"])
        self.assertEqual(self.agent.run(run["id"])["state"], "cancelled")
        self.assertEqual(self.agent.candidate(run["payload"]["candidate_id"])["decision"], "needs_attention")

    def test_external_http_package_is_allowlisted_and_upload_resumes(self):
        run = self.waiting()
        (self.agent.external_folder(run) / "unrelated-secret.json").write_text('{"private":"test"}', encoding="utf-8")
        server = make_server(self.engine, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        token = (self.engine.data_dir / ".local-token").read_text()
        endpoint = f"http://127.0.0.1:{server.server_port}/api/agent/runs/{run['id']}"
        try:
            request = urllib.request.Request(endpoint + "/external-package", headers={"X-App-Token": token})
            with urllib.request.urlopen(request) as r, zipfile.ZipFile(io.BytesIO(r.read())) as archive:
                self.assertIn("instructions.md", archive.namelist())
                self.assertNotIn("unrelated-secret.json", archive.namelist())
            request = urllib.request.Request(endpoint + "/external-output", data=json.dumps({"result": self.external_result(run)}).encode(), headers={"X-App-Token": token, "Content-Type": "application/json"})
            with urllib.request.urlopen(request) as r:
                self.assertTrue(json.load(r)["ok"])
        finally:
            server.shutdown()
            server.server_close()
