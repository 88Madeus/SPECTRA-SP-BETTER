"""Буфер спектров: состав, отметки, имена, свойства, сохранение между запусками."""
import numpy as np

from spectra_better import buffer as B
from spectra_better.buffer import SpectrumBuffer
from spectra_better.instrument import Measurement


def meas(k=1.0, wl=546.0, label=""):
    x = np.linspace(wl - 5, wl + 5, 2048)
    y = k * np.exp(-((x - wl) / 0.1) ** 2) * 1000
    return Measurement(x=x, x_unit="nm", y=y, raw=y + 300, image=np.ones((506, 2048)), exposure_ms=200,
                       center_wl=wl, grating_title="№2, 600 штр/мм", slit_um=50, label=label)


def test_add_strips_image_and_names():
    b = SpectrumBuffer()
    e = b.add(meas())
    assert e.m.image is None                       # кадр (8 МБ) в буфере не хранится
    assert "546.0 нм" in e.name and "200 мс" in e.name
    assert b.add(meas(label="Hg лампа")).name == "Hg лампа"
    props = dict(b.properties(e.key))
    assert props["Экспозиция, мс"] == "200" and props["Входная щель, мкм"] == "50"


def test_visibility_rename_remove():
    b = SpectrumBuffer()
    e1, e2, e3 = (b.add(meas(k)) for k in (1, 2, 3))
    b.set_visible(e2.key, False)
    assert [e.key for e in b.visible()] == [e1.key, e3.key]
    b.set_all_visible(None)                        # обратить
    assert [e.key for e in b.visible()] == [e2.key]
    b.rename(e2.key, "  образец 1 ")
    assert b.get(e2.key).name == "образец 1"
    b.rename(e2.key, "   ")                        # пустое имя не принимается
    assert b.get(e2.key).name == "образец 1"
    b.remove([e1.key, e3.key])
    assert len(b) == 1


def test_buffer_is_bounded(monkeypatch):
    monkeypatch.setattr(B, "MAX_ENTRIES", 5)
    b = SpectrumBuffer()
    keys = [b.add(meas()).key for _ in range(8)]
    assert [e.key for e in b.entries] == keys[3:]  # выбрасываются самые старые


def test_state_roundtrip(tmp_path):
    b = SpectrumBuffer()
    b.add(meas(1.0), "первый")
    e2 = b.add(meas(2.0, wl=585.2), "второй")
    b.set_visible(e2.key, False)
    path = str(tmp_path / "buffer.npz")
    b.save_state(path)
    c = SpectrumBuffer()
    assert c.load_state(path) == 2
    assert [e.name for e in c.entries] == ["первый", "второй"]
    assert [e.visible for e in c.entries] == [True, False]
    np.testing.assert_allclose(c.entries[1].m.y, e2.m.y)
    np.testing.assert_allclose(c.entries[1].m.raw, e2.m.raw)
    assert c.entries[1].m.center_wl == 585.2 and c.entries[1].props["exposure_ms"] == "200"
    c.clear()
    c.save_state(path)                             # пустой буфер — файл удаляется
    assert SpectrumBuffer().load_state(path) == 0


def test_comment_in_name_and_props():
    b = SpectrumBuffer()
    m = meas()
    m.comment = "образец 7, 77 K"
    e = b.add(m)
    assert e.name.startswith("образец 7, 77 K · ")
    assert dict(b.properties(e.key))["Комментарий"] == "образец 7, 77 K"
