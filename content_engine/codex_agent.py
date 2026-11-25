"""Bounded Codex agent runner with real web-search activity and structured output.

No raw reasoning, shell output, credentials or user configuration is exposed to UI.
"""
import collections
import json
import queue
import shutil
import subprocess
import threading
import time
import tomllib
import urllib.parse
from pathlib import Path
import os


DEFAULT_MODEL = "gpt-6.1-terra"
DEFAULT_EFFORT = "high"


def model_status(model=DEFAULT_MODEL, effort=DEFAULT_EFFORT):
    """Read only non-secret local model preferences and the explicit probe report."""
    status = {"model": model, "provider": "codex", "reasoning_effort": effort, "mechanism": "local_python_codex_cli", "execution": "云端模型；本地队列与素材", "cli_available": bool(shutil.which("codex")), "scope": "项目设置，不改全局 Codex 配置"}
    folder = Path(__file__).resolve().parents[1] / "data" / "model-probe"
    probe = folder / (model + "-" + effort + ".json")
    if not probe.is_file():
        probe = folder / "verified.json"
    try:
        observed = json.loads(probe.read_text(encoding="utf-8"))
        headers = observed.get("runtime_headers", {})
        if observed.get("exit_code") == 0 and headers.get("model") == status["model"] and headers.get("reasoning effort") == status["reasoning_effort"]:
            status["verified_model"] = headers["model"]
    except (OSError, ValueError):
        pass
    return status


class AgentCancelled(Exception):
    pass


def restricted_config(effort=DEFAULT_EFFORT):
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("不支持的推理强度")
    arguments = ["-c", "features.shell_tool=false", "-c", "features.apps=false", "-c", "features.multi_agent=false", "-c", 'model_reasoning_effort="' + effort + '"']
    # Use the editorial instruction set instead of the coding-agent preamble.
    # Task skills and constraints are passed explicitly, equally to all providers.
    instruction_path = Path(__file__).resolve().parents[1] / "prompts" / "agent-system.md"
    arguments += ["-c", "model_instructions_file=" + json.dumps(str(instruction_path)), "-c", "project_doc_max_bytes=0", "-c", "memories.use_memories=false"]
    # Disable named MCP servers for this run without reading or printing secrets.
    try:
        import os
        root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
        for name in config.get("mcp_servers", {}):
            if all(character.isalnum() or character in "_-" for character in name):
                arguments += ["-c", f"mcp_servers.{name}.enabled=false"]
    except (OSError, ValueError):
        pass
    return arguments


class CodexAgentProvider:
    def __init__(self, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT):
        self.model = model
        self.effort = effort

    def run(self, folder, stem, prompt, schema, on_event, should_stop, search=False, timeout=420, image_paths=None):
        executable = shutil.which("codex")
        if not executable:
            raise ValueError("未找到 Codex CLI；请安装并登录后重试。不会切换到付费 API")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        schema_path, result_path = folder / f"{stem}.schema.json", folder / f"{stem}.result.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        (folder / f"{stem}.instructions.md").write_text(prompt, encoding="utf-8")
        if result_path.exists():
            result_path.unlink()  # A stale result is never accepted after a failed run.
        command = [executable, "--no-daemon"]
        status = model_status(self.model, self.effort)
        model = status["model"]
        if model and not model.startswith("CLI 默认"):
            command += ["--model", model]
        on_event("model", "正在调用 " + model + " · " + self.effort, status)
        if search:
            command += ["--search"]
        command += restricted_config(self.effort) + ["-a", "never", "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", str(folder), "--json", "--color", "never", "--output-schema", str(schema_path), "-o", str(result_path), "-"]
        if image_paths:
            for path in image_paths:
                candidate = Path(path).resolve()
                if candidate.parent != folder.resolve() or not candidate.is_file():
                    raise ValueError("视觉核对只允许本任务目录中的缩略图")
                command[-1:-1] = ["--image", str(candidate)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=folder, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines, errors = queue.Queue(), collections.deque(maxlen=20)
        def read_lines():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)
        def read_errors():
            for line in process.stderr:
                errors.append(line[:500])
        threading.Thread(target=read_lines, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()
        started, finished, usage, searches = time.monotonic(), False, {}, 0
        heartbeat = started
        observed_urls, queries = set(), []
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            while not finished or process.poll() is None:
                if should_stop():
                    raise AgentCancelled("任务已停止；未执行平台发布")
                if time.monotonic() - started > timeout:
                    raise ValueError("Agent 本阶段超过等待上限，已停止。可以缩小范围后重试")
                if time.monotonic() - heartbeat > 5:
                    on_event("heartbeat", "", {})
                    heartbeat = time.monotonic()
                try:
                    line = lines.get(timeout=.4)
                except queue.Empty:
                    continue
                if line is None:
                    finished = True
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                item = event.get("item", {})
                if event.get("type") == "item.completed" and item.get("type") in ("web_search", "web_search_call"):
                    searches += 1
                    action = item.get("action") or {}
                    values = action.get("queries", []) or ([action.get("query")] if action.get("query") else [])
                    values = [str(value)[:250] for value in values if value]
                    queries.extend(values)
                    def collect(value, key=""):
                        if isinstance(value, dict):
                            for k, v in value.items():
                                collect(v, k)
                        elif isinstance(value, list):
                            for v in value:
                                collect(v, key)
                        elif isinstance(value, str) and key in ("url", "urls", "ref_id", "source_url") and value.startswith("https://"):
                            parsed = urllib.parse.urlsplit(value)
                            if not parsed.username and not parsed.password:
                                observed_urls.add(value[:2000])
                    collect(item)
                    on_event("search", "联网检索：" + ("；".join(values) if values else str(item.get("query") or action.get("type") or "查找与核对来源"))[:350], {"queries": values, "searches": searches})
                elif event.get("type") == "turn.completed":
                    raw_usage = event.get("usage") or {}
                    usage = {key: value for key, value in raw_usage.items() if key in ("input_tokens", "cached_input_tokens", "output_tokens") and isinstance(value, int)}
                elif event.get("type") in ("error", "turn.failed"):
                    errors.append(str(event.get("message") or event.get("error") or "run failed")[:500])
            process.wait(timeout=3)
            if process.returncode != 0:
                error = " ".join(errors).lower()
                if "quota" in error or "usage limit" in error or "rate limit" in error:
                    raise ValueError("Codex 额度或速率受限。请等待恢复或降低找题频率；未切换到付费 API")
                if any(term in error for term in ("access is denied", "permission denied", "os error 5", "拒绝访问", "sandbox")):
                    raise ValueError("Codex 本地进程／凭据目录访问受限；请在正常 Windows 终端启动工作台。任务尚未取得模型结果，没有冒用缓存或其他模型")
                if any(term in error for term in ("401", "unauthorized", "not logged", "login required", "authentication")):
                    raise ValueError("Codex 登录状态无效；请在正常终端运行 codex login 后重试，不要把登录凭据粘贴到工作台")
                if any(term in error for term in ("connection", "tls", "ssl", "network", "dns", "timed out", "certificate")):
                    raise ValueError("Codex 连接服务失败；请检查当前网络／代理与 CLI 连通性，再重试本任务。未消耗已完成内容，也未切换模型")
                raise ValueError("Codex 未能完成本阶段，请检查本机登录、网络和 CLI 状态后重试")
            if not result_path.is_file() or result_path.stat().st_size > 800_000:
                raise ValueError("Agent 没有返回有效结构化结果")
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
            if search and not searches:
                raise ValueError("本次没有检测到真实联网检索，不能把模型记忆当作今日选题。请检查 CLI 的联网搜索能力后重试")
            telemetry = {"web_searches": searches, "observed_urls": sorted(observed_urls), "queries": list(dict.fromkeys(queries)), "usage": usage, "elapsed_seconds": round(time.monotonic() - started, 1), "provider": "codex", "requested_model": model, "reasoning_effort": self.effort}
            on_event("provider", "本阶段完成" + (f"，执行 {searches} 次联网检索" if search else ""), telemetry)
            return result, telemetry
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdout, process.stderr):
                stream.close()
