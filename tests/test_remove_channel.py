import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import remove_channel as rmc  # noqa: E402
import common  # noqa: E402


SAMPLE_ISSUE_BODY_NAME = """### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)

  홍길동
"""

SAMPLE_ISSUE_BODY_ID = """### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)

UCEXISTING00000000000000
"""

SAMPLE_ISSUE_BODY_HANDLE = """### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)

https://www.youtube.com/@example
"""

SAMPLE_ISSUE_BODY_EMPTY = """### 삭제할 채널 (표시 이름 또는 URL/핸들/채널ID)

_No response_
"""


def _write_channels(tmp_path, channels):
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps({"_설명": "설명", "channels": channels}, ensure_ascii=False), encoding="utf-8"
    )
    return channels_path


def _run(monkeypatch, body_text, tmp_path):
    body_file = tmp_path / "body.txt"
    body_file.write_text(body_text, encoding="utf-8")
    output_path = tmp_path / "gh_output.txt"
    monkeypatch.setattr(
        sys, "argv",
        ["remove_channel.py", "--issue-body-file", str(body_file), "--github-output", str(output_path)],
    )
    return output_path


def test_extract_field_parses_target():
    value = rmc.extract_field(SAMPLE_ISSUE_BODY_NAME, rmc.FIELD_TARGET)
    assert value == "홍길동"


def test_extract_field_no_response_is_none():
    value = rmc.extract_field(SAMPLE_ISSUE_BODY_EMPTY, rmc.FIELD_TARGET)
    assert value is None


def test_find_channel_index_by_id():
    channels = [{"id": "UCEXISTING00000000000000", "name": "이미있음"}]
    idx = rmc.find_channel_index(channels, "UCEXISTING00000000000000")
    assert idx == 0


def test_find_channel_index_by_name_case_and_space_insensitive():
    channels = [{"id": "UCEXISTING000000000000", "name": "홍길동"}]
    idx = rmc.find_channel_index(channels, "  홍길동  ")
    assert idx == 0

    channels_en = [{"id": "UCEXISTING000000000001", "name": "HongGilDong"}]
    idx_en = rmc.find_channel_index(channels_en, "honggildong")
    assert idx_en == 0


def test_find_channel_index_not_found_returns_none():
    channels = [{"id": "UCEXISTING000000000000", "name": "이미있음"}]
    idx = rmc.find_channel_index(channels, "다른이름")
    assert idx is None


def test_main_removes_by_display_name(tmp_path, monkeypatch):
    channels_path = _write_channels(tmp_path, [{"id": "UCEXISTING000000000000", "name": "홍길동"}])
    monkeypatch.setattr(rmc, "CHANNELS_PATH", channels_path)

    output_path = _run(monkeypatch, SAMPLE_ISSUE_BODY_NAME, tmp_path)
    rmc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=true" in out
    assert "channel_id=UCEXISTING000000000000" in out

    doc = json.loads(channels_path.read_text(encoding="utf-8"))
    assert doc["channels"] == []


def test_main_removes_by_channel_id(tmp_path, monkeypatch):
    channels_path = _write_channels(tmp_path, [{"id": "UCEXISTING00000000000000", "name": "이미있음"}])
    monkeypatch.setattr(rmc, "CHANNELS_PATH", channels_path)

    output_path = _run(monkeypatch, SAMPLE_ISSUE_BODY_ID, tmp_path)
    rmc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=true" in out

    doc = json.loads(channels_path.read_text(encoding="utf-8"))
    assert doc["channels"] == []


def test_main_falls_back_to_resolve_for_handle(tmp_path, monkeypatch):
    channels_path = _write_channels(tmp_path, [{"id": "UCRESOLVEDID000000000", "name": "예시채널"}])
    monkeypatch.setattr(rmc, "CHANNELS_PATH", channels_path)
    monkeypatch.setattr(
        rmc, "resolve_channel", lambda raw: {"id": "UCRESOLVEDID000000000", "name": "예시채널"}
    )

    output_path = _run(monkeypatch, SAMPLE_ISSUE_BODY_HANDLE, tmp_path)
    rmc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=true" in out

    doc = json.loads(channels_path.read_text(encoding="utf-8"))
    assert doc["channels"] == []


def test_main_reports_not_found_when_resolve_fails(tmp_path, monkeypatch):
    channels_path = _write_channels(tmp_path, [{"id": "UCOTHERID0000000000000", "name": "다른채널"}])
    monkeypatch.setattr(rmc, "CHANNELS_PATH", channels_path)

    def boom(raw):
        raise common.ParseError("채널 ID를 찾을 수 없음")

    monkeypatch.setattr(rmc, "resolve_channel", boom)

    output_path = _run(monkeypatch, SAMPLE_ISSUE_BODY_HANDLE, tmp_path)
    rmc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=false" in out


def test_main_channel_id_not_registered_skips_resolve(tmp_path, monkeypatch):
    channels_path = _write_channels(tmp_path, [{"id": "UCOTHERID000000000000000", "name": "다른채널"}])
    monkeypatch.setattr(rmc, "CHANNELS_PATH", channels_path)

    def should_not_be_called(raw):
        raise AssertionError("채널ID 형식일 때는 resolve_channel을 호출하면 안 됩니다")

    monkeypatch.setattr(rmc, "resolve_channel", should_not_be_called)

    output_path = _run(monkeypatch, SAMPLE_ISSUE_BODY_ID, tmp_path)
    rmc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=false" in out


def test_main_missing_field(tmp_path, monkeypatch):
    channels_path = _write_channels(tmp_path, [])
    monkeypatch.setattr(rmc, "CHANNELS_PATH", channels_path)

    output_path = _run(monkeypatch, SAMPLE_ISSUE_BODY_EMPTY, tmp_path)
    rmc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=false" in out
