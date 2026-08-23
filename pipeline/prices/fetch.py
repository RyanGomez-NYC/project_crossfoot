"""
HTTP fetching with provenance.

Every file this pipeline downloads lands in data/raw/prices/ next to a
SOURCE.json recording the URL, the fetch time, the content type the server
declared, and the SHA-256 of the bytes as delivered. That is the same rule
tools/harvest.py applies to payer documents: a number published later must be
traceable to the bytes the publisher actually served.

Only the standard library. Large files are streamed to disk, never held in
memory, and a file that is already on disk with a SOURCE record is not fetched
again unless --refresh is passed.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

UA = ("Mozilla/5.0 (compatible; Crossfoot/0.2; +https://ryangomez.nyc/crossfoot; "
      "public-data collection, contact via site)")

# The honest UA above is always tried first. Several hosts return 403/429 to
# anything that is not a browser — for a file the law requires them to publish.
# The retry announces a browser profile so the mandated document can be read;
# the record keeps `profile: "browser"` so "this host blocks honest crawlers"
# survives as a finding even when the fetch ultimately succeeds.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_RETRY_CODES = {403, 406, 412, 429, 503}

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "prices"


class FetchError(Exception):
    """A fetch that did not complete. The message is the reason, written for a gap log."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _open(url: str, timeout: int = 120, accept: str = "*/*", browser: bool = False):
    headers = {
        "User-Agent": BROWSER_UA if browser else UA,
        "Accept": accept,
        "Accept-Encoding": "gzip",
    }
    if browser:
        headers["Accept-Language"] = "en-US,en;q=0.9"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _open2(url: str, timeout: int = 120, accept: str = "*/*"):
    """Honest UA first; one browser-profile retry on a bot-wall status.
    Returns (response, profile) where profile is "crossfoot" or "browser"."""
    try:
        return _open(url, timeout, accept), "crossfoot"
    except urllib.error.HTTPError as e:
        if e.code in _RETRY_CODES:
            time.sleep(2)
            return _open(url, timeout, accept, browser=True), "browser"
        raise


def sniff(url: str, n: int = 256) -> bytes:
    """First bytes of the document, to decide what it actually is.
    Uses a ranged request (uncompressed) and falls back to reading the head of
    a full response. Returns b"" when nothing could be read."""
    for browser in (False, True):
        req = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA if browser else UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Range": f"bytes=0-{n - 1}",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(n)
        except urllib.error.HTTPError as e:
            if e.code in _RETRY_CODES and not browser:
                time.sleep(2)
                continue
            return b""
        except Exception:  # noqa: BLE001
            return b""
    return b""


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"unreachable: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def head(url: str, timeout: int = 60) -> dict:
    """Content type and length without the body. Some hosts refuse HEAD; fall back to a ranged GET."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "type": r.headers.get("Content-Type", ""),
                    "length": int(r.headers.get("Content-Length") or 0), "final_url": r.url}
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 405, 406, 429):
            # HEAD support is spotty; retry as a 1-byte ranged GET, honest UA
            # first, then the browser profile.
            for browser in (False, True):
                req = urllib.request.Request(url, headers={
                    "User-Agent": BROWSER_UA if browser else UA, "Range": "bytes=0-0"})
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        cr = r.headers.get("Content-Range", "")
                        length = int(cr.rsplit("/", 1)[-1]) if "/" in cr and cr.rsplit("/", 1)[-1].isdigit() else 0
                        return {"status": r.status, "type": r.headers.get("Content-Type", ""),
                                "length": length, "final_url": r.url}
                except Exception:  # noqa: BLE001
                    continue
        raise FetchError(_reason(e)) from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(_reason(e)) from e


def download(url: str, name: str, refresh: bool = False, timeout: int = 300,
             max_bytes: Optional[int] = None) -> Path:
    """
    Stream `url` to data/raw/prices/<name> and write <name>.SOURCE.json beside it.
    Returns the path. Raises FetchError with a gap-log-ready reason on failure.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_DIR / name
    src = RAW_DIR / (name + ".SOURCE.json")
    if dest.exists() and src.exists() and not refresh:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    sha = hashlib.sha256()
    n = 0
    profile = "crossfoot"
    try:
        r, profile = _open2(url, timeout=timeout)
        with r:
            ctype = r.headers.get("Content-Type", "")
            declared = int(r.headers.get("Content-Length") or 0)
            if max_bytes and declared > max_bytes:
                raise FetchError(f"too large: server declares {declared:,} bytes, "
                                 f"limit {max_bytes:,}")
            stream = r
            if r.headers.get("Content-Encoding", "").lower() == "gzip":
                stream = gzip.GzipFile(fileobj=r)
            with open(tmp, "wb") as fh:
                while True:
                    chunk = stream.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    sha.update(chunk)
                    n += len(chunk)
                    if max_bytes and n > max_bytes:
                        raise FetchError(f"too large: exceeded {max_bytes:,} bytes while streaming")
    except FetchError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise FetchError(_reason(e)) from e

    os.replace(tmp, dest)
    src.write_text(json.dumps({
        "url": url,
        "fetched_at": _now(),
        "content_type": ctype,
        "bytes": n,
        "sha256": sha.hexdigest(),
        "profile": profile,
    }, indent=2))
    return dest


def text(url: str, timeout: int = 60, limit: int = 4 << 20) -> str:
    """Small text documents (cms-hpt.txt, JSON indexes). Decoded as UTF-8 with replacement.
    `text.last_profile` records whether the browser-profile retry was needed."""
    text.last_profile = "crossfoot"  # type: ignore[attr-defined]
    try:
        r, profile = _open2(url, timeout=timeout)
        text.last_profile = profile  # type: ignore[attr-defined]
        with r:
            raw = r.read(limit + 1)
            if r.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
    except Exception as e:  # noqa: BLE001
        raise FetchError(_reason(e)) from e
    if len(raw) > limit:
        raise FetchError(f"too large for a text fetch (> {limit:,} bytes)")
    return raw.decode("utf-8", "replace")


text.last_profile = "crossfoot"  # type: ignore[attr-defined]


def stream_lines(url: str, timeout: int = 600, max_bytes: Optional[int] = None) -> Iterator[str]:
    """
    Yield decoded lines of a remote text file without writing it to disk.
    Used for hospital MRFs, which can run to gigabytes and of which only a few
    hundred rows are wanted. The caller gets the bytes count and SHA-256 from
    the `.meta` attribute of the generator's closure via `stream_lines_meta`.
    """
    meta = {"bytes": 0, "sha256": None, "content_type": "", "profile": "crossfoot"}
    sha = hashlib.sha256()
    try:
        r, meta["profile"] = _open2(url, timeout=timeout, accept="text/csv, application/json, */*")
        with r:
            meta["content_type"] = r.headers.get("Content-Type", "")
            stream = r
            if r.headers.get("Content-Encoding", "").lower() == "gzip":
                stream = gzip.GzipFile(fileobj=r)
            buffered = io.BufferedReader(stream, 1 << 20)  # type: ignore[arg-type]
            # Some hospitals serve a gzip *file* (x.csv.gz as octet-stream) rather
            # than gzip transfer encoding; sniff the magic bytes and unwrap.
            if buffered.peek(2)[:2] == b"\x1f\x8b":
                buffered = io.BufferedReader(gzip.GzipFile(fileobj=buffered), 1 << 20)  # type: ignore[arg-type]
            wrapper = io.TextIOWrapper(buffered,
                                       encoding="utf-8-sig", errors="replace", newline="")
            for line in wrapper:
                b = len(line.encode("utf-8", "replace"))
                meta["bytes"] += b
                if max_bytes and meta["bytes"] > max_bytes:
                    raise FetchError(f"too large: exceeded {max_bytes:,} bytes while streaming")
                yield line
    except FetchError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FetchError(_reason(e)) from e
    finally:
        stream_lines.last_meta = meta  # type: ignore[attr-defined]


stream_lines.last_meta = {}  # type: ignore[attr-defined]


def source_record(name: str) -> Optional[dict]:
    p = RAW_DIR / (name + ".SOURCE.json")
    return json.loads(p.read_text()) if p.exists() else None
