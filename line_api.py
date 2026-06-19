import hashlib
import hmac
import base64
import json
import urllib.request
from typing import Optional

LINE_API_BASE = "https://api.line.me/v2/bot"


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    mac = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode()
    return hmac.compare_digest(expected, signature)


def _call(endpoint: str, payload: dict, token: str) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{LINE_API_BASE}/message/{endpoint}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"LINE API error: {e}")
        return False


def push_message(user_id: str, messages: list, token: str) -> bool:
    chunks = [messages[i:i+5] for i in range(0, len(messages), 5)]
    for chunk in chunks:
        ok = _call("push", {"to": user_id, "messages": chunk}, token)
        if not ok:
            return False
    return True


def push_text(user_id: str, text: str, token: str) -> bool:
    chunks = [text[i:i+4900] for i in range(0, len(text), 4900)]
    messages = [{"type": "text", "text": c} for c in chunks]
    return push_message(user_id, messages, token)


def reply_message(reply_token: str, messages: list, token: str) -> bool:
    return _call("reply", {"replyToken": reply_token, "messages": messages}, token)


def get_profile(user_id: str, token: str) -> Optional[dict]:
    req = urllib.request.Request(
        f"{LINE_API_BASE}/profile/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"LINE profile error: {e}")
        return None


def build_message(msg_type: str, content: str) -> dict:
    if msg_type == "text":
        return {"type": "text", "text": content}
    if msg_type == "image":
        try:
            d = json.loads(content)
            return {
                "type": "image",
                "originalContentUrl": d.get("original", ""),
                "previewImageUrl": d.get("preview", d.get("original", "")),
            }
        except Exception:
            return {"type": "text", "text": "[image error]"}
    if msg_type == "flex":
        try:
            return {"type": "flex", "altText": "メッセージ", "contents": json.loads(content)}
        except Exception:
            return {"type": "text", "text": "[flex error]"}
    return {"type": "text", "text": content}


def get_all_follower_ids(token: str) -> list:
    """LINE APIから全フォロワーのuserIDを取得（ページネーション対応）"""
    ids = []
    url = f"{LINE_API_BASE}/followers/ids?count=300"
    while url:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
                ids.extend(data.get("userIds", []))
                nxt = data.get("next")
                url = f"{LINE_API_BASE}/followers/ids?count=300&start={nxt}" if nxt else None
        except Exception as e:
            print(f"LINE followers/ids error: {e}")
            break
    return ids
