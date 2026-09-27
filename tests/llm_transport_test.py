# -*- coding: utf-8 -*-
"""LLM 接入层的护栏用例 —— §六（**P0a** C 阶段）。

## 本文件补的是自检补不了的判据

`llm.py` 的 `run_selftest()` 跑的是**纯逻辑**（替身 + 内存端口）。有三类判据必须在
真环境里钉：

1. **L3 闸门真的接上了吗**——不是"函数存在"，而是"没开沙盒时 `HttpTransport`
   在**发请求之前**就拒了，而且请求器一次都没被调用"；
2. **重试策略的信号方向对不对**——5xx/网络异常重试、**4xx 不重试**
   （4xx 是配置/请求问题，重试只会重复付费）；这两条要用"请求器被调了几次"断言；
3. **不写盘**（红线③）——本模块登记为 `("forbid", ())`，它的自检也不能写盘，
   单测更不该为了造夹具而往包目录里落文件。
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interfacetester_ai.llm as llm  # noqa: E402
from interfacetester_ai.cache import CACHE_KEY_FIELDS  # noqa: E402
from interfacetester_ai.l3 import L3Refused, SANDBOX_ENV  # noqa: E402
from interfacetester_ai.llm import (  # noqa: E402
    AI_API_KEY_ENV,
    AI_BASE_URL_ENV,
    AI_ENABLE_ENV,
    AI_MODEL_ENV,
    AI_TIMEOUT_ENV,
    FakeTransport,
    HttpTransport,
    LLMConfig,
    LLMConfigMissing,
    LLMRequest,
    LLMTransportError,
    complete,
    run_selftest,
)

LOCAL = "http://127.0.0.1:11434/v1"
CLOUD = "https://api.example.com/v1"


def _request(**overrides):
    base = {"system": "sys", "user": "usr", "model": "qwen2.5:14b", "base_url": LOCAL}
    base.update(overrides)
    return LLMRequest(**base)


class _FakeResponse:
    """最小响应替身（只实现 `HttpTransport` 用到的三样：status_code / json / text）。"""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _openai_payload(content='{"ok": true}'):
    return {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 7}}


class TestConfigStrictness(unittest.TestCase):
    """配置：缺必填要报，且报错里要**告诉人怎么配**。"""

    def test_missing_base_url_and_model_are_both_reported(self):
        with self.assertRaises(LLMConfigMissing) as ctx:
            LLMConfig.from_env({})

        message = str(ctx.exception)
        self.assertIn(AI_BASE_URL_ENV, message)
        self.assertIn(AI_MODEL_ENV, message)
        self.assertIn("配置指引", message, "报错必须带配置指引（否则等于只说了'不行'）")

    def test_local_needs_no_key_and_cloud_does(self):
        """★成对判据：回环免 key（Ollama 类）／云端必须 key——两个方向都要钉。"""
        local = LLMConfig.from_env({AI_BASE_URL_ENV: LOCAL, AI_MODEL_ENV: "qwen2.5:14b"})
        self.assertEqual(local.api_key, "")

        with self.assertRaises(LLMConfigMissing) as ctx:
            LLMConfig.from_env({AI_BASE_URL_ENV: CLOUD, AI_MODEL_ENV: "gpt-4o-mini"})
        self.assertIn(AI_API_KEY_ENV, str(ctx.exception))

    def test_explicit_api_key_is_kept(self):
        cfg = LLMConfig.from_env(
            {AI_BASE_URL_ENV: CLOUD, AI_MODEL_ENV: "m", AI_API_KEY_ENV: "sk-test-1234567890"}
        )
        self.assertEqual(cfg.api_key, "sk-test-1234567890")

    def test_defaults_are_applied_when_unset(self):
        cfg = LLMConfig.from_env({AI_BASE_URL_ENV: LOCAL, AI_MODEL_ENV: "m"})

        self.assertEqual(cfg.timeout, llm.DEFAULT_TIMEOUT)
        self.assertEqual(cfg.retries, llm.DEFAULT_RETRIES)
        self.assertEqual(cfg.max_output_cases, llm.DEFAULT_MAX_OUTPUT_CASES)
        self.assertTrue(cfg.enabled, "默认应当启用")

    def test_hint_gives_environment_variables_not_dot_env(self):
        """★实测纠正后的判据：指引必须给**环境变量**写法，且必须**明说**不读 `.env`。

        起因（2026-09-25 实测）：指引原先照抄方案 §6 的
        "写进项目 `.env` 即可被 loader 带起"——**不成立**。`LLMConfig.from_env()`
        跑在内核 `load_test_file` 之前，项目 `.env` **永远**进不到 AI 配置里。
        用户照指引做会反复失败，之后可能把 key 塞进全局环境变量或命令行，
        **凭据暴露面反而变大**。
        """
        with self.assertRaises(LLMConfigMissing) as ctx:
            LLMConfig.from_env({})

        message = str(ctx.exception)

        # ① 三种 shell 的写法都要给（Windows 是主战场，Linux/macOS 也要能照做）
        self.assertIn("$env:", message, "要给 PowerShell 写法")
        self.assertIn("export ", message, "要给 Linux/macOS 写法")
        # ② 必须点明 SANDBOX——否则用户配好 BASE_URL/MODEL 仍被 L3 拒，且不知道为什么
        self.assertIn(SANDBOX_ENV, message)
        # ③ ★反向判据：不许再声称 `.env` 会被带起
        self.assertNotIn("写进项目 .env 即可被 loader 带起", message)
        # ④ 而且要**主动**把"不读 .env"说出口，不是让人自己踩
        self.assertIn("不读", message)

    def test_bad_integer_is_reported_not_silently_defaulted(self):
        with self.assertRaises(LLMConfigMissing) as ctx:
            LLMConfig.from_env(
                {AI_BASE_URL_ENV: LOCAL, AI_MODEL_ENV: "m", AI_TIMEOUT_ENV: "六十秒"}
            )
        self.assertIn(AI_TIMEOUT_ENV, str(ctx.exception))

    def test_disable_flag_is_honoured(self):
        cfg = LLMConfig.from_env({AI_BASE_URL_ENV: LOCAL, AI_MODEL_ENV: "m", AI_ENABLE_ENV: "off"})
        self.assertFalse(cfg.enabled)


class TestHttpTransportRetryPolicy(unittest.TestCase):
    """重试策略：**5xx/网络异常重试，4xx 不重试**（用"请求器被调几次"断言）。"""

    def _transport(self, responses, calls):
        def requester(url, json=None, headers=None, timeout=None):  # noqa: A002, ARG001
            calls.append(url)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        return HttpTransport(requester=requester)

    def test_success_on_first_attempt(self):
        calls = []
        transport = self._transport([_FakeResponse(200, _openai_payload())], calls)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=False):
            response = transport.complete(_request(), LLMConfig(base_url=LOCAL, model="m"))

        self.assertEqual(response.text, '{"ok": true}')
        self.assertEqual(response.attempts, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(response.usage, {"total_tokens": 7})

    def test_5xx_is_retried(self):
        calls = []
        transport = self._transport(
            [_FakeResponse(503, text="boom"), _FakeResponse(200, _openai_payload())], calls
        )
        cfg = LLMConfig(base_url=LOCAL, model="m", retries=2)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=False):
            response = transport.complete(_request(), cfg)

        self.assertEqual(response.attempts, 2)
        self.assertEqual(len(calls), 2, "5xx 应当重试")
        self.assertTrue(any("503" in line for line in response.trace))

    def test_4xx_is_not_retried(self):
        """**核心**：4xx 不重试——重试只会重复付费，还会掩盖"配置错了"这个真原因。"""
        calls = []
        transport = self._transport([_FakeResponse(401, text="unauthorized")], calls)
        cfg = LLMConfig(base_url=LOCAL, model="m", retries=3)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=False):
            with self.assertRaises(LLMTransportError) as ctx:
                transport.complete(_request(), cfg)

        self.assertEqual(len(calls), 1, "4xx 不该重试")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("401", str(ctx.exception))

    def test_network_exception_is_retried_then_gives_up_with_trace(self):
        calls = []

        def requester(url, **kwargs):  # noqa: ARG001
            calls.append(url)
            raise ConnectionError("connection refused")

        transport = HttpTransport(requester=requester)
        cfg = LLMConfig(base_url=LOCAL, model="m", retries=2)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=False):
            with self.assertRaises(LLMTransportError) as ctx:
                transport.complete(_request(), cfg)

        self.assertEqual(len(calls), 3, "1 次首发 + 2 次重试")
        self.assertIn("ConnectionError", str(ctx.exception))

    def test_non_openai_shape_is_reported_with_body_preview(self):
        calls = []
        transport = self._transport([_FakeResponse(200, {"unexpected": True})], calls)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=False):
            with self.assertRaises(LLMTransportError) as ctx:
                transport.complete(_request(), LLMConfig(base_url=LOCAL, model="m"))

        self.assertIn("choices", str(ctx.exception))


class TestTimeoutHint(unittest.TestCase):
    """★超时文案（2026-09-25）：**报错处就是该改配置的地方**。

    起因（实测）：`DEFAULT_TIMEOUT` 曾为 **60s**（★2026-09-25 已调到 **180s**）——60s 对**本地大模型**普遍不够，
    而超时的症状只是 trace 里一行
    `网络异常 ReadTimeout`，最终报"重试 N 次仍失败"——**看起来像模型不行**。
    所以判据有两条、且成对：
    1. 超时类失败 → 必须点名 `INTERFACETESTER_AI_TIMEOUT` **和当前上限值**；
    2. **非超时**失败 → 一个字的超时提示都不许带（提示到处刷，真提示就会被无视）。
    """

    def _transport(self, exc_type):
        def requester(url, **kwargs):  # noqa: ARG001
            raise exc_type("boom")

        return HttpTransport(requester=requester)

    class _ReadTimeout(Exception):
        """只靠**类名**匹配（`HttpTransport` 不 import requests，判据也不该依赖它）。"""

    def test_timeout_failure_names_the_env_var_and_current_cap(self):
        cfg = LLMConfig(base_url=LOCAL, model="m", timeout=123, retries=1)
        calls = []

        def requester(url, **kwargs):  # noqa: ARG001
            calls.append(url)
            raise self._ReadTimeout("read timed out")

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=False):
            with self.assertRaises(LLMTransportError) as ctx:
                HttpTransport(requester=requester).complete(_request(), cfg)

        text = str(ctx.exception)
        self.assertIn(AI_TIMEOUT_ENV, text, "超时报错必须点名要调哪个变量")
        self.assertIn("123", text, "必须写出**当前**上限值（否则人不知道该调到多少）")
        self.assertIn("本地大模型", text, "要说明是给本地模型的提示，否则云端用户会误调")
        self.assertEqual(len(calls), 2, "1 次首发 + 1 次重试（超时属可重试）")
        self.assertTrue(ctx.exception.retryable)

    def test_non_timeout_failure_carries_no_timeout_hint(self):
        """**非超时**失败不许带超时提示 —— 否则真提示会被噪声淹掉（本仓对噪声的一贯态度）。"""
        cfg = LLMConfig(base_url=LOCAL, model="m", timeout=60, retries=0)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=False):
            with self.assertRaises(LLMTransportError) as ctx:
                self._transport(ConnectionError).complete(_request(), cfg)

        text = str(ctx.exception)
        self.assertIn("ConnectionError", text)
        self.assertNotIn(AI_TIMEOUT_ENV, text, "连接失败不该附超时提示")
        self.assertNotIn("疑似超时", text)

    def test_timeout_hint_is_exported_and_uses_config_value(self):
        """提示是**函数**、值取自 `config`（不是抄一份写死的文案）。"""
        from interfacetester_ai.llm import __all__ as exported  # noqa: PLC0415
        from interfacetester_ai.llm import timeout_hint  # noqa: PLC0415

        self.assertIn("timeout_hint", exported)
        self.assertIn("900", timeout_hint(LLMConfig(base_url=LOCAL, model="m", timeout=900)))
        self.assertNotIn("900", timeout_hint(LLMConfig(base_url=LOCAL, model="m", timeout=60)))


class TestL3GateIsWiredIntoHttpTransport(unittest.TestCase):
    """★关键：「闸门真的接在**真出网**那条路上」的机器形态。"""

    def _transport(self, calls):
        def requester(url, **kwargs):  # noqa: ARG001
            calls.append(url)
            return _FakeResponse(200, _openai_payload())

        return HttpTransport(requester=requester)

    def test_without_sandbox_it_refuses_before_sending(self):
        """没开沙盒 → 拒；而且请求器**一次都没被调用**（发出去就已经晚了）。"""
        calls = []
        transport = self._transport(calls)

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(L3Refused) as ctx:
                transport.complete(_request(), LLMConfig(base_url=LOCAL, model="m"))

        self.assertIn(SANDBOX_ENV, str(ctx.exception), "拒绝理由要告诉人开哪个开关")
        self.assertEqual(calls, [], "L3 必须挡在发请求**之前**")

    def test_loopback_is_allowed_once_sandbox_is_on(self):
        """照护另一半：开了沙盒的本地回环不该被误伤（Ollama 场景）。"""
        calls = []
        transport = self._transport(calls)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=True):
            response = transport.complete(_request(), LLMConfig(base_url=LOCAL, model="m"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(response.attempts, 1)

    def test_cloud_host_still_refused_even_with_sandbox_on(self):
        """非回环要**显式列名**——沙盒开关不是"万能放行"。"""
        calls = []
        transport = self._transport(calls)

        with mock.patch.dict(os.environ, {SANDBOX_ENV: "on"}, clear=True):
            with self.assertRaises(L3Refused):
                transport.complete(
                    _request(base_url=CLOUD), LLMConfig(base_url=CLOUD, model="m")
                )

        self.assertEqual(calls, [])


class TestCacheKeyAlignment(unittest.TestCase):
    """缓存键字段：`LLMRequest` ↔ `CACHE_KEY_FIELDS` 必须**恰好相等**。"""

    def test_request_fields_match_the_manifest_list_exactly(self):
        fields = set(_request().cache_fields())

        self.assertEqual(fields, set(CACHE_KEY_FIELDS))
        self.assertEqual(len(fields), len(CACHE_KEY_FIELDS), "不许有重复键")

    def test_changing_any_single_field_changes_the_key(self):
        from interfacetester_ai.cache import build_cache_key  # noqa: PLC0415

        base = _request()
        baseline = build_cache_key(base.cache_fields())
        variants = {
            "base_url": "http://127.0.0.1:9999/v1",
            "model": "another-model",
            "prompt_version": "v-next",
            "system": "另一份 system",
            "user": "另一份 user",
            "temperature": 0.5,
            "seed": 42,
            "max_tokens": 128,
            "response_format": "json_object",
        }

        for name, value in variants.items():
            with self.subTest(field=name):
                changed = _request(**{name: value})
                self.assertNotEqual(
                    build_cache_key(changed.cache_fields()),
                    baseline,
                    f"只改 {name} 竟然还是同一个缓存键",
                )


class TestCachingBehaviour(unittest.TestCase):
    """缓存：命中即回放（不再调模型）；`no_cache` 强制实调。"""

    class _MemCache:
        def __init__(self):
            self.data = {}

        def get(self, key):
            return self.data.get(key)

        def put(self, key, text):
            self.data[key] = text

    class _MemAudit:
        def __init__(self):
            self.entries = []

        def record(self, entry):
            self.entries.append(entry)

    def test_cache_hit_does_not_call_the_model_again(self):
        transport = FakeTransport({"*": '{"ok": true}'})
        cache = self._MemCache()
        request = _request()

        first = complete(request, transport=transport, cache=cache)
        second = complete(request, transport=transport, cache=cache)

        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(transport.call_count, 1, "命中缓存时不该再调模型")

    def test_audit_records_both_the_real_call_and_the_replay(self):
        """命中回放那次也要留痕——否则复盘会以为"模型又跑了一遍"。"""
        transport = FakeTransport({"*": "{}"})
        cache, audit = self._MemCache(), self._MemAudit()
        request = _request()

        complete(request, transport=transport, cache=cache, audit=audit)
        complete(request, transport=transport, cache=cache, audit=audit)

        self.assertEqual(len(audit.entries), 2)
        self.assertFalse(audit.entries[0]["cached"])
        self.assertTrue(audit.entries[1]["cached"])
        self.assertIn("cache_key", audit.entries[0])

    def test_no_cache_forces_a_real_call(self):
        transport = FakeTransport({"*": "{}"})
        cache = self._MemCache()
        request = _request()

        complete(request, transport=transport, cache=cache)
        complete(request, transport=transport, cache=cache, no_cache=True)

        self.assertEqual(transport.call_count, 2)


class TestContentGate(unittest.TestCase):
    """内容闸：命中即替换为 `${ENV(...)}`，且**清单本身不再泄露凭据**。"""

    def test_detects_and_replaces_known_secret_shapes(self):
        text = 'password: "hunter2" 和 sk-abcdefghijklmnopqrstuvwx0 和 -----BEGIN RSA PRIVATE KEY-----'

        findings = llm.scan_sensitive(text)
        kinds = {item["kind"] for item in findings}

        self.assertIn("CREDENTIAL", kinds)
        self.assertIn("OPENAI_KEY", kinds)
        self.assertIn("PEM", kinds)

        redacted, listed = llm.redact_sensitive(text)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx0", redacted)
        self.assertIn("${ENV(", redacted)
        self.assertEqual(len(listed), len(findings))

    def test_redaction_list_does_not_leak_the_secret_itself(self):
        """清单会被打印/落盘——把凭据原文抄进去等于**换个地方再泄一次**。"""
        secret = "sk-abcdefghijklmnopqrstuvwx0"
        _, listed = llm.redact_sensitive(f"key={secret}")

        rendered = str(listed)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("abcdefghijklmnop", rendered)

    def test_clean_text_is_left_alone(self):
        """反向护栏：普通接口文档不能被误伤（假报会让真报被无视）。"""
        clean = '{"code": 0, "message": "ok", "userId": "1001"}'

        redacted, listed = llm.redact_sensitive(clean)

        self.assertEqual(redacted, clean)
        self.assertEqual(listed, [])

    def test_key_name_scan_reuses_the_kernel_vocabulary(self):
        """键名级复用内核 `is_sensitive_key`（含 `X-Api-Key` 的分隔符归一化）。"""
        payload = {"X-Api-Key": "v", "Authorization": "v", "username": "u", "nested": {"token": "v"}}

        found = llm.scan_sensitive_keys(payload)

        self.assertIn("X-Api-Key", found)
        self.assertIn("nested.token", found)
        self.assertNotIn("username", found)


class TestModuleWriteBoundary(unittest.TestCase):
    """红线③：`llm.py` 一律不写盘（T18 注册为 `("forbid", ())`）。"""

    def test_source_contains_no_write_calls(self):
        import inspect  # noqa: PLC0415

        source = inspect.getsource(llm)
        forbidden = ("open(", "write_text", "write_bytes", "makedirs", "mkdir(", "os.remove", "shutil.")

        hits = [needle for needle in forbidden if needle in source]
        self.assertEqual(hits, [], f"llm.py 里出现了写盘调用：{hits}")


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：这套断言真的在检查东西。"""

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_a_cache_field_drifts(self):
        """注入：给 `cache_fields()` 塞一个清单外的字段 → 必须**响亮变红**。

        实测形态有两种（都算红，都是对的）：
        ① `build_cache_key` 更早地抛 `CacheKeyFieldsMismatch`（自检的 ⑨~⑪ 直接调它，
           所以异常通常先到）——这是比"返回非 0"**更早更响**的拦截；
        ② 自检第 ① 组判据自己报出来（`run_selftest()` 非 0）。

        所以这里判"**要么抛错、要么非 0**"，而不是把某一种形态写死——
        否则实现顺序稍变，这条元护栏就会**假红**（而假红会让真红被无视）。
        """
        from interfacetester_ai.cache import CacheKeyFieldsMismatch  # noqa: PLC0415

        original = llm.LLMRequest.cache_fields

        def drifted(self):
            return {**original(self), "top_p": 0.9}

        with mock.patch.object(llm.LLMRequest, "cache_fields", drifted):
            try:
                result = run_selftest()
            except CacheKeyFieldsMismatch:
                return
            self.assertNotEqual(result, 0, "缓存键字段漂移后自检竟然还是绿的")


class TestCliExitCode(unittest.TestCase):
    """`python -m interfacetester_ai.llm` 在重定向 + 无 `PYTHONIOENCODING` 下退出码 0。"""

    def test_module_selftest_exits_zero(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)

        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.llm"],
            cwd=BASE,
            capture_output=True,
            timeout=180,
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("llm 接入层自检全部通过".encode("utf-8"), proc.stdout)


if __name__ == "__main__":
    unittest.main()
