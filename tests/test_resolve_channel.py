import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import resolve_channel as rc  # noqa: E402
import common  # noqa: E402


SAMPLE_ISSUE_BODY = """### 채널 URL 또는 핸들

https://www.youtube.com/@example

### 표시 이름 (선택)

_No response_
"""

SAMPLE_ISSUE_BODY_WITH_NAME = """### 채널 URL 또는 핸들

@example2

### 표시 이름 (선택)

커스텀표시이름
"""


def test_extract_field_parses_issue_form_body():
    url = rc.extract_field(SAMPLE_ISSUE_BODY, rc.FIELD_CHANNEL_URL)
    name = rc.extract_field(SAMPLE_ISSUE_BODY, rc.FIELD_DISPLAY_NAME)
    assert url == "https://www.youtube.com/@example"
    assert name is None  # "_No response_" 는 미입력으로 취급


def test_extract_field_with_custom_name():
    name = rc.extract_field(SAMPLE_ISSUE_BODY_WITH_NAME, rc.FIELD_DISPLAY_NAME)
    assert name == "커스텀표시이름"


def test_main_adds_new_channel(tmp_path, monkeypatch):
    body_file = tmp_path / "body.txt"
    body_file.write_text(SAMPLE_ISSUE_BODY, encoding="utf-8")

    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    monkeypatch.setattr(rc, "CHANNELS_PATH", channels_path)

    monkeypatch.setattr(rc, "resolve_channel", lambda raw: {"id": "UCNEW12345678901234567", "name": "새채널"})

    output_path = tmp_path / "gh_output.txt"
    monkeypatch.setattr(
        sys, "argv",
        ["resolve_channel.py", "--issue-body-file", str(body_file), "--github-output", str(output_path)],
    )
    rc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=true" in out
    assert "channel_id=UCNEW12345678901234567" in out

    channels_doc = json.loads(channels_path.read_text(encoding="utf-8"))
    assert channels_doc["channels"][0]["id"] == "UCNEW12345678901234567"
    assert channels_doc["channels"][0]["name"] == "새채널"


def test_main_rejects_duplicate_channel(tmp_path, monkeypatch):
    body_file = tmp_path / "body.txt"
    body_file.write_text(SAMPLE_ISSUE_BODY, encoding="utf-8")

    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps({"channels": [{"id": "UCEXISTING000000000000", "name": "이미있음"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(rc, "CHANNELS_PATH", channels_path)
    monkeypatch.setattr(rc, "resolve_channel", lambda raw: {"id": "UCEXISTING000000000000", "name": "이미있음"})

    output_path = tmp_path / "gh_output.txt"
    monkeypatch.setattr(
        sys, "argv",
        ["resolve_channel.py", "--issue-body-file", str(body_file), "--github-output", str(output_path)],
    )
    rc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=false" in out
    assert "이미 등록" in out


def test_main_reports_resolve_failure(tmp_path, monkeypatch):
    body_file = tmp_path / "body.txt"
    body_file.write_text(SAMPLE_ISSUE_BODY, encoding="utf-8")

    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    monkeypatch.setattr(rc, "CHANNELS_PATH", channels_path)

    def boom(raw):
        raise common.ParseError("채널 ID를 찾을 수 없음")

    monkeypatch.setattr(rc, "resolve_channel", boom)

    output_path = tmp_path / "gh_output.txt"
    monkeypatch.setattr(
        sys, "argv",
        ["resolve_channel.py", "--issue-body-file", str(body_file), "--github-output", str(output_path)],
    )
    rc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=false" in out


def test_main_missing_url_field(tmp_path, monkeypatch):
    body_file = tmp_path / "body.txt"
    body_file.write_text("### 표시 이름 (선택)\n\n_No response_\n", encoding="utf-8")

    output_path = tmp_path / "gh_output.txt"
    monkeypatch.setattr(
        sys, "argv",
        ["resolve_channel.py", "--issue-body-file", str(body_file), "--github-output", str(output_path)],
    )
    rc.main()

    out = output_path.read_text(encoding="utf-8")
    assert "success=false" in out
