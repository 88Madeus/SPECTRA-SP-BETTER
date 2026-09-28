"""Чтение журналов оригинальной SpectraSP (*.logsp и *.zip из папки Logs).

Формат восстановлен по файлам: заголовок «LOGSPV01», размер записи (uint32),
число записей (uint32), далее записи фиксированной длины, каждый байт
которых «зашифрован» операцией XOR 0xAA. Запись: время (double, дата OLE),
уровень (int32), текст (ANSI/UTF-8, завершается нулём).
"""
from __future__ import annotations

import datetime as _dt
import io
import struct
import zipfile
from dataclasses import dataclass
from typing import Iterator, List

MAGIC = b"LOGSPV01"
_OLE_EPOCH = _dt.datetime(1899, 12, 30)


@dataclass
class LogRecord:
    time: _dt.datetime
    level: int
    text: str

    def format(self) -> str:
        return f"{self.time:%Y-%m-%d %H:%M:%S.%f}"[:-3] + f"\t{self.level}\t{self.text}"


def _decode_text(raw: bytes) -> str:
    raw = raw.split(b"\0", 1)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", "replace")


def parse(data: bytes) -> Iterator[LogRecord]:
    if data[:8] != MAGIC:
        raise ValueError("Это не журнал SpectraSP (нет сигнатуры LOGSPV01)")
    rec_size, _count = struct.unpack_from("<II", data, 8)
    if not 16 <= rec_size <= 65536:
        raise ValueError(f"Неожиданный размер записи: {rec_size}")
    for off in range(16, len(data) - rec_size + 1, rec_size):
        r = bytes(b ^ 0xAA for b in data[off:off + rec_size])
        ole, level = struct.unpack_from("<di", r, 0)
        try:
            t = _OLE_EPOCH + _dt.timedelta(days=ole)
        except (OverflowError, ValueError):
            t = _OLE_EPOCH
        yield LogRecord(t, level, _decode_text(r[12:]))


def read(path: str) -> List[LogRecord]:
    """Прочитать .logsp или .zip (в архиве берутся все .logsp)."""
    if zipfile.is_zipfile(path):
        out: List[LogRecord] = []
        with zipfile.ZipFile(path) as z:
            for name in sorted(z.namelist()):
                if name.lower().endswith(".logsp"):
                    out.extend(parse(z.read(name)))
        return out
    with open(path, "rb") as f:
        return list(parse(f.read()))


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Расшифровка журналов SpectraSP (*.logsp, *.zip) в текст")
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--output", help="файл результата (по умолчанию — рядом, с расширением .txt)")
    ap.add_argument("--errors", action="store_true", help="только строки с ошибками/предупреждениями")
    args = ap.parse_args(argv)
    for path in args.files:
        recs = read(path)
        lines = [r.format() for r in recs]
        if args.errors:
            key = ("error", "fail", "false", "ошиб", "не удал", "timeout", "not found")
            lines = [l for l in lines if any(k in l.lower() for k in key)]
        out = args.output or path.rsplit(".", 1)[0] + ".txt"
        with io.open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"{path}: {len(recs)} записей → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
