#!/usr/bin/env python3
"""
""채널 등록된 유튜브 채널들의 시작하는 URL/핸들을
찾아내고, API 키 없이 페이지를 스크레이핑해서 채널ID+이름을 알아낸 뒤
data/channels.json 에 추가한다.

사용:
    python scripts/resolve_channel.py --issue-body-file body.txt \
        --github-output "$GITHUB_OUTPUT"

이슈 폼(.github/ISSUE_TEMPLATE/add-channel.yml)의 필드 label과
아래 FIELD_HEADING_* 정규식이 서로 맞아야 한다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import FetchError, ParseError, resolve_channel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CHANNELS_PATH = ROOT / "data" / "channels.json"

# GitHub Issue Form은 각 필드를 "### <label>\n\n<입력값>\n\n" 형태로 렌더링한다.
FIELD_CHANNEL_URL = re.compile(r"###\s*채널 URL 또는 핸들\s*\n+([^\n]+)")
FIELD_DISPLAY_NAME = re.compile(r"###\s*표시 이름\s*\(선택\)\s*\n+([^\n]+)")


def extract_field(body: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(body)
    if not m:
        return None
    value = m.group(1).strip()
    if not value or value == "_No response_":
        return None
    return value


def load_channels() -> dict:
    if CHANNELS_PATH.exists():
        return json.loads(CHANNELS_PATH.read_text(encoding="utf-8"))
    return {"channels": []}


def save_channels(doc: dict) -> None:
    CHANNELS_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_output(github_output: str | None, **kv):
    if not github_output:
        for k, v in kv.items():
            print(f"{k}={v}")
        return
    with open(github_output, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            # 값에 개행이 없다고 가정 (채널명에 개행 나올 일 거의 없음)
            f.write(f"{k}={v}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-body-file", required=True)
    ap.add_argument("--github-output", default=None)
    args = ap.parse_args()

    body = Path(args.issue_body_file).read_text(encoding="utf-8")
    raw_url = extract_field(body, FIELD_CHANNEL_URL)
    display_name_override = extract_field(body, FIELD_DISPLAY_NAME)

    if not raw_url:
        write_output(
            args.github_output,
            success="false",
            message="이슈 본문에서 '채널 URL 또는 핸들' 값을 찾지 못했습니다. 이슈 폼 형식 그대로 작성했는지 확인해주세요.",
        )
        return 0  # 워크플로 자체는 정상 종료시켜서 안내 코멘트를 달게 한다

    try:
        resolved = resolve_channel(raw_url)
    except (FetchError, ParseError) as e:
        write_output(
            args.github_output,
            success="false",
            message=f"'{raw_url}"' 에서 채널 정보를 가져오지 못했습니다: {e}",
        )
        return 0

    channels_doc = load_channels()
    channels = channels_doc.setdefault("channels", [])

    existing = next((c for c in channels if c.get("id") == resolved["id"]), None)
    display_name = display_name_override or resolved["name"]

    if existing:
        write_output(
            args.github_output,
            success="false",
            message=f"이미 등록되어 있는 채널입니다: {existing.get('name')} ({resolved['id']})",
        )
        return 0

    import datetime as dt

    channels.append(
        {
            "id": resolved["id"],
            "name": display_name,
            "addedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    save_channels(channels_doc)

    write_output(
        args.github_output,
        success="true",
        message=f"채널 완료: {display_name} ({resolved['id']})",
        channel_id=resolved["id"],
        channel_name=display_name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
