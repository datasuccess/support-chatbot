"""Client address resolution behind a reverse proxy.

Using `request.client.host` directly is correct when the app is exposed straight
to the internet, and badly wrong behind nginx: every request then carries the
proxy's address, so per-IP rate limiting collapses into a single shared bucket and
one abusive client throttles everybody.

Blindly trusting `X-Forwarded-For` is the opposite failure — anyone can set it and
sidestep the limiter entirely. So the header is honoured only when the immediate
peer is a configured trusted proxy.
"""
import ipaddress

from fastapi import Request

from app.core.config import settings


def _trusted_networks() -> list:
    nets = []
    for entry in settings.trusted_proxies_list:
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return nets


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if not peer:
        return "unknown"

    nets = _trusted_networks()
    if not nets:
        return peer

    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer

    if not any(peer_addr in net for net in nets):
        return peer  # direct client; the header is not ours to trust

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer
    # Left-most entry is the original client; the rest were appended by proxies.
    return forwarded.split(",")[0].strip() or peer
