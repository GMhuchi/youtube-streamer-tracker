#!/usr/bin/env python3
"""
"채널 삭제 요청" 이슈(GitHub Issue Form)의 본문을 읽어서 data/channels.json
에서 해당 채널을 찾아 제거한다.

사용:
    python scripts/remove_channel.py --issue-body-file body.txt \
        --github-output "$GITHUB_OUTPUT"

이슈 폼(.github/ISSUE_TEMPLATE/remove-channel.yml)의 필드 label과
아래 FIELD_TARGET 정규식이 서로 맞아야 한다.

매칭 우선순위 (네트워크 요청 없이 되도록 빠르게 처리):
  1. 입력값이 채널 ID(UC로 시작하는 24자리)와 정확히 같은 채널
  2. 입력값이 등록된 채널의 표시 이름(name)과 대소문자/공백 무시하고 같은 채널
  3. 위 두 가지로 못 찾으면, URL/핸들로 보고 실제로 접속해 채널ID를 알아낸 뒤 매칭
     (채널이 이미 사라졌거나 핸들이 바뀌었으면 이 단계는 실패할 수 있음 -> 그런
     경우엔 1번이나 2번 방식으로 다시 요청하도록 안내한다)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import CHANNEL_ID_RE, FetchError, ParseError, resolve_channel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CHANNELS_PATH = ROOT / "data" / "channels.json"

# GitHub Issue Form은 각 필드를 "### <label>\n\n<입력값>\n\n" 형태로 렌더링한다.
FIELD_TARGET = re.compile(r"###\s*삭제할 채널\s*\(표시 이름 또는 URL/핸들/채널ID\)\s*\n+([^\n]+)")


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
            f.write(f"{k}={v}\n")


def find_channel_index(channels: list[dict], raw: str) -> int | None:
    raw_stripped = raw.strip()

    # 1) 채널 ID 그대로 일치
    if CHANNEL_ID_RE.match(raw_stripped):
        for i, c in enumerate(channels):
            if c.get("id") == raw_stripped:
                return i
        return None  # 채널ID 형식이지만 목록에 없음 -> 더 진행할 필요 없음

    # 2) 표시 이름과 대소문자/앞뒤공백 무시하고 일치
    normalized = raw_stripped.casefold()
    for i, c in enumerate(channels):
        name = (c.get("name") or "").strip().casefold()
        if name and name == normalized:
            return i

    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-body-file", required=True)
    ap.add_argument("--github-output", default=None)
    args = ap.parse_args()

    body = Path(args.issue_body_file).read_text(encoding="utf-8")
    raw_target = extract_field(body, FIELD_TARGET)

    if not raw_target:
        write_output(
            args.github_output,
            success="false",
            message="이슈 본문에서 '삭제할 채널' 값을 찾지 못했습니다. 이슈 폼 형식 그대로 작성했는지 확인해주세요.",
        )
        return 0

    channels_doc = load_channels()
    channels = channels_doc.setdefault("channels", [])

    idx = find_channel_index(channels, raw_target)
    looks_like_channel_id = bool(CHANNEL_ID_RE.match(raw_target.strip()))

    if idx is None and not looks_like_channel_id:
        # 3) URL/핸들일 수 있으니 실제로 접속해서 채널ID를 알아내 다시 매칭 시도
        try:
            resolved = resolve_channel(raw_target)
        except (FetchError, ParseError):
            resolved = None

        if resolved:
            idx = next((i for i, c in enumerate(channels) if c.get("id") == resolved["id"]), None)

    if idx is None:
            write_output(
                args.github_output,
                success="false",
                message=(
                    f"'{raw_target}' 와(과) 일치하는 등록된 채널을 찾지 못했습니다. "
                    "사이트에 표시된 채널 이름을 정확히 복사해서 다시 시도해주세요."
                ),
            )
            return 0

    removed = channels.pop(idx)
    save_channels(channels_doc)

    write_output(
        args.github_output,
        success="true",
        message=f"삭제 완료: {removed.get('name')} ({removed.get('id')})",
        channel_id=removed.get("id") or "",
        channel_name=removed.get("name") or "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
