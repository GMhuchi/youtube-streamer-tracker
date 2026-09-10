"""
API 키 없이 유튜브 페이지를 직접 확인해서 정보를 얻는 공용 함수 모음.

전제:
- 유튜브 공식 API(Data API v3)를 전혀 쓰지 않는다.
- 대신 (1) 채널 RSS 피드, (2) 채널의 /live 리다이렉트, (3) 검색결과 페이지에
  박혀 있는 초기 데이터(ytInitialData / ytInitialPlayerResponse) JSON을
  그대로 읽어서 파싱한다.
- 이 페이지들은 로그인 없이 접근 가능한 공개 페이지이고, 별도 발급/승인
  절차가 필요 없다. 다만 유튜브가 페이지 구조를 바꾸면 파싱이 깨질 수
  있으므로, 모든 파싱은 실패해도 전체 실행이 죽지 않도록 방어적으로 작성한다.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9,ja;q=0.8,en-US;q=0.7,en;q=0.6",
}

REQUEST_TIMEOUT = 15
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


class FetchError(RuntimeError):
    """네트워크/HTTP 오류 등, 이 실행에서 복구할 수 없는 오류."""


class ParseError(RuntimeError):
    """페이지는 받았지만 기대한 데이터 구조를 찾지 못했을 때."""


def get_html(url: str, *, params: Optional[dict] = None, session: Optional[requests.Session] = None) -> str:
    sess = session or requests
    try:
        resp = sess.get(
            url,
            params=params,
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        raise FetchError(f"{url} 요청 실패: {e}") from e
    if resp.status_code >= 400:
        raise FetchError(f"{url} -> HTTP {resp.status_code}")
    return resp.text, resp.url


def extract_json_after_marker(html: str, marker: str) -> Any:
    """`var ytInitialData = {...};` 같은 패턴에서 marker 뒤의 JSON 객체를
    괄호 짝을 맞춰가며(raw_decode) 안전하게 뽑아낸다. 정규식으로 `.*?};`를
    쓰면 JSON 문자열 내부에 있는 '};' 때문에 잘못 잘릴 수 있어서 이 방식을 쓴다.
    """
    idx = html.find(marker)
    if idx == -1:
        raise ParseError(f"marker not found: {marker}")
    start = idx + len(marker)
    # marker 뒤에 '=' 와 공백이 있을 수 있으므로 첫 '{' 위치까지 이동
    brace_idx = html.find("{", start)
    if brace_idx == -1:
        raise ParseError(f"'{{' not found after marker: {marker}")
    decoder = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(html, brace_idx)
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON decode failed after {marker}: {e}") from e
    return obj


def dig(obj: Any, *path, default=None):
    """중첩 dict/list 를 안전하게 파고드는 헬퍼. 중간에 키가 없거나 타입이
    안 맞으면 조용히 default 를 반환한다."""
    cur = obj
    for key in path:
        try:
            if isinstance(key, int):
                cur = cur[key]
            else:
                cur = cur.get(key)
        except (KeyError, IndexError, TypeError, AttributeError):
            return default
        if cur is None:
            return default
    return cur


# ---------------------------------------------------------------------------
# 1) 채널 RSS (완전 무료, 스크레이핑도 아님 - 유튜브가 공식 제공하는 피드)
# ---------------------------------------------------------------------------

import xml.etree.ElementTree as ET

ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"


def fetch_channel_rss(channel_id: str, session: Optional[requests.Session] = None) -> list[dict]:
    """채널의 최근 업로드 목록(보통 최신 15개)을 가져온다. 각 항목:
    {videoId, title, publishedAt, thumbnail}
    라이브 여부는 RSS만으로는 알 수 없다 (그래서 fetch_watch_page_live_status 를 같이 쓴다).
    """
    url = "https://www.youtube.com/feeds/videos.xml"
    html, _final_url = get_html(url, params={"channel_id": channel_id}, session=session)
    try:
        root = ET.fromstring(html)
    except ET.ParseError as e:
        raise ParseError(f"RSS XML 파싱 실패 ({channel_id}): {e}") from e

    entries = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        video_id = entry.findtext(f"{YT_NS}videoId")
        title = entry.findtext(f"{ATOM_NS}title")
        published = entry.findtext(f"{ATOM_NS}published")
        media_group = entry.find(f"{MEDIA_NS}group")
        thumb = None
        if media_group is not None:
            thumb_el = media_group.find(f"{MEDIA_NS}thumbnail")
            if thumb_el is not None:
                thumb = thumb_el.get("url")
        if video_id:
            entries.append(
                {
                    "videoId": video_id,
                    "title": title,
                    "publishedAt": published,
                    "thumbnail": thumb,
                }
            )
    return entries


# ---------------------------------------------------------------------------
# 2) 채널이 지금 라이브 중인지 확인 (/live 리다이렉트 + 워치 페이지 파싱)
# ---------------------------------------------------------------------------

@dataclass
class LiveStatus:
    is_live: bool
    video_id: Optional[str] = None
    title: Optional[str] = None
    channel_title: Optional[str] = None
    thumbnail: Optional[str] = None
    viewers: Optional[int] = None
    started_at: Optional[str] = None
    game: Optional[str] = None


def extract_game_title(html: str) -> Optional[str]:
    """워치 페이지에 게임 방송이면 박혀 있는 "게임 박스아트" 메타데이터에서
    게임 이름을 뽑아낸다. 없으면 None (게임 방송이 아니거나 구조가 바뀐 경우).

    유튜브는 게임 스트림 워치 페이지에 richMetadataRenderer(style이
    RICH_METADATA_RENDERER_STYLE_BOX_ART)로 게임 제목을 넣어준다.
    구조가 바뀌어도 전체 실행이 죽지 않도록 조용히 None 을 반환한다.
    """
    try:
        data = extract_json_after_marker(html, "var ytInitialData =")
    except ParseError:
        return None

    found: list[str] = []

    def walk(node):
        if found:
            return
        if isinstance(node, dict):
            rmr = node.get("richMetadataRenderer")
            if isinstance(rmr, dict):
                style = rmr.get("style") or ""
                if "BOX_ART" in str(style):
                    title = dig(rmr, "title", "simpleText") or dig(rmr, "title", "runs", 0, "text")
                    if title:
                        found.append(str(title))
                        return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found[0] if found else None


@dataclass
class VideoLiveDetails:
    """이미 끝난(또는 진행 중인) 라이브 영상의 정확한 시작/종료 시각."""

    video_id: str
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    is_live_now: bool = False
    title: Optional[str] = None
    game: Optional[str] = None


def fetch_video_live_details(video_id: str, session: Optional[requests.Session] = None) -> VideoLiveDetails:
    """워치 페이지에서 liveBroadcastDetails 의 startTimestamp/endTimestamp 를 읽는다.
    방송이 끝난 뒤 정확한 종료 시각을 알아내는 용도 (5~10분 간격 확인으로는
    종료 시각이 최대 그만큼 밀리기 때문에, 끝난 시점에 한 번만 확인한다).
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    html, _final_url = get_html(url, session=session)
    try:
        player = extract_json_after_marker(html, "var ytInitialPlayerResponse =")
    except ParseError as e:
        raise ParseError(f"영상 {video_id} 플레이어 데이터를 찾지 못했습니다: {e}") from e

    details = dig(player, "microformat", "playerMicroformatRenderer", "liveBroadcastDetails", default={}) or {}
    video_details = dig(player, "videoDetails", default={}) or {}
    return VideoLiveDetails(
        video_id=video_id,
        start_timestamp=details.get("startTimestamp"),
        end_timestamp=details.get("endTimestamp"),
        is_live_now=bool(details.get("isLiveNow", False)),
        title=video_details.get("title"),
        game=extract_game_title(html),
    )


def fetch_channel_live_status(channel_id: str, session: Optional[requests.Session] = None) -> LiveStatus:
    """`/channel/<id>/live` 는 그 채널이 지금 방송 중이면 해당 watch 페이지로,
    아니면 채널 홈으로 리다이렉트된다. 다만 리다이렉트 여부만으로 판단하지
    않고, 도착한 페이지에 박힌 ytInitialPlayerResponse.microformat 의
    liveBroadcastDetails.isLiveNow 값으로 최종 판단한다 (더 신뢰도 높음).
    """
    url = f"https://www.youtube.com/channel/{channel_id}/live"
    html, final_url = get_html(url, session=session)

    if "/watch" not in final_url:
        # 라이브 중이 아니면 채널 홈으로 리다이렉트됨
        return LiveStatus(is_live=False)

    try:
        player = extract_json_after_marker(html, "var ytInitialPlayerResponse =")
    except ParseError:
        # 워치 페이지이긴 한데 플레이어 데이터를 못 찾음 -> 보수적으로 오프라인 처리
        return LiveStatus(is_live=False)

    is_live_now = bool(
        dig(player, "microformat", "playerMicroformatRenderer", "liveBroadcastDetails", "isLiveNow", default=False)
    )
    video_details = dig(player, "videoDetails", default={}) or {}
    video_id = video_details.get("videoId")

    if not is_live_now:
        return LiveStatus(is_live=False, video_id=video_id)

    viewers_raw = video_details.get("viewCount")
    viewers = None
    if viewers_raw is not None:
        try:
            viewers = int(viewers_raw)
        except (TypeError, ValueError):
            viewers = None

    thumbs = dig(video_details, "thumbnail", "thumbnails", default=[]) or []
    thumbnail = thumbs[-1]["url"] if thumbs else None

    started_at = dig(
        player, "microformat", "playerMicroformatRenderer", "liveBroadcastDetails", "startTimestamp", default=None
    )

    return LiveStatus(
        is_live=True,
        video_id=video_id,
        title=video_details.get("title"),
        channel_title=video_details.get("author"),
        thumbnail=thumbnail,
        viewers=viewers,
        started_at=started_at,
        game=extract_game_title(html),
    )


# ---------------------------------------------------------------------------
# 3) 채널 URL/핸들 -> 정식 채널 ID + 표시 이름 (등록용)
# ---------------------------------------------------------------------------

CANONICAL_CHANNEL_RE = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})">'
)
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')


def normalize_channel_input(raw: str) -> str:
    """사용자가 입력한 값(전체 URL / @핸들 / 채널ID)을 접근 가능한 URL로 변환."""
    raw = raw.strip()
    if CHANNEL_ID_RE.match(raw):
        return f"https://www.youtube.com/channel/{raw}"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}"
    # "youtube.com/..." 처럼 스킴이 빠졌거나, 핸들만 온 경우
    if raw.startswith("youtube.com") or raw.startswith("www.youtube.com"):
        return f"https://{raw}"
    return f"https://www.youtube.com/@{raw.lstrip('@')}"


def resolve_channel(raw: str, session: Optional[requests.Session] = None) -> dict:
    """URL/핸들/채널ID 입력을 받아 {"id":..., "name":...} 를 돌려준다.
    실패하면 ParseError/FetchError 를 던진다 (호출부에서 사용자에게 안내).
    """
    url = normalize_channel_input(raw)
    html, final_url = get_html(url, session=session)

    m = CANONICAL_CHANNEL_RE.search(html)
    channel_id = m.group(1) if m else None

    if not channel_id:
        # about 페이지가 아니라면 canonical 태그가 없을 수 있으니 한 번 더 시도
        html2, _ = get_html(url.rstrip("/") + "/about", session=session)
        m2 = CANONICAL_CHANNEL_RE.search(html2)
        if m2:
            channel_id = m2.group(1)
            html = html2

    if not channel_id:
        raise ParseError(f"채널 ID를 찾지 못했습니다: {raw} (최종 URL: {final_url})")

    name_match = OG_TITLE_RE.search(html)
    name = name_match.group(1) if name_match else channel_id

    return {"id": channel_id, "name": name}


# ---------------------------------------------------------------------------
# 4) 태그(검색어)로 실시간 방송 검색 (신규 스트리머 발견용)
# ---------------------------------------------------------------------------

# sp=EgJAAQ%3D%3D 는 유튜브 검색 필터 중 "실시간(라이브)" 을 의미하는 값.
LIVE_FILTER_PARAM = "EgJAAQ%3D%3D"


def search_live_by_query(query: str, session: Optional[requests.Session] = None, max_items: int = 20) -> list[dict]:
    search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query) + "&sp=" + LIVE_FILTER_PARAM
    html, _final_url = get_html(search_url, session=session)
    data = extract_json_after_marker(html, "var ytInitialData =")

    sections = (
        dig(
            data,
            "contents",
            "twoColumnSearchResultsRenderer",
            "primaryContents",
            "sectionListRenderer",
            "contents",
            default=[],
        )
        or []
    )

    results = []
    for section in sections:
        items = dig(section, "itemSectionRenderer", "contents", default=[]) or []
        for item in items:
            vr = item.get("videoRenderer") if isinstance(item, dict) else None
            if not vr:
                continue
            video_id = vr.get("videoId")
            title = dig(vr, "title", "runs", 0, "text")
            owner_runs = dig(vr, "ownerText", "runs", 0, default={}) or {}
            channel_title = owner_runs.get("text")
            channel_id = dig(owner_runs, "navigationEndpoint", "browseEndpoint", "browseId")
            thumbs = dig(vr, "thumbnail", "thumbnails", default=[]) or []
            thumbnail = thumbs[-1]["url"] if thumbs else None
            view_count_text = dig(vr, "viewCountText", "runs", 0, "text")

            # sp=live 필터로 이미 걸렀지만, 뱃지로 한 번 더 확인(있으면 더 신뢰)
            overlays = vr.get("thumbnailOverlays", []) or []
            looks_live = any(
                dig(o, "thumbnailOverlayTimeStatusRenderer", "style") == "LIVE" for o in overlays
            ) or True  # sp 필터 자체가 이미 live 전용이므로 뱃지 못 찾아도 통과

            if not (video_id and channel_id):
                continue
            results.append(
                {
                    "videoId": video_id,
                    "title": title,
                    "channelId": channel_id,
                    "channelTitle": channel_title,
                    "thumbnail": thumbnail,
                    "viewCountText": view_count_text,
                }
            )
            if len(results) >= max_items:
                return results
    return results


def polite_sleep(seconds: float = 0.6):
    """연속 요청 사이에 짧게 쉬어서 너무 공격적으로 보이지 않게 한다."""
    time.sleep(seconds)
