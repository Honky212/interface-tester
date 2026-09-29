"""
UniFSP（统一文件服务平台）接口测试 - debugtalk.py 辅助函数。

用法：
- 放在用例目录下，interfacetester 会自动加载这里的函数；
- YAML 里用 ${函数名(参数)} 调用，例如 ${api_base()}、${test_dir()}。
"""
import base64
import hashlib
import hmac
import os
import random
import string
import time
import uuid


def api_base():
    """API 根地址（去掉 /index 后缀）。

    .env 里 UNIFSP_BASE_URL 可能形如 http://host/index，
    业务接口实际挂在根路径下（/api/...），故这里去掉 /index。
    """
    base = os.environ.get("UNIFSP_BASE_URL", "http://unifsp-test.example.com")
    base = base.rstrip("/")
    if base.endswith("/index"):
        base = base[: -len("/index")]
    return base


def token_url():
    """OAuth2 token 端点（位于根路径）。"""
    return f"{api_base()}/oauth2/token"


def app_path():
    """应用根路径，格式 /{client_id}/（client_id 同时用作应用目录）。"""
    client_id = os.environ.get("UNIFSP_CLIENT_ID", "")
    return f"/{client_id}/"


def test_dir():
    """带时间戳的测试目录路径，格式 /{client_id}/test_{ts}/，末尾带斜杠。"""
    return f"{app_path()}test_{int(time.time())}/"


# ============================================================
# 通用签名 / 加密 / 动态参数工具函数（按需沉淀，可跨项目复用）
#
# 说明：当前 UniFSP 接口只用 OAuth2 Bearer + X-Path，暂不需要签名。
#       以下函数作为「企业通用工具库」沉淀，未来接入需要签名的企业接口
#       时，可直接在 YAML 里用 ${函数名(参数)} 调用，无需改框架核心。
# ============================================================


def get_timestamp():
    """秒级时间戳（10 位）。"""
    return int(time.time())


def gen_random_string(length=16, chars=None):
    """生成指定长度的随机字符串（默认字母+数字）。"""
    if chars is None:
        chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def gen_uuid():
    """生成 UUID4（去掉横杠的 32 位十六进制串）。"""
    return uuid.uuid4().hex


def md5(text):
    """MD5 十六进制摘要（小写）。"""
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.md5(text).hexdigest()


def sha256(text):
    """SHA-256 十六进制摘要（小写）。"""
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def hmac_sha256(data, secret):
    """HMAC-SHA256 签名，返回十六进制小写。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, data, hashlib.sha256).hexdigest()


def md5_sign(params, secret):
    """通用 MD5 签名：参数字典按 key 升序拼接 k=v、用 & 连接 + 秘钥 → MD5。

    适用于「参数签名 + 秘钥」这类常见企业接口鉴权方式。
    """
    if isinstance(params, dict):
        raw = "&".join(f"{k}={params[k]}" for k in sorted(params))
    else:
        raw = str(params)
    return md5(f"{raw}{secret}")


def base64_encode(text):
    """Base64 编码（UTF-8 输入 → 字符串输出）。"""
    if isinstance(text, str):
        text = text.encode("utf-8")
    return base64.b64encode(text).decode("ascii")


def base64_decode(text):
    """Base64 解码（字符串输入 → UTF-8 字符串输出）。"""
    if isinstance(text, str):
        text = text.encode("ascii")
    return base64.b64decode(text).decode("utf-8")
