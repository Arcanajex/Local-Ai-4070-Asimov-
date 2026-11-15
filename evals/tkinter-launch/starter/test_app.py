from pathlib import Path

from app import output_file


def test_output_file():
    assert output_file(Path("data"), "items.txt") == Path("data") / "items.txt"
