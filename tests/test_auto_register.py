import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import track  # noqa: E402
import history as h  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
KWS = h.DEFAULT_GAME_KEYWORDS


class Details:
    def __init__(self, title=None, game=None):
        self.title = title
        self.game = game


def discovery(items):
    return {"items": items}


def item(channel_id, video_id="vid", title="방송", channel_title="채널", query="이터널리턴"):
    return {
        "channelId": channel_id,
        "videoId": video_id,
        "title": title,
        "channelTitle": channel_title,
        "matchedQuery": query,
    }


def test_registers_unregistered_channel_playing_target_game():
    channels = []
    added, warnings = track.auto_register_discovered(
        channels,
        discovery([item("UCNEW", title="아무 제목", channel_title="새채널")]),
        keywords=KWS,
        max_channels=60,
        session=None,
        now=NOW,
        verifier=lambda vid: Details(title="아무 제목", game="Eternal Return"),
    )
    assert len(added) == 1
    assert channels[0]["id"] == "UCNEW"
    assert channels[0]["name"] == "새채널"
    assert channels[0]["autoAdded"] is True
    assert channels[0]["autoAddedGame"] == "Eternal Return"
    assert warnings == []


def test_skips_channel_playing_other_game():
    """검색어에 걸렸을 뿐 실제로는 다른 게임이면 등록하지 않는다."""
    channels = []
    added, _ = track.auto_register_discovered(
        channels,
        discovery([item("UCOTHER", title="발로란트 한판")]),
        keywords=KWS,
        max_channels=60,
        session=None,
        now=NOW,
        verifier=lambda vid: Details(title="발로란트 한판", game="VALORANT"),
    )
    assert added == []
    assert channels == []


def test_skips_already_registered_channel_without_network_check():
    channels = [{"id": "UCKNOWN", "name": "이미있음"}]

    def should_not_be_called(vid):
        raise AssertionError("이미 등록된 채널은 확인 요청을 보내면 안 됩니다")

    added, _ = track.auto_register_discovered(
        channels,
        discovery([item("UCKNOWN")]),
        keywords=KWS,
        max_channels=60,
        session=None,
        now=NOW,
        verifier=should_not_be_called,
    )
    assert added == []
    assert len(channels) == 1


def test_respects_max_channels_cap():
    channels = [{"id": f"UC{i}", "name": f"채널{i}"} for i in range(5)]
    added, warnings = track.auto_register_discovered(
        channels,
        discovery([item("UCNEW1"), item("UCNEW2")]),
        keywords=KWS,
        max_channels=5,
        session=None,
        now=NOW,
        verifier=lambda vid: Details(game="Eternal Return"),
    )
    assert added == []
    assert len(channels) == 5
    assert any("상한" in w for w in warnings)


def test_verification_failure_skips_channel_and_retries_later():
    channels = []
    added, _ = track.auto_register_discovered(
        channels,
        discovery([item("UCNEW")]),
        keywords=KWS,
        max_channels=60,
        session=None,
        now=NOW,
        verifier=lambda vid: None,  # 확인 실패
    )
    assert added == []
    assert channels == []  # 다음 사이클에 다시 시도


def test_title_only_match_when_game_metadata_missing():
    """게임 메타데이터가 없어도 제목이 키워드에 맞으면 등록한다."""
    channels = []
    added, _ = track.auto_register_discovered(
        channels,
        discovery([item("UCNEW", title="이터널리턴 랭크 방송")]),
        keywords=KWS,
        max_channels=60,
        session=None,
        now=NOW,
        verifier=lambda vid: Details(title="이터널리턴 랭크 방송", game=None),
    )
    assert len(added) == 1


def test_no_duplicate_when_same_channel_appears_twice():
    channels = []
    added, _ = track.auto_register_discovered(
        channels,
        discovery([
            item("UCNEW", video_id="v1", query="이터널리턴"),
            item("UCNEW", video_id="v2", query="エターナルリターン"),
        ]),
        keywords=KWS,
        max_channels=60,
        session=None,
        now=NOW,
        verifier=lambda vid: Details(game="Eternal Return"),
    )
    assert len(added) == 1
    assert len(channels) == 1


def test_empty_discovery_is_noop():
    channels = []
    added, warnings = track.auto_register_discovered(
        channels, discovery([]), keywords=KWS, max_channels=60, session=None, now=NOW,
        verifier=lambda vid: Details(game="Eternal Return"),
    )
    assert added == []
    assert warnings == []
