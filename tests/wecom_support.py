"""测试用的企业微信回调构造器（加密模式）。

生产代码只做解密，不提供加密：加解密在一处实现，测试侧独立构造一遍，两边
才有交叉验证的意义（如果测试调用生产代码的加解密，那只能证明它自己跟自己一致）。
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 43 个 base64 字符 → 32 字节。企业微信后台给的就是这个长度。
ENCODING_AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
CALLBACK_TOKEN = "workpanda-callback-token-for-tests"
CORP_ID = "ww-workpanda-test-corp"
AGENT_ID = 1000002


def _key_bytes(encoding_aes_key: str) -> bytes:
    return base64.b64decode(encoding_aes_key + "=")


def encrypt_message(
    plaintext: str, *, encoding_aes_key: str = ENCODING_AES_KEY, receive_id: str = CORP_ID
) -> str:
    """按企业微信的明文结构加密：16 字节随机数 + 4 字节长度 + 消息 + receive_id。"""

    key = _key_bytes(encoding_aes_key)
    body = plaintext.encode("utf-8")
    message = (
        b"0123456789abcdef" + len(body).to_bytes(4, "big") + body + receive_id.encode("utf-8")
    )
    # 微信系官方示例按 32 字节块填充（PKCS#7）。解密侧只校验填充字节，两种块长都收。
    pad = 32 - len(message) % 32
    padded = message + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def callback_signature(*parts: str) -> str:
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def sign(
    *,
    encrypt: str,
    token: str = CALLBACK_TOKEN,
    timestamp: str = "1700000000",
    nonce: str = "nonce1",
) -> str:
    return callback_signature(token, timestamp, nonce, encrypt)


def xml(fields: Mapping[str, str]) -> str:
    inner = "".join(f"<{tag}><![CDATA[{value}]]></{tag}>" for tag, value in fields.items())
    return f"<xml>{inner}</xml>"


def message_body(
    *,
    content: str,
    from_user: str = "zhangsan",
    msg_id: str = "1000001",
    msg_type: str = "text",
    create_time: str = "1700000000",
    encoding_aes_key: str = ENCODING_AES_KEY,
    receive_id: str = CORP_ID,
) -> bytes:
    """构造 POST body（外层信封 + 加密的内层消息）。"""

    inner = xml(
        {
            "ToUserName": receive_id,
            "FromUserName": from_user,
            "CreateTime": create_time,
            "MsgType": msg_type,
            "Content": content,
            "MsgId": msg_id,
            "AgentID": str(AGENT_ID),
        }
    )
    encrypted = encrypt_message(inner, encoding_aes_key=encoding_aes_key, receive_id=receive_id)
    return xml({"ToUserName": receive_id, "Encrypt": encrypted, "AgentID": str(AGENT_ID)}).encode(
        "utf-8"
    )


def callback_params(
    body: bytes,
    *,
    token: str = CALLBACK_TOKEN,
    timestamp: str = "1700000000",
    nonce: str = "nonce1",
    signature: str | None = None,
) -> dict[str, str]:
    """从 body 里取出密文重新算签名。`signature` 显式给出时不覆盖（用于构造坏签名）。"""

    text = body.decode("utf-8")
    start = text.find("<Encrypt><![CDATA[") + len("<Encrypt><![CDATA[")
    end = text.find("]]></Encrypt>")
    encrypt = text[start:end]
    return {
        "msg_signature": signature if signature is not None else sign(
            encrypt=encrypt, token=token, timestamp=timestamp, nonce=nonce
        ),
        "timestamp": timestamp,
        "nonce": nonce,
    }
