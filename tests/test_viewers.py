import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import history as h  # noqa: E402

UTC = dt.timezone.utc


def T(y, mo, d, hh=0, mm=0):
    return dt.datetime(y, mo, d, hh, mm, tzinfo=UTC)


class Details:
    def __init__(self, start=None, end=None, game=None):
        self.start_timestamp = start
        self.end_timestamp = end
        self.game = game


def test_samples_recorded_each_cycle():
    hist = h.empty_history()
    for i, viewers in enumerate([10, 20, 30]):
        h.update_channel(
            hist, channel_id="UC1", name="c", is_live=True,
            now=T(2026, 9, 11, 1, i * 10), video_id="v", viewers=viewers,
        )
    samples = hist["open"]["UC1"]["samples"]
    assert [s[1] for s in samples] == [10, 20, 30]
    assert samples[0][0] == "2026-09-11T01:00:00Z"


def test_missing_viewer_count_does_not_create_sample():
    hist = h.empty_history()
    h.update_channel(hist, channel_id="UC1", name="c", is_live=True, now=T(2026, 9, 11, 1), video_id="v")
    assert hist["open"]["UC1"]["samples"] == []
    h.update_channel(hist, channel_id="UC1", name="c", is_live=True, now=T(2026, 9, 11, 1, 10), video_id="v", viewers=5)
    assert [s[1] for s in hist["open"]["UC1"]["samples"]] == [5]


def test_average_viewers_is_time_weighted():
    # 10분 간격으로 100, 100, 100 -> 평균 100
    samples = [["2026-09-11T01:00:00Z", 100], ["2026-09-11T01:10:00Z", 100], ["2026-09-11T01:20:00Z", 100]]
    assert h.average_viewers(samples) == 100.0

    # 앞 구간이 길면(1시간) 그 값이 더 크게 반영된다
    uneven = [["2026-09-11T01:00:00Z", 100], ["2026-09-11T02:00:00Z", 10], ["2026-09-11T02:10:00Z", 10]]
    avg = h.average_viewers(uneven)
    assert 60 < avg < 90, avg


def test_average_viewers_edge_cases():
    assert h.average_viewers([]) is None
    assert h.average_viewers(None) is None
    assert h.average_viewers([["2026-09-11T01:00:00Z", 42]]) == 42.0
    # 깨진 표본은 무시
    assert h.average_viewers([["nonsense", 5], ["2026-09-11T01:00:00Z", 10]]) == 10.0


def test_closed_session_keeps_samples_and_average():
    hist = h.empty_history()
    for i, v in enumerate([10, 30, 50]):
        h.update_channel(
            hist, channel_id="UC1", name="c", is_live=True,
            now=T(2026, 9, 11, 1, i * 10), video_id="v", viewers=v,
        )
    closed = h.update_channel(
        hist, channel_id="UC1", name="c", is_live=False, now=T(2026, 9, 11, 1, 40),
        end_lookup=lambda vid: Details(start="2026-09-11T00:55:00Z", end="2026-09-11T01:35:00Z"),
    )
    assert closed["peakViewers"] == 50
    assert closed["avgViewers"] is not None
    assert len(closed["samples"]) == 3
    # 종료 시점에 유튜브가 알려준 정확한 시작/종료 시각으로 보정되어야 한다
    assert closed["start"] == "2026-09-11T00:55:00Z"
    assert closed["end"] == "2026-09-11T01:35:00Z"
    assert closed["startSource"] == "youtube"
    assert closed["endSource"] == "youtube"
    assert closed["minutes"] == 40.0


def test_start_corrected_even_when_observed_late():
    """방송 시작 한참 뒤에 처음 관측했어도, 끝날 때 실제 시작 시각으로 보정된다."""
    hist = h.empty_history()
    h.update_channel(hist, channel_id="UC1", name="c", is_live=True, now=T(2026, 9, 11, 5), video_id="v", viewers=3)
    closed = h.update_channel(
        hist, channel_id="UC1", name="c", is_live=False, now=T(2026, 9, 11, 6),
        end_lookup=lambda vid: Details(start="2026-09-11T02:00:00Z", end="2026-09-11T05:50:00Z"),
    )
    assert closed["start"] == "2026-09-11T02:00:00Z"
    assert closed["minutes"] == 230.0


def test_thin_samples_keeps_shape_and_last_point():
    samples = [["2026-09-11T%02d:00:00Z" % (i % 24), i] for i in range(200)]
    thinned = h.thin_samples(samples, 20)
    assert len(thinned) <= 21
    assert thinned[0][1] == 0
    assert thinned[-1][1] == 199  # 마지막 점은 반드시 보존
    assert h.thin_samples([["t", 1]], 60) == [["t", 1]]
    assert h.thin_samples(None, 60) == []


def test_sample_cap_while_live():
    hist = h.empty_history()
    base = T(2026, 9, 11, 0)
    for i in range(h.MAX_SAMPLES_PER_SESSION + 5):
        h.update_channel(
            hist, channel_id="UC1", name="c", is_live=True,
            now=base + dt.timedelta(minutes=i), video_id="v", viewers=i,
        )
    assert len(hist["open"]["UC1"]["samples"]) <= h.MAX_SAMPLES_PER_SESSION + 5


def test_aggregate_exposes_viewer_stats_per_channel():
    hist = h.empty_history()
    hist["sessions"] = [
        {
            "channelId": "UC1", "name": "c", "start": "2026-09-11T01:00:00Z", "end": "2026-09-11T03:00:00Z",
            "minutes": 120, "isTargetGame": True, "avgViewers": 100, "peakViewers": 180, "samples": [],
        },
        {
            "channelId": "UC1", "name": "c", "start": "2026-09-11T05:00:00Z", "end": "2026-09-11T06:00:00Z",
            "minutes": 60, "isTargetGame": True, "avgViewers": 40, "peakViewers": 60, "samples": [],
        },
    ]
    summary = h.aggregate(hist, [{"id": "UC1", "name": "c"}], T(2026, 9, 11, 7))
    b = summary["channels"][0]["buckets"]["today"]
    assert b["peakViewers"] == 180
    # 방송 길이 가중평균: (100*120 + 40*60) / 180 = 80
    assert b["avgViewers"] == 80.0
    assert summary["totals"]["today"]["avgViewers"] == 80.0


def test_aggregate_includes_live_viewer_series():
    hist = h.empty_history()
    hist["open"] = {
        "UC1": {
            "channelId": "UC1", "name": "c", "videoId": "vid1",
            "start": "2026-09-11T01:00:00Z", "title": "이터널리턴", "isTargetGame": True,
            "peakViewers": 90,
            "samples": [["2026-09-11T01:00:00Z", 50], ["2026-09-11T01:10:00Z", 90]],
        }
    }
    summary = h.aggregate(hist, [{"id": "UC1", "name": "c"}], T(2026, 9, 11, 1, 20))
    entry = summary["channels"][0]
    assert entry["isLive"] is True
    assert entry["liveViewers"] == 90
    assert entry["livePeakViewers"] == 90
    assert entry["liveAvgViewers"] is not None
    assert len(entry["liveSamples"]) == 2
    assert entry["liveVideoUrl"].endswith("vid1")


def test_recent_sessions_carry_samples_only_for_recent_ones():
    hist = h.empty_history()
    now = T(2026, 9, 11, 12)
    sessions = []
    for i in range(5):
        day = 11 - i
        sessions.append({
            "channelId": "UC1", "name": "c",
            "start": "2026-09-%02dT01:00:00Z" % day, "end": "2026-09-%02dT03:00:00Z" % day,
            "minutes": 120, "isTargetGame": True, "peakViewers": 10,
            "samples": [["2026-09-%02dT01:00:00Z" % day, 10], ["2026-09-%02dT02:00:00Z" % day, 20]],
        })
    hist["sessions"] = sessions
    summary = h.aggregate(hist, [{"id": "UC1", "name": "c"}], now)
    recent = summary["channels"][0]["recentSessions"]
    assert len(recent) == 5
    # 최근 3개만 표본을 싣는다
    assert sum(1 for s in recent if s["samples"]) == 3
    # 전체 최근 목록에는 표본을 싣지 않는다 (페이지 무게 방어)
    assert all("samples" not in s for s in summary["recentSessions"])
    assert all(s.get("avgViewers") is not None for s in summary["recentSessions"])


def test_prune_drops_samples_for_old_sessions_but_keeps_average():
    hist = h.empty_history()
    hist["sessions"] = [{
        "channelId": "UC1", "start": "2026-06-01T01:00:00Z", "end": "2026-06-01T03:00:00Z",
        "samples": [["2026-06-01T01:00:00Z", 10], ["2026-06-01T02:00:00Z", 30]],
    }]
    h.prune(hist, T(2026, 9, 11))
    s = hist["sessions"][0]
    assert s["samples"] == []
    assert s["avgViewers"] is not None
