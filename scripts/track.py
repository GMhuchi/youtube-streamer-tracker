#!/usr/bin/env python3
"""
등록된 유튜브 채널들의 실시간 방송 여부를 확인하고, 태그 검색으로 신규
스트리머를 발견해서 docs/status.json 에 기록한다.

API 키를 전혀 쓰지 않는다 (common.py 참고). GitHub Actions에서
5~10분 간격으로 이 스크립트를 실행하는 것을 전제로 한다.

실행: python scripts/track.py
환경변수: 없음 (필요 없음)
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import history as history_mod  # noqa: E402
from common import (  # noqa: E402
    FetchError,
    ParseError,
    fetch_channel_live_status,
    fetch_channel_rss,
    fetch_video_live_details,
    polite_sleep,
    search_live_by_query,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

CHANNELS_PATH = DATA_DIR / "channels.json"
TAGS_PATH = DATA_DIR / "tags.json"
STATE_PATH = DATA_DIR / "state.json"
HISTORY_PATH = DATA_DIR / "history.json"
STATUS_PATH = DOCS_DIR / "status.json"
HISTORY_OUT_PATH = DOCS_DIR / "history.json"

# 채널이 많아지면 매 사이클 RSS까지 확인하면 요청 수가 2배가 된다.
# 라이브 확인은 매번, RSS(최근 업로드 정보)는 이 주기마다만 확인한다.
RSS_INTERVAL_CYCLES = 12


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"::warning::{path.name} 파싱 실패, 기본값 사용: {e}")
        return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_registered_channels(
    channels: list[dict],
    session: requests.Session,
    *,
    check_rss: bool = True,
    prev_by_id: dict | None = None,
) -> tuple[list[dict], list[str]]:
    results = []
    warnings = []
    prev_by_id = prev_by_id or {}
    for ch in channels:
        channel_id = ch.get("id")
        name = ch.get("name") or channel_id
        entry = {
            "id": channel_id,
            "name": name,
            "autoAdded": bool(ch.get("autoAdded")),
            "isLive": False,
            "title": None,
            "channelUrl": f"https://www.youtube.com/channel/{channel_id}",
            "videoUrl": None,
            "thumbnail": None,
            "viewers": None,
            "startedAt": None,
            "game": None,
            "videoId": None,
            "latestVideoTitle": None,
            "latestVideoAt": None,
            "lastCheckedAt": now_iso(),
            "error": None,
        }
        if not channel_id:
            entry["error"] = "채널 ID가 비어 있습니다 (data/channels.json 확인)"
            results.append(entry)
            continue

        # 최근 업로드(폴백 정보). RSS는 어디까지나 부가 정보라서:
        #  - 채널 수가 많으면 매 사이클 확인하지 않고(요청 수 절감) 이전 결과를 재사용하고,
        #  - 실패해도 사이트 상단 경고에는 띄우지 않고 실행 로그에만 남긴다.
        #    (유튜브가 이 피드를 막거나 채널에 공개 업로드가 없으면 404가 나는데,
        #     라이브 확인은 정상 동작하므로 사용자에게 경고할 일이 아니다.)
        prev = prev_by_id.get(channel_id) or {}
        entry["latestVideoTitle"] = prev.get("latestVideoTitle")
        entry["latestVideoAt"] = prev.get("latestVideoAt")
        if check_rss:
            try:
                rss_entries = fetch_channel_rss(channel_id, session=session)
                if rss_entries:
                    entry["latestVideoTitle"] = rss_entries[0].get("title")
                    entry["latestVideoAt"] = rss_entries[0].get("publishedAt")
            except (FetchError, ParseError) as e:
                print(f"[{name}] RSS 확인 실패(부가 정보라 무시): {e}")
            polite_sleep(0.3)

        # 실시간 방송 여부 - 핵심 체크
        try:
            live = fetch_channel_live_status(channel_id, session=session)
            entry["isLive"] = live.is_live
            entry["videoId"] = live.video_id
            if live.debug:
                print(f"[진단][{name}] 라이브={live.is_live} {live.debug}")
            if live.note:
                entry["error"] = live.note
                warnings.append(f"[{name}] {live.note}")
            if live.is_live:
                entry["title"] = live.title
                entry["videoUrl"] = f"https://www.youtube.com/watch?v={live.video_id}" if live.video_id else None
                entry["thumbnail"] = live.thumbnail
                entry["viewers"] = live.viewers
                entry["startedAt"] = live.started_at
                entry["game"] = live.game
        except FetchError as e:
            entry["error"] = f"확인 실패: {e}"
            warnings.append(f"[{name}] 라이브 상태 확인 실패: {e}")

        results.append(entry)
        polite_sleep(0.4)

    return results, warnings


def run_discovery(queries: list[dict], session: requests.Session, registered_ids: set[str]) -> tuple[dict, list[str]]:
    warnings = []
    items = []
    for q in queries:
        query_text = q.get("query")
        label = q.get("label") or query_text
        if not query_text:
            continue
        try:
            hits = search_live_by_query(query_text, session=session)
        except (FetchError, ParseError) as e:
            warnings.append(f"[검색: {label}] 실패: {e}")
            continue
        for h in hits:
            items.append(
                {
                    "matchedQuery": label,
                    "videoId": h.get("videoId"),
                    "videoUrl": f"https://www.youtube.com/watch?v={h.get('videoId')}" if h.get("videoId") else None,
                    "title": h.get("title"),
                    "channelId": h.get("channelId"),
                    "channelTitle": h.get("channelTitle"),
                    "thumbnail": h.get("thumbnail"),
                    "viewCountText": h.get("viewCountText"),
                    "alreadyRegistered": h.get("channelId") in registered_ids,
                }
            )
        polite_sleep(0.8)

    # 같은 채널이 여러 검색어에 걸리면 첫 번째만 남긴다
    dedup = {}
    for it in items:
        key = it.get("channelId")
        if key and key not in dedup:
            dedup[key] = it

    discovery = {
        "updatedAt": now_iso(),
        "queries": [q.get("label") or q.get("query") for q in queries],
        "items": list(dedup.values()),
    }
    return discovery, warnings


def auto_register_discovered(
    channels: list[dict],
    discovery: dict,
    *,
    keywords: list[str],
    max_channels: int,
    session: requests.Session,
    now: dt.datetime,
    verifier=None,
) -> tuple[list[dict], list[str]]:
    """태그 검색으로 발견한 '미등록' 채널 중 실제로 대상 게임을 방송 중인 채널을
    등록 목록에 자동으로 추가한다.

    검색 결과는 검색어에 걸렸을 뿐이라 실제 게임이 다를 수 있으므로, 후보마다
    워치 페이지를 한 번 확인해서 게임 이름/제목이 키워드와 맞을 때만 추가한다
    (후보 1개당 요청 1번, 이미 등록된 채널은 아예 확인하지 않는다).

    verifier: 테스트용 주입점. `verifier(video_id)` -> common.VideoLiveDetails|None.
    반환: (추가된 채널 목록, 경고 메시지 목록)
    """
    added: list[dict] = []
    warnings: list[str] = []
    items = (discovery or {}).get("items") or []
    if not items:
        return added, warnings

    known_ids = {c.get("id") for c in channels if c.get("id")}

    def verify(video_id: str):
        if verifier is not None:
            return verifier(video_id)
        try:
            return fetch_video_live_details(video_id, session=session)
        except (FetchError, ParseError) as e:
            warnings.append(f"[자동등록] 영상 {video_id} 확인 실패: {e}")
            return None

    for item in items:
        channel_id = item.get("channelId")
        video_id = item.get("videoId")
        if not channel_id or channel_id in known_ids:
            continue
        if len(channels) >= max_channels:
            warnings.append(
                f"등록 채널이 상한({max_channels}개)에 도달해 자동 추가를 멈췄습니다. "
                "data/tags.json 의 maxChannels 를 조절하거나 필요 없는 채널을 삭제해주세요."
            )
            break

        title = item.get("title")
        game = None
        if video_id:
            details = verify(video_id)
            if details is None:
                continue  # 확인 실패 -> 이번엔 건너뛰고 다음 사이클에 다시 시도
            game = getattr(details, "game", None)
            title = getattr(details, "title", None) or title

        if not history_mod.is_target_game(title, game, keywords):
            continue

        name = item.get("channelTitle") or channel_id
        entry = {
            "id": channel_id,
            "name": name,
            "autoAdded": True,
            "autoAddedAt": history_mod.to_iso(now),
            "autoAddedFrom": item.get("matchedQuery"),
            "autoAddedGame": game,
        }
        channels.append(entry)
        known_ids.add(channel_id)
        added.append(entry)
        polite_sleep(0.4)

    return added, warnings


def record_history(
    history: dict,
    channel_results: list[dict],
    *,
    now: dt.datetime,
    keywords: list[str],
    session: requests.Session,
) -> tuple[list[dict], list[str]]:
    """이번 사이클 관측 결과를 이력에 반영하고, 종료된 세션 목록을 돌려준다."""
    warnings: list[str] = []
    closed_sessions: list[dict] = []

    def end_lookup(video_id: str):
        # 방송이 끝난 직후 한 번만 호출된다(정확한 종료 시각 확보용).
        try:
            return fetch_video_live_details(video_id, session=session)
        except (FetchError, ParseError) as e:
            warnings.append(f"[{video_id}] 종료 시각 확인 실패(관측 시각으로 대체): {e}")
            return None

    for entry in channel_results:
        channel_id = entry.get("id")
        if not channel_id:
            continue
        if entry.get("error") and not entry.get("isLive"):
            # 확인 자체가 실패한 경우엔 "방송이 끝났다"고 단정하지 않는다
            # (일시적 오류로 세션이 잘못 끊기는 것을 막는다).
            continue
        closed = history_mod.update_channel(
            history,
            channel_id=channel_id,
            name=entry.get("name") or channel_id,
            is_live=bool(entry.get("isLive")),
            now=now,
            video_id=entry.get("videoId"),
            title=entry.get("title") or entry.get("latestVideoTitle"),
            game=entry.get("game"),
            viewers=entry.get("viewers"),
            started_at=entry.get("startedAt"),
            keywords=keywords,
            end_lookup=end_lookup,
        )
        if closed:
            closed_sessions.append(closed)

    return closed_sessions, warnings


def main() -> int:
    channels_doc = load_json(CHANNELS_PATH, {"channels": []})
    tags_doc = load_json(TAGS_PATH, {"queries": [], "discoveryIntervalCycles": 6})
    state = load_json(STATE_PATH, {"cycleCount": 0, "channels": {}, "discovery": {"lastRunAt": None}})
    history = load_json(HISTORY_PATH, history_mod.empty_history())

    channels = channels_doc.get("channels", [])
    queries = tags_doc.get("queries", [])
    interval_cycles = max(1, int(tags_doc.get("discoveryIntervalCycles", 6)))
    keywords = tags_doc.get("gameKeywords") or history_mod.DEFAULT_GAME_KEYWORDS

    cycle_count = int(state.get("cycleCount", 0))
    should_run_discovery = (cycle_count % interval_cycles == 0)
    should_check_rss = (cycle_count % RSS_INTERVAL_CYCLES == 0)

    now = dt.datetime.now(dt.timezone.utc)
    all_warnings: list[str] = []

    prev_status = load_json(STATUS_PATH, {})
    prev_by_id = {c.get("id"): c for c in (prev_status.get("channels") or []) if c.get("id")}
    newly_added: list[dict] = []

    with requests.Session() as session:
        channel_results, warn1 = check_registered_channels(
            channels, session, check_rss=should_check_rss, prev_by_id=prev_by_id
        )
        all_warnings.extend(warn1)

        closed_sessions, warn_hist = record_history(
            history, channel_results, now=now, keywords=keywords, session=session
        )
        all_warnings.extend(warn_hist)

        prev_discovery = prev_status.get("discovery", {
            "updatedAt": None,
            "queries": [q.get("label") or q.get("query") for q in queries],
            "items": [],
        })

        if should_run_discovery and queries:
            registered_ids = {c.get("id") for c in channels if c.get("id")}
            discovery, warn2 = run_discovery(queries, session, registered_ids)
            all_warnings.extend(warn2)

            # 미등록 채널 중 실제로 이터널리턴을 방송 중인 채널을 자동 등록
            if tags_doc.get("autoRegisterDiscovered"):
                newly_added, warn3 = auto_register_discovered(
                    channels,
                    discovery,
                    keywords=keywords,
                    max_channels=int(tags_doc.get("maxChannels", 60)),
                    session=session,
                    now=now,
                    )
                all_warnings.extend(warn3)
                if newly_added:
                    channels_doc["channels"] = channels
                    save_json(CHANNELS_PATH, channels_doc)
                    # 방금 추가된 채널도 이번 사이클부터 바로 확인한다
                    extra_results, warn4 = check_registered_channels(
                        newly_added, session, check_rss=False, prev_by_id=prev_by_id
                    )
                    all_warnings.extend(warn4)
                    channel_results.extend(extra_results)
                    closed_extra, warn5 = record_history(
                        history, extra_results, now=now, keywords=keywords, session=session
                    )
                    all_warnings.extend(warn5)
                    closed_sessions.extend(closed_extra)
                    # 자동 추가된 채널은 이미 등록되었으므로 "등록됨" 표시를 갱신한다
                    added_ids = {c["id"] for c in newly_added}
                    for item in discovery.get("items", []):
                        if item.get("channelId") in added_ids:
                            item["alreadyRegistered"] = True
        else:
            discovery = prev_discovery  # 이번 사이클엔 검색 안 함 -> 이전 결과 유지

    history_mod.prune(history, now)
    save_json(HISTORY_PATH, history)

    summary = history_mod.aggregate(history, channels, now, keywords=keywords)
    save_json(HISTORY_OUT_PATH, summary)

    status = {
        "updatedAt": now_iso(),
        "ok": True,
        "warning": "; ".join(all_warnings) if all_warnings else None,
        "channels": sorted(
            channel_results,
            key=lambda c: (not c["isLive"], -(c.get("viewers") or 0)),
        ),
        "discovery": discovery,
        "historySummary": {
            "updatedAt": summary["updatedAt"],
            "totals": summary["totals"],
        },
    }

    save_json(STATUS_PATH, status)

    state["cycleCount"] = cycle_count + 1
    if should_run_discovery and queries:
        state["discovery"] = {"lastRunAt": status["updatedAt"]}
    save_json(STATE_PATH, state)

    live_count = sum(1 for c in channel_results if c["isLive"])
    print(f"완료: 등록 채널 {len(channels)}개 중 {live_count}개 라이브 중.")
    for c in newly_added:
        print(f"자동 등록: {c['name']} ({c['id']}) - 게임 {c.get('autoAddedGame') or '제목으로 판정'}")
    if closed_sessions:
        for s in closed_sessions:
            print(f"방송 종료 기록: {s.get('name')} {s.get('start')} ~ {s.get('end')} ({s.get('minutes')}분)")
    print(
        "누적(오늘/이번주/이번달, 시간): "
        f"{summary['totals']['today']['totalHours']} / "
        f"{summary['totals']['week']['totalHours']} / "
        f"{summary['totals']['month']['totalHours']}"
    )
    if should_run_discovery:
        print(f"이번 사이클에 태그 검색 실행함 (발견 {len(discovery.get('items', []))}건).")
    for w in all_warnings:
        print(f"::warning::{w}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
