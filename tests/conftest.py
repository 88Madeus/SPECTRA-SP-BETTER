import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


@pytest.fixture
def spectrasp_dir(tmp_path):
    """Копия файлов настроек SpectraSP во временном каталоге (+ пустой SSPL_IM.dll для распознавания)."""
    import shutil
    d = tmp_path / "SpectraSP for MS7504i N020126"
    shutil.copytree(os.path.join(DATA, "spectrasp"), d)
    (d / "SSPL_IM.dll").write_bytes(b"")
    return str(d)
