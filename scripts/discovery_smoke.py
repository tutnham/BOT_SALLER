"""Resolve tg-parser-api and check health plus unauthenticated 401."""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request


def main() -> None:
    socket.getaddrinfo("tg-parser-api", 8000)
    base = os.environ["PARSER_API_URL"].rstrip("/")
    with urllib.request.urlopen(base + "/health") as response:
        if response.status != 200:
            raise SystemExit(response.status)
    try:
        urllib.request.urlopen(base + "/channels")
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise SystemExit(exc.code)
    else:
        raise SystemExit("expected 401")
    print("discovery ok")


if __name__ == "__main__":
    main()
