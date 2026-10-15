"""钉钉自定义机器人发送（支持加签 / 关键词 / @手机号）。"""

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request


class DingTalkError(Exception):
    pass


def _chunk(text, limit=20000):
    """钉钉单条消息正文有长度上限，超长时截断。"""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n…（内容过长已截断）"


def send_markdown(webhook, title, text, secret="", keyword="", at_mobiles=None, timeout=10):
    """发送 markdown 消息。返回 (是否成功, 说明)。"""
    if not webhook:
        return False, "未配置钉钉 Webhook"

    text = _chunk(text)
    if keyword and keyword not in text:
        text = f"{text}\n\n{keyword}"

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    mobiles = [m.strip() for m in (at_mobiles or []) if m.strip()]
    if mobiles:
        payload["at"] = {"atMobiles": mobiles, "isAtAll": False}

    url = webhook
    if secret:
        timestamp = str(int(time.time() * 1000))
        digest = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}\n{secret}".encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        separator = "&" if "?" in webhook else "?"
        url = f"{webhook}{separator}timestamp={timestamp}&sign={sign}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json;charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception as exc:  # 网络异常统一转成可展示的说明
        return False, f"请求失败：{exc}"

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, f"响应无法解析：{body[:200]}"
    if data.get("errcode") == 0:
        return True, "ok"
    return False, f"钉钉返回 errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
