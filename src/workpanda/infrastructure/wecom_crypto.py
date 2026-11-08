"""企业微信回调的验签、解密与两层 XML 解析。

企业微信（以及微信系）的回调有两种模式：

- **明文模式**：`signature = sha1(sort(token, timestamp, nonce))`。注意它**不覆盖
  消息体**——同一个 (signature, timestamp, nonce) 三元组可以被重放并配上任意
  body。"验签通过"在这种模式下不能证明"这段内容来自企业微信"，所以本实现
  **不接受明文模式**：收下它等于给自己一个看起来在验签、实际没验的安全感。
- **加密模式**（本实现）：`msg_signature = sha1(sort(token, timestamp, nonce, encrypt))`，
  签名覆盖密文；解开后明文尾部还带 `receive_id`（企业微信的 corpid），
  能把"发给别家企业微信的包"挡在外面。这就是本节的全部工作。

加解密用 `cryptography` 的 AES-256-CBC。**不手写 AES**：加密实现里最贵的不是
算对，是常数时间与侧信道；自己写的那一份永远缺一次审计。为此新增一个依赖，
取舍写在 `docs/IM-GATEWAY.md`。

XML 解析只用标准库，并且在做任何解析之前先拒掉 `<!DOCTYPE` / `<!ENTITY`：
`xml.etree` 对"十亿笑声"这类实体展开攻击没有防护，而正常回调里永远不需要 DTD。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from workpanda.domain.im_gateway import ImPayloadRejected, ImSignatureRejected

# 企业微信要求的 EncodingAESKey 长度（43 个 base64 字符，补齐后 32 字节）。
AES_KEY_CHARS = 43
_AES_KEY_BYTES = 32
# 明文结构：16 字节随机数 + 4 字节大端长度 + 消息体 + receive_id。
_RANDOM_PREFIX_BYTES = 16
_LENGTH_BYTES = 4
# PKCS#7 常见块长 32（微信系官方示例）与 16（AES 块长）都要接受：
# 我们只做解密，按最后一个字节校验填充即可，不必猜对方用哪种块长。
_MAX_PADDING = 32
# 正常的回调 XML 在几 KB 量级；1 MB 已经远超任何合法消息，先挡在外面再解析。
MAX_CALLBACK_BYTES = 1024 * 1024

_DTD_MARKERS = ("<!DOCTYPE", "<!ENTITY")


def callback_signature(*parts: str) -> str:
    """企业微信的签名算法：把参数按字典序拼接后取 sha1。"""

    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def verify_callback_signature(
    *, token: str, signature: str, timestamp: str, nonce: str, encrypt: str
) -> None:
    """校验 `msg_signature`。不通过一律抛 `ImSignatureRejected`。

    用 `hmac.compare_digest` 而不是 `==`：签名比较必须是常数时间，否则逐字节
    试探就能把签名"猜"出来（虽然这里还要先有合法的时间戳与 nonce，但少一层
    假设比多一层好）。
    """

    given = (signature or "").strip()
    if not given or not encrypt:
        raise ImSignatureRejected("missing signature")
    expected = callback_signature(token, timestamp or "", nonce or "", encrypt)
    if not hmac.compare_digest(expected, given.lower()):
        raise ImSignatureRejected("signature mismatch")


def aes_key_bytes(encoding_aes_key: str) -> bytes:
    """把 43 字符的 EncodingAESKey 还原成 32 字节密钥。

    长度或 base64 不合法时直接抛错：配置写错必须在启动期就失败，而不是等到
    第一条回调进来才发现"所有请求都验签失败"。
    """

    value = (encoding_aes_key or "").strip()
    if len(value) != AES_KEY_CHARS:
        raise ImPayloadRejected(
            f"EncodingAESKey 必须是 {AES_KEY_CHARS} 个字符，实际 {len(value)} 个。"
        )
    try:
        key = base64.b64decode(value + "=", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImPayloadRejected("EncodingAESKey 不是合法的 base64。") from exc
    if len(key) != _AES_KEY_BYTES:
        raise ImPayloadRejected("EncodingAESKey 解码后不是 32 字节。")
    return key


def decrypt_message(encoding_aes_key: str, encrypted: str, receive_id: str) -> str:
    """解开一条加密回调，返回明文字符串（内层 XML）。

    `receive_id` 不匹配即拒绝：那说明这个包是加密给另一家企业微信的，
    能解开只说明双方 key 相同，不代表它是发给我们的。
    """

    key = aes_key_bytes(encoding_aes_key)
    try:
        raw = base64.b64decode((encrypted or "").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImPayloadRejected("密文不是合法的 base64。") from exc
    if not raw or len(raw) % 16:
        raise ImPayloadRejected("密文长度不是 AES 块长的整数倍。")

    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:_RANDOM_PREFIX_BYTES])).decryptor()
    try:
        padded = decryptor.update(raw) + decryptor.finalize()
    except ValueError as exc:  # pragma: no cover - 只有坏实现才会走到
        raise ImPayloadRejected("密文解密失败。") from exc

    content = _unpad(padded)
    if len(content) < _RANDOM_PREFIX_BYTES + _LENGTH_BYTES:
        raise ImPayloadRejected("解密后的内容过短。")
    length = int.from_bytes(
        content[_RANDOM_PREFIX_BYTES : _RANDOM_PREFIX_BYTES + _LENGTH_BYTES], "big"
    )
    start = _RANDOM_PREFIX_BYTES + _LENGTH_BYTES
    if length <= 0 or start + length > len(content):
        raise ImPayloadRejected("解密后的长度字段与实际内容不符。")
    message = content[start : start + length]
    tail = content[start + length :]
    try:
        text = message.decode("utf-8")
        actual_receive_id = tail.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImPayloadRejected("解密后的内容不是 UTF-8。") from exc
    if actual_receive_id != receive_id:
        raise ImPayloadRejected("receive_id 与配置的 corpid 不一致。")
    return text


def _unpad(padded: bytes) -> bytes:
    if not padded:
        raise ImPayloadRejected("解密结果为空。")
    pad = padded[-1]
    if pad < 1 or pad > _MAX_PADDING or pad > len(padded):
        raise ImPayloadRejected("填充字节非法。")
    if any(byte != pad for byte in padded[-pad:]):
        raise ImPayloadRejected("填充字节不一致。")
    return padded[:-pad]


def parse_xml(text: str) -> ElementTree.Element:
    """解析回调 XML，拒绝 DTD 与超长/空内容。

    先看原文再解析，而不是"解析完再检查"：实体展开发生在解析过程中，
    检查要在它之前。
    """

    value = (text or "").strip()
    if not value:
        raise ImPayloadRejected("回调内容为空。")
    if len(value.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ImPayloadRejected("回调内容超出大小上限。")
    lowered = value.upper()
    if any(marker in lowered for marker in _DTD_MARKERS):
        raise ImPayloadRejected("回调内容含 DTD/实体声明，拒绝解析。")
    try:
        return ElementTree.fromstring(value)
    except ElementTree.ParseError as exc:
        raise ImPayloadRejected("回调内容不是合法 XML。") from exc


def parse_fields(text: str) -> dict[str, str]:
    """把一层 XML 摊平成 `{标签: 文本}`。同名标签只取第一个——企业微信的回调
    没有重复标签，重复出现只可能是构造出来的包。"""

    root = parse_xml(text)
    fields: dict[str, str] = {}
    for child in root:
        if child.tag in fields:
            continue
        fields[str(child.tag)] = (child.text or "").strip()
    return fields
