"""Public type and member signatures from a .NET assembly.

NuGet packages sometimes ship a DLL and no XML docs. The harness has no
runtime dependencies, and Linux CI does not install the dotnet SDK, so this
module reads the ECMA-335 metadata tables itself. It understands the tables a
C# compiler emits and returns public signatures. A native DLL, a truncated
file, or a metadata stream this reader cannot size raises MetadataError. The
caller logs that and continues with the packages it could read.

IL is not disassembled. Return types and parameter types come from the
signature blobs on MethodDef, Field, and Property.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

# HeapSizes bits in the #~ header.
_HEAP_STRING = 0x01
_HEAP_GUID = 0x02
_HEAP_BLOB = 0x04

# A signature blob that does not parse becomes this token instead of failing
# the whole assembly. One bad member should not hide the rest of the package.
_BAD_TYPE = "?"

_MAX_DLL_BYTES = 64 * 1024 * 1024


class MetadataError(Exception):
    """The file is not a managed assembly this reader can index."""


@dataclass(frozen=True)
class MetaMember:
    """One public type or member. `member_id` uses the XML doc id shape."""

    member_id: str
    kind: str
    name: str
    parent: str
    signature: str


@dataclass(frozen=True)
class _TypeName:
    display: str
    doc: str


# Element types that are a complete type by themselves (ECMA-335 II.23.1.16).
_PRIMITIVE = {
    0x01: _TypeName("void", "System.Void"),
    0x02: _TypeName("bool", "System.Boolean"),
    0x03: _TypeName("char", "System.Char"),
    0x04: _TypeName("sbyte", "System.SByte"),
    0x05: _TypeName("byte", "System.Byte"),
    0x06: _TypeName("short", "System.Int16"),
    0x07: _TypeName("ushort", "System.UInt16"),
    0x08: _TypeName("int", "System.Int32"),
    0x09: _TypeName("uint", "System.UInt32"),
    0x0A: _TypeName("long", "System.Int64"),
    0x0B: _TypeName("ulong", "System.UInt64"),
    0x0C: _TypeName("float", "System.Single"),
    0x0D: _TypeName("double", "System.Double"),
    0x0E: _TypeName("string", "System.String"),
    0x18: _TypeName("nint", "System.IntPtr"),
    0x19: _TypeName("nuint", "System.UIntPtr"),
    0x1C: _TypeName("object", "System.Object"),
}

# tag bits, then the tables those tags point at, in tag order.
# Unused tags are "" and do not contribute a row count.
_CODED: dict[str, tuple[int, tuple[str, ...]]] = {
    "TypeDefOrRef": (2, ("TypeDef", "TypeRef", "TypeSpec")),
    "HasConstant": (2, ("Field", "Param", "Property")),
    "HasCustomAttribute": (
        5,
        (
            "MethodDef",
            "Field",
            "TypeRef",
            "TypeDef",
            "Param",
            "InterfaceImpl",
            "MemberRef",
            "Module",
            "",
            "Property",
            "Event",
            "StandAloneSig",
            "ModuleRef",
            "TypeSpec",
            "Assembly",
            "AssemblyRef",
            "File",
            "ExportedType",
            "ManifestResource",
            "GenericParam",
            "GenericParamConstraint",
            "MethodSpec",
        ),
    ),
    "HasFieldMarshal": (1, ("Field", "Param")),
    "HasDeclSecurity": (2, ("TypeDef", "MethodDef", "Assembly")),
    "MemberRefParent": (3, ("TypeDef", "TypeRef", "ModuleRef", "MethodDef", "TypeSpec")),
    "HasSemantics": (1, ("Event", "Property")),
    "MethodDefOrRef": (1, ("MethodDef", "MemberRef")),
    "MemberForwarded": (1, ("Field", "MethodDef")),
    "Implementation": (2, ("File", "AssemblyRef", "ExportedType")),
    "CustomAttributeType": (3, ("", "", "MethodDef", "MemberRef")),
    "ResolutionScope": (2, ("Module", "ModuleRef", "AssemblyRef", "TypeRef")),
    "TypeOrMethodDef": (1, ("TypeDef", "MethodDef")),
}

# Column kinds: u2, u4, str, guid, blob, simple:<table>, coded:<name>.
# Every table a C# compiler sets in the Valid mask is listed so a real
# assembly can be skipped past even when this reader does not display it.
_TABLES: dict[int, list[str]] = {
    0x00: ["u2", "str", "guid", "guid", "guid"],  # Module
    0x01: ["coded:ResolutionScope", "str", "str"],  # TypeRef
    0x02: ["u4", "str", "str", "coded:TypeDefOrRef", "simple:Field", "simple:MethodDef"],  # TypeDef
    0x03: ["simple:Field"],  # FieldPtr
    0x04: ["u2", "str", "blob"],  # Field
    0x05: ["simple:MethodDef"],  # MethodPtr
    0x06: ["u4", "u2", "u2", "str", "blob", "simple:Param"],  # MethodDef
    0x07: ["simple:Param"],  # ParamPtr
    0x08: ["u2", "u2", "str"],  # Param
    0x09: ["simple:TypeDef", "coded:TypeDefOrRef"],  # InterfaceImpl
    0x0A: ["coded:MemberRefParent", "str", "blob"],  # MemberRef
    0x0B: ["u2", "coded:HasConstant", "blob"],  # Constant (type byte + pad)
    0x0C: ["coded:HasCustomAttribute", "coded:CustomAttributeType", "blob"],  # CustomAttribute
    0x0D: ["coded:HasFieldMarshal", "blob"],  # FieldMarshal
    0x0E: ["u2", "coded:HasDeclSecurity", "blob"],  # DeclSecurity
    0x0F: ["u2", "u4", "simple:TypeDef"],  # ClassLayout
    0x10: ["u4", "simple:Field"],  # FieldLayout
    0x11: ["blob"],  # StandAloneSig
    0x12: ["simple:TypeDef", "simple:Event"],  # EventMap
    0x13: ["simple:Event"],  # EventPtr
    0x14: ["u2", "str", "coded:TypeDefOrRef"],  # Event
    0x15: ["simple:TypeDef", "simple:Property"],  # PropertyMap
    0x16: ["simple:Property"],  # PropertyPtr
    0x17: ["u2", "str", "blob"],  # Property
    0x18: ["u2", "simple:MethodDef", "coded:HasSemantics"],  # MethodSemantics
    0x19: ["simple:TypeDef", "coded:MethodDefOrRef", "coded:MethodDefOrRef"],  # MethodImpl
    0x1A: ["str"],  # ModuleRef
    0x1B: ["blob"],  # TypeSpec
    0x1C: ["u2", "coded:MemberForwarded", "str", "simple:ModuleRef"],  # ImplMap
    0x1D: ["u4", "simple:Field"],  # FieldRVA
    0x1E: ["u4", "u4"],  # ENCLog
    0x1F: ["u4"],  # ENCMap
    0x20: ["u4", "u2", "u2", "u2", "u2", "u4", "blob", "str", "str"],  # Assembly
    0x21: ["u4"],  # AssemblyProcessor
    0x22: ["u4", "u4", "u4"],  # AssemblyOS
    0x23: ["u2", "u2", "u2", "u2", "u4", "blob", "str", "str", "blob"],  # AssemblyRef
    0x24: ["u4", "simple:AssemblyRef"],  # AssemblyRefProcessor
    0x25: ["u4", "u4", "u4", "simple:AssemblyRef"],  # AssemblyRefOS
    0x26: ["u4", "str", "blob"],  # File
    0x27: ["u4", "u4", "str", "str", "coded:Implementation"],  # ExportedType
    0x28: ["u4", "u4", "str", "coded:Implementation"],  # ManifestResource
    0x29: ["simple:TypeDef", "simple:TypeDef"],  # NestedClass
    0x2A: ["u2", "u2", "coded:TypeOrMethodDef", "str"],  # GenericParam
    0x2B: ["coded:MethodDefOrRef", "blob"],  # MethodSpec
    0x2C: ["simple:GenericParam", "coded:TypeDefOrRef"],  # GenericParamConstraint
}

_TABLE_NAME = {
    0x00: "Module",
    0x01: "TypeRef",
    0x02: "TypeDef",
    0x04: "Field",
    0x06: "MethodDef",
    0x08: "Param",
    0x09: "InterfaceImpl",
    0x0A: "MemberRef",
    0x0B: "Constant",
    0x0C: "CustomAttribute",
    0x0D: "FieldMarshal",
    0x0E: "DeclSecurity",
    0x0F: "ClassLayout",
    0x10: "FieldLayout",
    0x11: "StandAloneSig",
    0x12: "EventMap",
    0x14: "Event",
    0x15: "PropertyMap",
    0x17: "Property",
    0x18: "MethodSemantics",
    0x19: "MethodImpl",
    0x1A: "ModuleRef",
    0x1B: "TypeSpec",
    0x1C: "ImplMap",
    0x1D: "FieldRVA",
    0x20: "Assembly",
    0x23: "AssemblyRef",
    0x26: "File",
    0x27: "ExportedType",
    0x28: "ManifestResource",
    0x29: "NestedClass",
    0x2A: "GenericParam",
    0x2B: "MethodSpec",
    0x2C: "GenericParamConstraint",
}


def read_public_api(path: Path) -> list[MetaMember]:
    """Public types, fields, properties, and methods in metadata order."""

    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise MetadataError(f"could not read {file_path.name}: {exc}") from exc
    if len(data) > _MAX_DLL_BYTES:
        raise MetadataError(f"{file_path.name} is larger than {_MAX_DLL_BYTES} bytes")
    return read_public_api_bytes(data)


def read_public_api_bytes(data: bytes) -> list[MetaMember]:
    """Same as read_public_api, for a fixture kept in memory."""

    metadata = _metadata_bytes(data)
    strings, guids, blobs, tables = _parse_metadata(metadata)
    return _public_members(strings, guids, blobs, tables)


def pack_metadata_root(
    *,
    strings: bytes,
    guids: bytes,
    blobs: bytes,
    tables: dict[int, list[tuple[int, ...]]],
) -> bytes:
    """Build a #~ metadata root. Tests use this to make a tiny assembly.

    Row values are already heap indexes or coded indexes, in column order.
    Index widths follow the same rules as the reader.
    """

    if not strings or strings[0] != 0:
        raise MetadataError("string heap must start with a 0 byte")
    counts = {name: 0 for name in set(_TABLE_NAME.values())}
    for index, rows in tables.items():
        name = _TABLE_NAME.get(index)
        if name is None:
            raise MetadataError(f"cannot pack unknown table {index}")
        counts[name] = len(rows)
    heap_sizes = 0
    if len(strings) >= 65536:
        heap_sizes |= _HEAP_STRING
    if len(guids) >= 65536:
        heap_sizes |= _HEAP_GUID
    if len(blobs) >= 65536:
        heap_sizes |= _HEAP_BLOB
    widths = _column_widths(counts, heap_sizes)
    present = sorted(index for index, rows in tables.items() if rows)
    valid = 0
    for index in present:
        valid |= 1 << index
    body = bytearray()
    body += struct.pack("<I", 0)
    body += bytes((2, 0, heap_sizes, 1))
    body += struct.pack("<QQ", valid, 0)
    for index in present:
        body += struct.pack("<I", len(tables[index]))
    for index in present:
        schema = _TABLES[index]
        for row in tables[index]:
            if len(row) != len(schema):
                raise MetadataError(f"table {index:#x} row has {len(row)} values, schema wants {len(schema)}")
            for value, width in zip(row, widths[index], strict=True):
                if value < 0 or value >= (1 << (8 * width)):
                    raise MetadataError(f"value {value} does not fit in {width} bytes")
                body += int(value).to_bytes(width, "little")
    streams: list[tuple[str, bytes]] = [
        ("#~", bytes(body)),
        ("#Strings", strings),
        ("#GUID", guids),
        ("#Blob", blobs),
    ]
    version = b"v4.0.30319\x00\x00"
    header_len = 16 + len(version) + 4
    for name, _payload in streams:
        header_len += 8 + _padded_name_len(name)
    chunks = bytearray(header_len)
    struct.pack_into("<IHHII", chunks, 0, 0x424A5342, 1, 1, 0, len(version))
    chunks[16 : 16 + len(version)] = version
    struct.pack_into("<HH", chunks, 16 + len(version), 0, len(streams))
    cursor = header_len
    write = 16 + len(version) + 4
    payloads: list[bytes] = []
    for name, payload in streams:
        padding = (4 - (cursor % 4)) % 4
        cursor += padding
        struct.pack_into("<II", chunks, write, cursor, len(payload))
        write += 8
        raw_name = name.encode("ascii") + b"\x00"
        pad = (4 - (len(raw_name) % 4)) % 4
        chunks[write : write + len(raw_name) + pad] = raw_name + (b"\x00" * pad)
        write += len(raw_name) + pad
        payloads.append((b"\x00" * padding) + payload)
        cursor += len(payload)
    if write != header_len:
        raise MetadataError("metadata header size mismatch")
    return bytes(chunks) + b"".join(payloads)


def wrap_managed_dll(metadata: bytes) -> bytes:
    """PE32 DLL whose CLI header points at `metadata`. Deterministic."""

    cli = bytearray(72)
    struct.pack_into("<IHH", cli, 0, 72, 2, 5)
    meta_rva = 0x2000 + 72
    struct.pack_into("<II", cli, 8, meta_rva, len(metadata))
    struct.pack_into("<I", cli, 16, 0x1)  # ILONLY
    content = bytes(cli) + metadata
    raw_size = (len(content) + 0x1FF) & ~0x1FF
    virtual_span = (len(content) + 0x1FFF) & ~0x1FFF

    opt = bytearray(0xE0)
    struct.pack_into("<H", opt, 0, 0x10B)
    opt[2] = 11
    struct.pack_into("<I", opt, 4, raw_size)
    struct.pack_into("<I", opt, 16, 0x2000)
    struct.pack_into("<I", opt, 20, 0x2000)
    struct.pack_into("<I", opt, 24, 0x2000)
    struct.pack_into("<I", opt, 28, 0x00400000)
    struct.pack_into("<I", opt, 32, 0x2000)
    struct.pack_into("<I", opt, 36, 0x200)
    struct.pack_into("<HH", opt, 40, 4, 0)
    struct.pack_into("<HH", opt, 48, 4, 0)
    struct.pack_into("<I", opt, 56, 0x2000 + virtual_span)
    struct.pack_into("<I", opt, 60, 0x200)
    struct.pack_into("<H", opt, 68, 3)
    struct.pack_into("<I", opt, 72, 0x100000)
    struct.pack_into("<I", opt, 76, 0x1000)
    struct.pack_into("<I", opt, 80, 0x100000)
    struct.pack_into("<I", opt, 84, 0x1000)
    struct.pack_into("<I", opt, 92, 16)
    struct.pack_into("<II", opt, 96 + 14 * 8, 0x2000, 72)

    section = bytearray(40)
    section[0:8] = b".text\x00\x00\x00"
    struct.pack_into("<IIIIII", section, 8, len(content), 0x2000, raw_size, 0x200, 0, 0)
    struct.pack_into("<HHI", section, 32, 0, 0, 0x60000020)

    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    coff = struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 0xE0, 0x2002)
    headers = bytes(dos) + b"PE\x00\x00" + coff + bytes(opt) + bytes(section)
    if len(headers) > 0x200:
        raise MetadataError("PE headers exceed FileAlignment")
    return headers.ljust(0x200, b"\x00") + content.ljust(raw_size, b"\x00")


def _padded_name_len(name: str) -> int:
    raw = len(name.encode("ascii")) + 1
    return raw + ((4 - (raw % 4)) % 4)


def _metadata_bytes(data: bytes) -> bytes:
    if len(data) < 0x40 or data[0:2] != b"MZ":
        raise MetadataError("not a PE file")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew <= 0 or e_lfanew + 24 > len(data):
        raise MetadataError("PE header is out of range")
    if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        raise MetadataError("missing PE signature")
    coff = e_lfanew + 4
    sections, optional_size = struct.unpack_from("<H", data, coff + 2)[0], struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    if opt + optional_size > len(data) or optional_size < 96:
        raise MetadataError("optional header is truncated")
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x10B:
        number_offset = 92
        dir_offset = 96
    elif magic == 0x20B:
        number_offset = 108
        dir_offset = 112
    else:
        raise MetadataError(f"unknown optional header magic {magic:#x}")
    if number_offset + 4 > optional_size:
        raise MetadataError("optional header has no data directories")
    count = struct.unpack_from("<I", data, opt + number_offset)[0]
    if count <= 14:
        raise MetadataError("not a managed assembly")
    entry = dir_offset + 14 * 8
    if entry + 8 > optional_size:
        raise MetadataError("CLI header directory is out of range")
    cli_rva, cli_size = struct.unpack_from("<II", data, opt + entry)
    if cli_rva == 0 or cli_size < 16:
        raise MetadataError("not a managed assembly")
    section_table = opt + optional_size
    parsed_sections = []
    for index in range(sections):
        start = section_table + index * 40
        if start + 40 > len(data):
            raise MetadataError("section table is truncated")
        virtual_size, virtual_address, raw_size, raw_ptr = struct.unpack_from("<IIII", data, start + 8)
        parsed_sections.append((virtual_address, virtual_size, raw_ptr, raw_size))
    cli_off = _rva_to_offset(cli_rva, parsed_sections)
    if cli_off + 16 > len(data):
        raise MetadataError("CLI header is truncated")
    meta_rva, meta_size = struct.unpack_from("<II", data, cli_off + 8)
    if meta_rva == 0 or meta_size < 20:
        raise MetadataError("assembly has no metadata")
    meta_off = _rva_to_offset(meta_rva, parsed_sections)
    if meta_off + meta_size > len(data):
        raise MetadataError("metadata is truncated")
    return data[meta_off : meta_off + meta_size]


def _rva_to_offset(rva: int, sections: list[tuple[int, int, int, int]]) -> int:
    for virtual_address, virtual_size, raw_ptr, raw_size in sections:
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            delta = rva - virtual_address
            if delta > raw_size:
                raise MetadataError("RVA points into section padding")
            return raw_ptr + delta
    raise MetadataError(f"RVA {rva:#x} is not in any section")


def _parse_metadata(
    data: bytes,
) -> tuple[bytes, bytes, bytes, dict[str, list[tuple[int, ...]]]]:
    if len(data) < 20 or data[0:4] != b"BSJB":
        raise MetadataError("metadata signature is not BSJB")
    version_len = struct.unpack_from("<I", data, 12)[0]
    if version_len <= 0 or version_len > 256 or 16 + version_len + 4 > len(data):
        raise MetadataError("metadata version is invalid")
    stream_count = struct.unpack_from("<H", data, 16 + version_len + 2)[0]
    cursor = 16 + version_len + 4
    streams: dict[str, tuple[int, int]] = {}
    for _ in range(stream_count):
        if cursor + 8 > len(data):
            raise MetadataError("stream header is truncated")
        offset, size = struct.unpack_from("<II", data, cursor)
        cursor += 8
        end = data.find(b"\x00", cursor)
        if end < 0:
            raise MetadataError("stream name is not terminated")
        name = data[cursor:end].decode("ascii", errors="replace")
        cursor = end + 1
        cursor = (cursor + 3) & ~3
        streams[name] = (offset, size)
    if "#~" not in streams and "#-" not in streams:
        raise MetadataError("metadata has no #~ stream")
    if "#Strings" not in streams or "#Blob" not in streams:
        raise MetadataError("metadata is missing the string or blob heap")
    tilde_off, tilde_size = streams.get("#~") or streams["#-"]
    strings = _slice_stream(data, *streams["#Strings"], "strings")
    guids = _slice_stream(data, *streams["#GUID"], "guid") if "#GUID" in streams else b""
    blobs = _slice_stream(data, *streams["#Blob"], "blob")
    tables = _parse_tilde(_slice_stream(data, tilde_off, tilde_size, "#~"), strings, guids, blobs)
    return strings, guids, blobs, tables


def _slice_stream(data: bytes, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise MetadataError(f"{label} stream is out of range")
    return data[offset : offset + size]


def _parse_tilde(
    stream: bytes,
    strings: bytes,
    guids: bytes,
    blobs: bytes,
) -> dict[str, list[tuple[int, ...]]]:
    del strings, guids, blobs
    if len(stream) < 24:
        raise MetadataError("#~ stream is truncated")
    heap_sizes = stream[6]
    valid = struct.unpack_from("<Q", stream, 8)[0]
    present = [index for index in range(64) if valid & (1 << index)]
    unknown = [index for index in present if index not in _TABLES]
    if unknown:
        listed = ", ".join(f"{index:#x}" for index in unknown)
        raise MetadataError(f"unsupported metadata tables: {listed}")
    cursor = 24
    counts_by_index: dict[int, int] = {}
    counts_by_name = {name: 0 for name in set(_TABLE_NAME.values())}
    for index in present:
        if cursor + 4 > len(stream):
            raise MetadataError("#~ row counts are truncated")
        count = struct.unpack_from("<I", stream, cursor)[0]
        cursor += 4
        counts_by_index[index] = count
        name = _TABLE_NAME.get(index)
        if name:
            counts_by_name[name] = count
    widths = _column_widths(counts_by_name, heap_sizes)
    tables: dict[str, list[tuple[int, ...]]] = {name: [] for name in counts_by_name}
    for index in present:
        width = widths[index]
        row_size = sum(width)
        rows: list[tuple[int, ...]] = []
        for _ in range(counts_by_index[index]):
            if cursor + row_size > len(stream):
                raise MetadataError(f"table {index:#x} is truncated")
            values = []
            cell = cursor
            for size in width:
                values.append(int.from_bytes(stream[cell : cell + size], "little"))
                cell += size
            cursor = cell
            rows.append(tuple(values))
        name = _TABLE_NAME.get(index)
        if name:
            tables[name] = rows
    return tables


def _column_widths(counts: dict[str, int], heap_sizes: int) -> dict[int, list[int]]:
    string_w = 4 if heap_sizes & _HEAP_STRING else 2
    guid_w = 4 if heap_sizes & _HEAP_GUID else 2
    blob_w = 4 if heap_sizes & _HEAP_BLOB else 2
    coded_w = {name: _coded_width(tag_bits, tables, counts) for name, (tag_bits, tables) in _CODED.items()}
    simple_w = {name: (4 if counts.get(name, 0) >= 65536 else 2) for name in set(_TABLE_NAME.values())}
    widths: dict[int, list[int]] = {}
    for index, schema in _TABLES.items():
        row: list[int] = []
        for column in schema:
            if column == "u2":
                row.append(2)
            elif column == "u4":
                row.append(4)
            elif column == "str":
                row.append(string_w)
            elif column == "guid":
                row.append(guid_w)
            elif column == "blob":
                row.append(blob_w)
            elif column.startswith("simple:"):
                row.append(simple_w[column.split(":", 1)[1]])
            elif column.startswith("coded:"):
                row.append(coded_w[column.split(":", 1)[1]])
            else:
                raise MetadataError(f"unknown column {column}")
        widths[index] = row
    return widths


def _coded_width(tag_bits: int, tables: tuple[str, ...], counts: dict[str, int]) -> int:
    limit = 1 << (16 - tag_bits)
    for name in tables:
        if name and counts.get(name, 0) >= limit:
            return 4
    return 2


def _public_members(
    strings: bytes,
    guids: bytes,
    blobs: bytes,
    tables: dict[str, list[tuple[int, ...]]],
) -> list[MetaMember]:
    del guids
    typedefs = tables.get("TypeDef", [])
    fields = tables.get("Field", [])
    methods = tables.get("MethodDef", [])
    params = tables.get("Param", [])
    nested_rows = tables.get("NestedClass", [])
    nested = {row[0]: row[1] for row in nested_rows if row[0] and row[1]}
    type_params, method_params = _generic_names(tables, strings)
    type_docs, type_display = _type_names(typedefs, strings, nested, type_params)
    typerefs = _typeref_names(tables.get("TypeRef", []), strings)
    typespecs = [row[0] for row in tables.get("TypeSpec", [])]

    def resolve(coded: int, depth: int) -> _TypeName:
        return _resolve_type(coded, depth, type_docs, type_display, typerefs, typespecs, blobs, type_params)

    flags = {index + 1: row[0] for index, row in enumerate(typedefs)}
    members: list[MetaMember] = []
    for index, row in enumerate(typedefs, start=1):
        if not _public_type(index, flags, nested):
            continue
        name = _string(strings, row[1])
        if not name or name == "<Module>" or "<" in name:
            continue
        parent_doc = type_docs.get(nested.get(index, 0), _TypeName("", "")).doc
        if not parent_doc:
            parent_doc = _string(strings, row[2])
        short = name
        members.append(
            MetaMember(
                member_id="T:" + type_docs[index].doc,
                kind="type",
                name=short,
                parent=parent_doc,
                signature=type_display[index].display,
            )
        )
        field_rows = _span(index, typedefs, 4, len(fields))
        for field in (fields[item - 1] for item in field_rows):
            if (field[0] & 0x7) != 0x6:
                continue
            field_name = _string(strings, field[1])
            if not field_name or "<" in field_name:
                continue
            parsed = _parse_field_sig(_blob(blobs, field[2]), resolve, type_params.get(index, []))
            if parsed is None:
                continue
            display_t, doc_t = parsed
            prefix = "static " if field[0] & 0x0010 else ""
            full = type_display[index].display
            members.append(
                MetaMember(
                    member_id=f"F:{type_docs[index].doc}.{field_name}",
                    kind="field",
                    name=field_name,
                    parent=type_docs[index].doc,
                    signature=f"{prefix}{display_t.display} {full}.{field_name}",
                )
            )
        prop_rows = _properties_for(index, tables)
        accessor_names: set[str] = set()
        for prop in prop_rows:
            prop_name = _string(strings, prop[1])
            if not prop_name or "<" in prop_name:
                continue
            accessors = _property_accessors(prop_name, tables, methods, strings)
            if not any(_method_public(flags) for _semantics, flags in accessors):
                continue
            accessor_names.add("get_" + prop_name)
            accessor_names.add("set_" + prop_name)
            parsed = _parse_property_sig(_blob(blobs, prop[2]), resolve, type_params.get(index, []))
            if parsed is None:
                continue
            parts = []
            if any(semantics & 0x0002 and _method_public(flags) for semantics, flags in accessors):
                parts.append("get;")
            if any(semantics & 0x0001 and _method_public(flags) for semantics, flags in accessors):
                parts.append("set;")
            full = type_display[index].display
            brace = "{ " + " ".join(parts) + " }" if parts else ""
            members.append(
                MetaMember(
                    member_id=f"P:{type_docs[index].doc}.{prop_name}",
                    kind="property",
                    name=prop_name,
                    parent=type_docs[index].doc,
                    signature=f"{parsed.display} {full}.{prop_name} {brace}".rstrip(),
                )
            )
        method_rows = _span(index, typedefs, 5, len(methods))
        for method_index in method_rows:
            method = methods[method_index - 1]
            if not _method_public(method[2]):
                continue
            method_name = _string(strings, method[3])
            if not method_name or method_name == ".cctor" or "<" in method_name:
                continue
            if method_name in accessor_names:
                continue
            param_rows = _param_span(method_index, methods, len(params))
            names = _param_names(param_rows, params, strings)
            sig = _parse_method_sig(
                _blob(blobs, method[4]),
                resolve,
                type_params.get(index, []),
                method_params.get(method_index, []),
            )
            if sig is None:
                continue
            ret, args_display, args_doc, generic = sig
            doc_name = "#ctor" if method_name == ".ctor" else method_name
            if generic and method_name != ".ctor":
                # Method arity uses two backticks in a doc id: Method``1.
                doc_name = f"{method_name}``{len(generic)}"
            arg_doc = ",".join(args_doc)
            id_args = f"({arg_doc})" if arg_doc else ""
            member_id = f"M:{type_docs[index].doc}.{doc_name}{id_args}"
            shown = type_display[index].display
            if method_name == ".ctor":
                head = shown
            else:
                generic_text = f"<{', '.join(generic)}>" if generic else ""
                head = f"{shown}.{method_name}{generic_text}"
            pairs = []
            for position, arg in enumerate(args_display):
                pname = names[position] if position < len(names) else ""
                pairs.append(f"{arg} {pname}".strip())
            prefix = "static " if method[2] & 0x0010 else ""
            signature = f"{prefix}{ret.display} {head}({', '.join(pairs)})"
            members.append(
                MetaMember(
                    member_id=member_id,
                    kind="method",
                    name="#ctor" if method_name == ".ctor" else method_name,
                    parent=type_docs[index].doc,
                    signature=signature,
                )
            )
    return members


def _method_public(flags: int) -> bool:
    return (flags & 0x7) == 0x6


def _public_type(index: int, flags: dict[int, int], nested: dict[int, int]) -> bool:
    seen: set[int] = set()
    current = index
    while current and current not in seen:
        seen.add(current)
        vis = flags.get(current, 0) & 0x7
        if vis == 0x1:
            return True
        if vis != 0x2:
            return False
        current = nested.get(current, 0)
    return False


def _generic_names(
    tables: dict[str, list[tuple[int, ...]]],
    strings: bytes,
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    type_params: dict[int, list[tuple[int, str]]] = {}
    method_params: dict[int, list[tuple[int, str]]] = {}
    for number, _flags, owner, name_index in tables.get("GenericParam", []):
        tag = owner & 0x1
        row = owner >> 1
        bucket = method_params if tag == 1 else type_params
        bucket.setdefault(row, []).append((number, _string(strings, name_index)))
    return (
        {key: [name for _number, name in sorted(values)] for key, values in type_params.items()},
        {key: [name for _number, name in sorted(values)] for key, values in method_params.items()},
    )


def _type_names(
    typedefs: list[tuple[int, ...]],
    strings: bytes,
    nested: dict[int, int],
    type_params: dict[int, list[str]],
) -> tuple[dict[int, _TypeName], dict[int, _TypeName]]:
    docs: dict[int, _TypeName] = {}
    display: dict[int, _TypeName] = {}

    def build(index: int) -> _TypeName:
        if index in docs:
            return docs[index]
        if index <= 0 or index > len(typedefs):
            return _TypeName(_BAD_TYPE, _BAD_TYPE)
        _flags, name_i, ns_i, _extends, _fields, _methods = typedefs[index - 1]
        name = _string(strings, name_i)
        namespace = _string(strings, ns_i)
        params = type_params.get(index, [])
        doc_name = f"{name}`{len(params)}" if params else name
        disp_name = name
        if params:
            disp_name += "<" + ", ".join(params) + ">"
        if index in nested:
            outer = build(nested[index])
            doc = _TypeName(f"{outer.display}.{disp_name}", f"{outer.doc}.{doc_name}")
        elif namespace:
            doc = _TypeName(f"{namespace}.{disp_name}", f"{namespace}.{doc_name}")
        else:
            doc = _TypeName(disp_name, doc_name)
        docs[index] = doc
        display[index] = doc
        return doc

    for index in range(1, len(typedefs) + 1):
        build(index)
    return docs, display


def _typeref_names(rows: list[tuple[int, ...]], strings: bytes) -> dict[int, _TypeName]:
    found: dict[int, _TypeName] = {}
    for index, row in enumerate(rows, start=1):
        name = _string(strings, row[1])
        namespace = _string(strings, row[2])
        full = f"{namespace}.{name}" if namespace else name
        found[index] = _TypeName(full, full)
    return found


def _resolve_type(
    coded: int,
    depth: int,
    type_docs: dict[int, _TypeName],
    type_display: dict[int, _TypeName],
    typerefs: dict[int, _TypeName],
    typespecs: list[int],
    blobs: bytes,
    type_params: dict[int, list[str]],
) -> _TypeName:
    del type_display
    if depth > 8:
        return _TypeName(_BAD_TYPE, _BAD_TYPE)
    tag = coded & 0x3
    row = coded >> 2
    if row <= 0:
        return _TypeName(_BAD_TYPE, _BAD_TYPE)
    if tag == 0:
        return type_docs.get(row, _TypeName(_BAD_TYPE, _BAD_TYPE))
    if tag == 1:
        return typerefs.get(row, _TypeName(_BAD_TYPE, _BAD_TYPE))
    if tag == 2 and 0 < row <= len(typespecs):
        blob = _blob(blobs, typespecs[row - 1])
        parsed, _pos = _parse_type(blob, 0, lambda coded, depth: _resolve_type(
            coded, depth, type_docs, type_display, typerefs, typespecs, blobs, type_params
        ), [], [], depth + 1)
        return parsed or _TypeName(_BAD_TYPE, _BAD_TYPE)
    return _TypeName(_BAD_TYPE, _BAD_TYPE)


def _span(index: int, typedefs: list[tuple[int, ...]], column: int, count: int) -> list[int]:
    start = typedefs[index - 1][column]
    end = typedefs[index][column] if index < len(typedefs) else count + 1
    if start <= 0:
        return []
    end = max(start, min(end, count + 1))
    return list(range(start, end))


def _param_span(method_index: int, methods: list[tuple[int, ...]], count: int) -> list[int]:
    start = methods[method_index - 1][5]
    end = methods[method_index][5] if method_index < len(methods) else count + 1
    if start <= 0:
        return []
    end = max(start, min(end, count + 1))
    return list(range(start, end))


def _param_names(rows: list[int], params: list[tuple[int, ...]], strings: bytes) -> list[str]:
    named = []
    for row in rows:
        flags, sequence, name_i = params[row - 1]
        del flags
        if sequence <= 0:
            continue
        named.append((sequence, _string(strings, name_i)))
    named.sort()
    return [name for _sequence, name in named]


def _properties_for(type_index: int, tables: dict[str, list[tuple[int, ...]]]) -> list[tuple[int, ...]]:
    maps = tables.get("PropertyMap", [])
    props = tables.get("Property", [])
    owned: list[tuple[int, ...]] = []
    for position, (parent, first) in enumerate(maps):
        if parent != type_index or first <= 0:
            continue
        end = maps[position + 1][1] if position + 1 < len(maps) else len(props) + 1
        end = max(first, min(end, len(props) + 1))
        owned.extend(props[item - 1] for item in range(first, end))
    return owned


def _property_accessors(
    prop_name: str,
    tables: dict[str, list[tuple[int, ...]]],
    methods: list[tuple[int, ...]],
    strings: bytes,
) -> list[tuple[int, int]]:
    """(semantics, method flags) for one property. Property tag in HasSemantics is 1."""

    props = tables.get("Property", [])
    found: list[tuple[int, int]] = []
    for semantics, method, association in tables.get("MethodSemantics", []):
        if method <= 0 or method > len(methods) or (association & 0x1) != 1:
            continue
        row = association >> 1
        if row <= 0 or row > len(props):
            continue
        if _string(strings, props[row - 1][1]) != prop_name:
            continue
        found.append((semantics, methods[method - 1][2]))
    return found


def _parse_field_sig(blob: bytes, resolve, type_params: list[str]) -> tuple[_TypeName, _TypeName] | None:
    if not blob or blob[0] != 0x06:
        return None
    parsed, _pos = _parse_type(blob, 1, resolve, type_params, [], 0)
    if parsed is None:
        return None
    return parsed, parsed


def _parse_property_sig(blob: bytes, resolve, type_params: list[str]) -> _TypeName | None:
    if not blob or blob[0] not in {0x08, 0x28}:
        return None
    try:
        _count, pos = _compressed(blob, 1)
    except MetadataError:
        return None
    parsed, _pos = _parse_type(blob, pos, resolve, type_params, [], 0)
    return parsed


def _parse_method_sig(
    blob: bytes,
    resolve,
    type_params: list[str],
    method_params: list[str],
) -> tuple[_TypeName, list[str], list[str], list[str]] | None:
    if not blob:
        return None
    try:
        pos = 1
        generic = method_params
        if blob[0] & 0x10:
            count, pos = _compressed(blob, pos)
            if not generic:
                generic = [_tparam(index, []) for index in range(count)]
            else:
                generic = list(generic[:count]) + [_tparam(index, []) for index in range(len(generic), count)]
        param_count, pos = _compressed(blob, pos)
        ret, pos = _parse_type(blob, pos, resolve, type_params, generic, 0)
        if ret is None:
            return None
        display: list[str] = []
        docs: list[str] = []
        for _ in range(param_count):
            if pos < len(blob) and blob[pos] == 0x41:
                pos += 1
                display.append("...")
                docs.append("...")
                continue
            parsed, pos = _parse_type(blob, pos, resolve, type_params, generic, 0)
            if parsed is None:
                return None
            display.append(parsed.display)
            docs.append(parsed.doc)
        return ret, display, docs, list(generic)
    except MetadataError:
        return None


def _parse_type(
    blob: bytes,
    pos: int,
    resolve,
    type_params: list[str],
    method_params: list[str],
    depth: int,
) -> tuple[_TypeName | None, int]:
    if depth > 8 or pos >= len(blob):
        return None, pos
    kind = blob[pos]
    pos += 1
    if kind in _PRIMITIVE:
        return _PRIMITIVE[kind], pos
    if kind in {0x1F, 0x20}:  # required/optional modifier, then the real type
        try:
            _token, pos = _compressed(blob, pos)
        except MetadataError:
            return None, pos
        return _parse_type(blob, pos, resolve, type_params, method_params, depth + 1)
    if kind == 0x45:  # pinned
        return _parse_type(blob, pos, resolve, type_params, method_params, depth + 1)
    if kind == 0x0F:  # pointer
        inner, pos = _parse_type(blob, pos, resolve, type_params, method_params, depth + 1)
        if inner is None:
            return None, pos
        return _TypeName(inner.display + "*", inner.doc + "*"), pos
    if kind == 0x10:  # byref
        inner, pos = _parse_type(blob, pos, resolve, type_params, method_params, depth + 1)
        if inner is None:
            return None, pos
        return _TypeName("ref " + inner.display, inner.doc + "@"), pos
    if kind == 0x1D:  # szarray
        inner, pos = _parse_type(blob, pos, resolve, type_params, method_params, depth + 1)
        if inner is None:
            return None, pos
        return _TypeName(inner.display + "[]", inner.doc + "[]"), pos
    if kind == 0x14:  # multidimensional array
        inner, pos = _parse_type(blob, pos, resolve, type_params, method_params, depth + 1)
        if inner is None:
            return None, pos
        try:
            rank, pos = _compressed(blob, pos)
            sizes, pos = _compressed(blob, pos)
            for _ in range(sizes):
                _value, pos = _compressed(blob, pos)
            bounds, pos = _compressed(blob, pos)
            for _ in range(bounds):
                _value, pos = _compressed(blob, pos)
        except MetadataError:
            return None, pos
        commas = "," * max(0, rank - 1)
        return _TypeName(f"{inner.display}[{commas}]", f"{inner.doc}[{commas}]"), pos
    if kind in {0x11, 0x12}:  # valuetype, class
        try:
            coded, pos = _compressed(blob, pos)
        except MetadataError:
            return None, pos
        return resolve(coded, depth + 1), pos
    if kind == 0x15:  # generic instantiation
        if pos >= len(blob) or blob[pos] not in {0x11, 0x12}:
            return None, pos
        pos += 1
        try:
            coded, pos = _compressed(blob, pos)
            count, pos = _compressed(blob, pos)
        except MetadataError:
            return None, pos
        open_type = resolve(coded, depth + 1)
        args_d: list[str] = []
        args_doc: list[str] = []
        for _ in range(count):
            arg, pos = _parse_type(blob, pos, resolve, type_params, method_params, depth + 1)
            if arg is None:
                return None, pos
            args_d.append(arg.display)
            args_doc.append(arg.doc)
        base = open_type.display.split("<", 1)[0]
        doc_base = open_type.doc.split("`", 1)[0]
        return (
            _TypeName(f"{base}<{', '.join(args_d)}>", f"{doc_base}{{{','.join(args_doc)}}}"),
            pos,
        )
    if kind == 0x13:  # type generic parameter
        try:
            number, pos = _compressed(blob, pos)
        except MetadataError:
            return None, pos
        label = _tparam(number, type_params)
        return _TypeName(label, f"`{number}"), pos
    if kind == 0x1E:  # method generic parameter
        try:
            number, pos = _compressed(blob, pos)
        except MetadataError:
            return None, pos
        label = _tparam(number, method_params)
        return _TypeName(label, f"``{number}"), pos
    return None, pos


def _tparam(number: int, names: list[str]) -> str:
    if 0 <= number < len(names) and names[number]:
        return names[number]
    fallback = ("T", "U", "V", "W")
    if number < len(fallback):
        return fallback[number]
    return f"T{number + 1}"


def _compressed(blob: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(blob):
        raise MetadataError("truncated signature")
    first = blob[pos]
    if first & 0x80 == 0:
        return first, pos + 1
    if first & 0xC0 == 0x80:
        if pos + 1 >= len(blob):
            raise MetadataError("truncated signature")
        return ((first & 0x3F) << 8) | blob[pos + 1], pos + 2
    if pos + 3 >= len(blob):
        raise MetadataError("truncated signature")
    value = ((first & 0x1F) << 24) | (blob[pos + 1] << 16) | (blob[pos + 2] << 8) | blob[pos + 3]
    return value, pos + 4


def _blob(heap: bytes, index: int) -> bytes:
    if index <= 0 or index >= len(heap):
        return b""
    try:
        length, pos = _compressed(heap, index)
    except MetadataError:
        return b""
    return heap[pos : pos + length]


def _string(heap: bytes, index: int) -> str:
    if index <= 0 or index >= len(heap):
        return ""
    end = heap.find(b"\x00", index)
    if end < 0:
        end = len(heap)
    return heap[index:end].decode("utf-8", errors="replace")
