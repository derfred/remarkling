"""Command line: inkbridge push | poll | inbox | register | serve | poller."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from .config import get_settings


async def _push(args) -> None:
    from .core.pipeline import get_bridge

    bridge = get_bridge()
    try:
        if args.source.startswith(("http://", "https://")):
            result = await bridge.push_document(args.title, url=args.source, ready_selector=args.ready_selector)
        else:
            path = Path(args.source)
            data = path.read_bytes()
            title = args.title or path.stem
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                result = await bridge.push_document(title, pdf=data, source_ref=str(path))
            elif suffix in (".md", ".markdown", ".txt"):
                result = await bridge.push_document(title, markdown=data.decode(), source_ref=str(path))
            else:
                result = await bridge.push_document(
                    title, html=data.decode(), ready_selector=args.ready_selector, source_ref=str(path)
                )
        print(json.dumps(asdict(result), indent=2))
    finally:
        await bridge.aclose()


async def _poll(args) -> None:
    from .core.pipeline import get_bridge

    bridge = get_bridge()
    try:
        print(json.dumps([asdict(d) for d in await bridge.poll_outbox()], indent=2))
    finally:
        await bridge.aclose()


async def _inbox(args) -> None:
    from .core.pipeline import get_bridge

    bridge = get_bridge()
    try:
        print(json.dumps(await bridge.list_inbox(), indent=2))
    finally:
        await bridge.aclose()


async def _register(args) -> None:
    from .core.transport.cloud import register_device

    token = await register_device(args.code, get_settings().rm_auth_host)
    print("Store this as RM_DEVICE_TOKEN:")
    print(token)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="inkbridge")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("push", help="render a file or URL and upload it to /Inbox")
    p.add_argument("source", help="HTML, Markdown or PDF file, or an http(s) URL")
    p.add_argument("--title")
    p.add_argument("--ready-selector")
    p.set_defaults(func=_push)

    sub.add_parser("poll", help="process /Outbox now").set_defaults(func=_poll)
    sub.add_parser("inbox", help="list /Inbox").set_defaults(func=_inbox)

    p = sub.add_parser("register", help="exchange a one-time code for a device token")
    p.add_argument("code", help="8-letter code from my.remarkable.com/device/desktop/connect")
    p.set_defaults(func=_register)

    p = sub.add_parser("serve", help="run the MCP + HTTP server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)

    sub.add_parser("poller", help="run the background poller")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr)
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("inkbridge.api:app", host=args.host, port=args.port, proxy_headers=True)
    elif args.cmd == "poller":
        from .poller import main as poller_main

        poller_main()
    else:
        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
