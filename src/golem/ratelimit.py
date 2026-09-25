"""Rate limits: a token bucket per key, and the client address to key them by (ADR 0012).

The buckets live in the memory of one replica. With N replicas behind a Service a client gets
up to N times the limit: there is no shared store in the stack to count in, and the hard,
shared limit on what costs money, starting runs, is the orchestrator's admission quota in
Postgres. These limits keep one client from swamping a replica and from filling the
insert-only audit log.
"""

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

DEFAULT_MAX_KEYS = 10_000

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Rate:
    per_minute: int
    burst: int

    def __post_init__(self) -> None:
        if self.per_minute <= 0 or self.burst <= 0:
            raise ValueError(f"a rate needs a positive rate and burst, got {self}")

    @property
    def per_second(self) -> float:
        return self.per_minute / 60


@dataclass(frozen=True)
class Bucket:
    tokens: float
    at: float
    # A refusal was reported since the key was last admitted: the audit-once rule.
    refusing: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Whole seconds until a token is back, for Retry-After (RFC 9110); 0 when allowed.
    retry_after: int = 0
    # The first refusal since the key was last admitted, the only one worth an audit row.
    first_refusal: bool = False


def refilled(bucket: Bucket, rate: Rate, now: float) -> Bucket:
    elapsed = max(0.0, now - bucket.at)
    tokens = min(float(rate.burst), bucket.tokens + elapsed * rate.per_second)
    return replace(bucket, tokens=tokens, at=max(now, bucket.at))


def taken(bucket: Bucket, rate: Rate, cost: int = 1) -> tuple[Bucket, Decision]:
    """Admit when a whole token is there, spending ``cost`` of it (0 only checks)."""
    if bucket.tokens >= 1:
        return replace(bucket, tokens=bucket.tokens - cost, refusing=False), Decision(True)
    wait = math.ceil((1 - bucket.tokens) / rate.per_second)
    refusal = Decision(False, retry_after=max(1, wait), first_refusal=not bucket.refusing)
    return replace(bucket, refusing=True), refusal


class Limiter:
    """Buckets by key, at most ``max_keys`` of them: the least recently used key is forgotten,
    so a flood of distinct keys costs bounded memory (a forgotten key starts full again)."""

    def __init__(
        self,
        rate: Rate,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, Bucket] = OrderedDict()
        # Callers run on one event loop, but verification runs in worker threads; a lock
        # keeps the read-modify-write whole wherever a caller is.
        self._lock = threading.Lock()

    def take(self, key: str) -> Decision:
        return self._decide(key, cost=1)

    def admits(self, key: str) -> Decision:
        """Whether a request would be admitted, without spending a token."""
        return self._decide(key, cost=0)

    @property
    def size(self) -> int:
        return len(self._buckets)

    def _decide(self, key: str, cost: int) -> Decision:
        with self._lock:
            now = self._clock()
            bucket = self._buckets.pop(key, None) or Bucket(tokens=float(self.rate.burst), at=now)
            bucket, decision = taken(refilled(bucket, self.rate, now), self.rate, cost)
            self._buckets[key] = bucket
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return decision


def parse_networks(text: str) -> tuple[Network, ...]:
    """Comma-separated CIDRs; a host address is its /32 or /128. Host bits set are refused,
    since ``10.0.0.1/8`` is more likely a typo than a meant ``10.0.0.0/8``."""
    return tuple(ipaddress.ip_network(part.strip()) for part in text.split(",") if part.strip())


def client_address(
    peer: str | None, forwarded_for: Iterable[str], trusted: tuple[Network, ...]
) -> str | None:
    """The client's address: the peer's, unless the peer is a trusted proxy.

    Behind trusted proxies, ``X-Forwarded-For`` is read from the right, where each proxy
    appended the address it saw, and the first hop that is not a trusted proxy is the client.
    Everything to its left was written by the client and is not believed. A malformed hop
    stops the walk at the nearest proxy.
    """
    current = _address(peer)
    if current is None:
        return None
    hops = [hop.strip() for header in forwarded_for for hop in header.split(",")]
    while _trusted(current, trusted) and hops:
        previous = _address(hops.pop())
        if previous is None:
            break
        current = previous
    return str(current)


def _address(text: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(text) if text else None
    except ValueError:
        return None
    # A dual-stack listener reports an IPv4 peer as ::ffff:a.b.c.d.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, trusted: tuple[Network, ...]
) -> bool:
    return any(address in network for network in trusted)
