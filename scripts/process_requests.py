#!/usr/bin/env python3
"""
열려 있는 "[등록]" / "[삭제]" 이슈를 모아서 한 번에 처리한다.

왜 이 스크립트가 필요한가
------------------------
처음에는 이슈 폼의 `labels:` 값(add-channel / remove-channel)에 의존해서
워크플로를 실행했는데, **GitHub는 저장소에 그 라벨이 미리 만들어져 있지 않으면
이슈 폼의 라벨을 조용히 무시한다.** 그래서 라벨이 안 붙고, 라벨 조건이 걸린
워크플로가 한 번도 실행되지 않아 등록 요청이 쌓여만 있었다.

그래서 이제는
  1) 라벨에 의존하지 않고 **이슈 제목의 접두사**([등록]/[삭제])로 판단하고,
  2) 이슈 이벤트를 놓쳐도 괜찮도록 **주기적으로 열린 이슈를 훑어서(sweep)** 처리한다.
이렇게 하면 이벤트가 유실되거나 라벨이 없어도 다음 확인 주기에 반드시 처리된다.

사용:
    python scripts/process_requests.py                # 열린 요청 전부 처리
    python scripts/process_requests.py --issue 42     # 특정 이슈만 처리
    python scripts/process_requests.py --dry-run      # 파일/이슈 변경 없이 확인만

환경변수:
    GITHUB_TOKEN      (Actions의 ${{ github.token }})
    GITHUB_REPOSITORY (예: owner/repo)
    GITHUB_OUTPUT     (선택) 변경 여부를 changed=true/false 로 기록
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from common import CHANNEL_ID_RE, FetchError, ParseError, resolve_channel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CHANNELS_PATH = ROOT / "data" / "channels.json"

API = "https://api.github.com"

ADD_PREFIXES = ("[등록]", "[등록 ]", "[add]")
REMOVE_PREFIXES = ("[삭제]", "[삭제 ]", "[remove]")

# 이슈 폼은 각 필드를 "### <label>\n\n<입력값>" 형태로 렌더링한다.
FIELD_ADD_URL = re.compile(r"###\s*채널 URL 또는 핸들\s*\n+([^\n]+)")
FIELD_ADD_NAME = re.compile(r"###\s*표시 이름[^\n]*\n+([^\n]+)")
FIELD_REMOVE_TARGET = re.compile(r"###\s*삭제할 채널[^\n]*\n+([^\n]+)")


def field(body: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(body or "")
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


def write_output(github_output: str | None, **kv) -> None:
    if not github_output:
        for k, v in kv.items():
            print(f"{k}={v}")
        return
    with open(github_output, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


class Gh:
    """GitHub REST API 최소 래퍼. 토큰이 없으면 읽기만 시도한다."""

    def __init__(self, repo: str, token: str | None):
        self.repo = repo
        self.token = token
        self.session = requests.Session()
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def list_open_issues(self) -> list[dict]:
        issues: list[dict] = []
        page = 1
        while page <= 5:  # 최대 500건이면 충분
            r = self.session.get(
                f"{API}/repos/{self.repo}/issues",
                params={"state": "open", "per_page": 100, "page": page},
                timeout=20,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            issues.extend([i for i in batch if "pull_request" not in i])
            if len(batch) < 100:
                break
            page += 1
        return issues

    def get_issue(self, number: int) -> dict:
        r = self.session.get(f"{API}/repos/{self.repo}/issues/{number}", timeout=20)
        r.raise_for_status()
        return r.json()

    def has_comment_containing(self, number: int, needle: str) -> bool:
        """같은 안내를 매 사이클 반복해서 달지 않도록 기존 코멘트를 확인한다."""
        try:
            r = self.session.get(
                f"{API}/repos/{self.repo}/issues/{number}/comments",
                params={"per_page": 100},
                timeout=20,
            )
            r.raise_for_status()
        except requests.RequestException:
            return False
        return any(needle in (c.get("body") or "") for c in r.json())

    def comment(self, number: int, body: str) -> None:
        if not self.token:
            print(f"(토큰 없음) #{number} 코멘트 생략: {body[:60]}")
            return
        r = self.session.post(
            f"{API}/repos/{self.repo}/issues/{number}/comments", json={"body": body}, timeout=20
        )
        if r.status_code >= 400:
            print(f"::warning::#{number} 코멘트 실패: HTTP {r.status_code} {r.text[:200]}")

    def close(self, number: int) -> None:
        if not self.token:
            print(f"(토큰 없음) #{number} 닫기 생략")
            return
        r = self.session.patch(
            f"{API}/repos/{self.repo}/issues/{number}", json={"state": "closed"}, timeout=20
        )
        if r.status_code >= 400:
            print(f"::warning::#{number} 닫기 실패: HTTP {r.status_code} {r.text[:200]}")


def classify(title: str) -> str | None:
    t = (title or "").strip()
    for p in ADD_PREFIXES:
        if t.lower().startswith(p.lower()):
            return "add"
    for p in REMOVE_PREFIXES:
        if t.lower().startswith(p.lower()):
            return "remove"
    return None


def find_channel_index(channels: list[dict], raw: str) -> int | None:
    """채널ID 완전일치 -> 표시 이름(대소문자/공백 무시) 일치 순으로 찾는다."""
    raw_stripped = (raw or "").strip()
    if not raw_stripped:
        return None
    if CHANNEL_ID_RE.match(raw_stripped):
        for i, c in enumerate(channels):
            if c.get("id") == raw_stripped:
                return i
        return None
    normalized = raw_stripped.casefold()
    for i, c in enumerate(channels):
        name = (c.get("name") or "").strip().casefold()
        if name and name == normalized:
            return i
    return None


def handle_add(issue: dict, channels: list[dict], resolver=resolve_channel) -> tuple[bool, str]:
    body = issue.get("body") or ""
    raw_url = field(body, FIELD_ADD_URL)
    display_name = field(body, FIELD_ADD_NAME)

    if not raw_url:
        return False, "이슈 본문에서 '채널 URL 또는 핸들' 값을 찾지 못했습니다. 이슈 폼 형식대로 작성해주세요."

    try:
        resolved = resolver(raw_url)
    except (FetchError, ParseError) as e:
        return False, f"채널을 확인하지 못했습니다: {e}"

    channel_id = resolved.get("id")
    if not channel_id:
        return False, f"'{raw_url}' 에서 채널 ID를 찾지 못했습니다."

    for c in channels:
        if c.get("id") == channel_id:
            return False, f"이미 등록된 채널입니다: {c.get('name')} ({channel_id})"

    name = display_name or resolved.get("name") or channel_id
    channels.append({"id": channel_id, "name": name, "sourceUrl": raw_url})
    return True, f"등록 완료: {name} ({channel_id})"


def handle_remove(issue: dict, channels: list[dict], resolver=resolve_channel) -> tuple[bool, str]:
    body = issue.get("body") or ""
    target = field(body, FIELD_REMOVE_TARGET)
    if not target:
        return False, "이슈 본문에서 '삭제할 채널' 값을 찾지 못했습니다. 이슈 폼 형식대로 작성해주세요."

    idx = find_channel_index(channels, target)
    looks_like_id = bool(CHANNEL_ID_RE.match(target.strip()))

    if idx is None and not looks_like_id:
        try:
            resolved = resolver(target)
        except (FetchError, ParseError):
            resolved = None
        if resolved:
            idx = next((i for i, c in enumerate(channels) if c.get("id") == resolved.get("id")), None)

    if idx is None:
        return False, (
            f"'{target}' 와(과) 일치하는 등록된 채널을 찾지 못했습니다. "
            "사이트에 표시된 채널 이름을 정확히 복사해서 다시 요청해주세요."
        )

    removed = channels.pop(idx)
    return True, f"삭제 완료: {removed.get('name')} ({removed.get('id')})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", type=int, default=None, help="이 번호의 이슈만 처리")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = ap.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo:
        print("::error::GITHUB_REPOSITORY 환경변수가 필요합니다 (owner/repo).")
        return 1

    gh = Gh(repo, token)

    if args.issue:
        issues = [gh.get_issue(args.issue)]
    else:
        issues = gh.list_open_issues()

    # 오래된 요청부터 처리 (등록/삭제 순서가 뒤바뀌지 않도록)
    issues.sort(key=lambda i: i.get("number") or 0)

    doc = load_channels()
    channels = doc.setdefault("channels", [])
    before = json.dumps(channels, ensure_ascii=False, sort_keys=True)

    handled = 0
    results: list[str] = []

    for issue in issues:
        number = issue.get("number")
        if issue.get("state") != "open":
            continue
        kind = classify(issue.get("title") or "")
        if not kind:
            continue

        if kind == "add":
            ok, message = handle_add(issue, channels)
        else:
            ok, message = handle_remove(issue, channels)

        handled += 1
        icon = "✅" if ok else "⚠️"
        results.append(f"#{number} {icon} {message}")
        print(f"#{number} {icon} {message}")

        if args.dry_run:
            continue

        if ok:
            gh.comment(
                number,
                f"{icon} {message}\n\n다음 확인 주기(최대 10분 이내)부터 사이트에 반영됩니다.",
            )
            gh.close(number)
        else:
            # 실패한 요청은 사용자가 고칠 수 있게 열어둔다. 다만 같은 안내를
            # 매 사이클 반복해서 달면 시끄러우니, 이미 같은 안내가 있으면 건너뛴다.
            if not gh.has_comment_containing(number, message):
                gh.comment(
                    number,
                    f"{icon} {message}\n\n내용을 수정하시면 다음 확인 주기에 자동으로 다시 시도합니다.",
                )

    after = json.dumps(channels, ensure_ascii=False, sort_keys=True)
    changed = before != after

    if changed and not args.dry_run:
        save_channels(doc)

    write_output(args.github_output, changed="true" if changed else "false", handled=str(handled))
    print(f"처리한 요청 {handled}건, 채널 목록 변경: {changed}, 현재 등록 채널 {len(channels)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
