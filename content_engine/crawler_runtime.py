"""Bounded public hotlist collection with the bundled browser."""
import json
import os
import subprocess
import threading


def bundled_media_status(root):
    component = root / "components" / "hotlists"
    runtime = root / "runtime"
    python = runtime / "python.exe"
    worker = component / "collect.py"
    media = runtime / "hotlists"
    driver = media / "dependencies" / "playwright" / "driver"
    browsers = media / "browsers"
    installed = all(path.is_file() for path in (
        python, worker, driver / "node.exe",
    ))
    browser_ready = False
    if installed:
        try:
            manifest = json.loads((driver / "package" / "browsers.json").read_text(encoding="utf-8"))
            revision = next(item["revision"] for item in manifest["browsers"] if item["name"] == "chromium-headless-shell")
            binary = browsers / f"chromium_headless_shell-{revision}" / "chrome-headless-shell-win64" / "chrome-headless-shell.exe"
            browser_ready = binary.is_file()
        except (OSError, ValueError, KeyError, StopIteration):
            pass
    return {"installed": installed, "browser_ready": browser_ready, "worker": str(worker),
            "python": str(python), "browser_cache": str(browsers), "bundled": True}


class HotlistRuntime:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()

    def status(self):
        from .engine import ROOT
        return {**bundled_media_status(ROOT), "purpose": "learning_research", "platform": "weibo,douyin",
                "max_items": 160,
                "comments": False, "uses_daily_browser": False}

    def collect(self, account_id, focus, mode, on_event=None, force=False, profile=None):
        from .engine import ROOT, iso, now, uid
        on_event = on_event or (lambda *args: None)
        config = self.engine.agent.bridge_config(account_id)
        if not config.get("enabled") or config.get("purpose") != "learning_research":
            return {"status": "disabled", "imported": 0}
        status = self.status()
        if not status["installed"] or not status["browser_ready"]:
            message = "内置热榜组件文件不完整，本轮继续使用公开赛事来源找题"
            on_event("crawler", message, {"status": "component_unavailable", "imported": 0})
            return {"status": "component_unavailable", "imported": 0, "message": message}
        if not self.lock.acquire(blocking=False):
            return {"status": "busy", "imported": 0, "message": "采集器正在处理另一任务，先使用已有线索"}
        try:
            from .topic_profiles import resolve
            profile = profile or resolve(mode)
            on_event("crawler", "正在读取微博体育热搜、微博实时热搜与抖音热点榜，筛选" + profile["scope"]["name"], {"max_items": 160, "bundled": True})
            try:
                cache = self.engine.data_dir / "crawler" / "hotlists-cache.json"
                if not force and cache.is_file() and now().timestamp() - cache.stat().st_mtime < 300:
                    output = json.loads(cache.read_text(encoding="utf-8"))
                    output["cached"] = True
                else:
                    path = cache.parent / uid() / "result.json"
                    path.parent.mkdir(parents=True)
                    env = {key: value for key, value in os.environ.items()
                           if not key.upper().startswith(("DEEPSEEK", "OPENAI", "CODEX", "PYTHON", "PLAYWRIGHT"))}
                    env["PLAYWRIGHT_BROWSERS_PATH"] = status["browser_cache"]
                    command = [status["python"], "-X", "utf8", status["worker"], "--output", str(path)]
                    subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=55, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    output = json.loads(path.read_text(encoding="utf-8")) if path.is_file() and path.stat().st_size < 1_000_000 else {"status": "source_unavailable", "records": [], "message": "内置榜单采集器未返回有效结果，继续使用公开赛事来源"}
                    if output.get("records"):
                        cache.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
            except subprocess.TimeoutExpired:
                output = {"status": "source_unavailable", "records": [], "message": "榜单采集超过 55 秒；继续使用已有线索和赛事公开检索"}
            except (OSError, ValueError):
                output = {"status": "source_unavailable", "records": [], "message": "榜单采集器或缓存不可读，本轮继续使用公开来源检索"}
            records = output.get("records", [])
            # Keep unrelated and betting topics away from the editorial model.
            subjects = [word.casefold() for word in profile["scope"]["keywords"]]
            accepted = []
            for record in records:
                corpus = str(record.get("title", "")) + " " + str(record.get("desc", "")) + " " + str(record.get("board_category", ""))
                sports_board = profile["scope"]["id"] == "all" and record.get("board_category") == "微博体育热搜"
                if self.engine.agent.signal_allowed(account_id, {"title": corpus}) and (
                        sports_board or any(word in corpus.casefold() for word in subjects)):
                    accepted.append(record)
            imported = 0
            try:
                for platform in ("weibo", "douyin"):
                    batch = [record for record in accepted[:30] if record["platform"] == platform]
                    if batch:
                        imported += self.engine.agent.import_signals(account_id, batch, platform)["imported"]
            except ValueError:
                if (self.engine.agent.bridge_config(account_id)["enabled"] and
                        not self.engine.is_paused() and not self.engine.account(account_id)["paused"]):
                    raise
                return {"status": "disabled", "imported": imported}
            boards = output.get("boards", [])
            counts = "、".join(f"{board['board']} {board['count']} 条" if board.get("status", "completed") == "completed"
                              else f"{board['board']} 暂不可读" for board in boards)
            if output["status"] == "completed":
                output["message"] = f"{counts}；筛出 {len(accepted)} 条{profile['scope']['name']}线索，新增 {imported} 条。" + ("复用五分钟内榜单快照。" if output.get("cached") else "") + "热榜只是选题入口，接下来核实日期与赛果"
            elif counts:
                output["message"] = counts + "。本轮继续使用公开赛事来源找题。"
            with self.engine.store.db() as db:
                key = "mediacrawler:" + account_id
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                saved = json.loads(current[0]) if current else config
                saved.update({"last_collect": iso(), "last_collect_status": output["status"], "last_collect_message": output.get("message", ""), "last_imported": imported})
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(saved, ensure_ascii=False)))
            on_event("crawler", output.get("message", "采集完成"), {"status": output["status"], "imported": imported, "boards": output.get("boards", []), "signals": accepted[:30]})
            return {"status": output["status"], "imported": imported, "message": output.get("message", ""), "error_type": output.get("error_type")}
        finally:
            self.lock.release()
