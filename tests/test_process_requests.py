import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import common  # noqa: E402
import process_requests as pr  # noqa: E402


ADD_BODY = """### 채널 URL 또는 핸들

https://www.youtube.com/@example

### 표시 이름 (선택)

표시이름
"""

ADD_BODY_NO_NAME = """### 채널 URL 또는 핸들

https://www.youtube.com/@example

### 표시 이름 (선택)

_No response_
"""

REMOVE_BODY = """### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)

표시이름
"""


def test_classify_by_title_prefix_not_labels():
    """라벨이 없어도 제목만으로 요청 종류를 판단해야 한다 (이번 장애의 핵심 원인)."""
    assert pr.classify("[등록]") == "add"
    assert pr.classify("[등록] 채널 추가해주세요") == "add"
    assert pr.classify("[삭제]") == "remove"
    assert pr.classify("[삭제] 이 채널 빼주세요") == "remove"
    assert pr.classify("버그 신고") is None
    assert pr.classify("") is None


ADD_BODY_NAME_FIRST = """### 표시 이름

표시이름

### 채널 URL 또는 핸들

https://www.youtube.com/@example
"""


def test_field_extraction_is_order_independent():
    """이슈 폼에서 필드 순서를 바꿔도(표시 이름을 먼저 물어봐도) 그대로 읽혀야 한다."""
    assert pr.field(ADD_BODY_NAME_FIRST, pr.FIELD_ADD_URL) == "https://www.youtube.com/@example"
    assert pr.field(ADD_BODY_NAME_FIRST, pr.FIELD_ADD_NAME) == "표시이름"


def test_remove_target_prefilled_with_channel_id():
    """사이트의 삭제 버튼은 채널ID를 미리 채워 보낸다 -> 그대로 매칭되어야 한다."""
    cid = "UC" + "z" * 22
    body = f"### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)\n\n{cid}\n"
    channels = [{"id": cid, "name": "지울채널"}]
    ok, msg = pr.handle_remove({"body": body}, channels, resolver=lambda r: None)
    assert ok is True
    assert channels == []
    assert "지울채널" in msg


def test_field_extraction():
    assert pr.field(ADD_BODY, pr.FIELD_ADD_URL) == "https://www.youtube.com/@example"
    assert pr.field(ADD_BODY, pr.FIELD_ADD_NAME) == "표시이름"
    assert pr.field(ADD_BODY_NO_NAME, pr.FIELD_ADD_NAME) is None
    assert pr.field(REMOVE_BODY, pr.FIELD_REMOVE_TARGET) == "표시이름"


def test_handle_add_uses_display_name_when_given():
    channels = []
    ok, msg = pr.handle_add(
        {"body": ADD_BODY},
        channels,
        resolver=lambda raw: {"id": "UC" + "a" * 22, "name": "유튜브상이름"},
    )
    assert ok is True
    assert channels[0]["id"] == "UC" + "a" * 22
    assert channels[0]["name"] == "표시이름"
    assert "등록 완료" in msg


def test_handle_add_falls_back_to_youtube_name():
    channels = []
    ok, _ = pr.handle_add(
        {"body": ADD_BODY_NO_NAME},
        channels,
        resolver=lambda raw: {"id": "UC" + "b" * 22, "name": "유튜브상이름"},
    )
    assert ok is True
    assert channels[0]["name"] == "유튜브상이름"


def test_handle_add_rejects_duplicate():
    cid = "UC" + "c" * 22
    channels = [{"id": cid, "name": "이미있음"}]
    ok, msg = pr.handle_add(
        {"body": ADD_BODY}, channels, resolver=lambda raw: {"id": cid, "name": "이미있음"}
    )
    assert ok is False
    assert "이미 등록" in msg
    assert len(channels) == 1


def test_handle_add_reports_resolve_failure():
    channels = []
    def boom(raw):
        raise common.ParseError("채널 ID를 찾지 못했습니다")

    ok, msg = pr.handle_add({"body": ADD_BODY}, channels, resolver=boom)
    assert ok is False
    assert "확인하지 못했습니다" in msg
    assert channels == []


def test_handle_add_missing_url_field():
    ok, msg = pr.handle_add({"body": "### 표시 이름 (선택)\n\n아무거나\n"}, [], resolver=lambda r: {})
    assert ok is False
    assert "채널 URL" in msg


def test_handle_remove_by_display_name():
    cid = "UC" + "d" * 22
    channels = [{"id": cid, "name": "표시이름"}]
    ok, msg = pr.handle_remove({"body": REMOVE_BODY}, channels, resolver=lambda r: None)
    assert ok is True
    assert channels == []
    assert "삭제 완료" in msg


def test_handle_remove_by_channel_id_skips_network():
    cid = "UC" + "e" * 22
    body = f"### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)\n\n{cid}\n"
    channels = [{"id": cid, "name": "아무이름"}]

    def should_not_be_called(raw):
        raise AssertionError("채널ID 형식이면 네트워크 확인을 하지 않아야 한다")

    ok, _ = pr.handle_remove({"body": body}, channels, resolver=should_not_be_called)
    assert ok is True
    assert channels == []


def test_handle_remove_falls_back_to_resolving_handle():
    cid = "UC" + "f" * 22
    body = "### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)\n\nhttps://www.youtube.com/@someone\n"
    channels = [{"id": cid, "name": "전혀다른이름"}]
    ok, _ = pr.handle_remove(
        {"body": body}, channels, resolver=lambda raw: {"id": cid, "name": "전혀다른이름"}
    )
    assert ok is True
    assert channels == []


def test_handle_remove_not_found():
    channels = [{"id": "UC" + "g" * 22, "name": "다른채널"}]
    ok, msg = pr.handle_remove({"body": REMOVE_BODY}, channels, resolver=lambda r: None)
    assert ok is False
    assert "찾지 못했습니다" in msg
    assert len(channels) == 1


def test_find_channel_index_name_match_is_case_and_space_insensitive():
    channels = [{"id": "UC" + "h" * 22, "name": "  HanaNoKi Maru "}]
    assert pr.find_channel_index(channels, "hananoki maru") == 0
