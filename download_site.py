#!/usr/bin/env python3
"""Mirror https://hashmind.fun/ into the current directory."""

from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://hashmind.fun"
ROOT = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; HashmindMirror/1.0)"

ASSET_RE = re.compile(
    r"""(?x)
    (?:href|src|poster)=["']([^"']+)["']
    | srcSet=["']([^"']+)["']
    | url\(\s*['"]?([^'")\s]+)['"]?\s*\)
    | ["'](/_next/[^"']+)["']
    | ["'](/art/[^"']+)["']
    | ["'](/[^"'\s]+\.(?:js|css|woff2?|ttf|otf|png|jpe?g|webp|avif|svg|gif|ico|json|mp4|webm|wasm|map))["']
    """
)

SKIP_SCHEMES = ("data:", "mailto:", "javascript:", "blob:", "#")


def same_origin_path(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith(SKIP_SCHEMES):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.netloc.lower() not in ("hashmind.fun", "www.hashmind.fun"):
            return None
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path
    if raw.startswith("/"):
        return raw.split("#", 1)[0]
    return None


def expand_srcset(value: str) -> list[str]:
    out: list[str] = []
    for part in value.split(","):
        token = part.strip().split()
        if token:
            out.append(token[0])
    return out


def extract_paths(html: str) -> set[str]:
    paths: set[str] = set()
    for match in ASSET_RE.finditer(html):
        groups = [g for g in match.groups() if g]
        for g in groups:
            candidates = expand_srcset(g) if "," in g and " " in g else [g]
            for c in candidates:
                p = same_origin_path(c)
                if p:
                    paths.add(p)
    return paths


def local_path_for(url_path: str) -> Path:
    parsed = urllib.parse.urlparse(url_path if "?" in url_path else f"//{url_path}")
    # urlparse with //path works poorly; handle manually
    if "?" in url_path:
        path_only, query = url_path.split("?", 1)
    else:
        path_only, query = url_path, ""

    if path_only.endswith("/") or path_only == "":
        path_only = (path_only.rstrip("/") or "") + "/index.html"

    # Next.js image optimizer URLs: keep query in filename
    if path_only.startswith("/_next/image") and query:
        safe = urllib.parse.quote(query, safe="")
        name = f"image_{safe[:180]}.bin"
        return ROOT / "_next" / "image" / name

    rel = path_only.lstrip("/")
    if not Path(rel).suffix and not path_only.startswith("/_next/"):
        # extensionless route -> index.html under folder
        rel = f"{rel}/index.html" if rel else "index.html"

    return ROOT / rel


def download(url_path: str, retries: int = 3) -> tuple[bool, bytes | None, str | None]:
    url = urllib.parse.urljoin(BASE + "/", url_path.lstrip("/"))
    if url_path.startswith("/"):
        url = BASE + url_path
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err: str | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return True, data, ctype
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (404, 403, 410):
                return False, None, last_err
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(0.5 * (attempt + 1))
    return False, None, last_err


def save_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def main() -> int:
    print(f"Mirroring {BASE} -> {ROOT}")

    ok, html_bytes, err = download("/")
    if not ok or html_bytes is None:
        print(f"Failed to fetch homepage: {err}", file=sys.stderr)
        return 1

    index = ROOT / "index.html"
    save_bytes(index, html_bytes)
    print(f"Saved index.html ({len(html_bytes)} bytes)")

    html = html_bytes.decode("utf-8", errors="replace")
    queue = extract_paths(html)
    # Seed common extras
    for extra in (
        "/favicon.ico",
        "/robots.txt",
        "/sitemap.xml",
        "/manifest.webmanifest",
        "/manifest.json",
        "/art/plate-hero-1536.webp",
    ):
        queue.add(extra)

    seen: set[str] = set()
    downloaded = 0
    failed = 0

    # Also follow CSS for url() references
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)

        dest = local_path_for(path)
        # Skip huge next/image optimizer variants; prefer original /art/ assets
        if path.startswith("/_next/image"):
            # Extract original url= param and queue that instead
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            if "url" not in qs:
                # path may be "/_next/image?url=..."
                qs = urllib.parse.parse_qs(path.split("?", 1)[-1] if "?" in path else "")
            orig = qs.get("url", [None])[0]
            if orig:
                decoded = urllib.parse.unquote(orig)
                p = same_origin_path(decoded) or (
                    decoded if decoded.startswith("/") else None
                )
                if p and p not in seen:
                    queue.add(p)
            continue

        print(f"GET {path}")
        success, data, info = download(path)
        if not success or data is None:
            print(f"  FAIL {info}")
            failed += 1
            continue

        # If HTML route without suffix, ensure .html path
        ctype = info or ""
        if "text/html" in ctype and dest.suffix == "":
            dest = dest.with_suffix("") / "index.html" if False else Path(str(dest) + "/index.html")

        save_bytes(dest, data)
        downloaded += 1
        print(f"  -> {dest.relative_to(ROOT)} ({len(data)} bytes)")

        if "text/css" in (ctype or "") or dest.suffix == ".css":
            text = data.decode("utf-8", errors="replace")
            for p in extract_paths(text):
                if p not in seen:
                    queue.add(p)
        elif dest.suffix in {".js", ".mjs"} or "javascript" in (ctype or ""):
            text = data.decode("utf-8", errors="replace")
            # Pull static chunk references
            for m in re.finditer(r'["\'](/_next/static/[^"\']+)["\']', text):
                p = m.group(1)
                if p not in seen:
                    queue.add(p)
            for m in re.finditer(r'["\'](/art/[^"\']+)["\']', text):
                p = m.group(1)
                if p not in seen:
                    queue.add(p)

        time.sleep(0.05)

    print(f"\nDone. downloaded={downloaded} failed={failed} discovered={len(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
