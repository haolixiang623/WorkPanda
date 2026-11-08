"""账号站：登录、锁定、扫码登录、跨站跳转、回环守卫。"""

from __future__ import annotations

import re

import httpx
import pytest
from mock_site.state import MockState
from support import APPLY_ORIGIN, FakeClock, login, login_wrong_captcha

QR_TICKET_PATTERN = re.compile(r'data-testid="qr-ticket"\s+data-ticket="([^"]+)"')


def qr_ticket(html: str) -> str:
    match = QR_TICKET_PATTERN.search(html)
    assert match is not None, "扫码登录页没有 qr-ticket"
    return match.group(1)


async def test_root_redirects_to_login(probe: httpx.AsyncClient) -> None:
    response = await probe.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_login_page_has_the_controls_an_executor_needs(
    probe: httpx.AsyncClient,
) -> None:
    page = await probe.get("/login")
    assert page.status_code == 200
    for testid in (
        "login-form",
        "input-username",
        "input-password",
        "input-captcha",
        "captcha-image",
        "captcha-id",
        "login-submit",
        "mock-banner",
    ):
        assert f'data-testid="{testid}"' in page.text
    # 验证码图片内联成 data URI：页面不请求任何外部资源。
    assert "data:image/svg+xml;base64," in page.text


async def test_login_success_lists_five_services_with_one_pilot(
    browser: httpx.AsyncClient,
) -> None:
    response = await login(browser)
    assert response.status_code == 200
    assert response.url.path == "/services"
    assert 'data-testid="current-user">demo' in response.text
    assert response.text.count('data-testid="service-item"') == 5
    assert response.text.count('data-testid="service-pilot"') == 1
    assert response.text.count('data-testid="service-start"') == 5


async def test_wrong_captcha_is_rejected_but_does_not_break_the_next_attempt(
    browser: httpx.AsyncClient,
) -> None:
    rejected = await login_wrong_captcha(browser)
    assert rejected.status_code == 401
    assert 'data-testid="login-error"' in rejected.text
    assert 'data-code="captcha_invalid"' in rejected.text

    accepted = await login(browser)
    assert accepted.status_code == 200
    assert accepted.url.path == "/services"


async def test_three_captcha_failures_lock_the_account(
    browser: httpx.AsyncClient, state: MockState
) -> None:
    for attempt in range(3):
        rejected = await login_wrong_captcha(browser)
        assert rejected.status_code == 401, f"第 {attempt + 1} 次应该只是 401"
    assert state.is_locked("demo") is True

    locked = await login(browser)
    assert locked.status_code == 423
    assert 'data-code="account_locked"' in locked.text
    assert 'data-testid="login-locked"' in locked.text

    await browser.post("/__mock__/reset")
    assert state.is_locked("demo") is False
    assert (await login(browser)).status_code == 200


async def test_wrong_password_also_counts_toward_the_lock(
    browser: httpx.AsyncClient,
) -> None:
    for _ in range(3):
        rejected = await login(browser, password="not-the-password")
        assert rejected.status_code == 401
        assert 'data-code="credentials_invalid"' in rejected.text
    locked = await login(browser)
    assert locked.status_code == 423


async def test_services_requires_a_session(probe: httpx.AsyncClient) -> None:
    response = await probe.get("/services")
    assert response.status_code == 401
    assert 'data-code="session_required"' in response.text


async def test_qr_login_pending_confirm_then_session(
    browser: httpx.AsyncClient,
) -> None:
    page = await browser.get("/login/qr")
    assert page.status_code == 200
    ticket = qr_ticket(page.text)
    assert 'data-testid="qr-image"' in page.text

    pending = await browser.get(f"/login/qr/{ticket}/status")
    assert pending.json()["status"] == "pending"
    assert "session_url" not in pending.json()

    confirmed = await browser.post(f"/__mock__/qr/{ticket}/confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    status = await browser.get(f"/login/qr/{ticket}/status")
    assert status.json()["status"] == "confirmed"
    session_url = status.json()["session_url"]

    signed_in = await browser.get(session_url)
    assert signed_in.status_code == 200
    assert signed_in.url.path == "/services"

    consumed = await browser.get(f"/login/qr/{ticket}/status")
    assert consumed.json()["status"] == "consumed"


async def test_qr_ticket_can_only_be_exchanged_once(
    browser: httpx.AsyncClient,
) -> None:
    page = await browser.get("/login/qr")
    ticket = qr_ticket(page.text)
    await browser.post(f"/__mock__/qr/{ticket}/confirm", json={})
    first = await browser.get(f"/login/qr/{ticket}/session")
    assert first.status_code == 200
    second = await browser.get(f"/login/qr/{ticket}/session")
    assert second.status_code == 409
    assert 'data-code="qr_consumed"' in second.text


async def test_qr_expires_after_ninety_seconds(
    browser: httpx.AsyncClient, clock: FakeClock
) -> None:
    page = await browser.get("/login/qr")
    ticket = qr_ticket(page.text)

    assert (await browser.get(f"/login/qr/{ticket}/status")).json()["status"] == "pending"

    clock.advance(90.5)
    expired = await browser.get(f"/login/qr/{ticket}/status")
    assert expired.json()["status"] == "expired"

    late = await browser.post(f"/__mock__/qr/{ticket}/confirm", json={})
    assert late.status_code == 410
    assert late.json()["error_code"] == "qr_expired"

    session = await browser.get(f"/login/qr/{ticket}/session")
    assert session.status_code == 410


async def test_handoff_redirects_to_the_other_origin(
    probe: httpx.AsyncClient,
) -> None:
    await login(probe)
    response = await probe.get("/handoff/elderly-subsidy")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"{APPLY_ORIGIN}/apply?ticket=")
    assert response.headers["x-mock-site"] == "account"


async def test_handoff_requires_login(probe: httpx.AsyncClient) -> None:
    response = await probe.get("/handoff/elderly-subsidy")
    assert response.status_code == 401
    assert 'data-code="session_required"' in response.text


async def test_handoff_rejects_unknown_service(probe: httpx.AsyncClient) -> None:
    await login(probe)
    response = await probe.get("/handoff/no-such-service")
    assert response.status_code == 404
    assert 'data-code="service_not_found"' in response.text


async def test_mock_endpoints_are_loopback_only(stranger: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/__mock__/state"),
        ("POST", "/__mock__/config"),
        ("POST", "/__mock__/reset"),
    ):
        response = await stranger.request(method, path, json={})
        assert response.status_code == 403, f"{method} {path} 应该拒绝非回环调用"
        assert response.json()["error_code"] == "loopback_only"


async def test_errors_are_json_when_the_client_asks_for_json(
    stranger: httpx.AsyncClient,
) -> None:
    response = await stranger.get("/__mock__/state", headers={"accept": "application/json"})
    assert response.status_code == 403
    assert response.json()["error_code"] == "loopback_only"


async def test_healthz_names_the_site(probe: httpx.AsyncClient) -> None:
    response = await probe.get("/healthz")
    assert response.json() == {
        "status": "ok",
        "site": "account",
        "origin": "http://127.0.0.1:8101",
    }


@pytest.mark.parametrize("path", ["/login", "/login/qr", "/services"])
async def test_pages_carry_no_external_urls(
    browser: httpx.AsyncClient, path: str
) -> None:
    await login(browser)
    page = await browser.get(path)
    assert page.status_code == 200
    assert "https://" not in page.text
    assert "http://" not in page.text.replace(APPLY_ORIGIN, "")
