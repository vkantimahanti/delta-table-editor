"""
Fetch a Confluence Cloud page as HTML or plain text (local use only).

Set credentials in environment or a local .env file (never commit tokens):

  CONFLUENCE_BASE_URL=https://example.atlassian.net/wiki
  CONFLUENCE_EMAIL=user@example.com
  CONFLUENCE_API_TOKEN=...

Usage:
  python scripts/fetch_confluence_page.py 123456789
  python scripts/fetch_confluence_page.py --url "https://example.atlassian.net/wiki/spaces/SPACE/pages/123456789/..."
  python scripts/fetch_confluence_page.py 123456789 --out documentation/confluence/example-page.html
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.confluence.local")
except ImportError:
    pass


def _auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _page_id_from_url(url: str) -> str:
    match = re.search(r"/pages/(\d+)", url)
    if not match:
        raise ValueError(f"Could not parse page id from URL: {url}")
    return match.group(1)


def fetch_page(
    page_id: str,
    *,
    base_url: str,
    email: str,
    token: str,
) -> dict:
    base = base_url.rstrip("/")
    api = f"{base}/rest/api/content/{page_id}?expand=body.storage,body.view,version,space,title"
    req = urllib.request.Request(
        api,
        headers={
            "Authorization": _auth_header(email, token),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Confluence API HTTP {exc.code}: {body}") from exc


def storage_to_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"</li>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<h[1-6][^>]*>", "\n\n", text, flags=re.I)
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Confluence page content")
    parser.add_argument("page_id", nargs="?", help="Numeric page id")
    parser.add_argument("--url", help="Full Confluence page URL")
    parser.add_argument("--out", help="Write output to file")
    parser.add_argument("--format", choices=("text", "html", "json"), default="text")
    args = parser.parse_args()

    page_id = args.page_id or (_page_id_from_url(args.url) if args.url else "")
    if not page_id:
        parser.error("Provide page_id or --url")

    base_url = os.environ.get(
        "CONFLUENCE_BASE_URL",
        "https://example.atlassian.net/wiki",
    ).strip()
    email = os.environ.get("CONFLUENCE_EMAIL", "").strip()
    token = os.environ.get("CONFLUENCE_API_TOKEN", "").strip()
    if not email or not token:
        print(
            "Set CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN in .env.confluence.local",
            file=sys.stderr,
        )
        return 1

    data = fetch_page(page_id, base_url=base_url, email=email, token=token)
    title = data.get("title", "")
    space = (data.get("space") or {}).get("key", "")
    version = (data.get("version") or {}).get("number", "?")
    storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")

    if args.format == "json":
        output = json.dumps(data, indent=2)
    elif args.format == "html":
        output = storage
    else:
        output = f"# {title}\n\nSpace: {space} | Page id: {page_id} | Version: {version}\n\n"
        output += storage_to_text(storage)

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
