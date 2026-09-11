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
# 한 방송의 시청자수 표본 개수 상한 (10분 간격이면 300개 = 50시간)
MAX_SAMPLES_PER_SESSION = 300
# 이 기간이 지난 방송은 추이 그래프용 표본을 버리고 평균/최고값만 남긴다
SAMPLE_RETENTION_DAYS = 45


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


def average_viewers(samples) -> Optional[float]:
    """시청자수 표본들의 평균. 표본 간격이 고르지 않을 수 있어 '시간 가중' 평균을 쓴다.

    (마지막 표본은 그 앞 간격의 평균 간격만큼 지속된 것으로 본다.)
    표본이 없으면 None, 1개뿐이면 그 값.
    """
    points = []
    for item in samples or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        t = parse_iso(item[0])
        v = item[1]
        if t is None or v is None:
            continue
        try:
            points.append((t, float(v)))
        except (TypeError, ValueError):
            continue

    if not points:
        return None
    if len(points) == 1:
        return round(points[0][1], 1)

    points.sort(key=lambda p: p[0])
    total_weight = 0.0
    total_value = 0.0
    for i, (t, v) in enumerate(points):
        if i < len(points) - 1:
            weight = (points[i + 1][0] - t).total_seconds()
        else:
            # 마지막 표본: 평균 간격만큼 지속되었다고 본다
            spans = [
                (points[j + 1][0] - points[j][0]).total_seconds() for j in range(len(points) - 1)
            ]
            weight = sum(spans) / len(spans) if spans else 0.0
        if weight <= 0:
            weight = 1.0
        total_weight += weight
        total_value += v * weight

    if total_weight <= 0:
        return round(sum(v for _, v in points) / len(points), 1)
    return round(total_value / total_weight, 1)


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
                # 시청자수 추이: [관측시각, 시청자수] 쌍을 확인 주기마다 쌓는다
                "samples": [[to_iso(now), viewers]] if viewers is not None else [],
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
            samples = current.setdefault("samples", [])
            samples.append([to_iso(now), viewers])
            if len(samples) > MAX_SAMPLES_PER_SESSION:
                # 너무 길어지면 간격을 두 배로 줄여(격번 삭제) 추이 모양은 유지한다
                current["samples"] = samples[::2]
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
    start_override = None
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
            # 방송이 끝나야 유튜브가 정확한 시작 시각도 함께 알려준다.
            # 진행 중에는 못 받는 경우가 많아서, 여기서 관측값을 실제값으로 보정한다.
            yt_start = parse_iso(getattr(details, "start_timestamp", None))
            if yt_start:
                start_override = yt_start
            yt_game = getattr(details, "game", None)
            if yt_game and not current.get("game"):
                current["game"] = yt_game

    start = start_override or parse_iso(current.get("start")) or end
    start_source = "youtube" if start_override else current.get("startSource", "observed")
    if end < start:
        end = start
    minutes = round((end - start).total_seconds() / 60, 1)

    samples = current.get("samples") or []
    session = {
        "channelId": channel_id,
        "name": current.get("name"),
        "videoId": video_id,
        "videoUrl": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        "start": to_iso(start),
        "end": to_iso(end),
        "minutes": minutes,
        "startSource": start_source,
        "endSource": end_source,
        "title": current.get("title"),
        "game": current.get("game"),
        "peakViewers": current.get("peakViewers"),
        "avgViewers": average_viewers(samples),
        "samples": samples,
        "isTargetGame": bool(current.get("isTargetGame")),
    }
    sessions.append(session)
    open_map.pop(channel_id, None)
    return session


def prune(history: dict, now: dt.datetime) -> dict:
    """오래된 기록을 정리한다 (파일 크기 방어)."""
    cutoff = now - dt.timedelta(days=MAX_SESSION_AGE_DAYS)
    sample_cutoff = now - dt.timedelta(days=SAMPLE_RETENTION_DAYS)
    sessions = history.get("sessions", []) or []
    kept = []
    for s in sessions:
        end = parse_iso(s.get("end"))
        if end is None or end >= cutoff:
            # 오래된 방송은 추이 표본을 버리고 평균/최고값만 남긴다
            if end is not None and end < sample_cutoff and s.get("samples"):
                if s.get("avgViewers") is None:
                    s["avgViewers"] = average_viewers(s.get("samples"))
                s["samples"] = []
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


def thin_samples(samples, max_points: int = 60) -> list:
    """표본이 너무 많으면 일정 간격으로 솎아낸다(추이 모양은 유지, 마지막 점은 보존)."""
    items = [s for s in (samples or []) if isinstance(s, (list, tuple)) and len(s) >= 2]
    if len(items) <= max_points:
        return [list(s) for s in items]
    step = len(items) / float(max_points)
    picked = []
    seen = set()
    for i in range(max_points):
        idx = int(i * step)
        if idx not in seen and idx < len(items):
            seen.add(idx)
            picked.append(list(items[idx]))
    last = len(items) - 1
    if last not in seen:
        picked.append(list(items[last]))
    return picked


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
        return {
            k: {
                "totalMinutes": 0.0,
                "gameMinutes": 0.0,
                "sessions": 0,
                # 시청자수: 방송 길이로 가중평균을 내기 위한 누적값
                "_viewerWeighted": 0.0,
                "_viewerMinutes": 0.0,
                "peakViewers": None,
            }
            for k in windows
        }

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

    def add(entry_buckets: dict, start: dt.datetime, end: dt.datetime, is_game: bool, avg=None, peak=None):
        for key, (w0, w1) in windows.items():
            mins = _overlap_minutes(start, end, w0, w1)
            if mins <= 0:
                continue
            bucket = entry_buckets[key]
            bucket["totalMinutes"] += mins
            if is_game:
                bucket["gameMinutes"] += mins
            bucket["sessions"] += 1
            if avg is not None:
                # 긴 방송의 평균이 더 크게 반영되도록 방송 길이로 가중한다
                bucket["_viewerWeighted"] += float(avg) * mins
                bucket["_viewerMinutes"] += mins
            if peak is not None:
                prev = bucket["peakViewers"]
                bucket["peakViewers"] = peak if prev is None else max(prev, peak)

    for s in sessions:
        cid = s.get("channelId")
        if cid not in per_channel:
            continue
        start = parse_iso(s.get("start"))
        end = parse_iso(s.get("end"))
        if not start or not end:
            continue
        is_game = bool(s.get("isTargetGame")) or is_target_game(s.get("title"), s.get("game"), kws)
        avg = s.get("avgViewers")
        if avg is None:
            avg = average_viewers(s.get("samples"))
        peak = s.get("peakViewers")
        add(per_channel[cid]["buckets"], start, end, is_game, avg, peak)
        add(totals, start, end, is_game, avg, peak)

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
        cur_avg = average_viewers(cur.get("samples"))
        add(per_channel[cid]["buckets"], start, now, is_game, cur_avg, cur.get("peakViewers"))
        add(totals, start, now, is_game, cur_avg, cur.get("peakViewers"))
        entry = per_channel[cid]
        entry["liveViewers"] = (cur.get("samples") or [[None, None]])[-1][1]
        entry["livePeakViewers"] = cur.get("peakViewers")
        entry["liveAvgViewers"] = cur_avg
        entry["liveSamples"] = cur.get("samples") or []
        entry["liveVideoUrl"] = (
            f"https://www.youtube.com/watch?v={cur.get('videoId')}" if cur.get("videoId") else None
        )

    # 채널별 최근 세션 목록(최신 8개).
    # 추이 그래프용 표본은 페이지가 무거워지지 않게 "최근 방송 3개 + 최근 7일"
    # 까지만, 그것도 최대 60포인트로 솎아서 싣는다.
    sample_window_start = now - dt.timedelta(days=7)
    by_channel: dict[str, list[dict]] = {}
    for s in sessions:
        by_channel.setdefault(s.get("channelId"), []).append(s)
    for cid, entry in per_channel.items():
        recent = sorted(by_channel.get(cid, []), key=lambda x: x.get("start") or "", reverse=True)[:8]
        trimmed = []
        for i, s in enumerate(recent):
            item = dict(s)
            end = parse_iso(s.get("end"))
            keep_samples = i < 3 and end is not None and end >= sample_window_start
            item["samples"] = thin_samples(s.get("samples"), 60) if keep_samples else []
            if item.get("avgViewers") is None:
                item["avgViewers"] = average_viewers(s.get("samples"))
            trimmed.append(item)
        entry["recentSessions"] = trimmed

    def round_buckets(buckets: dict) -> dict:
        out = {}
        for k, v in buckets.items():
            avg = None
            if v["_viewerMinutes"] > 0:
                avg = round(v["_viewerWeighted"] / v["_viewerMinutes"], 1)
            out[k] = {
                "totalMinutes": round(v["totalMinutes"], 1),
                "gameMinutes": round(v["gameMinutes"], 1),
                "totalHours": round(v["totalMinutes"] / 60, 2),
                "gameHours": round(v["gameMinutes"] / 60, 2),
                "sessions": v["sessions"],
                "avgViewers": avg,
                "peakViewers": v["peakViewers"],
            }
        return out

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

    # 전체 최근 목록은 표본 없이 요약만 싣는다 (표본은 채널별 상세에서 본다)
    all_recent = []
    for s in sorted(sessions, key=lambda s: s.get("start") or "", reverse=True)[:60]:
        item = {k: v for k, v in s.items() if k != "samples"}
        if item.get("avgViewers") is None:
            item["avgViewers"] = average_viewers(s.get("samples"))
        all_recent.append(item)

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
