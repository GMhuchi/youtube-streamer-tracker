#!/usr/bin/env python3
"""
방송 이력(언제 켜고 언제 껐는지) 기록 + 일/주/월 누적 방송시간 집계.

설계
----
- `data/history.json` 에 채널별로 "지금 진행 중인 방송(open)" 과 "끝난 방송
  기록(sessions)" 을 쌓는다.
- 매 확인 사이클마다 track.py 가 라이브 상태를 넘겨주고, 이 모듈이 상태 전이를
  판단한다:
    오프라인 -> 라이브 : 새 세션 시작 (시작 시각은 유튜브가 알려주는
                         liveBroadcastDetails.startTimestamp 를 우선 사용)
    라이브   -> 라이브 : 진행 중 세션 갱신 (최고 시청자수, 제목, 게임)
    라이브   -> 오프라인: 세션 종료. 이때 정확한 종료 시각을 알아내기 위해
                         해당 영상 워치 페이지를 한 번만 더 확인한다
                         (확인 주기가 5~10분이라 그냥 "지금"으로 적으면 오차가 큼).
- 시작/종료 시각은 유튜브 값이 있으면 그걸 쓰고, 없으면 관측 시각으로 대체한다.
  어느 쪽을 썼는지 `startSource` / `endSource` 에 남겨서 나중에 신뢰도를 알 수 있게 한다.

집계는 한국 시간(KST, UTC+9) 기준이며 "오늘 / 이번 주(월~일) / 이번 달" 세 구간을
쓴다. 세션이 구간 경계를 걸치면 겹치는 만큼만 더한다(자정을 넘긴 방송도 정확히
쪼개서 계산).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Optional

KST = dt.timezone(dt.timedelta(hours=9))

# 이터널리턴 방송으로 볼 키워드(기본값). data/tags.json 의 gameKeywords 로 덮어쓸 수 있다.
DEFAULT_GAME_KEYWORDS = [
    "이터널리턴",
    "이터널 리턴",
    "エターナルリターン",
    "エタリタ",
    "eternal return",
    "eternalreturn",
]

# 보관 정책: 너무 오래된 기록/과도한 개수는 잘라내 파일이 무한히 커지지 않게 한다.
MAX_SESSION_AGE_DAYS = 400
MAX_SESSIONS = 5000


def parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    """유튜브/자체 기록의 ISO8601 문자열을 timezone-aware UTC datetime 으로."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def to_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_target_game(title: Optional[str], game: Optional[str], keywords: Iterable[str]) -> bool:
    """제목 또는 게임 메타데이터가 대상 게임(기본: 이터널리턴)인지 판단.

    게임 메타데이터가 잡히면 그게 가장 정확하고, 없으면 제목 키워드로 추정한다.
    """
    normalized = [k.strip().casefold() for k in keywords if k and k.strip()]
    if not normalized:
        return False
    for source in (game, title):
        if not source:
            continue
        haystack = re.sub(r"\s+", " ", str(source)).casefold()
        for kw in normalized:
            if kw in haystack or kw.replace(" ", "") in haystack.replace(" ", ""):
                return True
    return False


def empty_history() -> dict:
    return {"version": 1, "open": {}, "sessions": []}


def update_channel(
    history: dict,
    *,
    channel_id: str,
    name: str,
    is_live: bool,
    now: dt.datetime,
    video_id: Optional[str] = None,
    title: Optional[str] = None,
    game: Optional[str] = None,
    viewers: Optional[int] = None,
    started_at: Optional[str] = None,
    keywords: Optional[Iterable[str]] = None,
    end_lookup=None,
) -> Optional[dict]:
    """한 채널의 이번 사이클 관측 결과를 반영한다.

    end_lookup: 방송이 끝났을 때 정확한 종료 시각을 알아내기 위한 콜백.
        `end_lookup(video_id)` -> common.VideoLiveDetails 또는 None.
        (네트워크를 쓰므로 테스트에서는 주입하지 않거나 가짜를 넣는다)

    반환값: 이번 사이클에 "종료된" 세션이 있으면 그 세션 dict, 없으면 None.
    """
    kws = list(keywords) if keywords is not None else DEFAULT_GAME_KEYWORDS
    open_map = history.setdefault("open", {})
    sessions = history.setdefault("sessions", [])
    current = open_map.get(channel_id)

    if is_live:
        yt_start = parse_iso(started_at)
        if current and video_id and current.get("videoId") and current["videoId"] != video_id:
            # 같은 채널이 방송을 껐다가 다시 켠 경우(영상이 바뀜) -> 이전 세션을 먼저 닫는다
            closed = _close_session(
                history, channel_id, current, now=now, end_lookup=end_lookup, fallback_end=yt_start or now
            )
            current = None
        else:
            closed = None

        if not current:
            start = yt_start or now
            current = {
                "channelId": channel_id,
                "name": name,
                "videoId": video_id,
                "start": to_iso(start),
                "startSource": "youtube" if yt_start else "observed",
                "title": title,
                "game": game,
                "peakViewers": viewers,
                "isTargetGame": is_target_game(title, game, kws),
                "firstSeenAt": to_iso(now),
                "lastSeenAt": to_iso(now),
            }
            open_map[channel_id] = current
            return closed

        # 진행 중 세션 갱신
        current["name"] = name
        current["lastSeenAt"] = to_iso(now)
        if title:
            current["title"] = title
        if game:
            current["game"] = game
        if video_id:
            current["videoId"] = video_id
        if yt_start:
            current["start"] = to_iso(yt_start)
            current["startSource"] = "youtube"
        if viewers is not None:
            prev_peak = current.get("peakViewers")
            current["peakViewers"] = viewers if prev_peak is None else max(prev_peak, viewers)
        # 방송 중 제목이 바뀌어 게임이 확인되는 경우가 있어 매번 다시 판정한다
        if is_target_game(current.get("title"), current.get("game"), kws):
            current["isTargetGame"] = True
        return closed

    # 오프라인
    if not current:
        return None
    return _close_session(history, channel_id, current, now=now, end_lookup=end_lookup)


def _close_session(
    history: dict,
    channel_id: str,
    current: dict,
    *,
    now: dt.datetime,
    end_lookup=None,
    fallback_end: Optional[dt.datetime] = None,
) -> dict:
    open_map = history.setdefault("open", {})
    sessions = history.setdefault("sessions", [])

    end = fallback_end or now
    end_source = "observed"
    video_id = current.get("videoId")
    if end_lookup and video_id:
        try:
            details = end_lookup(video_id)
        except Exception:  # noqa: BLE001 - 종료 시각 확인 실패가 전체를 멈추면 안 됨
            details = None
        if details is not None:
            yt_end = parse_iso(getattr(details, "end_timestamp", None))
            if yt_end:
                end = yt_end
                end_source = "youtube"
            yt_game = getattr(details, "game", None)
            if yt_game and not current.get("game"):
                current["game"] = yt_game

    start = parse_iso(current.get("start")) or end
    if end < start:
        end = start
    minutes = round((end - start).total_seconds() / 60, 1)

    session = {
        "channelId": channel_id,
        "name": current.get("name"),
        "videoId": video_id,
        "videoUrl": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        "start": to_iso(start),
        "end": to_iso(end),
        "minutes": minutes,
        "startSource": current.get("startSource", "observed"),
        "endSource": end_source,
        "title": current.get("title"),
        "game": current.get("game"),
        "peakViewers": current.get("peakViewers"),
        "isTargetGame": bool(current.get("isTargetGame")),
    }
    sessions.append(session)
    open_map.pop(channel_id, None)
    return session


def prune(history: dict, now: dt.datetime) -> dict:
    """오래된 기록을 정리한다 (파일 크기 방어)."""
    cutoff = now - dt.timedelta(days=MAX_SESSION_AGE_DAYS)
    sessions = history.get("sessions", []) or []
    kept = []
    for s in sessions:
        end = parse_iso(s.get("end"))
        if end is None or end >= cutoff:
            kept.append(s)
    kept.sort(key=lambda s: s.get("start") or "")
    if len(kept) > MAX_SESSIONS:
        kept = kept[-MAX_SESSIONS:]
    history["sessions"] = kept
    return history


# ---------------------------------------------------------------------------
# 집계 (KST 기준 오늘 / 이번 주 / 이번 달)
# ---------------------------------------------------------------------------


def kst_windows(now: dt.datetime) -> dict[str, tuple[dt.datetime, dt.datetime]]:
    local = now.astimezone(KST)
    today_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - dt.timedelta(days=local.weekday())  # 월요일 시작
    month_start = today_start.replace(day=1)
    return {
        "today": (today_start.astimezone(dt.timezone.utc), now),
        "week": (week_start.astimezone(dt.timezone.utc), now),
        "month": (month_start.astimezone(dt.timezone.utc), now),
    }


def _overlap_minutes(start: dt.datetime, end: dt.datetime, w0: dt.datetime, w1: dt.datetime) -> float:
    lo = max(start, w0)
    hi = min(end, w1)
    if hi <= lo:
        return 0.0
    return (hi - lo).total_seconds() / 60


def aggregate(history: dict, channels: list[dict], now: dt.datetime, keywords: Optional[Iterable[str]] = None) -> dict:
    """등록된 모든 채널에 대해 구간별 방송시간을 집계한다.

    등록되어 있지만 아직 방송 기록이 없는 채널도 0분으로 반드시 포함한다
    (사이트에서 등록 채널 전체가 보여야 하므로).
    """
    kws = list(keywords) if keywords is not None else DEFAULT_GAME_KEYWORDS
    windows = kst_windows(now)
    open_map = history.get("open", {}) or {}
    sessions = history.get("sessions", []) or []

    # 채널 목록 = 등록 채널 + 기록에만 남아 있는 채널(삭제된 채널의 과거 기록)
    order: list[str] = []
    names: dict[str, str] = {}
    registered: set[str] = set()
    for ch in channels:
        cid = ch.get("id")
        if not cid:
            continue
        registered.add(cid)
        names[cid] = ch.get("name") or cid
        order.append(cid)
    for s in sessions:
        cid = s.get("channelId")
        if cid and cid not in names:
            names[cid] = s.get("name") or cid
            order.append(cid)
    for cid, cur in open_map.items():
        if cid not in names:
            names[cid] = cur.get("name") or cid
            order.append(cid)

    def blank_buckets() -> dict:
        return {k: {"totalMinutes": 0.0, "gameMinutes": 0.0, "sessions": 0} for k in windows}

    per_channel: dict[str, dict] = {
        cid: {
            "channelId": cid,
            "name": names[cid],
            "registered": cid in registered,
            "channelUrl": f"https://www.youtube.com/channel/{cid}",
            "isLive": cid in open_map,
            "liveSince": (open_map.get(cid) or {}).get("start"),
            "liveTitle": (open_map.get(cid) or {}).get("title"),
            "lastEnd": None,
            "lastTitle": None,
            "buckets": blank_buckets(),
            "recentSessions": [],
        }
        for cid in order
    }
    totals = blank_buckets()

    def add(entry_buckets: dict, start: dt.datetime, end: dt.datetime, is_game: bool):
        for key, (w0, w1) in windows.items():
            mins = _overlap_minutes(start, end, w0, w1)
            if mins <= 0:
                continue
            entry_buckets[key]["totalMinutes"] += mins
            if is_game:
                entry_buckets[key]["gameMinutes"] += mins
            entry_buckets[key]["sessions"] += 1

    for s in sessions:
        cid = s.get("channelId")
        if cid not in per_channel:
            continue
        start = parse_iso(s.get("start"))
        end = parse_iso(s.get("end"))
        if not start or not end:
            continue
        is_game = bool(s.get("isTargetGame")) or is_target_game(s.get("title"), s.get("game"), kws)
        add(per_channel[cid]["buckets"], start, end, is_game)
        add(totals, start, end, is_game)

        entry = per_channel[cid]
        if entry["lastEnd"] is None or (s.get("end") or "") > entry["lastEnd"]:
            entry["lastEnd"] = s.get("end")
            entry["lastTitle"] = s.get("title")

    # 진행 중인 방송도 "지금까지" 분량을 포함
    for cid, cur in open_map.items():
        if cid not in per_channel:
            continue
        start = parse_iso(cur.get("start"))
        if not start:
            continue
        is_game = bool(cur.get("isTargetGame")) or is_target_game(cur.get("title"), cur.get("game"), kws)
        add(per_channel[cid]["buckets"], start, now, is_game)
        add(totals, start, now, is_game)

    # 채널별 최근 세션 목록(최신 5개)
    by_channel: dict[str, list[dict]] = {}
    for s in sessions:
        by_channel.setdefault(s.get("channelId"), []).append(s)
    for cid, entry in per_channel.items():
        recent = sorted(by_channel.get(cid, []), key=lambda x: x.get("start") or "", reverse=True)[:5]
        entry["recentSessions"] = recent

    def round_buckets(buckets: dict) -> dict:
        return {
            k: {
                "totalMinutes": round(v["totalMinutes"], 1),
                "gameMinutes": round(v["gameMinutes"], 1),
                "totalHours": round(v["totalMinutes"] / 60, 2),
                "gameHours": round(v["gameMinutes"] / 60, 2),
                "sessions": v["sessions"],
            }
            for k, v in buckets.items()
        }

    channel_list = []
    for cid in order:
        entry = per_channel[cid]
        entry["buckets"] = round_buckets(entry["buckets"])
        channel_list.append(entry)

    # 방송시간이 긴 채널이 위로, 그다음 이름순
    channel_list.sort(
        key=lambda e: (
            not e["isLive"],
            -e["buckets"]["month"]["totalMinutes"],
            e["name"] or "",
        )
    )

    all_recent = sorted(sessions, key=lambda s: s.get("start") or "", reverse=True)[:60]

    return {
        "updatedAt": to_iso(now),
        "timezone": "Asia/Seoul (UTC+9)",
        "windows": {
            "today": "오늘 00:00부터",
            "week": "이번 주(월요일 00:00부터)",
            "month": "이번 달(1일 00:00부터)",
        },
        "keywords": kws,
        "totals": round_buckets(totals),
        "channels": channel_list,
        "recentSessions": all_recent,
    }
