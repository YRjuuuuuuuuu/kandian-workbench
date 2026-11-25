"""Project-scoped model selection and provider-independent editorial contracts."""
import copy
import hashlib
import json
import os
import re
import shutil
import time
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .codex_agent import AgentCancelled, CodexAgentProvider, DEFAULT_MODEL, DEFAULT_EFFORT, model_status
from .network import tls_context
from .store import dumps

DEFAULTS = {"research_model": DEFAULT_MODEL, "codex_model": DEFAULT_MODEL, "reasoning_effort": "medium", "research_reasoning_effort": DEFAULT_EFFORT, "production_provider": "codex", "deepseek_model": "deepseek-flash", "deepseek_max_tokens": 4096, "workbuddy_model": "由 WorkBuddy 内选择"}
MODEL_IDS = ["gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"]
STAGES = {"discovery": "找题与筛选", "research": "查证与补读", "writing": "正文与标题", "polish": "润色与扩写", "revision": "按意见改稿", "image_review": "配图规划与核对", "style_learning": "写作风格学习"}
OPTIONAL_STAGES = {"image_review", "style_learning"}

def deepseek_only(selection):
    """Preserve DeepSeek stage choices and replace all other configured routes."""
    result = normalize(selection)
    result["production_provider"] = "deepseek"
    for stage, route in result["stage_routes"].items():
        if route["provider"] != "deepseek":
            route.update(provider="deepseek", model=result["deepseek_model"])
        if stage == "image_review":
            route["model"] = "deepseek-flash"
    result["deepseek_model"] = result["stage_routes"]["writing"]["model"]
    return result


def legacy_routes(selection):
    """Preserve the actual mixed routing of old task snapshots."""
    production = {"provider": selection["production_provider"], "model": selection["deepseek_model"] if selection["production_provider"] == "deepseek" else selection["workbuddy_model"] if selection["production_provider"] == "workbuddy" else selection["codex_model"], "effort": selection["reasoning_effort"], "max_tokens": selection["deepseek_max_tokens"]}
    research = {**production, "provider": "codex", "model": selection["research_model"], "effort": selection["research_reasoning_effort"]}
    routes = {name: dict(research if name in ("discovery", "research") else production) for name in STAGES}
    if production["provider"] == "workbuddy":
        routes["image_review"] = dict(research)
        routes["style_learning"] = dict(research)
    return routes


def stage_name(stem):
    return {"discovery": "discovery", "research": "research", "enrichment": "research", "review": "polish", "expansion": "polish", "repair": "polish", "revision": "revision", "asset_plan": "image_review", "style_sources": "style_learning", "style_learning": "style_learning"}.get(stem, "writing")


def for_stage(selection, stage):
    route = selection["stage_routes"][stage]
    return {**selection, "production_provider": route["provider"], "codex_model": route["model"] if route["provider"] == "codex" else selection["codex_model"], "research_model": route["model"], "reasoning_effort": route["effort"], "research_reasoning_effort": route["effort"], "deepseek_model": route["model"] if route["provider"] == "deepseek" else selection["deepseek_model"], "deepseek_max_tokens": route["max_tokens"], "workbuddy_model": route["model"] if route["provider"] == "workbuddy" else selection["workbuddy_model"]}


def merge_selection(previous, updates):
    merged = {**previous, **updates}
    if updates and "stage_routes" not in updates:
        if previous.get("stage_routes") and previous["stage_routes"] != legacy_routes(previous):
            raise ValueError("模型配置已升级为分阶段选择，请刷新工作台后重新提交；没有改回 Codex")
        merged.pop("stage_routes", None)  # Legacy API callers retain legacy semantics.
    return normalize(merged)


def normalize(selection=None):
    if selection is not None and not isinstance(selection, dict):
        raise ValueError("模型设置需为对象")
    result = {**DEFAULTS, **{k: v for k, v in (selection or {}).items() if k in DEFAULTS}}
    if selection and "research_reasoning_effort" not in selection:
        result["research_reasoning_effort"] = selection.get("reasoning_effort", DEFAULT_EFFORT)
    for name in ("research_model", "codex_model", "deepseek_model"):
        if not isinstance(result[name], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{1,99}", result[name]):
            raise ValueError("模型 ID 格式无效")
    if result["production_provider"] not in ("codex", "deepseek", "workbuddy"):
        raise ValueError("请选择 Codex、DeepSeek 或 WorkBuddy")
    if any(result[key] not in ("low", "medium", "high", "xhigh", "max") for key in ("reasoning_effort", "research_reasoning_effort")):
        raise ValueError("推理强度无效")
    limit = result["deepseek_max_tokens"]
    if type(limit) is not int or not 1024 <= limit <= 8192:
        raise ValueError("DeepSeek 输出上限为 1024–8192 token")
    if not isinstance(result["workbuddy_model"], str) or not 1 <= len(result["workbuddy_model"]) <= 100:
        raise ValueError("WorkBuddy 模型说明为 1–100 字")
    routes = (selection or {}).get("stage_routes")
    if routes is not None and (not isinstance(routes, dict) or not set(STAGES) - OPTIONAL_STAGES <= set(routes) or set(routes) - set(STAGES)):
        raise ValueError("请完整配置找题、查证、正文、润色和改稿五个阶段")
    result["stage_routes"] = copy.deepcopy(routes if routes is not None else legacy_routes(result))
    for stage, fallback in (("image_review", "writing"), ("style_learning", "polish")):
        route = result["stage_routes"][fallback]
        if not isinstance(route, dict) or "provider" not in route:
            raise ValueError(STAGES[fallback] + "模型配置格式无效")
        result["stage_routes"].setdefault(stage, copy.deepcopy(route if route["provider"] != "workbuddy" else legacy_routes(result)[stage]))
    for stage, route in result["stage_routes"].items():
        if not isinstance(route, dict) or set(route) != {"provider", "model", "effort", "max_tokens"}:
            raise ValueError(STAGES[stage] + "模型配置格式无效")
        if route["provider"] not in ("codex", "deepseek", "workbuddy") or (stage in ("discovery", "research", "image_review", "style_learning") and route["provider"] == "workbuddy"):
            raise ValueError(STAGES[stage] + "请选择 Codex 或 DeepSeek 联网模型")
        if not isinstance(route["model"], str) or not 1 <= len(route["model"]) <= 100 or (route["provider"] != "workbuddy" and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{1,99}", route["model"])):
            raise ValueError(STAGES[stage] + "模型 ID 无效")
        if route["effort"] not in ("low", "medium", "high", "xhigh", "max") or type(route["max_tokens"]) is not int or not 1024 <= route["max_tokens"] <= 8192:
            raise ValueError(STAGES[stage] + "推理强度或输出上限无效")
    return result


def skills_for(stem):
    names = ["evidence-fact-review"] if stem in ("discovery", "research", "review", "enrichment") else ["evidence-content-writer", "natural-evidence-editor"]
    root = Path(__file__).resolve().parents[1] / "skills"
    return {name: (root / name / "SKILL.md").read_text(encoding="utf-8") for name in names}


class SchemaError(ValueError):
    """A malformed model result, distinct from network and identity failures."""


def validate_schema(value, schema, path="result"):
    if "const" in schema and value != schema["const"]:
        raise ValueError(path + " 身份固定值不匹配")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str), "boolean": type(value) is bool, "integer": type(value) is int, "number": type(value) in (int, float), "null": value is None}
    if types and not any(matches.get(t) for t in types):
        raise SchemaError(path + " 类型不符合任务 schema")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(path + " 值不在允许范围")
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise SchemaError(path + " 缺少字段：" + "、".join(sorted(missing)))
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - props.keys():
            raise SchemaError(path + " 存在 schema 外字段")
        for key, item in value.items():
            if key in props:
                validate_schema(item, props[key], path + "." + key)
    if isinstance(value, list):
        for i, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), path + f"[{i}]")
    return value


class SecretKey:
    """Only a readiness flag leaves this class; persistent keys use Windows DPAPI."""
    def __init__(self, folder):
        self.path = Path(folder) / "credentials" / "deepseek.dpapi"
        self.lock = threading.RLock()

    def _crypt(self, data, decrypt=False):
        if os.name != "nt":
            raise ValueError("此系统请通过 DEEPSEEK_API_KEY 环境变量配置")
        import ctypes
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        target = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError("DeepSeek 凭据解密失败：请在当前 Windows 用户下重新保存 API Key" if decrypt else "Windows 本地密钥加密失败，未保存 API Key")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def get(self):
        with self.lock:
            for attempt in range(3):
                try:
                    protected = self.path.read_bytes()
                    break
                except FileNotFoundError:
                    return os.environ.get("DEEPSEEK_API_KEY", "").strip()
                except PermissionError:
                    if attempt == 2:
                        raise ValueError("DeepSeek 凭据文件权限不足或被占用，请关闭占用程序并重试保存 Key；手动编辑与导出仍可使用") from None
                    time.sleep(.08 * (attempt + 1))
                except OSError:
                    raise ValueError("DeepSeek 凭据文件读取失败，请重试或重新保存 Key") from None
            try:
                return self._crypt(protected, decrypt=True).decode("utf-8")
            except UnicodeError:
                raise ValueError("DeepSeek 凭据解密内容无效，请重新保存 API Key") from None

    def set(self, key):
        if not isinstance(key, str) or not 10 <= len(key.strip()) <= 500 or any(c.isspace() for c in key.strip()):
            raise ValueError("请输入有效的 DeepSeek API Key")
        with self.lock:
            temporary = None
            try:
                protected = self._crypt(key.strip().encode("utf-8"))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".deepseek-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(protected)
                    stream.flush()
                    os.fsync(stream.fileno())
                for attempt in range(3):
                    try:
                        os.replace(temporary, self.path)
                        break
                    except PermissionError:
                        if attempt == 2:
                            raise
                        time.sleep(.08 * (attempt + 1))
            except PermissionError:
                raise ValueError("DeepSeek 凭据保存权限不足或文件被占用；旧 Key 未被删除，请稍后重试") from None
            except OSError:
                raise ValueError("DeepSeek 凭据文件无法写入；旧 Key 未被删除，请检查数据目录") from None
            finally:
                if temporary:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    def status(self):
        try:
            configured = bool(self.get())
            return {"configured": configured, "source": ("windows_dpapi" if self.path.is_file() else "environment") if configured else None}
        except (ValueError, OSError) as exc:
            return {"configured": False, "source": "unavailable", "error": str(exc), "error_code": "credential_permission" if isinstance(exc, PermissionError) or "权限" in str(exc) else "credential_decrypt" if "解密" in str(exc) else "credential_io"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def deepseek_request(key, path, payload=None, timeout=90):
    # The credential is only sent to the documented official endpoint.
    if path not in ("/models", "/chat/completions", "/tools/search", "/tools/fetch"):
        raise ValueError("不支持的 DeepSeek 接口")
    request = urllib.request.Request("https://api.deepseek.com/v1" + path, data=dumps(payload).encode("utf-8") if payload else None, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("DeepSeek 响应超过 1MB")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        reason = {401: "Key 无效", 402: "余额不足", 403: "访问权限不足", 429: "速率或额度受限"}.get(exc.code, "服务拒绝本次请求")
        raise ValueError(f"DeepSeek HTTP {exc.code}：{reason}；未改用其他模型") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("DeepSeek 网络或 TLS 连接失败；未改用其他模型") from None


class DeepSeekProvider:
    def __init__(self, secret, selection):
        self.secret, self.selection = secret, selection

    def run(self, folder, stem, prompt, schema, on_event, should_stop, search=False, timeout=420):
        if search:
            from .deepseek_research import research
            return research(self, folder, stem, prompt, schema, on_event, should_stop, timeout)
        key = self.secret.get()
        if not key:
            raise ValueError("尚未配置 DeepSeek API Key，请在 AI 模型设置中本地填写")
        if should_stop():
            raise AgentCancelled("任务已停止")
        model = self.selection["deepseek_model"]
        on_event("model", "正在调用 DeepSeek · " + model + "（独立 API 计费）", {"provider": "deepseek", "requested_model": model})
        body = {"model": model, "messages": [{"role": "system", "content": "仅输出符合给定 schema 的 JSON，不添加解释。"}, {"role": "user", "content": prompt + "\nJSON schema:\n" + dumps(schema)}], "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "max_tokens": self.selection["deepseek_max_tokens"], "stream": False}
        started = time.monotonic()
        output = deepseek_request(key, "/chat/completions", body, timeout=min(timeout, 120))
        if should_stop():
            raise AgentCancelled("任务已停止，返回结果未采用")
        choices = output.get("choices") or []
        if not choices or choices[0].get("finish_reason") != "stop":
            raise ValueError("DeepSeek 输出不完整，请缩短任务或提高输出上限")
        try:
            result = validate_schema(json.loads(choices[0]["message"]["content"]), schema)
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("DeepSeek 未返回有效 JSON；未导入稿件") from None
        raw = output.get("usage") or {}
        usage = {key: raw.get(value) for key, value in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("cached_input_tokens", "prompt_cache_hit_tokens")) if type(raw.get(value)) is int}
        telemetry = {"provider": "deepseek", "requested_model": model, "reported_model": output.get("model"), "usage": usage, "web_searches": 0, "elapsed_seconds": round(time.monotonic()-started, 1), "thinking": "disabled"}
        on_event("provider", "DeepSeek 本阶段完成", telemetry)
        return result, telemetry


class ExternalPending(Exception):
    def __init__(self, task):
        self.task = task


class WorkBuddyProvider:
    def __init__(self, selection):
        self.selection = selection

    def run(self, folder, stem, prompt, schema, on_event, should_stop, search=False, timeout=420):
        if search:
            raise ValueError("WorkBuddy 桌面交接用于生产；联网查证使用所选 Codex")
        folder = Path(folder) / (stem + ".workbuddy")
        folder.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256((prompt + dumps(schema)).encode()).hexdigest()
        existing = folder / "contract.json"
        contract = {"request_hash": fingerprint, "provider": "workbuddy", "declared_model": self.selection["workbuddy_model"], "stem": stem}
        if existing.exists() and json.loads(existing.read_text(encoding="utf-8")) != contract:
            raise ValueError("外部任务与当前上下文发生变化，请重新建立任务")
        existing.write_text(dumps(contract), encoding="utf-8")
        (folder / "instructions.md").write_text(prompt + "\n\n仅将最终 JSON 写入此目录 output.json；遵循 output.schema.json。不要操作平台、修改事实或访问凭据。模型在 WorkBuddy 内选择。", encoding="utf-8")
        (folder / "output.schema.json").write_text(dumps(schema), encoding="utf-8")
        for name, content in skills_for(stem).items():
            skill = folder / "skills" / name
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(content, encoding="utf-8")
        result_file = folder / "output.json"
        if result_file.is_file():
            if result_file.stat().st_size > 800_000:
                raise ValueError("WorkBuddy 结果超过 800KB")
            try:
                result = validate_schema(json.loads(result_file.read_text(encoding="utf-8-sig")), schema)
            except json.JSONDecodeError:
                raise ValueError("WorkBuddy output.json 不是有效 JSON") from None
            telemetry = {"provider": "workbuddy", "requested_model": self.selection["workbuddy_model"], "model_verified": False, "usage": None, "web_searches": 0, "elapsed_seconds": None, "transport": "desktop_file_handoff"}
            on_event("provider", "已读取 WorkBuddy 结果，继续本地校验；模型及用量未由接口验证", telemetry)
            return result, telemetry
        raise ExternalPending({"folder": str(folder), "stem": stem, "declared_model": self.selection["workbuddy_model"], "instruction": "在 WorkBuddy 打开此任务目录，读取 instructions.md、output.schema.json 和 skills，选择模型，按 schema 将结果写入 output.json。工作台会继续校验并生产配图。"})


class AIModels:
    def __init__(self, engine):
        self.engine = engine
        self.secret = SecretKey(engine.data_dir)

    def selection(self, account_id):
        row = self.engine.store.one("SELECT value FROM settings WHERE key=?", ("agent:" + account_id,))
        return self.configure(json.loads(row["value"]).get("ai") if row else None)

    def configure(self, selection):
        return deepseek_only(selection) if self.engine.deepseek_only else normalize(selection)

    def check_execution(self, selection):
        if self.engine.deepseek_only and any(route["provider"] != "deepseek" for route in selection["stage_routes"].values()):
            raise ValueError("此任务保存的是旧版模型配置，请使用 DeepSeek 重新创建任务")

    def adapter(self, selection, search=False, stage=None):
        selection = normalize(selection)
        self.check_execution(selection)
        selection = for_stage(selection, stage or ("research" if search else "writing"))
        if selection["production_provider"] == "codex":
            return CodexAgentProvider(selection["research_model"] if search else selection["codex_model"], selection["research_reasoning_effort"] if search else selection["reasoning_effort"])
        if selection["production_provider"] == "deepseek":
            return DeepSeekProvider(self.secret, selection)
        return WorkBuddyProvider(selection)

    def status(self, account_id):
        selection = self.selection(account_id)
        standalone = self.engine.deepseek_only
        return {"selection": selection, "stages": STAGES, "deepseek_only": standalone, "codex": {"cli_available": False} if standalone else model_status(selection["codex_model"], selection["reasoning_effort"]), "deepseek": self.secret.status(), "workbuddy": {"transport": "desktop_file_handoff", "cli_available": not standalone and bool(shutil.which("codebuddy")), "model_verified": False}, "models": [] if standalone else MODEL_IDS, "deepseek_models": ["deepseek-flash", "deepseek-v4-pro"], "research_provider": selection["stage_routes"]["research"]["provider"], "tools": {"deepseek_search": "DeepSeek Search / Fetch 官方 API（独立按次计费）", "images": "素材规划与视觉核对使用所选配图模型；下载、去重和图表绘制使用本地工具", "checks": "本地规则，不调用 AI"}}

    def check_deepseek(self, generate=False, account_id="main"):
        key = self.secret.get()
        if not key:
            raise ValueError("请先在本机填写 DeepSeek Key")
        result = deepseek_request(key, "/models", timeout=12)
        models = [item["id"] for item in result.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if not generate:
            return {"ok": True, "models": models, "kind": "models_list_only", "note": "已验证鉴权和模型列表；尚未进行收费文本生成"}
        selection = self.selection(account_id)
        route = selection["stage_routes"]["writing"]
        model = route["model"] if route["provider"] == "deepseek" else selection["deepseek_model"]
        output = deepseek_request(key, "/chat/completions", {"model": model, "messages": [{"role": "user", "content": "连接测试，仅回复：连接正常"}], "max_tokens": 32, "thinking": {"type": "disabled"}, "stream": False}, timeout=30)
        choices = output.get("choices") or []
        if not choices or not choices[0].get("message", {}).get("content"):
            raise ValueError("DeepSeek 鉴权通过，但生成未返回文本")
        return {"ok": True, "models": models, "kind": "generation", "requested_model": model, "reported_model": output.get("model"), "usage": output.get("usage"), "text": choices[0]["message"]["content"]}
