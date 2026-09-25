import ipaddress
import threading

import pytest

from golem.ratelimit import (
    Bucket,
    Limiter,
    Rate,
    address_key,
    client_address,
    parse_networks,
    refilled,
    taken,
)

RATE = Rate(per_minute=60, burst=3)
PROXIES = parse_networks("10.0.0.0/8, fd00::/8")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- Pure refill and take ------------------------------------------------------------------------


def test_a_bucket_refills_at_the_rate_up_to_the_burst() -> None:
    assert refilled(Bucket(tokens=0, at=0), RATE, now=1.5).tokens == pytest.approx(1.5)
    assert refilled(Bucket(tokens=0, at=0), RATE, now=3600).tokens == 3


def test_a_clock_going_backwards_adds_nothing() -> None:
    bucket = refilled(Bucket(tokens=1, at=10), RATE, now=5)

    assert bucket.tokens == 1
    assert bucket.at == 10


def test_taking_needs_a_whole_token() -> None:
    bucket, decision = taken(Bucket(tokens=1, at=0), RATE)
    assert decision.allowed and bucket.tokens == 0

    _, refused = taken(Bucket(tokens=0.25, at=0), RATE)
    assert not refused.allowed
    # 0.75 tokens at one per second: Retry-After is whole seconds, rounded up.
    assert refused.retry_after == 1


def test_retry_after_is_the_wait_for_one_token() -> None:
    _, refused = taken(Bucket(tokens=0, at=0), Rate(per_minute=6, burst=1))

    assert refused.retry_after == 10


def test_only_the_first_refusal_after_an_admission_is_reported_first() -> None:
    bucket, first = taken(Bucket(tokens=0, at=0), RATE)
    bucket, second = taken(bucket, RATE)
    bucket, allowed = taken(Bucket(tokens=1, at=0, refusing=True), RATE)
    _, again = taken(bucket, RATE)

    assert first.first_refusal and not second.first_refusal
    assert allowed.allowed and not allowed.first_refusal
    assert again.first_refusal


@pytest.mark.parametrize(("per_minute", "burst"), [(0, 1), (1, 0), (-1, 5)])
def test_a_rate_must_be_positive(per_minute: int, burst: int) -> None:
    with pytest.raises(ValueError):
        Rate(per_minute=per_minute, burst=burst)


# --- The keyed limiter ---------------------------------------------------------------------------


def test_each_key_has_its_own_bucket() -> None:
    limiter = Limiter(RATE, clock=Clock())

    assert [limiter.take("a").allowed for _ in range(4)] == [True, True, True, False]
    assert limiter.take("b").allowed


def test_tokens_come_back_with_time() -> None:
    clock = Clock()
    limiter = Limiter(RATE, clock=clock)
    for _ in range(3):
        limiter.take("a")

    refused = limiter.take("a")
    clock.now += refused.retry_after

    assert not refused.allowed
    assert limiter.take("a").allowed
    assert not limiter.take("a").allowed


def test_admits_checks_without_taking() -> None:
    limiter = Limiter(Rate(per_minute=60, burst=1), clock=Clock())

    assert limiter.admits("a").allowed
    assert limiter.admits("a").allowed
    assert limiter.take("a").allowed
    refused = limiter.admits("a")
    assert not refused.allowed and refused.first_refusal
    assert not limiter.admits("a").first_refusal


def test_memory_is_bounded_by_evicting_the_least_recently_used_key() -> None:
    limiter = Limiter(Rate(per_minute=1, burst=1), max_keys=2, clock=Clock())
    limiter.take("a")
    limiter.take("b")
    limiter.take("a")  # refused, and now the most recently used
    limiter.take("c")  # evicts b

    assert limiter.size == 2
    assert not limiter.take("a").allowed
    assert limiter.take("b").allowed  # forgotten, so a fresh bucket


def test_a_flood_of_distinct_keys_stays_within_the_bound() -> None:
    limiter = Limiter(RATE, max_keys=100, clock=Clock())

    for i in range(10_000):
        limiter.take(f"10.1.{i // 256}.{i % 256}")

    assert limiter.size == 100


def test_concurrent_takes_never_hand_out_more_than_the_burst() -> None:
    limiter = Limiter(Rate(per_minute=1, burst=50), clock=Clock())
    allowed: list[bool] = []

    def worker() -> None:
        for _ in range(100):
            allowed.append(limiter.take("a").allowed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(allowed) == 50


# --- Client address ------------------------------------------------------------------------------


def test_the_peer_is_the_client_when_it_is_not_a_trusted_proxy() -> None:
    assert client_address("203.0.113.5", [], PROXIES) == "203.0.113.5"


def test_forwarded_for_from_an_untrusted_peer_is_ignored() -> None:
    spoofed = client_address("203.0.113.5", ["198.51.100.1"], PROXIES)

    assert spoofed == "203.0.113.5"


def test_behind_a_trusted_proxy_the_right_most_untrusted_address_is_the_client() -> None:
    # The client put 198.51.100.1 in the header itself; the proxy appended what it saw.
    address = client_address("10.0.0.2", ["198.51.100.1, 203.0.113.9"], PROXIES)

    assert address == "203.0.113.9"


def test_trusted_hops_are_skipped_across_several_headers() -> None:
    address = client_address("10.0.0.2", ["198.51.100.1, 203.0.113.9", "10.0.0.3"], PROXIES)

    assert address == "203.0.113.9"


def test_when_every_hop_is_trusted_the_left_most_one_is_the_client() -> None:
    assert client_address("10.0.0.2", ["10.0.0.4, 10.0.0.3"], PROXIES) == "10.0.0.4"


def test_a_trusted_proxy_without_the_header_is_itself_the_client() -> None:
    assert client_address("10.0.0.2", [], PROXIES) == "10.0.0.2"


def test_a_malformed_hop_stops_the_walk_at_the_nearest_trusted_address() -> None:
    address = client_address("10.0.0.2", ["203.0.113.9, not-an-ip, 10.0.0.3"], PROXIES)

    assert address == "10.0.0.3"


def test_addresses_are_normalized() -> None:
    assert client_address("fd00::0:1", ["2001:DB8::0:5"], PROXIES) == "2001:db8::5"


def test_an_ipv4_mapped_peer_is_its_ipv4_address() -> None:
    assert client_address("::ffff:10.0.0.2", ["203.0.113.9"], PROXIES) == "203.0.113.9"


def test_a_peer_that_is_not_an_address_is_no_address() -> None:
    assert client_address("testclient", ["198.51.100.1"], PROXIES) is None
    assert client_address(None, [], PROXIES) is None


def test_trusted_proxies_are_parsed_as_networks() -> None:
    assert parse_networks("") == ()
    assert parse_networks(" 10.0.0.0/8 ,192.0.2.7") == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("192.0.2.7/32"),
    )
    with pytest.raises(ValueError):
        parse_networks("10.0.0.1/8")
    with pytest.raises(ValueError):
        parse_networks("proxy.example.test")


def test_an_ipv4_client_is_limited_by_its_address() -> None:
    assert address_key("203.0.113.7") == "203.0.113.7"


def test_an_ipv6_client_is_limited_by_its_slash_64() -> None:
    # One host commonly holds a whole /64 and can rotate through it; a /128 key would give
    # it a fresh bucket per request.
    assert address_key("2001:db8:1:2:aaaa::1") == address_key("2001:db8:1:2:ffff::9")
    assert address_key("2001:db8:1:2::1") != address_key("2001:db8:1:3::1")


def test_no_address_shares_one_key() -> None:
    assert address_key(None) == address_key(None) == "unknown"
