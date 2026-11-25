"""Verify an extracted Windows release. The optional live run uses the local DeepSeek key."""
import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from content_engine.ai import SecretKey


def acceptance(app, port, live, crawler_live=False):
    python = app / "runtime" / "python.exe"
    data = app / "data"
    url = f"http://127.0.0.1:{port}"
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("CODEX", "OPENAI", "PYTHON", "CONTENT_ENGINE", "DEEPSEEK"))}
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    if live:
        key = SecretKey(ROOT / "data").get()
        if not key:
            raise RuntimeError("本机未配置 DeepSeek Key")
        env["DEEPSEEK_API_KEY"] = key
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def control(action):
        command = [str(python), "-X", "utf8", str(app / "scripts" / "workbench_control.py"), action, "--port", str(port)]
        result = subprocess.run(command, env=env, cwd=app, capture_output=True, encoding="utf-8", timeout=55, check=True)
        return json.loads(result.stdout)

    def call(path, body=None, binary=False):
        headers = {"X-App-Token": (data / ".local-token").read_text().strip(), "Origin": url}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url + path, headers=headers, data=json.dumps(body).encode() if body is not None else None)
        with opener.open(request, timeout=45) as response:
            return (response.read(), response.headers) if binary else json.load(response)

    def wait(run_id):
        deadline = time.monotonic() + 700
        while time.monotonic() < deadline:
            result = call("/api/agent/runs/" + run_id)
            run = result.get("run", result)
            if run["state"] not in ("queued", "running"):
                if run["state"] not in ("needs_review", "awaiting_selection", "no_candidates", "skill_ready"):
                    raise RuntimeError(f'{run["kind"]}: {run.get("error")}')
                events = result["events"]
                assert all(event["data"].get("provider") in (None, "deepseek", "deepseek_search") for event in events)
                execution = [{"stage": event["data"].get("stage_key"), "model": event["data"].get("reported_model")}
                             for event in events if event["kind"] == "provider"]
                assert execution and all(row["model"] for row in execution), execution
                print(run["kind"], run["state"], execution, flush=True)
                return run["result"], execution
            time.sleep(3)
        raise RuntimeError("验收任务超时")

    assert shutil.which("codex", path=env["PATH"]) is None
    report = {"checks": {}}
    status = control("start")
    assert status["state"] == "running", status
    try:
        assert call("/health")["deepseek_only"]
        state = call("/api/agent/state?account_id=main")
        media = state["runtime"]["crawler"]
        if media.get("bundled"):
            assert media["installed"] and media["browser_ready"] and state["bridges"]["main"]["enabled"], media
            assert Path(media["python"]) == python and not (app / "tools").exists()
            env["PLAYWRIGHT_BROWSERS_PATH"] = media["browser_cache"]
            smoke = app.parent / "crawler-smoke.json"
            subprocess.run([str(python), "-X", "utf8", media["worker"], "--smoke", "--output", str(smoke)],
                           cwd=app, env=env, timeout=55, check=True)
            sample = json.loads(smoke.read_text(encoding="utf-8"))
            assert sample["adapter"] == "bundled_public_hotlists"
            assert [board["count"] for board in sample["boards"]] == [1, 1, 1], sample
            assert all(row["title"] == "NBA 赛后" for row in sample["records"]), sample
            report["checks"]["bundled_media_browser"] = True
        if crawler_live:
            assert report["checks"].get("bundled_media_browser"), media
            # The same collection path as discovery, through the engine and signal store.
            script = (
                "import json,sys; from content_engine.engine import Engine; "
                "engine=Engine(sys.argv[1]); events=[]; "
                "result=engine.agent.crawler.collect('main','',engine.modes.get('sports'),"
                "lambda kind,message,data: events.append(data),force=True); "
                "print(json.dumps({'result':result,'boards':events[-1].get('boards',[]),"
                "'stored_signals':len(engine.agent.recent_signals('main'))},ensure_ascii=False))"
            )
            outcome = subprocess.run([str(python), "-X", "utf8", "-c", script, str(data)], cwd=app,
                                     env=env, encoding="utf-8", capture_output=True, check=True, timeout=70)
            report["live_crawler"] = json.loads(outcome.stdout)
            assert report["live_crawler"]["boards"], report["live_crawler"]
        configuration = call("/api/ai")
        routes = configuration["selection"]["stage_routes"]
        assert len(routes) == 7 and all(route["provider"] == "deepseek" for route in routes.values())
        routes["writing"]["provider"] = "codex"
        try:
            call("/api/ai/settings", {"ai": {"stage_routes": routes}})
        except urllib.error.HTTPError as exc:
            assert exc.code == 400 and "DeepSeek" in exc.read().decode()
        else:
            raise AssertionError("独立版接受了 Codex 配置")
        report["checks"].update(clean_package=True, seven_deepseek_routes=True, rejects_codex_override=True)
        if live:
            report["connection"] = call("/api/ai/deepseek-test", {"generate": True})
            run = call("/api/agent/discover", {"topic_profile_id": "adaptive"})
            topics, report["discovery_execution"] = wait(run["id"])
            report["candidates"] = len(topics["candidate_ids"])
            if topics["candidate_ids"]:
                address = "/api/agent/candidates/" + topics["candidate_ids"][0] + "/decision"
                run = call(address, {"decision": "select", "content_depth": "standard"})
            else:
                # Verify the independent manual path even when news hour has no accepted candidate.
                from content_engine.ai import deepseek_request
                source = "https://www.nba.com/news/nba-cup-101"
                raw = deepseek_request(key, "/tools/fetch", {"url": source}, timeout=25)
                original = raw.get("content", "")
                assert len(original) >= 250 and "Group Play" in original
                topic = call("/api/topics", {"title": "NBA杯小组赛怎样决定晋级", "template_id": "rules",
                    "facts": [{"id": "f1", "url": source, "text": "NBA杯分小组赛和淘汰赛两个阶段。", "verified": True},
                              {"id": "f2", "url": source, "text": "NBA杯小组赛战绩决定哪些球队晋级淘汰赛。", "verified": True}]})
                run = call(f'/api/topics/{topic["id"]}/generate', {"provider": "agent"})
            result, report["production_execution"] = wait(run["id"])
            article_id = result["article_id"]
            article_path = f"/api/articles/{article_id}"
            loaded = call(article_path)
            document = loaded["document"]
            document["title"] += "（验收稿）"
            call(article_path + "/document", {"base_revision": loaded["article"]["revision"], "document": document})
            loaded = call(article_path)
            assert loaded["document"]["title"].endswith("（验收稿）")
            word, headers = call(article_path + "/export-download", {"format": "docx", "revision": loaded["article"]["revision"]}, binary=True)
            with zipfile.ZipFile(io.BytesIO(word)) as docx:
                assert docx.testzip() is None
                assert "验收稿" in docx.read("word/document.xml").decode()
                report["embedded_media"] = sum(name.startswith("word/media/") for name in docx.namelist())
            call(article_path + "/export-complete", {"receipt": headers["X-Export-Id"]})
            (app.parent / "独立版验收稿.docx").write_bytes(word)
            revised = call("/api/agent/revise", {"article_id": article_id, "base_revision": loaded["article"]["revision"],
                "feedback": "把导语写得更紧凑，保留所有已有事实及其依据。"})
            _, report["revision_execution"] = wait(revised["id"])
            report["checks"].update(actual_discovery=True, actual_production=True, edit_and_docx=True, actual_revision=True)
        with sqlite3.connect(data / "workbench.sqlite3") as database:
            assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return report
    finally:
        if control("status")["state"] == "running":
            control("stop")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--crawler-live", action="store_true", help="实际读取公开热榜并检查线索入库，不调用模型")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    staging = ROOT / "data" / ("v09-acceptance-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    staging.mkdir(parents=True)
    with zipfile.ZipFile(args.archive) as bundle:
        assert not any({"data", "credentials", ".codex"} & set(Path(name).parts[1:2]) for name in bundle.namelist())
        bundle.extractall(staging)
    print("Isolated release:", staging, flush=True)
    report = acceptance(staging / "看点工场", args.port, args.live, args.crawler_live)
    (staging / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
