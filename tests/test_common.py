"""
common.py의 파싱 로직을 검증하는 테스트.

이 샌드박스에서는 youtube.com/googleapis.com 으로 나가는 네트워크가
막혀 있어서(프록시 allowlist 문제) 실제 요청을 보낼 수 없다. 대신
get_html()이 반환할 법한 실제 페이지 구조를 최대한 그대로 흉내낸
합성 fixture를 만들어 monkeypatch로 주입하고, 파싱 결과가 맞는지만
검증한다. (진짜 유튜브 페이지 구조가 바뀌면 이 테스트는 여전히
통과하지만 실제 스크레이핑은 깨질 수 있다 - README의 한계 설명 참고.)
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import common  # noqa: E402


# ---------------------------------------------------------------------------
# extract_json_after_marker: 문자열 안에 '};' 가 들어있어도 안전해야 한다
# ---------------------------------------------------------------------------

def test_extract_json_after_marker_handles_embedded_brace_semicolon():
    payload = {
        "title": "이 문자열엔 }; 이 들어있음",
        "nested": {"a": [1, 2, {"b": "another }; trap"}]},
    }
    html = "var ytInitialData = " + json.dumps(payload) + ";\nwindow.foo = 1;"
    result = common.extract_json_after_marker(html, "var ytInitialData =")
    assert result == payload


def test_extract_json_after_marker_missing_marker_raises():
    with pytest.raises(common.ParseError):
        common.extract_json_after_marker("no data here", "var ytInitialData =")


# ---------------------------------------------------------------------------
# dig(): 안전한 nested 접근
# ---------------------------------------------------------------------------

def test_dig_returns_default_on_missing_path():
    obj = {"a": {"b": [1, 2, 3]}}
    assert common.dig(obj, "a", "b", 1) == 2
    assert common.dig(obj, "a", "x", "y", default="fallback") == "fallback"
    assert common.dig(obj, "a", "b", 99, default=None) is None


# ---------------------------------------------------------------------------
# fetch_channel_rss: 실제 유튜브 RSS(Atom) 스키마를 흉내낸 fixture
# ---------------------------------------------------------------------------

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
  <link rel="self" href="https://www.youtube.com/feeds/videos.xml?channel_id=UCTEST"/>
  <id>yt:channel:UCTEST</id>
  <yt:channelId>UCTEST</yt:channelId>
  <title>테스트채널</title>
  <link rel="alternate" href="https://www.youtube.com/channel/UCTEST"/>
  <author><name>테스트채널</name><uri>https://www.youtube.com/channel/UCTEST</uri></author>
  <published>2026-09-01T00:00:00+00:00</published>
  <entry>
    <id>yt:video:VID1</id>
    <yt:videoId>VID1</yt:videoId>
    <yt:channelId>UCTEST</yt:channelId>
    <title>최신 영상 제목</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=VID1"/>
    <author><name>테스트채널</name><uri>https://www.youtube.com/channel/UCTEST</uri></author>
    <published>2026-09-08T12:00:00+00:00</published>
    <updated>2026-09-08T12:05:00+00:00</updated>
    <media:group>
      <media:title>최신 영상 제목</media:title>
      <media:thumbnail url="https://i.ytimg.com/vi/VID1/hqdefault.jpg" width="480" height="360"/>
      <media:description>설명</media:description>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:VID0</id>
    <yt:videoId>VID0</yt:videoId>
    <yt:channelId>UCTEST</yt:channelId>
    <title>이전 영상</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=VID0"/>
    <author><name>테스트채널</name><uri>https://www.youtube.com/channel/UCTEST</uri></author>
    <published>2026-09-01T09:00:00+00:00</published>
    <updated>2026-09-01T09:05:00+00:00</updated>
    <media:group>
      <media:title>이전 영상</media:title>
      <media:thumbnail url="https://i.ytimg.com/vi/VID0/hqdefault.jpg" width="480" height="360"/>
    </media:group>
  </entry>
</feed>
"""


def test_fetch_channel_rss_parses_entries(monkeypatch):
    def fake_get_html(url, params=None, session=None):
        assert "feeds/videos.xml" in url
        assert params == {"channel_id": "UCTEST"}
        return SAMPLE_RSS, url

    monkeypatch.setattr(common, "get_html", fake_get_html)
    entries = common.fetch_channel_rss("UCTEST")
    assert len(entries) == 2
    assert entries[0]["videoId"] == "VID1"
    assert entries[0]["title"] == "최신 영상 제목"
    assert entries[0]["thumbnail"] == "https://i.ytimg.com/vi/VID1/hqdefault.jpg"
    assert entries[1]["videoId"] == "VID0"


def test_fetch_channel_rss_bad_xml_raises_parse_error(monkeypatch):
    monkeypatch.setattr(common, "get_html", lambda url, params=None, session=None: ("<not-xml", url))
    with pytest.raises(common.ParseError):
        common.fetch_channel_rss("UCTEST")


# ---------------------------------------------------------------------------
# fetch_channel_live_status: watch 페이지의 ytInitialPlayerResponse 파싱
# ---------------------------------------------------------------------------

def _player_response_html(is_live: bool, viewers_text="현재 1,234명 시청 중", with_microformat=False):
    """실제 유튜브 워치 페이지 구조를 그대로 흉내낸 픽스처.

    ⚠️ 중요: **방송이 진행 중인 페이지에는 microformat 이 비어 있다.**
    (liveBroadcastDetails 는 방송이 끝난 뒤에야 start/end 와 함께 채워진다.)
    진행 여부는 videoDetails.isLive 로 판정해야 한다 — 예전에 microformat 기준으로
    판정하다가 라이브를 한 건도 못 잡는 버그가 있었다.
    """
    player = {
        "videoDetails": {
            "videoId": "LIVEVID",
            "title": "라이브 방송 제목",
            "author": "테스트채널",
            "isLive": is_live,
            "isLiveContent": True,
            "viewCount": "999999",  # 라이브에서도 '누적 조회수'라 동시 시청자수가 아니다
            "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/LIVEVID/default.jpg"}]},
        },
        "microformat": {} if not with_microformat else {
            "playerMicroformatRenderer": {
                "liveBroadcastDetails": {
                    "isLiveNow": is_live,
                    "startTimestamp": "2026-09-09T01:00:00+00:00",
                }
            }
        },
    }
    # ⚠️ liveStreamability 는 방송이 꺼져 있을 때도 그대로 들어있다.
    # 방송 중 여부를 가르는 건 status ("OK" vs "LIVE_STREAM_OFFLINE") 와 isLive 다.
    player["playabilityStatus"] = {
        "status": "OK" if is_live else "LIVE_STREAM_OFFLINE",
        "liveStreamability": {"liveStreamabilityRenderer": {}},
    }

    data = {
        "contents": {
            "x": {
                "videoViewCountRenderer": {
                    "isLive": is_live,
                    "viewCount": {"simpleText": viewers_text},
                    "extraShortViewCount": {"simpleText": "1.2천"},
                }
            }
        }
    }
    return (
        "some html <script>var ytInitialPlayerResponse = " + json.dumps(player) + ";</script>"
        "<script>var ytInitialData = " + json.dumps(data) + ";</script>"
    )


def test_fetch_channel_live_status_when_live(monkeypatch):
    html = _player_response_html(True)

    def fake_get_html(url, params=None, session=None):
        return html, "https://www.youtube.com/watch?v=LIVEVID"

    monkeypatch.setattr(common, "get_html", fake_get_html)
    status = common.fetch_channel_live_status("UCTEST")
    assert status.is_live is True
    assert status.video_id == "LIVEVID"
    # 누적 조회수(999999)가 아니라 동시 시청자수(1234)를 읽어야 한다
    assert status.viewers == 1234
    assert status.title == "라이브 방송 제목"
    assert status.thumbnail.endswith("default.jpg")


def test_live_detected_without_microformat():
    """진행 중 방송 페이지에 microformat 이 없어도 라이브로 잡아야 한다(과거 버그)."""
    status = common.parse_live_status(_player_response_html(True), "https://www.youtube.com/watch?v=LIVEVID")
    assert status.is_live is True
    assert status.started_at is None  # 진행 중에는 시작 시각을 못 받을 수 있다


def test_live_uses_microformat_start_when_present():
    html = _player_response_html(True, with_microformat=True)
    status = common.parse_live_status(html, "https://www.youtube.com/watch?v=LIVEVID")
    assert status.is_live is True
    assert status.started_at == "2026-09-09T01:00:00+00:00"


def test_viewer_count_parses_plain_and_large_numbers():
    assert common.parse_live_status(_player_response_html(True, "현재 7명 시청 중"), "/watch").viewers == 7
    assert common.parse_live_status(_player_response_html(True, "현재 12,345명 시청 중"), "/watch").viewers == 12345
    assert common.parse_live_status(_player_response_html(True, "7 watching now"), "/watch").viewers == 7


def test_fetch_channel_live_status_when_video_but_not_live(monkeypatch):
    html = _player_response_html(False)

    def fake_get_html(url, params=None, session=None):
        # 과거에 라이브였던 다시보기 영상으로 리다이렉트되는 경우를 흉내
        return html, "https://www.youtube.com/watch?v=OLDVOD"

    monkeypatch.setattr(common, "get_html", fake_get_html)
    status = common.fetch_channel_live_status("UCTEST")
    assert status.is_live is False


def test_offline_live_tab_is_not_counted_as_live():
    """회귀 방지: 방송이 꺼진 채널의 /live 페이지도 liveStreamability 를 갖고 있다.

    이걸 라이브 신호로 쓰면 방송을 안 하는 채널까지 전부 '방송 중'으로 잡힌다.
    """
    status = common.parse_live_status(_player_response_html(False), "https://www.youtube.com/channel/UCX/live")
    assert status.is_live is False
    assert status.note is None


def test_live_detected_when_youtube_serves_live_url_without_redirect(monkeypatch):
    """회귀 방지: 유튜브가 `/live` 주소 그대로 워치 페이지를 내려주는 경우.

    예전에는 '최종 URL 에 /watch 가 없으면 오프라인' 으로 판정했는데, 유튜브가
    리다이렉트를 없애면서 라이브 중인 채널이 전부 오프라인으로 잡히던 버그가 있었다.
    """
    html = _player_response_html(True, "현재 6명 시청 중")

    def fake_get_html(url, params=None, session=None):
        # 리다이렉트 없이 요청한 주소 그대로 응답
        return html, "https://www.youtube.com/channel/UCTEST/live"

    monkeypatch.setattr(common, "get_html", fake_get_html)
    status = common.fetch_channel_live_status("UCTEST")
    assert status.is_live is True
    assert status.viewers == 6


def test_fetch_channel_live_status_when_redirected_to_channel_home(monkeypatch):
    def fake_get_html(url, params=None, session=None):
        return "<html>channel home</html>", "https://www.youtube.com/channel/UCTEST"

    monkeypatch.setattr(common, "get_html", fake_get_html)
    status = common.fetch_channel_live_status("UCTEST")
    assert status.is_live is False
    assert status.video_id is None


# ---------------------------------------------------------------------------
# resolve_channel: canonical link + og:title 스크레이핑
# ---------------------------------------------------------------------------

def test_resolve_channel_from_handle_page(monkeypatch):
    html = (
        '<html><head>'
        '<link rel="canonical" href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv">'
        '<meta property="og:title" content="핸들채널 표시이름">'
        '</head></html>'
    )

    def fake_get_html(url, params=None, session=None):
        assert url == "https://www.youtube.com/@testhandle"
        return html, url

    monkeypatch.setattr(common, "get_html", fake_get_html)
    result = common.resolve_channel("@testhandle")
    assert result == {"id": "UCabcdefghijklmnopqrstuv", "name": "핸들채널 표시이름"}


def test_resolve_channel_raw_channel_id_passthrough_shape():
    url = common.normalize_channel_input("UCabcdefghijklmnopqrstuv")
    assert url == "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"


def test_resolve_channel_not_found_raises(monkeypatch):
    monkeypatch.setattr(
        common, "get_html", lambda url, params=None, session=None: ("<html>no canonical here</html>", url)
    )
    with pytest.raises(common.ParseError):
        common.resolve_channel("@doesnotexist")


# ---------------------------------------------------------------------------
# search_live_by_query: 검색결과 ytInitialData 파싱
# ---------------------------------------------------------------------------

def _search_results_html(video_items):
    content_items = [{"videoRenderer": v} for v in video_items]
    data = {
        "contents": {
            "twoColumnSearchResultsRenderer": {
                "primaryContents": {
                    "sectionListRenderer": {
                        "contents": [{"itemSectionRenderer": {"contents": content_items}}]
                    }
                }
            }
        }
    }
    return "<html><script>var ytInitialData = " + json.dumps(data) + ";</script></html>"


def test_search_live_by_query_parses_video_renderers(monkeypatch):
    video_items = [
        {
            "videoId": "V1",
            "title": {"runs": [{"text": "라이브 제목 1"}]},
            "ownerText": {
                "runs": [
                    {
                        "text": "채널원",
                        "navigationEndpoint": {"browseEndpoint": {"browseId": "UCOWNER1"}},
                    }
                ]
            },
            "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/V1/a.jpg"}, {"url": "https://i.ytimg.com/vi/V1/b.jpg"}]},
            "viewCountText": {"runs": [{"text": "123명 시청 중"}]},
            "thumbnailOverlays": [{"thumbnailOverlayTimeStatusRenderer": {"style": "LIVE"}}],
        }
    ]
    html = _search_results_html(video_items)
    monkeypatch.setattr(common, "get_html", lambda url, params=None, session=None: (html, url))

    results = common.search_live_by_query("테스트태그")
    assert len(results) == 1
    r = results[0]
    assert r["videoId"] == "V1"
    assert r["title"] == "라이브 제목 1"
    assert r["channelId"] == "UCOWNER1"
    assert r["channelTitle"] == "채널원"
    assert r["thumbnail"] == "https://i.ytimg.com/vi/V1/b.jpg"
    assert r["viewCountText"] == "123명 시청 중"


def test_search_live_by_query_skips_items_without_channel_id(monkeypatch):
    video_items = [
        {"videoId": "V2", "title": {"runs": [{"text": "채널ID 없는 항목"}]}, "ownerText": {"runs": [{"text": "x"}]}},
    ]
    html = _search_results_html(video_items)
    monkeypatch.setattr(common, "get_html", lambda url, params=None, session=None: (html, url))
    results = common.search_live_by_query("테스트태그")
    assert results == []
