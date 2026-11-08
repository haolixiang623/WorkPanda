"""企业微信 adapter 的单元回归：验签算法、解密边界、出站发送。

这几条用例守的是"协议实现有没有做对"，与网关的业务边界分开：

- 签名必须按企业微信的字典序拼 sha1（拿一个固定的向量钉住，避免"改了排序顺序
  但测试也跟着改"）；
- 密文里的 `receive_id` 必须与配置的 corpid 一致，否则"发给别家企业的包"也能解开；
- DTD/实体声明直接拒绝：`xml.etree` 对实体展开攻击没有防护；
- 出站必须真的把状态发出去、token 只取一次、token 过期只重试一次。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from uuid import UUID

import httpx
import pytest

from tests.wecom_support import (
    CALLBACK_TOKEN,
    CORP_ID,
    ENCODING_AES_KEY,
    callback_signature,
    encrypt_message,
    xml,
)
from workpanda.domain.im_gateway import ImPayloadRejected, ImSignatureRejected, TaskCard
from workpanda.infrastructure.wecom import (
    WeComCallbackAdapter,
    WeComCardSender,
    WeComConfigError,
    WeComSendError,
)
from workpanda.infrastructure.wecom_crypto import aes_key_bytes, parse_fields

CARD = TaskCard(
    task_id=UUID("11111111-1111-1111-1111-111111111111"),
    status="waiting_for_confirmation",
    title="任务状态：待人确认",
    description="任务号：11111111-1111-1111-1111-111111111111",
    url="https://wpa.example.com/tasks/11111111-1111-1111-1111-111111111111",
)


def adapter(**overrides) -> WeComCallbackAdapter:
    base = {
        "token": CALLBACK_TOKEN,
        "encoding_aes_key": ENCODING_AES_KEY,
        "corp_id": CORP_ID,
    }
    base.update(overrides)
    return WeComCallbackAdapter(**base)


def body_with(encrypt: str) -> bytes:
    return xml({"ToUserName": CORP_ID, "Encrypt": encrypt, "AgentID": "1"}).encode("utf-8")


def params_for(encrypt: str, *, signature: str | None = None) -> dict[str, str]:
    return {
        "msg_signature": signature
        if signature is not None
        else callback_signature(CALLBACK_TOKEN, "1700000000", "nonce1", encrypt),
        "timestamp": "1700000000",
        "nonce": "nonce1",
    }


# ---------- 验签 ----------


def test_signature_uses_sorted_concatenation() -> None:
    # 字典序拼接后取 sha1：("a","1","2","3") → "123a"
    assert callback_signature("a", "1", "2", "3") == (
        "b7b975da6a95a9e891efad76d30b43b75a267911"
    )


def test_valid_signature_is_accepted() -> None:
    plaintext = xml({"MsgId": "1", "FromUserName": "zhangsan", "MsgType": "text", "Content": "hi"})
    encrypt = encrypt_message(plaintext)
    message = adapter().read(params_for(encrypt), body_with(encrypt))
    assert message.message_id == "1"
    assert message.im_user_id == "zhangsan"
    assert message.text == "hi"


def test_body_swapped_under_a_valid_signature_is_rejected() -> None:
    """签名覆盖的是密文本身：换掉密文，签名就不再对得上。"""

    original = encrypt_message(xml({"MsgId": "1", "FromUserName": "zhangsan"}))
    other = encrypt_message(xml({"MsgId": "2", "FromUserName": "lisi"}))
    with pytest.raises(ImSignatureRejected):
        adapter().read(params_for(original), body_with(other))


def test_missing_signature_is_rejected() -> None:
    encrypt = encrypt_message(xml({"MsgId": "1", "FromUserName": "zhangsan"}))
    with pytest.raises(ImSignatureRejected):
        adapter().read({"timestamp": "1700000000", "nonce": "nonce1"}, body_with(encrypt))


def test_url_echo_is_decrypted_after_verification() -> None:
    echo = encrypt_message("echo-ok")
    assert adapter().verify_url(params_for(echo) | {"echostr": echo}) == "echo-ok"


def test_url_echo_without_echostr_is_rejected() -> None:
    with pytest.raises(ImPayloadRejected):
        adapter().verify_url({"msg_signature": "x", "timestamp": "1", "nonce": "n"})


# ---------- 解密 ----------


def test_receive_id_mismatch_is_rejected() -> None:
    """能解开只说明 key 相同；receive_id 才能证明这个包是发给我们的。"""

    encrypt = encrypt_message(
        xml({"MsgId": "1", "FromUserName": "zhangsan"}), receive_id="ww-someone-else"
    )
    with pytest.raises(ImPayloadRejected):
        adapter().read(params_for(encrypt), body_with(encrypt))


def test_wrong_aes_key_is_rejected() -> None:
    other_key = base64.b64encode(b"x" * 32).decode().rstrip("=")
    assert len(other_key) == 43
    encrypt = encrypt_message(xml({"MsgId": "1", "FromUserName": "zhangsan"}))
    with pytest.raises(ImPayloadRejected):
        adapter(encoding_aes_key=other_key).read(params_for(encrypt), body_with(encrypt))


def test_bad_aes_key_length_is_rejected_at_startup() -> None:
    with pytest.raises(ImPayloadRejected):
        aes_key_bytes("too-short")


def test_doctype_is_rejected_before_parsing() -> None:
    payload = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><xml><Encrypt>e</Encrypt></xml>'
    with pytest.raises(ImPayloadRejected):
        adapter().read(
            {"msg_signature": "x", "timestamp": "1", "nonce": "n"}, payload
        )


def test_oversized_body_is_rejected() -> None:
    with pytest.raises(ImPayloadRejected):
        adapter(max_body_bytes=16).read(
            {"msg_signature": "x", "timestamp": "1", "nonce": "n"}, b"<xml>" + b"a" * 100
        )


def test_event_without_msg_id_gets_a_stable_id() -> None:
    """事件回调没有 MsgId：用内层报文摘要当幂等键，重投仍然会被认出来。"""

    plaintext = xml({"FromUserName": "zhangsan", "MsgType": "event", "Event": "enter_agent"})
    encrypt = encrypt_message(plaintext)
    first = adapter().read(params_for(encrypt), body_with(encrypt))
    second = adapter().read(params_for(encrypt), body_with(encrypt))
    assert first.message_id.startswith("sha1:")
    assert first.message_id == second.message_id


def test_parse_fields_flattens_first_occurrence_only() -> None:
    fields = parse_fields("<xml><A>1</A><A>2</A><B>3</B></xml>")
    assert fields == {"A": "1", "B": "3"}


# ---------- 出站发送 ----------


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sender_posts_textcard_and_caches_token() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "tok-1", "expires_in": 7200}
            )
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = mock_client(handler)
    sender = WeComCardSender(
        corp_id=CORP_ID, corp_secret="secret", agent_id=1000002, client=client
    )
    try:
        sender.send("zhangsan", CARD)
        sender.send("zhangsan", CARD)
    finally:
        client.close()

    token_calls = [call for call in calls if call.url.path.endswith("/gettoken")]
    send_calls = [call for call in calls if call.url.path.endswith("/message/send")]
    assert len(token_calls) == 1  # token 缓存生效
    assert len(send_calls) == 2
    payload = json.loads(send_calls[0].content)
    assert payload["touser"] == "zhangsan"
    assert payload["msgtype"] == "textcard"
    assert payload["agentid"] == 1000002
    assert payload["textcard"]["url"] == CARD.url
    # 卡片内容里不该有除这三样之外的字段
    assert set(payload["textcard"]) == {"title", "description", "url", "btntxt"}


def test_sender_refreshes_token_exactly_once() -> None:
    state = {"tokens": 0, "sends": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gettoken"):
            state["tokens"] += 1
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "access_token": f"tok-{state['tokens']}",
                    "expires_in": 7200,
                },
            )
        state["sends"] += 1
        if state["sends"] == 1:
            return httpx.Response(200, json={"errcode": 42001, "errmsg": "expired"})
        return httpx.Response(200, json={"errcode": 0})

    client = mock_client(handler)
    sender = WeComCardSender(
        corp_id=CORP_ID, corp_secret="secret", agent_id=1000002, client=client
    )
    try:
        sender.send("zhangsan", CARD)
    finally:
        client.close()

    assert state == {"tokens": 2, "sends": 2}  # token 过期 → 重取一次 → 成功


def test_sender_raises_on_business_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "tok", "expires_in": 7200}
            )
        return httpx.Response(200, json={"errcode": 81013, "errmsg": "user invalid"})

    client = mock_client(handler)
    sender = WeComCardSender(
        corp_id=CORP_ID, corp_secret="secret", agent_id=1000002, client=client
    )
    try:
        with pytest.raises(WeComSendError):
            sender.send("ghost", CARD)
    finally:
        client.close()


def test_sender_raises_on_http_error_and_bad_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "tok", "expires_in": 7200}
            )
        return httpx.Response(502, json={"errcode": 0})

    client = mock_client(handler)
    sender = WeComCardSender(
        corp_id=CORP_ID, corp_secret="secret", agent_id=1000002, client=client
    )
    try:
        with pytest.raises(WeComSendError):
            sender.send("zhangsan", CARD)

        def not_json(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>gateway</html>")

        broken = WeComCardSender(
            corp_id=CORP_ID,
            corp_secret="secret",
            agent_id=1000002,
            client=mock_client(not_json),
        )
        with pytest.raises(WeComSendError):
            broken.send("zhangsan", CARD)
    finally:
        client.close()


def test_sender_requires_agent_id_and_secret() -> None:
    with pytest.raises(WeComConfigError):
        WeComCardSender(corp_id=CORP_ID, corp_secret="", agent_id=1000002)
    with pytest.raises(WeComConfigError):
        WeComCardSender(corp_id=CORP_ID, corp_secret="secret", agent_id=0)
