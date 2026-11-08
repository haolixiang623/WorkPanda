"""办理站：三个 tab、多附件、提交与回执，以及五个不稳定开关里的四个。"""

from __future__ import annotations

import re

import httpx
import pytest
from support import APPLY_ORIGIN, enter_pilot_service, fill_basic, two_files

ATTACHMENT_ID_PATTERN = re.compile(
    r'data-testid="attachment-item" data-file-id="([^"]+)"'
)
RECEIPT_NO_PATTERN = re.compile(r'data-testid="receipt-no">(MOCK-[0-9]{8}-[0-9]{4})<')


async def open_pilot_site(browser: httpx.AsyncClient) -> httpx.AsyncClient:
    response = await enter_pilot_service(browser)
    assert response.status_code == 200, response.text
    assert response.url.path == "/apply/tab1"
    assert response.url.port == 8102
    return browser


@pytest.fixture
async def onsite(browser: httpx.AsyncClient) -> httpx.AsyncClient:
    return await open_pilot_site(browser)


async def ready_to_submit(client: httpx.AsyncClient) -> None:
    await fill_basic(client)
    await client.post(f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(2))


def attachment_ids(html: str) -> list[str]:
    return ATTACHMENT_ID_PATTERN.findall(html)


def receipt_no(html: str) -> str:
    match = RECEIPT_NO_PATTERN.search(html)
    assert match is not None, "回执页没有回执号"
    return match.group(1)


# ------------------------------------------------------------------ tab1


async def test_tab1_rejects_bad_phone_and_keeps_nothing(
    onsite: httpx.AsyncClient,
) -> None:
    response = await fill_basic(onsite, phone="12345")
    assert response.status_code == 400
    assert 'data-testid="error-phone"' in response.text
    assert 'name="phone" value=""' in response.text


async def test_tab1_requires_at_least_one_situation(
    onsite: httpx.AsyncClient,
) -> None:
    response = await onsite.post(f"{APPLY_ORIGIN}/apply/tab1", data={
        "domicile_type": "local",
        "phone": "13800001111",
        "address": "苏州市姑苏区示例路 18 号",
    })
    assert response.status_code == 400
    assert 'data-testid="error-situations"' in response.text


async def test_tab1_rejects_unknown_domicile(onsite: httpx.AsyncClient) -> None:
    response = await fill_basic(onsite, domicile_type="mars")
    assert response.status_code == 400
    assert 'data-testid="error-domicile_type"' in response.text


async def test_tab1_values_survive_moving_between_tabs(
    onsite: httpx.AsyncClient,
) -> None:
    saved = await fill_basic(onsite)
    assert saved.status_code == 200
    assert saved.url.path == "/apply/tab2"

    back = await onsite.get(f"{APPLY_ORIGIN}/apply/tab1")
    assert 'name="phone" value="13800001111"' in back.text
    assert 'value="local" selected' in back.text
    assert 'value="first" checked' in back.text

    upload = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(2)
    )
    assert upload.status_code == 200

    summary = await onsite.get(f"{APPLY_ORIGIN}/apply/tab3")
    assert 'data-testid="summary-service">高龄补贴资格核验' in summary.text
    assert 'data-testid="summary-domicile">本市户籍' in summary.text
    assert 'data-testid="summary-situations">首次申请' in summary.text
    assert 'data-testid="summary-phone">13800001111' in summary.text
    assert 'data-testid="summary-attachments">2 个' in summary.text
    assert 'data-testid="submit-blockers"' not in summary.text


# ------------------------------------------------------------------ tab2


async def test_upload_accepts_multiple_files_with_chinese_names(
    onsite: httpx.AsyncClient,
) -> None:
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(2)
    )
    assert response.status_code == 200
    assert response.text.count('data-testid="attachment-item"') == 2
    assert "户主页.pdf" in response.text
    assert "身份证.pdf" in response.text
    assert 'data-testid="attachment-count" data-count="2"' in response.text


async def test_upload_rejects_an_oversized_file(onsite: httpx.AsyncClient) -> None:
    oversized = b"x" * (256 * 1024 + 1)
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload",
        files=[("files", ("大文件.pdf", oversized, "application/pdf"))],
        follow_redirects=False,
    )
    assert response.status_code == 413
    assert "单个文件上限" in response.text


async def test_upload_keeps_the_files_that_passed_when_one_is_rejected(
    onsite: httpx.AsyncClient,
) -> None:
    """部分成功是刻意的：执行器必须能处理「三个文件两个成功」。"""

    oversized = b"x" * (256 * 1024 + 1)
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload",
        files=[
            ("files", ("合格.pdf", b"%PDF-1.4 ok", "application/pdf")),
            ("files", ("超限.pdf", oversized, "application/pdf")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 413
    assert 'data-count="1"' in response.text
    assert "合格.pdf" in response.text


async def test_upload_rejects_an_unknown_content_type(
    onsite: httpx.AsyncClient,
) -> None:
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload",
        files=[("files", ("笔记.txt", b"hello", "text/plain"))],
        follow_redirects=False,
    )
    assert response.status_code == 415
    assert "不接受的文件类型" in response.text


async def test_upload_stops_at_five_attachments(onsite: httpx.AsyncClient) -> None:
    first = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(5)
    )
    assert first.status_code == 200
    assert first.text.count('data-testid="attachment-item"') == 5

    extra = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload",
        files=[("files", ("第六个.pdf", b"%PDF-1.4 6", "application/pdf"))],
        follow_redirects=False,
    )
    assert extra.status_code == 409
    assert "最多 5 个附件" in extra.text


async def test_upload_requires_multipart(onsite: httpx.AsyncClient) -> None:
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload",
        data={"files": "not-a-file"},
        follow_redirects=False,
    )
    assert response.status_code == 415
    assert 'data-code="multipart_required"' in response.text


async def test_upload_without_a_selected_file_is_a_400(
    onsite: httpx.AsyncClient,
) -> None:
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload",
        files=[("files", ("", b"", "application/octet-stream"))],
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "没有收到文件" in response.text


async def test_delete_removes_one_attachment(onsite: httpx.AsyncClient) -> None:
    uploaded = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(2)
    )
    file_ids = attachment_ids(uploaded.text)
    assert len(file_ids) == 2

    deleted = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/delete", data={"file_id": file_ids[0]}
    )
    assert deleted.status_code == 200
    assert deleted.text.count('data-testid="attachment-item"') == 1


async def test_deleting_an_unknown_attachment_is_idempotent(
    onsite: httpx.AsyncClient,
) -> None:
    await onsite.post(f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(2))
    again = await onsite.post(
        f"{APPLY_ORIGIN}/apply/tab2/delete", data={"file_id": "f_not_there"}
    )
    assert again.status_code == 200
    assert again.text.count('data-testid="attachment-item"') == 2


# ------------------------------------------------------------------ tab3 与提交


async def test_confirm_page_lists_what_is_missing(onsite: httpx.AsyncClient) -> None:
    page = await onsite.get(f"{APPLY_ORIGIN}/apply/tab3")
    assert 'data-testid="submit-blockers"' in page.text
    assert "基本信息还没填" in page.text
    assert 'data-testid="submit-application" disabled' in page.text


async def test_submit_is_refused_when_attachments_are_insufficient(
    onsite: httpx.AsyncClient,
) -> None:
    await fill_basic(onsite)
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/submit", follow_redirects=False
    )
    assert response.status_code == 409
    assert 'data-code="not_submittable"' in response.text
    assert "材料还差 2 个" in response.text


async def test_submit_returns_a_receipt_that_can_be_read_back(
    onsite: httpx.AsyncClient,
) -> None:
    await ready_to_submit(onsite)
    receipt = await onsite.post(f"{APPLY_ORIGIN}/apply/submit")
    assert receipt.status_code == 200
    assert receipt.url.path.startswith("/receipt/")
    assert 'data-testid="receipt-status" data-state="accepted"' in receipt.text
    number = receipt_no(receipt.text)

    status = await onsite.get(f"{APPLY_ORIGIN}/receipt/{number}/status")
    body = status.json()
    assert body["receipt_no"] == number
    assert body["status"] == "accepted"
    assert body["service_id"] == "elderly-subsidy"
    assert body["attachments"] == 2

    negotiated = await onsite.get(
        f"{APPLY_ORIGIN}/receipt/{number}", headers={"accept": "application/json"}
    )
    assert negotiated.json()["receipt_no"] == number


async def test_two_submissions_make_two_receipts(onsite: httpx.AsyncClient) -> None:
    """模拟站不查重。重复请求会不会重复受理，是产品要证明的事，不是靶子替它兜底。"""

    await ready_to_submit(onsite)
    first = receipt_no((await onsite.post(f"{APPLY_ORIGIN}/apply/submit")).text)
    second = receipt_no((await onsite.post(f"{APPLY_ORIGIN}/apply/submit")).text)
    assert first != second

    listing = await onsite.get(f"{APPLY_ORIGIN}/__mock__/receipts")
    assert len(listing.json()["receipts"]) == 2


async def test_error_500_still_means_the_application_was_accepted(
    onsite: httpx.AsyncClient,
) -> None:
    """「响应说失败、服务端其实已受理」——执行器必须靠回执回读，而不是靠状态码。"""

    await onsite.post(f"{APPLY_ORIGIN}/__mock__/config", json={
        "submit_mode": "error_but_accepted"
    })
    await ready_to_submit(onsite)
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/submit", follow_redirects=False
    )
    assert response.status_code == 500
    assert 'data-code="submit_error_after_accept"' in response.text
    assert "MOCK-" not in response.text  # 响应里不能泄漏回执号，否则「未知」就不叫未知

    listing = await onsite.get(f"{APPLY_ORIGIN}/__mock__/receipts")
    assert len(listing.json()["receipts"]) == 1


async def test_no_response_mode_leaves_no_receipt(
    onsite: httpx.AsyncClient,
) -> None:
    await onsite.post(f"{APPLY_ORIGIN}/__mock__/config", json={
        "submit_mode": "no_response",
        "submit_hang_seconds": 0.05,
    })
    await ready_to_submit(onsite)
    response = await onsite.post(
        f"{APPLY_ORIGIN}/apply/submit", follow_redirects=False
    )
    assert response.status_code == 504
    assert 'data-code="submit_no_response"' in response.text

    listing = await onsite.get(f"{APPLY_ORIGIN}/__mock__/receipts")
    assert listing.json()["receipts"] == []


async def test_slow_mode_still_produces_a_receipt(onsite: httpx.AsyncClient) -> None:
    await onsite.post(f"{APPLY_ORIGIN}/__mock__/config", json={
        "submit_mode": "slow",
        "submit_delay_seconds": 0.02,
    })
    await ready_to_submit(onsite)
    response = await onsite.post(f"{APPLY_ORIGIN}/apply/submit")
    assert response.status_code == 200
    assert 'data-state="accepted"' in response.text


# ------------------------------------------------------------------ 会话与票据


async def test_apply_pages_need_a_handoff_session(
    browser: httpx.AsyncClient,
) -> None:
    response = await browser.get(f"{APPLY_ORIGIN}/apply/tab1")
    assert response.status_code == 401
    assert 'data-code="session_required"' in response.text


async def test_handoff_ticket_cannot_be_redeemed_twice(
    probe: httpx.AsyncClient,
) -> None:
    from support import login

    await login(probe)
    handoff = await probe.get("/handoff/elderly-subsidy")
    location = handoff.headers["location"]

    first = await probe.get(location)
    assert first.status_code == 303
    second = await probe.get(location)
    assert second.status_code == 409
    assert 'data-code="handoff_used"' in second.text


async def test_apply_pages_are_served_by_the_apply_site(
    onsite: httpx.AsyncClient,
) -> None:
    page = await onsite.get(f"{APPLY_ORIGIN}/apply/tab1")
    assert page.headers["x-mock-site"] == "apply"
    assert 'data-testid="apply-service">高龄补贴资格核验' in page.text


async def test_healthz_names_the_site(onsite: httpx.AsyncClient) -> None:
    response = await onsite.get(f"{APPLY_ORIGIN}/healthz")
    assert response.json() == {
        "status": "ok",
        "site": "apply",
        "origin": "http://127.0.0.1:8102",
    }
