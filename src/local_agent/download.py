"""Resumable HTTP download used for GGUF weights and llama.cpp zips.

Hugging Face answers the resolve URL with a 302 onto a short-lived CDN
address. A Range header has to survive that redirect or a resumed download
starts over. urllib's default redirect handler drops Range, so this module
puts it back. The setup script calls `python -m local_agent.download`.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "local-coding-agent"
CHUNK = 1024 * 1024


class DownloadError(RuntimeError):
    pass


class _KeepRangeOnRedirect(urllib.request.HTTPRedirectHandler):
    """Re-apply Range after a 302. The CDN URL is not the URL we asked for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        range_header = req.get_header("Range")
        if range_header and not redirected.has_header("Range"):
            redirected.add_header("Range", range_header)
        return redirected


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_KeepRangeOnRedirect)


def download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    timeout: int = 120,
    retries: int = 5,
    opener: urllib.request.OpenerDirector | None = None,
) -> Path:
    """Download `url` to `dest`, resuming a partial file when the server allows it.

    A file that already has exactly `expected_size` bytes is left alone.
    After a successful transfer the size is checked when `expected_size` is set.
    """

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if expected_size is not None and dest.is_file() and dest.stat().st_size == expected_size:
        return dest

    client = opener or _opener()
    last_error: Exception | None = None
    for _attempt in range(max(1, retries)):
        try:
            _transfer(client, url, dest, timeout=timeout)
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise DownloadError(f"download failed for {url}: HTTP {exc.code}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if expected_size is not None and dest.is_file() and dest.stat().st_size == expected_size:
                return dest
    if last_error is not None:
        raise DownloadError(f"download failed for {url}: {last_error}") from last_error
    if expected_size is not None:
        actual = dest.stat().st_size if dest.is_file() else 0
        if actual != expected_size:
            raise DownloadError(
                f"{dest} is {actual} bytes, expected {expected_size}. "
                "The transfer stopped early. Re-run the same command to resume."
            )
    return dest


def _transfer(
    opener: urllib.request.OpenerDirector,
    url: str,
    dest: Path,
    *,
    timeout: int,
    allow_restart: bool = True,
) -> None:
    existing = dest.stat().st_size if dest.is_file() else 0
    headers = {"User-Agent": USER_AGENT}
    append = False
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
        append = True
    request = urllib.request.Request(url, headers=headers)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # 416 means the range starts past the end. If we already have the
        # whole object, the caller checks the size. Otherwise start over once.
        if exc.code == 416 and existing > 0 and allow_restart:
            dest.unlink(missing_ok=True)
            return _transfer(opener, url, dest, timeout=timeout, allow_restart=False)
        raise
    with response:
        status = getattr(response, "status", None) or response.getcode()
        # A server that ignores Range answers 200 with the full body.
        # Appending that would corrupt the file, so replace it.
        if append and status == 200:
            append = False
        mode = "ab" if append else "wb"
        with dest.open(mode) as handle:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
