import pytest

from vtrade.copytrade import INFO_URL, LEADERBOARD_URL, TopTraders, bias_of, pick_leaders, position_in


def row(addr, account, month_pnl, month_vlm=1e6, alltime=0.0):
    return {
        "ethAddress": addr,
        "accountValue": str(account),
        "displayName": None,
        "windowPerformances": [
            ["day", {"pnl": "0", "roi": "0", "vlm": "0"}],
            ["week", {"pnl": "0", "roi": "0", "vlm": "0"}],
            ["month", {"pnl": str(month_pnl), "roi": "0.1", "vlm": str(month_vlm)}],
            ["allTime", {"pnl": str(alltime), "roi": "0.2", "vlm": "0"}],
        ],
    }


A, B, C, V, S = ("0x" + c * 40 for c in "abcde")
ROWS = [row(A, 5e6, 3e6), row(B, 2e6, 9e6), row(C, 8e6, 1e6), row(V, 9e8, 5e7, month_vlm=0), row(S, 5e4, 2e7)]


def state(coin="BTC", size=0.0, value=0.0, entry=80000.0, lev=10):
    positions = []
    if size:
        positions.append({"position": {"coin": coin, "szi": str(size), "positionValue": str(value), "entryPx": str(entry),
                                       "leverage": {"type": "cross", "value": lev}, "unrealizedPnl": "123.4", "liquidationPx": "70000"}})
    positions.append({"position": {"coin": "ETH", "szi": "5", "positionValue": "20000", "entryPx": "4000", "leverage": {"value": 5}}})
    return {"marginSummary": {"accountValue": "1000000"}, "assetPositions": positions}


def test_pick_leaders_skips_vaults_and_small_accounts():
    leaders = pick_leaders(ROWS, "month", 10, 1e6)
    assert [x.address for x in leaders] == [B, A, C]  # V has no volume (vault), S is too small
    assert leaders[0].pnl["month"] == 9e6


def test_position_parsing_and_bias():
    long = position_in(state(size=2.5, value=200000), "BTC")
    assert long["side"] == "long" and long["notional"] == 200000 and long["leverage"] == 10 and long["open_positions"] == 2
    flat = position_in(state(), "BTC")
    assert flat["side"] == "flat" and flat["notional"] == 0
    short = position_in(state(size=-1, value=100000), "BTC")
    assert short["side"] == "short"
    assert bias_of([long, short, flat]) == pytest.approx((200000 - 100000) / 300000)
    assert bias_of([flat]) is None


def test_consensus_from_leaderboard(cfg):
    states = {A: state(size=1, value=300000), B: state(size=-2, value=100000), C: state()}
    calls = {"get": 0}

    def get(url):
        assert url == LEADERBOARD_URL
        calls["get"] += 1
        return {"leaderboardRows": ROWS}

    def post(url, payload):
        assert url == INFO_URL and payload["type"] == "clearinghouseState"
        return states[payload["user"]]

    traders = TopTraders(cfg, get=get, post=post)
    c = traders.consensus()
    assert (c["longs"], c["shorts"], c["flat"], c["failed"]) == (1, 1, 1, 0)
    assert c["bias"] == pytest.approx(0.5)
    assert c["accounts_ranked"] == len(ROWS)
    # The leaderboard is cached on disk: a new instance doesn't download it again.
    TopTraders(cfg, get=get, post=post).consensus()
    assert calls["get"] == 1


def test_manual_leaders_and_failures(cfg):
    cfg.copy.leaders = [A, C]

    def post(url, payload):
        if payload["user"] == C:
            raise TimeoutError("slow")
        return state(size=1, value=50000)

    c = TopTraders(cfg, get=lambda url: {"leaderboardRows": ROWS}, post=post).consensus()
    assert [t["address"] for t in c["traders"]] == [A, C]
    assert c["source"] == "manual" and c["failed"] == 1 and c["bias"] == 1.0
