import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import history as h  # noqa: E402

UTC = dt.timezone.utc


def T(y, mo, d, hh=0, mm=0):
    return dt.datetime(y, mo, d, hh, mm, tzinfo=UTC)


class FakeDetails:
    def __init__(self, end_timestamp=None, game=None):
        self.end_timestamp = end_timestamp
        self.game = game


def test_is_target_game_matches_title_and_game():
    kws = h.DEFAULT_GAME_KEYWORDS
    assert h.is_target_game("오늘도 이터널리턴 랭크", None, kws)
    assert h.is_target_game(None, "Eternal Return", kws)
    assert h.is_target_game("エターナルリターン やる", None, kws)
    assert not h.is_target_game("리그오브레전드 방송", "League of Legends", kws)


def test_is_target_game_ignores_spacing_and_case():
    kws = ["eternal return"]
    assert h.is_target_game("ETERNALRETURN stream", None, kws)
    assert h.is_target_game("Eternal   Return", None, kws)


def test_session_starts_uses_youtube_timestamp():
    hist = h.empty_history()
    h.update_channel(
        hist,
        channel_id="UC1",
        name="채널1",
        is_live=True,
        now=T(2026, 9, 10, 12, 5),
        video_id="vid1",
        title="이터널리턴 방송",
        started_at="2026-09-10T11:40:00Z",
    )
    open_session = hist["open"]["UC1"]
    assert open_session["start"] == "2026-09-10T11:40:00Z"
    assert open_session["startSource"] == "youtube"
    assert open_session["isTargetGame"] is True


def test_session_falls_back_to_observed_start():
    hist = h.empty_history()
    h.update_channel(
        hist, channel_id="UC1", name="채널1", is_live=True, now=T(2026, 9, 10, 12, 0), video_id="v"
    )
    assert hist["open"]["UC1"]["start"] == "2026-09-10T12:00:00Z"
    assert hist["open"]["UC1"]["startSource"] == "observed"


def test_session_closes_with_youtube_end_timestamp():
    hist = h.empty_history()
    h.update_channel(
        hist,
        channel_id="UC1",
        name="채널1",
        is_live=True,
        now=T(2026, 9, 10, 12, 0),
        video_id="vid1",
        started_at="2026-09-10T11:00:00Z",
    )
    closed = h.update_channel(
        hist,
        channel_id="UC1",
        name="채널1",
        is_live=False,
        now=T(2026, 9, 10, 12, 10),
        end_lookup=lambda vid: FakeDetails(end_timestamp="2026-09-10T12:03:00Z"),
    )
    assert closed is not None
    assert closed["start"] == "2026-09-10T11:00:00Z"
    assert closed["end"] == "2026-09-10T12:03:00Z"
    assert closed["endSource"] == "youtube"
    assert closed["minutes"] == 63.0
    assert hist["open"] == {}
    assert len(hist["sessions"]) == 1


def test_session_closes_with_observed_time_when_lookup_fails():
    hist = h.empty_history()
    h.update_channel(
        hist, channel_id="UC1", name="채널1", is_live=True, now=T(2026, 9, 10, 12, 0), video_id="vid1"
    )

    def boom(vid):
        raise RuntimeError("네트워크 오류")

    closed = h.update_channel(
        hist, channel_id="UC1", name="채널1", is_live=False, now=T(2026, 9, 10, 12, 30), end_lookup=boom
    )
    assert closed["end"] == "2026-09-10T12:30:00Z"
    assert closed["endSource"] == "observed"
    assert closed["minutes"] == 30.0


def test_peak_viewers_tracked_across_cycles():
    hist = h.empty_history()
    h.update_channel(
        hist, channel_id="UC1", name="c", is_live=True, now=T(2026, 9, 10, 1), video_id="v", viewers=10
    )
    h.update_channel(
        hist, channel_id="UC1", name="c", is_live=True, now=T(2026, 9, 10, 2), video_id="v", viewers=50
    )
    h.update_channel(
        hist, channel_id="UC1", name="c", is_live=True, now=T(2026, 9, 10, 3), video_id="v", viewers=20
    )
    assert hist["open"]["UC1"]["peakViewers"] == 50


def test_new_video_closes_previous_session():
    """방송을 껐다 켠 사이에 확인 주기가 걸리지 않아 영상만 바뀐 경우."""
    hist = h.empty_history()
    h.update_channel(
        hist,
        channel_id="UC1",
        name="c",
        is_live=True,
        now=T(2026, 9, 10, 1),
        video_id="vidA",
        started_at="2026-09-10T01:00:00Z",
    )
    h.update_channel(
        hist,
        channel_id="UC1",
        name="c",
        is_live=True,
        now=T(2026, 9, 10, 5),
        video_id="vidB",
        started_at="2026-09-10T04:30:00Z",
        end_lookup=lambda vid: FakeDetails(end_timestamp="2026-09-10T03:00:00Z"),
    )
    assert len(hist["sessions"]) == 1
    assert hist["sessions"][0]["videoId"] == "vidA"
    assert hist["sessions"][0]["end"] == "2026-09-10T03:00:00Z"
    assert hist["open"]["UC1"]["videoId"] == "vidB"


def test_target_game_detected_later_in_stream_sticks():
    hist = h.empty_history()
    h.update_channel(
        hist, channel_id="UC1", name="c", is_live=True, now=T(2026, 9, 10, 1), video_id="v", title="잡담"
    )
    assert hist["open"]["UC1"]["isTargetGame"] is False
    h.update_channel(
        hist,
        channel_id="UC1",
        name="c",
        is_live=True,
        now=T(2026, 9, 10, 2),
        video_id="v",
        title="이터널리턴 시작",
    )
    assert hist["open"]["UC1"]["isTargetGame"] is True


def test_aggregate_includes_registered_channel_with_no_history():
    """등록만 되어 있고 방송 기록이 없는 채널도 목록에 반드시 나와야 한다."""
    hist = h.empty_history()
    channels = [{"id": "UC1", "name": "무기록채널"}]
    summary = h.aggregate(hist, channels, T(2026, 9, 10, 12))
    assert len(summary["channels"]) == 1
    entry = summary["channels"][0]
    assert entry["channelId"] == "UC1"
    assert entry["name"] == "무기록채널"
    assert entry["registered"] is True
    assert entry["isLive"] is False
    assert entry["buckets"]["today"]["totalMinutes"] == 0.0


def test_aggregate_all_registered_channels_present():
    hist = h.empty_history()
    channels = [{"id": f"UC{i}", "name": f"채널{i}"} for i in range(21)]
    summary = h.aggregate(hist, channels, T(2026, 9, 10, 12))
    assert len(summary["channels"]) == 21
    assert {c["channelId"] for c in summary["channels"]} == {f"UC{i}" for i in range(21)}


def test_aggregate_splits_session_across_kst_midnight():
    """KST 자정을 넘긴 방송은 어제/오늘로 나눠서 계산되어야 한다.

    2026-09-10 14:00Z ~ 16:00Z = KST 9/10 23:00 ~ 9/11 01:00.
    KST 기준 '오늘'이 9/11이면 오늘 몫은 60분이어야 한다.
    """
    hist = h.empty_history()
    hist["sessions"] = [
        {
            "channelId": "UC1",
            "name": "c",
            "start": "2026-09-10T14:00:00Z",
            "end": "2026-09-10T16:00:00Z",
            "minutes": 120,
            "isTargetGame": True,
        }
    ]
    channels = [{"id": "UC1", "name": "c"}]
    now = dt.datetime(2026, 9, 10, 17, 0, tzinfo=UTC)  # KST 9/11 02:00
    summary = h.aggregate(hist, channels, now)
    today = summary["channels"][0]["buckets"]["today"]
    assert today["totalMinutes"] == 60.0
    assert today["gameMinutes"] == 60.0
    # 이번 달 전체로는 120분 다 들어간다
    assert summary["channels"][0]["buckets"]["month"]["totalMinutes"] == 120.0


def test_aggregate_counts_only_target_game_in_game_minutes():
    hist = h.empty_history()
    hist["sessions"] = [
        {
            "channelId": "UC1",
            "name": "c",
            "start": "2026-09-10T01:00:00Z",
            "end": "2026-09-10T03:00:00Z",
            "minutes": 120,
            "title": "이터널리턴 랭크",
            "isTargetGame": True,
        },
        {
            "channelId": "UC1",
            "name": "c",
            "start": "2026-09-10T04:00:00Z",
            "end": "2026-09-10T05:00:00Z",
            "minutes": 60,
            "title": "그냥 잡담",
            "isTargetGame": False,
        },
    ]
    channels = [{"id": "UC1", "name": "c"}]
    summary = h.aggregate(hist, channels, dt.datetime(2026, 9, 10, 6, tzinfo=UTC))
    b = summary["channels"][0]["buckets"]["today"]
    assert b["totalMinutes"] == 180.0
    assert b["gameMinutes"] == 120.0
    assert b["totalHours"] == 3.0
    assert b["gameHours"] == 2.0


def test_aggregate_includes_ongoing_session_up_to_now():
    hist = h.empty_history()
    hist["open"] = {
        "UC1": {
            "channelId": "UC1",
            "name": "c",
            "start": "2026-09-10T02:00:00Z",
            "title": "이터널리턴",
            "isTargetGame": True,
        }
    }
    channels = [{"id": "UC1", "name": "c"}]
    summary = h.aggregate(hist, channels, dt.datetime(2026, 9, 10, 3, 30, tzinfo=UTC))
    entry = summary["channels"][0]
    assert entry["isLive"] is True
    assert entry["buckets"]["today"]["totalMinutes"] == 90.0
    assert entry["liveSince"] == "2026-09-10T02:00:00Z"


def test_aggregate_week_starts_monday_kst():
    # 2026-09-10 is a Thursday. Week start = Monday 2026-09-07 00:00 KST.
    hist = h.empty_history()
    hist["sessions"] = [
        {  # Sunday 9/6 KST -> 지난 주
            "channelId": "UC1", "name": "c",
            "start": "2026-09-05T15:00:00Z", "end": "2026-09-05T16:00:00Z",
            "minutes": 60, "isTargetGame": True,
        },
        {  # Monday 9/7 KST -> 이번 주
            "channelId": "UC1", "name": "c",
            "start": "2026-09-06T15:30:00Z", "end": "2026-09-06T16:30:00Z",
            "minutes": 60, "isTargetGame": True,
        },
    ]
    channels = [{"id": "UC1", "name": "c"}]
    summary = h.aggregate(hist, channels, dt.datetime(2026, 9, 10, 3, tzinfo=UTC))
    assert summary["channels"][0]["buckets"]["week"]["totalMinutes"] == 60.0


def test_aggregate_keeps_history_of_removed_channel_but_marks_unregistered():
    hist = h.empty_history()
    hist["sessions"] = [
        {
            "channelId": "UCOLD", "name": "삭제된채널",
            "start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z",
            "minutes": 60, "isTargetGame": True,
        }
    ]
    channels = [{"id": "UC1", "name": "현재채널"}]
    summary = h.aggregate(hist, channels, dt.datetime(2026, 9, 10, 3, tzinfo=UTC))
    by_id = {c["channelId"]: c for c in summary["channels"]}
    assert by_id["UC1"]["registered"] is True
    assert by_id["UCOLD"]["registered"] is False


def test_prune_drops_ancient_sessions():
    hist = h.empty_history()
    hist["sessions"] = [
        {"channelId": "UC1", "start": "2020-01-01T00:00:00Z", "end": "2020-01-01T01:00:00Z"},
        {"channelId": "UC1", "start": "2026-09-09T00:00:00Z", "end": "2026-09-09T01:00:00Z"},
    ]
    h.prune(hist, dt.datetime(2026, 9, 10, tzinfo=UTC))
    assert len(hist["sessions"]) == 1
    assert hist["sessions"][0]["start"] == "2026-09-09T00:00:00Z"


def test_parse_iso_handles_offsets_and_junk():
    assert h.parse_iso("2026-09-10T12:00:00Z") == T(2026, 9, 10, 12)
    assert h.parse_iso("2026-09-10T21:00:00+09:00") == T(2026, 9, 10, 12)
    assert h.parse_iso(None) is None
    assert h.parse_iso("나중에") is None
