"""Resumable downloads, including a redirect that must keep the Range header."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_agent.download import DownloadError, download_file

PAYLOAD = b"abcdefghijklmnopqrstuvwxyz0123456789"


class _Server(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.payload = PAYLOAD


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        server: _Server = self.server  # type: ignore[assignment]
        if self.path.startswith("/redirect"):
            target = f"http://127.0.0.1:{server.server_address[1]}/file"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if not self.path.startswith("/file"):
            self.send_error(404)
            return
        data = server.payload
        range_header = self.headers.get("Range")
        if range_header:
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            if start >= len(data):
                self.send_error(416)
                return
            chunk = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # noqa: A003
        return


@pytest.fixture()
def server():
    httpd = _Server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()


def test_download_follows_redirect_and_resumes(server, tmp_path):
    dest = tmp_path / "model.gguf"
    url = f"http://127.0.0.1:{server.server_address[1]}/redirect"
    download_file(url, dest, expected_size=len(PAYLOAD))
    assert dest.read_bytes() == PAYLOAD

    partial = tmp_path / "partial.gguf"
    partial.write_bytes(PAYLOAD[:11])
    download_file(url, partial, expected_size=len(PAYLOAD))
    assert partial.read_bytes() == PAYLOAD


def test_size_mismatch_is_an_error(server, tmp_path):
    dest = tmp_path / "model.gguf"
    url = f"http://127.0.0.1:{server.server_address[1]}/file"
    with pytest.raises(DownloadError, match="expected"):
        download_file(url, dest, expected_size=len(PAYLOAD) + 5)


def test_complete_file_is_not_downloaded_again(server, tmp_path):
    dest = tmp_path / "model.gguf"
    dest.write_bytes(PAYLOAD)
    # Point at a URL that would 404 if the client asked for it.
    download_file("http://127.0.0.1:1/missing", dest, expected_size=len(PAYLOAD), retries=1)
    assert dest.read_bytes() == PAYLOAD
