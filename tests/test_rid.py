from backend.storage.rid import RID, RID_SIZE


def test_rid_round_trip():
    assert RID_SIZE == 6
    for rid in (RID(0, 0), RID(123456, 65535), RID(-7, 3)):
        raw = rid.pack()
        assert len(raw) == 6
        assert RID.unpack(raw) == rid
    assert RID(-7, 3).in_overflow and not RID(7, 3).in_overflow
    assert RID.unpack(b"\x00" + RID(9, 1).pack(), 1) == RID(9, 1)
