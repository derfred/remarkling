"""Command line: inkbridge push | pull | status | docs | poll | inbox | register | serve | poller."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from . import logs
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
                result = await bridge.push_document(title, pdf=data, source_ref=str(path), layout=args.layout)
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
        print(json.dumps(asdict(await bridge.poll()), indent=2))
    finally:
        await bridge.aclose()


async def _pull(args) -> None:
    from .core.pipeline import get_bridge

    bridge = get_bridge()
    try:
        print(json.dumps([asdict(o) for o in await bridge.pull_document(args.doc_id, force=args.force)], indent=2))
    finally:
        await bridge.aclose()


async def _status(args) -> None:
    from .core.pipeline import get_bridge

    bridge = get_bridge()
    try:
        if args.doc_id:
            print(json.dumps(await bridge.document_status(args.doc_id), indent=2))
        else:
            print(json.dumps(await bridge.list_documents(changed_only=args.changed), indent=2))
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
    from .core.cloud import register_device

    token = await register_device(args.code, get_settings().rm_auth_host)
    print("Store this as RM_DEVICE_TOKEN:")
    print(token)


def main(argv: list[str] | None = None) -> None:
    load_dotenv(find_dotenv(usecwd=True))
    parser = argparse.ArgumentParser(prog="inkbridge")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("push", help="render a file or URL and upload it to /Inbox")
    p.add_argument("source", help="HTML, Markdown or PDF file, or an http(s) URL")
    p.add_argument("--title")
    p.add_argument("--ready-selector")
    p.add_argument("--layout", choices=["pages", "tall"], default="pages",
                   help="PDFs: keep their pages, or join them into tall pages (see README)")
    p.set_defaults(func=_push)

    p = sub.add_parser("pull", help="fetch a pushed doc's annotations from wherever it is on the tablet")
    p.add_argument("doc_id")
    p.add_argument("--force", action="store_true", help="pull even if the ink has not changed")
    p.set_defaults(func=_pull)

    p = sub.add_parser("status", help="tracked docs and whether they have new ink (one doc if given)")
    p.add_argument("doc_id", nargs="?")
    p.add_argument("--changed", action="store_true", help="only docs with ink not yet pulled")
    p.set_defaults(func=_status)

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
    logs.setup(logging.DEBUG if args.verbose else None)
    if args.cmd == "serve":
        import uvicorn

        # log_config=None: keep our JSON handler instead of uvicorn's own format.
        uvicorn.run("inkbridge.api:app", host=args.host, port=args.port, proxy_headers=True, log_config=None)
    elif args.cmd == "poller":
        from .poller import main as poller_main

        poller_main()
    else:
        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
