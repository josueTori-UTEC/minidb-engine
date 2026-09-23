import pytest

from backend.storage.page import (
    FULL_PAGE,
    PAGE_HEADER,
    FileKind,
    MetaCodec,
    PageFormatError,
    PageFullError,
    PageHeader,
    PageType,
    RecordPage,
    RecordPageLayout,
    bitmap_size,
    clear_bit,
    first_clear_bit,
    iter_set_bits,
    read_common_meta,
    record_page_capacity,
    set_bit,
)


def test_header_mide_24_bytes_y_round_trip():
    assert PAGE_HEADER.size == 24
    h = PageHeader(
        page_id=4_000_000_000,
        page_type=PageType.BTREE_LEAF,
        flags=7,
        record_count=65535,
        free_space_offset=FULL_PAGE,
        next_page_id=-1,
        prev_page_id=2_147_483_647,
        aux_page_id=-2_147_483_648,
    )
    raw = h.pack()
    assert len(raw) == 24
    assert PageHeader.unpack(raw) == h
    buf = bytearray(64)
    h.pack_into(buf)
    assert PageHeader.unpack(buf) == h
    assert h.as_dict()["page_type_name"] == "BTREE_LEAF"


def test_capacidades_de_referencia_empleados():
    # record_size = 62 -> 16 / 32 / 65 / 131 registros por página
    assert [record_page_capacity(b, 62) for b in (1024, 2048, 4096, 8192)] == [16, 32, 65, 131]
    for b in (1024, 2048, 4096, 8192):
        for rs in (1, 7, 62, 100, 333):
            n = record_page_capacity(b, rs)
            assert 24 + bitmap_size(n) + n * rs <= b
            assert 24 + bitmap_size(n + 1) + (n + 1) * rs > b


def test_bitmap():
    buf = bytearray(4)
    for i in (0, 3, 8, 9, 31):
        set_bit(buf, 0, i)
    assert list(iter_set_bits(buf, 0, 32)) == [0, 3, 8, 9, 31]
    assert first_clear_bit(buf, 0, 32) == 1
    clear_bit(buf, 0, 3)
    assert list(iter_set_bits(buf, 0, 32)) == [0, 8, 9, 31]
    full = bytearray(b"\xff\xff\x01")
    assert first_clear_bit(full, 0, 20) == 17
    assert first_clear_bit(bytearray(b"\xff"), 0, 8) == -1


def test_meta_codec_round_trip():
    codec = MetaCodec(FileKind.HASH, [("global_depth", "B"), ("key_size", "H"), ("segments", "4i")])
    raw = codec.pack(1024, 9, {"global_depth": 3, "key_size": 4, "segments": [1, 2, -1, -1]})
    assert len(raw) == 1024
    page_size, num_pages, values = codec.unpack(raw)
    assert (page_size, num_pages) == (1024, 9)
    assert values == {"global_depth": 3, "key_size": 4, "segments": [1, 2, -1, -1]}
    common = read_common_meta(raw)
    assert common["file_kind_name"] == "HASH" and common["magic"] == "MDB1"
    with pytest.raises(PageFormatError):
        MetaCodec(FileKind.HEAP, [("x", "i")]).unpack(raw)  # tipo de archivo distinto


def test_record_page_heap_reutiliza_slots():
    layout = RecordPageLayout(1024, 62)
    page = RecordPage.new(layout, 3, PageType.HEAP_DATA)
    slots = [page.insert(bytes([i]) * 62) for i in range(layout.capacity)]
    assert slots == list(range(16))
    assert not page.has_space() and page.header.record_count == 16
    with pytest.raises(PageFullError):
        page.insert(b"x" * 62)
    page.delete(5)
    page.delete(2)
    assert page.header.free_space_offset == 2
    assert page.insert(b"n" * 62) == 2
    assert page.header.free_space_offset == 5
    again = RecordPage.from_bytes(layout, page.to_bytes(), PageType.HEAP_DATA)
    assert again.header.record_count == 15
    assert again.get(2) == b"n" * 62
    assert [s for s, _ in again.records()] == [s for s in range(16) if s != 5]


def test_record_page_seq_main_no_reutiliza_slots():
    layout = RecordPageLayout(1024, 62)
    page = RecordPage.new(layout, 1, PageType.SEQ_MAIN)
    for i in range(3):
        page.insert(bytes([i]) * 62)
    page.delete(0)
    # el slot 0 queda como lápida: la marca de agua sigue en 3
    assert page.header.free_space_offset == 3 and page.high_water_mark() == 3
    assert page.insert(b"z" * 62) == 3
    with pytest.raises(PageFormatError):
        RecordPage.from_bytes(layout, page.to_bytes(), PageType.HEAP_DATA)
