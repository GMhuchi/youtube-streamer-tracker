import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import track  # noqa: E402
import common  # noqa: E402


def make_live_status(**kwargs):
    defaults = dict(is_live=False, video_id=None, title=None, channel_title=None, thumbnail=None, viewers=None, started_at=None)
    defaults.update(kwargs)
    return common.LiveStatus(**defaults)


# ---------------------------------------------------------------------------
# check_registered_channels
# ---------------------------------------------------------------------------

def test_check_registered_channels_merges_rss_and_live(monkeypatch):
    monkeypatch.setattr(
        track, "fetch_channel_rss",
        lambda channel_id, session=None: [{"videoId": "V1", "title": "최근 영상", "publishedAt": "2026-09-01T00:00:00Z"}],
    )
    monkeypatch.setattr(
        track, "fetch_channel_live_status",
        lambda channel_id, session=None: make_live_status(is_live=True, video_id="V2", title="라이브중", viewers=99, thumbnail="thumb.jpg"),
    )
    monkeypatch.setattr(track, "polite_sleep", lambda *a, **k: None)

    channels = [{"id": "UCONE", "name": "채널원"}]
    results, warnings = track.check_registered_channels(channels, session=None)

    assert warnings == []
    assert len(results) == 1
    r = results[0]
    assert r["isLive"] is True
    assert r["title"] == "라이브중"
    assert r["viewers"] == 99
    assert r["videoUrl"] == "https://www.youtube.com/watch?v=V2"
    # RSS 기반 최근 영상 정보도 같이 채워져야 함
    assert r["latestVideoTitle"] == "최근 영상"


def test_check_registered_channels_survives_rss_failure(monkeypatch):
    def boom(channel_id, session=None):
        raise common.FetchError("network down")

    monkeypatch.setattr(track, "fetch_channel_rss", boom)
    monkeypatch.setattr(track, "fetch_channel_live_status", lambda channel_id, session=None: make_live_status(is_live=False))
    monkeypatch.setattr(track, "polite_sleep", lambda *a, **k: None)

    channels = [{"id": "UCONE", "name": "채널원"}]
    results, warnings = track.check_registered_channels(channels, session=None)

    assert len(results) == 1
    assert results[0]["isLive"] is False
    assert results[0]["error"] is None  # RSS 실패는 치명적이지 않음
    # RSS는 부가 정보이므로 실패해도 사이트 상단 경고로 올리지 않는다
    # (라이브 확인은 정상 동작하는데 경고 배너가 뜨면 오히려 오해를 준다).
    assert warnings == []


def test_check_registered_channels_reuses_previous_rss_when_skipping(monkeypatch):
    """RSS 확인 주기가 아니면 네트워크를 쓰지 않고 이전 결과를 재사용해야 한다."""
    def should_not_be_called(channel_id, session=None):
        raise AssertionError("RSS 주기가 아니면 호출하면 안 됩니다")

    monkeypatch.setattr(track, "fetch_channel_rss", should_not_be_called)
    monkeypatch.setattr(track, "fetch_channel_live_status", lambda channel_id, session=None: make_live_status(is_live=False))
    monkeypatch.setattr(track, "polite_sleep", lambda *a, **k: None)

    channels = [{"id": "UCONE", "name": "채널원"}]
    prev = {"UCONE": {"id": "UCONE", "latestVideoTitle": "지난 영상", "latestVideoAt": "2026-09-01T00:00:00Z"}}
    results, warnings = track.check_registered_channels(
        channels, session=None, check_rss=False, prev_by_id=prev
    )

    assert results[0]["latestVideoTitle"] == "지난 영상"
    assert warnings == []


def test_check_registered_channels_records_live_check_error(monkeypatch):
    monkeypatch.setattr(track, "fetch_channel_rss", lambda channel_id, session=None: [])
    monkeypatch.setattr(
        track, "fetch_channel_live_status",
        lambda channel_id, session=None: (_ for _ in ()).throw(common.FetchError("timeout")),
    )
    monkeypatch.setattr(track, "polite_sleep", lambda *a, **k: None)

    channels = [{"id": "UCONE", "name": "채널원"}]
    results, warnings = track.check_registered_channels(channels, session=None)

    assert results[0]["error"] is not None
    assert results[0]["isLive"] is False
    assert len(warnings) == 1


def test_check_registered_channels_handles_missing_id(monkeypatch):
    results, warnings = track.check_registered_channels([{"name": "ID없는채널"}], session=None)
    assert results[0]["error"] is not None
    assert "채널 ID" in results[0]["error"]


# ---------------------------------------------------------------------------
# run_discovery
# ---------------------------------------------------------------------------

def test_run_discovery_dedups_and_flags_registered(monkeypatch):
    def fake_search(query, session=None, max_items=20):
        if query == "태그1":
            return [
                {"videoId": "V1", "title": "T1", "channelId": "UCA", "channelTitle": "채널A", "thumbnail": None, "viewCountText": "10명"},
                {"videoId": "V2", "title": "T2", "channelId": "UCB", "channelTitle": "채널B", "thumbnail": None, "viewCountText": "20명"},
            ]
        return [
            {"videoId": "V3", "title": "T3", "channelId": "UCA", "channelTitle": "채널A(중복)", "thumbnail": None, "viewCountText": "30명"},
        ]

    monkeypatch.setattr(track, "search_live_by_query", fake_search)
    monkeypatch.setattr(track, "polite_sleep", lambda *a, **k: None)

    queries = [{"query": "태그1", "label": "태그1"}, {"query": "태그2", "label": "태그2"}]
    discovery, warnings = track.run_discovery(queries, session=None, registered_ids={"UCB"})

    assert warnings == []
    assert len(discovery["items"]) == 2  # UCA는 태그1에서 먼저 잡혀서 태그2 중복이 버려짐
    by_channel = {it["channelId"]: it for it in discovery["items"]}
    assert by_channel["UCA"]["title"] == "T1"  # 첫 매칭 유지
    assert by_channel["UCA"]["alreadyRegistered"] is False
    assert by_channel["UCB"]["alreadyRegistered"] is True


def test_run_discovery_survives_one_query_failing(monkeypatch):
    def fake_search(query, session=None, max_items=20):
        if query == "실패태그":
            raise common.ParseError("페이지 구조 변경됨")
        return [{"videoId": "V9", "title": "T9", "channelId": "UCZ", "channelTitle": "Z", "thumbnail": None, "viewCountText": None}]

    monkeypatch.setattr(track, "search_live_by_query", fake_search)
    monkeypatch.setattr(track, "polite_sleep", lambda *a, **k: None)

    queries = [{"query": "실패태그", "label": "실패태그"}, {"query": "성공태그", "label": "성공태그"}]
    discovery, warnings = track.run_discovery(queries, session=None, registered_ids=set())

    assert len(warnings) == 1
    assert len(discovery["items"]) == 1
    assert discovery["items"][0]["channelId"] == "UCZ"


# ---------------------------------------------------------------------------
# main(): 전체 사이클, discovery 주기 로직, 파일 IO
# ---------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    docs_dir = tmp_path / "docs"
    data_dir.mkdir()
    docs_dir.mkdir()

    (data_dir / "channels.json").write_text(
        json.dumps({"channels": [{"id": "UCONE", "name": "채널원"}]}), encoding="utf-8"
    )
    (data_dir / "tags.json").write_text(
        json.dumps({"discoveryIntervalCycles": 3, "queries": [{"query": "태그", "label": "태그"}]}),
        encoding="utf-8",
    )
    (data_dir / "state.json").write_text(json.dumps({"cycleCount": 0}), encoding="utf-8")

    monkeypatch.setattr(track, "DATA_DIR", data_dir)
    monkeypatch.setattr(track, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(track, "CHANNELS_PATH", data_dir / "channels.json")
    monkeypatch.setattr(track, "TAGS_PATH", data_dir / "tags.json")
    monkeypatch.setattr(track, "STATE_PATH", data_dir / "state.json")
    monkeypatch.setattr(track, "STATUS_PATH", docs_dir / "status.json")

    monkeypatch.setattr(track, "fetch_channel_rss", lambda channel_id, session=None: [])
    monkeypatch.setattr(track, "fetch_channel_live_status", lambda channel_id, session=None: make_live_status(is_live=False))
    monkeypatch.setattr(track, "polite_sleep", lambda *a, **k: None)

    search_calls = []

    def fake_search(query, session=None, max_items=20):
        search_calls.append(query)
        return []

    monkeypatch.setattr(track, "search_live_by_query", fake_search)

    return {"data_dir": data_dir, "docs_dir": docs_dir, "search_calls": search_calls}


def test_main_runs_discovery_on_cycle_zero(project):
    track.main()
    status = json.loads((project["docs_dir"] / "status.json").read_text(encoding="utf-8"))
    assert status["discovery"]["updatedAt"] is not None
    assert project["search_calls"] == ["태그"]

    state = json.loads((project["data_dir"] / "state.json").read_text(encoding="utf-8"))
    assert state["cycleCount"] == 1


def test_main_skips_discovery_when_not_due(project):
    (project["data_dir"] / "state.json").write_text(json.dumps({"cycleCount": 1}), encoding="utf-8")
    track.main()
    assert project["search_calls"] == []  # cycle 1 % 3 != 0 이므로 검색 안 함

    state = json.loads((project["data_dir"] / "state.json").read_text(encoding="utf-8"))
    assert state["cycleCount"] == 2


def test_main_runs_discovery_again_on_third_cycle(project):
    (project["data_dir"] / "state.json").write_text(json.dumps({"cycleCount": 3}), encoding="utf-8")
    track.main()
    assert project["search_calls"] == ["태그"]
