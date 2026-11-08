"""真起两个 uvicorn、两个端口、两个 origin。

接口级测试用 ASGI 传输跑得又快又全，但它证明不了「跨 origin」这件事真的成立。
这里把两个 app 各绑一个真实套接字，用真 HTTP 走一遍全链路——端口、cookie 作用域、
重定向、读超时都是真的。
"""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from mock_site.app import build_apps
from mock_site.config import MockConfig
from mock_site.state import MockState

CAPTCHA_ID_PATTERN = re.compile(r'name="captcha_id" value="([^"]+)"')
RECEIPT_NO_PATTERN = re.compile(r'data-testid="receipt-no">(MOCK-[0-9]{8}-[0-9]{4})<')


def _free_socket() -> socket.socket:
    """先占住一个端口再交给 uvicorn：这样 origin 配置和真实监听端口必然一致。"""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


def _wait_started(server: uvicorn.Server, thread: threading.Thread, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn 没有在超时前启动")
        if not thread.is_alive():
            raise RuntimeError("uvicorn 线程提前退出")
        time.sleep(0.01)


@contextmanager
def running_sites(
    *, submit_mode: str = "normal", hang_seconds: float = 30.0
) -> Iterator[tuple[str, str, MockState]]:
    account_sock = _free_socket()
    apply_sock = _free_socket()
    config = MockConfig(
        account_port=account_sock.getsockname()[1],
        apply_port=apply_sock.getsockname()[1],
        submit_mode=submit_mode,
        submit_hang_seconds=hang_seconds,
    )
    account_app, apply_app, state = build_apps(config)
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []
    for app, sock in ((account_app, account_sock), (apply_app, apply_sock)):
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off")
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        servers.append((server, thread))
    try:
        for server, thread in servers:
            _wait_started(server, thread, timeout=10.0)
        yield config.account_origin, config.apply_origin, state
    finally:
        for server, _thread in servers:
            server.should_exit = True
        for _server, thread in servers:
            thread.join(timeout=5)
        account_sock.close()
        apply_sock.close()


def sign_in(client: httpx.Client, account_origin: str) -> None:
    page = client.get(f"{account_origin}/login")
    match = CAPTCHA_ID_PATTERN.search(page.text)
    assert match is not None
    captcha_id = match.group(1)
    code = client.get(f"{account_origin}/__mock__/captcha/{captcha_id}").json()["code"]
    response = client.post(
        f"{account_origin}/login",
        data={
            "username": "demo",
            "password": "demo1234",
            "captcha_id": captcha_id,
            "captcha_code": code,
        },
    )
    assert response.status_code == 200
    assert response.url.path == "/services"


def fill_and_upload(client: httpx.Client, apply_origin: str) -> None:
    saved = client.post(
        f"{apply_origin}/apply/tab1",
        data={
            "domicile_type": "local",
            "situations": ["first", "change"],
            "phone": "13900002222",
            "address": "苏州市姑苏区平江路 8 号",
        },
    )
    assert saved.status_code == 200
    assert saved.url.path == "/apply/tab2"
    uploaded = client.post(
        f"{apply_origin}/apply/tab2/upload",
        files=[
            ("files", ("户口簿.pdf", b"%PDF-1.4 a", "application/pdf")),
            ("files", ("身份证.jpg", b"\xff\xd8\xff\xe0jpeg", "image/jpeg")),
        ],
    )
    assert uploaded.status_code == 200
    assert uploaded.text.count('data-testid="attachment-item"') == 2


def test_full_flow_crosses_two_real_origins() -> None:
    with running_sites() as (account_origin, apply_origin, _state):
        account_port = int(account_origin.rsplit(":", 1)[1])
        apply_port = int(apply_origin.rsplit(":", 1)[1])
        assert account_port != apply_port

        with httpx.Client(follow_redirects=True, timeout=10.0, trust_env=False) as client:
            sign_in(client, account_origin)
            services = client.get(f"{account_origin}/services")
            assert services.text.count('data-testid="service-item"') == 5

            handoff = client.get(f"{account_origin}/handoff/elderly-subsidy")
            assert handoff.url.port == apply_port
            assert handoff.headers["x-mock-site"] == "apply"
            assert handoff.history[0].headers["x-mock-site"] == "account"
            assert handoff.url.path == "/apply/tab1"

            fill_and_upload(client, apply_origin)

            confirm = client.get(f"{apply_origin}/apply/tab3")
            assert confirm.text.count('data-testid="submit-blockers"') == 0
            assert 'data-testid="summary-situations">首次申请、变更申请' in confirm.text

            submitted = client.post(f"{apply_origin}/apply/submit")
            assert submitted.status_code == 200
            assert submitted.url.path.startswith("/receipt/")
            match = RECEIPT_NO_PATTERN.search(submitted.text)
            assert match is not None
            number = match.group(1)

            status = client.get(f"{apply_origin}/receipt/{number}/status")
            assert status.json()["status"] == "accepted"
            assert status.json()["attachments"] == 2

            # 两个应用是两套路由，不是一个应用的两个挂载点。
            assert client.get(f"{apply_origin}/services").status_code == 404
            assert client.get(f"{account_origin}/apply/tab1").status_code == 404


def test_no_response_shape_is_a_real_read_timeout() -> None:
    with running_sites(submit_mode="no_response", hang_seconds=0.6) as (
        account_origin,
        apply_origin,
        _state,
    ):
        with httpx.Client(follow_redirects=True, timeout=10.0, trust_env=False) as client:
            sign_in(client, account_origin)
            client.get(f"{account_origin}/handoff/elderly-subsidy")
            fill_and_upload(client, apply_origin)

            with pytest.raises(httpx.ReadTimeout):
                client.post(f"{apply_origin}/apply/submit", timeout=0.2)

            receipts = client.get(f"{apply_origin}/__mock__/receipts").json()["receipts"]
            assert receipts == []
            # 让服务端把挂住的请求处理完，避免关停时留下半截任务。
            time.sleep(0.7)


def test_error_500_with_an_accepted_application() -> None:
    with running_sites() as (account_origin, apply_origin, _state):
        with httpx.Client(follow_redirects=True, timeout=10.0, trust_env=False) as client:
            sign_in(client, account_origin)
            client.get(f"{account_origin}/handoff/elderly-subsidy")
            fill_and_upload(client, apply_origin)

            flipped = client.post(
                f"{apply_origin}/__mock__/config", json={"submit_mode": "error_but_accepted"}
            )
            assert flipped.status_code == 200
            assert flipped.json()["submit_mode"] == "error_but_accepted"

            failed = client.post(f"{apply_origin}/apply/submit")
            assert failed.status_code == 500
            assert "MOCK-" not in failed.text

            receipts = client.get(f"{apply_origin}/__mock__/receipts").json()["receipts"]
            assert len(receipts) == 1
            assert receipts[0]["status"] == "accepted"
