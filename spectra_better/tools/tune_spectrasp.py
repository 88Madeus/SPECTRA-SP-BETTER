"""Безопасная настройка оригинальной SpectraSP (ускорение запуска).

При каждом запуске SpectraSP опрашивает все включённые типы камер. По журналам
установки включены ещё Andor (не подключена, ~1,2 с) и PSI Ver5 IR с адресом
192.168.242.181 (такого устройства нет — 3 с ожидания ping). Утилита
выключает опрос отсутствующих камер в SpectraSP.xml (UsedSensors).

Сетевые адреса, порты и настройки приборов НЕ изменяются.
Перед изменением создаётся резервная копия; --restore возвращает последнюю.

    py -m spectra_better.tools.tune_spectrasp            (показать, что будет сделано)
    py -m spectra_better.tools.tune_spectrasp --apply    (применить)
    py -m spectra_better.tools.tune_spectrasp --restore  (вернуть из резервной копии)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import os
import re
import shutil
import sys

from ..spectrasp_config import find_spectrasp_dir, read_spectrasp_xml

DEFAULT_DISABLE = ("Sensor_ANDOR", "Sensor_PSI_CCD_VER5_IR")
BACKUP_SUFFIX = ".bak-"


def plan_changes(text: str, disable) -> str:
    for tag in disable:
        text = re.sub(rf'(<{re.escape(tag)}\s+Used=")true(")', r"\1false\2", text, flags=re.IGNORECASE)
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="каталог SpectraSP")
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    ap.add_argument("--restore", action="store_true", help="восстановить последнюю резервную копию")
    ap.add_argument("--disable", default=",".join(DEFAULT_DISABLE),
                    help="какие типы камер выключить (через запятую), по умолчанию: %(default)s")
    args = ap.parse_args(argv)

    d = find_spectrasp_dir(args.dir)
    if not d:
        print("Каталог SpectraSP не найден, укажите --dir")
        return 2
    path = os.path.join(d, "SpectraSP.xml")
    if args.restore:
        backups = sorted(glob.glob(path + BACKUP_SUFFIX + "*"))
        if not backups:
            print("Резервных копий нет")
            return 1
        shutil.copy2(backups[-1], path)
        print(f"Восстановлено из {backups[-1]}")
        return 0

    settings = read_spectrasp_xml(d)
    print(f"Файл: {path}")
    print("Сейчас опрашиваются камеры:")
    for k, v in settings.used_sensors.items():
        print(f"  {k:28s} {'ВКЛ' if v else 'выкл'}")
    disable = [s.strip() for s in args.disable.split(",") if s.strip()]
    if "Sensor_PSI_CCD_VER3" in disable:
        print("Sensor_PSI_CCD_VER3 — это рабочая камера, её выключать нельзя")
        return 2
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8")
    new = plan_changes(text, disable)
    if new == text:
        print("Изменять нечего.")
        return 0
    print("Будут выключены: " + ", ".join(t for t in disable if settings.used_sensors.get(t)))
    if not args.apply:
        print("Это пробный запуск. Для применения добавьте --apply (SpectraSP должна быть закрыта).")
        return 0
    backup = f"{path}{BACKUP_SUFFIX}{_dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, backup)
    with open(path, "wb") as f:
        f.write(new.encode("utf-8"))
    print(f"Готово. Резервная копия: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
