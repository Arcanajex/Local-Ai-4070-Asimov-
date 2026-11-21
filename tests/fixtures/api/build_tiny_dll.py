"""Build tests/fixtures/api/TinyLib.dll. Run from the repo root:

    python tests/fixtures/api/build_tiny_dll.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from local_agent.dotnet_meta import pack_metadata_root, wrap_managed_dll  # noqa: E402


def _compress(value: int) -> bytes:
    if value <= 0x7F:
        return bytes((value,))
    if value <= 0x3FFF:
        return bytes((0x80 | (value >> 8), value & 0xFF))
    raise ValueError(value)


class _Heap:
    def __init__(self, strings: bool = False) -> None:
        self.data = bytearray(b"\x00")
        self._seen: dict[bytes, int] = {b"": 0}

    def add(self, text: str) -> int:
        raw = text.encode("utf-8")
        if raw in self._seen:
            return self._seen[raw]
        index = len(self.data)
        self.data += raw + b"\x00"
        self._seen[raw] = index
        return index

    def add_blob(self, payload: bytes) -> int:
        key = b"\x01" + payload
        if key in self._seen:
            return self._seen[key]
        index = len(self.data)
        self.data += _compress(len(payload)) + payload
        self._seen[key] = index
        return index


def build() -> bytes:
    strings = _Heap()
    blobs = _Heap()
    module = strings.add("TinyLib")
    system = strings.add("System")
    obj = strings.add("Object")
    mscorlib = strings.add("mscorlib")
    demo = strings.add("Demo")
    widget = strings.add("Widget")
    inner = strings.add("Inner")
    module_type = strings.add("<Module>")
    ctor = strings.add(".ctor")
    draw = strings.add("Draw")
    add = strings.add("Add")
    hidden = strings.add("Hidden")
    get_name = strings.add("get_Name")
    ping = strings.add("Ping")
    count = strings.add("count")
    a_name = strings.add("a")
    b_name = strings.add("b")
    id_name = strings.add("Id")
    name_name = strings.add("Name")

    def sig(payload: bytes) -> int:
        return blobs.add_blob(payload)

    # TypeDefOrRef: TypeRef row 1, tag 1 -> (1 << 2) | 1
    extends_object = (1 << 2) | 1
    # ResolutionScope: AssemblyRef row 1, tag 2 -> (1 << 2) | 2
    scope_mscorlib = (1 << 2) | 2
    # HasSemantics: Property row 1, tag 1 -> (1 << 1) | 1
    property_1 = (1 << 1) | 1

    void_ret = 0x01
    i4 = 0x08
    string = 0x0E
    hasthis = 0x20
    field_sig = sig(bytes((0x06, i4)))
    ctor_sig = sig(bytes((hasthis, 0x00, void_ret)))
    draw_sig = sig(bytes((hasthis, 0x01, void_ret, i4)))
    add2_sig = sig(bytes((hasthis, 0x02, i4, i4, i4)))
    add1_sig = sig(bytes((hasthis, 0x01, i4, i4)))
    getter_sig = sig(bytes((hasthis, 0x00, string)))
    ping_sig = sig(bytes((hasthis, 0x00, void_ret)))
    prop_sig = sig(bytes((0x28, 0x00, string)))

    public_type = 0x00100001
    nested_public = 0x00100002
    public_method = 0x0086
    public_ctor = 0x1886
    public_getter = 0x0886
    private_method = 0x0081
    public_field = 0x0006

    tables: dict[int, list[tuple[int, ...]]] = {
        0x00: [(0, module, 1, 0, 0)],
        0x01: [(scope_mscorlib, obj, system)],
        0x02: [
            (0, module_type, 0, 0, 1, 1),
            (public_type, widget, demo, extends_object, 1, 1),
            (nested_public, inner, 0, extends_object, 2, 7),
        ],
        0x04: [(public_field, id_name, field_sig)],
        0x06: [
            (0, 0, public_ctor, ctor, ctor_sig, 1),
            (0, 0, public_method, draw, draw_sig, 1),
            (0, 0, public_method, add, add2_sig, 2),
            (0, 0, public_method, add, add1_sig, 4),
            (0, 0, public_getter, get_name, getter_sig, 5),
            (0, 0, private_method, hidden, ping_sig, 5),
            (0, 0, public_method, ping, ping_sig, 5),
        ],
        0x08: [
            (0, 1, count),
            (0, 1, a_name),
            (0, 2, b_name),
            (0, 1, a_name),
        ],
        0x15: [(2, 1)],
        0x17: [(0, name_name, prop_sig)],
        0x18: [(0x0002, 5, property_1)],
        0x20: [(0, 1, 2, 3, 0, 0, 0, module, 0)],
        0x23: [(4, 0, 0, 0, 0, 0, mscorlib, 0, 0)],
        0x29: [(3, 2)],
    }
    metadata = pack_metadata_root(strings=bytes(strings.data), guids=bytes(16), blobs=bytes(blobs.data), tables=tables)
    return wrap_managed_dll(metadata)


def main() -> None:
    target = Path(__file__).with_name("TinyLib.dll")
    target.write_bytes(build())
    print(target, target.stat().st_size)


if __name__ == "__main__":
    main()
