import pytest

from backend.storage.disk_manager import DiskCounter, DiskError, DiskManager


def test_allocate_write_read_cuenta_exacta(tmp_path, counter):
    dm = DiskManager(tmp_path / "f.bin", 1024, counter, create=True)
    assert dm.num_pages() == 0
    pid = dm.allocate_page()
    assert pid == 0 and dm.num_pages() == 1
    assert counter.snapshot() == (0, 0)  # reservar no hace I/O
    dm.write_page(pid, b"\x07" * 1024)
    assert counter.snapshot() == (0, 1)
    assert dm.read_page(pid) == b"\x07" * 1024
    assert counter.snapshot() == (1, 1)
    dm.close()


def test_page_size_es_parametro_por_archivo(tmp_path, counter):
    for size in (1024, 2048, 4096, 8192):
        with DiskManager(tmp_path / f"f{size}.bin", size, counter, create=True) as dm:
            for i in range(3):
                dm.write_page(dm.allocate_page(), bytes([i]) * size)
            assert dm.file_size() == 3 * size
        assert (tmp_path / f"f{size}.bin").stat().st_size == 3 * size


def test_persistencia_al_reabrir(tmp_path, counter):
    path = tmp_path / "f.bin"
    with DiskManager(path, 2048, counter, create=True) as dm:
        for i in range(5):
            dm.write_page(dm.allocate_page(), bytes([i]) * 2048)
    with DiskManager(path, 2048, counter) as dm:
        assert dm.num_pages() == 5
        assert dm.read_page(3) == bytes([3]) * 2048


def test_errores(tmp_path, counter):
    dm = DiskManager(tmp_path / "f.bin", 1024, counter, create=True)
    with pytest.raises(DiskError):
        dm.read_page(0)  # fuera de rango
    pid = dm.allocate_page()
    with pytest.raises(DiskError):
        dm.write_page(pid, b"x" * 100)  # no es una página completa
    with pytest.raises(DiskError):
        dm.write_page(5, b"x" * 1024)  # no asignada
    dm.close()
    with pytest.raises(FileExistsError):
        DiskManager(tmp_path / "f.bin", 1024, counter, create=True)
    with pytest.raises(DiskError):
        DiskManager(tmp_path / "g.bin", 1000, counter, create=True)  # no es potencia de 2


def test_tamano_no_multiplo_es_corrupto(tmp_path, counter):
    path = tmp_path / "roto.bin"
    path.write_bytes(b"x" * 1500)
    with pytest.raises(DiskError):
        DiskManager(path, 1024, counter)


def test_truncate(tmp_path, counter):
    with DiskManager(tmp_path / "f.bin", 1024, counter, create=True) as dm:
        for _ in range(4):
            dm.write_page(dm.allocate_page(), b"\x00" * 1024)
        dm.truncate(1)
        assert dm.num_pages() == 1
        assert dm.file_size() == 1024
        with pytest.raises(DiskError):
            dm.read_page(1)


def test_counter_reset_y_compartido(tmp_path):
    counter = DiskCounter()
    a = DiskManager(tmp_path / "a.bin", 1024, counter, create=True)
    b = DiskManager(tmp_path / "b.bin", 4096, counter, create=True)
    a.write_page(a.allocate_page(), b"a" * 1024)
    b.write_page(b.allocate_page(), b"b" * 4096)
    b.read_page(0)
    assert counter.as_dict() == {"disk_reads": 1, "disk_writes": 2}
    counter.reset()
    assert counter.snapshot() == (0, 0)
    a.close()
    b.close()
