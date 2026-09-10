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

        # 최근 업로드(폴백 정보) - RSS는 실패해도 치명적이지 않음.
        # 채널 수가 많으면 매 사이클 확인하지 않고 이전 결과를 재사용한다.
        prev = prev_by_id.get(channel_id) or {}
        if check_rss or not prev.get("latestVideoTitle"):
            try:
                rss_entries = fetch_channel_rss(channel_id, session=session)
                if rss_entries:
                    entry["latestVideoTitle"] = rss_entries[0].get("title")
                    entry["latestVideoAt"] = rss_entries[0].get("publishedAt")
            except (FetchError, ParseError) as e:
                warnings.append(f"[{name}] RSS 확인 실패: {e}")
            polite_sleep(0.3)
        else:
            entry["latestVideoTitle"] = prev.get("latestVideoTitle")
            entry["latestVideoAt"] = prev.get("latestVideoAt")

        # 실시간 방송 여부 - 핵심 체크
        try:
            live = fetch_channel_live_status(channel_id, session=session)
            entry["isLive"] = live.is_live
            entry["videoId"] = live.video_id
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
