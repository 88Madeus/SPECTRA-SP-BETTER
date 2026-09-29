"""Диагностика оборудования по шагам (запускать на компьютере с приборами).

    py -3.12-32 -m spectra_better.tools.probe
    py -3.12-32 -m spectra_better.tools.probe --expose 100
    py -3.12-32 -m spectra_better.tools.probe --mono-only

По умолчанию выполняются только безопасные операции чтения: подключение,
чтение параметров, температуры, длины волны и т. п. Экспозиция (--expose),
перемещение монохроматора (--goto) и охлаждение (--cooling) — только по ключам.
Отчёт сохраняется в текстовый файл — его удобно приложить к сообщению об ошибке.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import sys
import time
import traceback

import numpy as np

from ..calibration import geometries_from_config
from ..hw.base import DeviceError, spectrum_region, full_frame_region, AcqSettings
from ..hw.winutil import PYTHON_BITS, spectrasp_is_running
from ..spectrasp_config import find_spectrasp_dir, read_installation
from ..storage import save_image, save_spectrum


class Report:
    def __init__(self, path: str):
        self.path = path
        self.lines = []

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def step(self, title: str, fn):
        self(f"\n=== {title}")
        t0 = time.monotonic()
        try:
            res = fn()
            self(f"    OK ({time.monotonic() - t0:.2f} с)")
            return res
        except DeviceError as e:
            self(f"    ОШИБКА: {e}")
        except Exception as e:  # noqa: BLE001
            self(f"    СБОЙ: {e!r}")
            self("    " + traceback.format_exc().replace("\n", "\n    "))
        return None

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")
        print(f"\nОтчёт сохранён: {self.path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Пошаговая проверка камеры и монохроматора")
    ap.add_argument("--spectrasp-dir")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--camera-only", action="store_true")
    g.add_argument("--mono-only", action="store_true")
    ap.add_argument("--expose", type=float, metavar="МС", help="сделать пробную экспозицию (спектр) длительностью МС")
    ap.add_argument("--full-frame", action="store_true", help="дополнительно снять полный кадр (медленно)")
    ap.add_argument("--rows", type=int, nargs=2, metavar=("ВЕРХ", "НИЗ"), help="строки полосы спектра")
    ap.add_argument("--speed", type=int, help="скорость считывания, кГц")
    ap.add_argument("--dll", default="auto", help="библиотека камеры (psi_ccd2_ver0.dll …)")
    ap.add_argument("--goto", type=float, metavar="НМ", help="установить длину волны монохроматора")
    ap.add_argument("--cooling", type=int, metavar="°C", help="включить охлаждение с уставкой")
    ap.add_argument("--warm-up", action="store_true",
                    help="не требуется: охлаждённая ниже 0 °C ПЗС отогревается до +2 °C при отключении всегда")
    ap.add_argument("--out", help="папка для отчёта и данных (по умолчанию текущая)")
    args = ap.parse_args(argv)

    out_dir = os.path.abspath(args.out or os.getcwd())
    os.makedirs(out_dir, exist_ok=True)
    stamp = f"{_dt.datetime.now():%Y%m%d_%H%M%S}"
    R = Report(os.path.join(out_dir, f"probe_{stamp}.txt"))
    R(f"SpectraSP Better — диагностика, {_dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    R(f"Python {platform.python_version()} ({PYTHON_BITS} бит), {platform.platform()}")

    directory = find_spectrasp_dir(args.spectrasp_dir)
    if not directory:
        R("Каталог SpectraSP не найден. Укажите --spectrasp-dir")
        R.save()
        return 2
    inst = read_installation(directory)
    R(f"Каталог SpectraSP: {directory}")
    R(f"  камера: {inst.sensor.sensor_type if inst.sensor else '?'} ID {inst.sensor.sensor_id if inst.sensor else '?'},"
      f" IP {inst.sensor.ip if inst.sensor else '?'}")
    R(f"  монохроматор: {inst.mono.caption} № {inst.mono.serial}, DevCtrl: {inst.devctrl.address} "
      f"({inst.devctrl.interface_text})")
    R("  решётки: " + ", ".join(f"№{g.index + 1} {g.lines_per_mm:g}/мм" for g in inst.mono.gratings))
    if spectrasp_is_running():
        # две программы не должны одновременно обращаться к приборам — ничего не трогаем
        R("ОСТАНОВЛЕНО: запущена SpectraSP/DevCtrl. Закройте её и запустите проверку снова.")
        R.save()
        return 3
    if PYTHON_BITS != 32:
        R("ВНИМАНИЕ: нужен 32-битный Python (драйверы 32-битные).")

    mono = None
    if not args.camera_only:
        from ..hw.sspl_mono import SsplMonochromator
        mono = SsplMonochromator(directory)
        info = R.step("Монохроматор: инициализация (SSPL_IM.Init)", mono.open)
        if info:
            R(f"    тип {info.device_type}, № {info.serial}, решёток {len(info.gratings)}, диапазон {info.wl_range}")
            R.step("Состояние", lambda: R(f"    GetStatus = {mono.status()} (0 — готов, 1 — движение)"))
            R.step("Длина волны", lambda: R(f"    {mono.get_wavelength():.4f} нм"))
            R.step("Активная решётка", lambda: R(f"    №{mono.get_grating() + 1}"))
            for i in range(max(1, len(inst.mono.slits))):
                R.step(f"Щель {i + 1}", lambda i=i: R(f"    {mono.get_slit(i):g} мкм"))
            R.step("Выходное зеркало", lambda: R(f"    положение {mono.get_mirror(0)}"))
            R.step("Шторка", lambda: R(f"    {'открыта' if mono.get_shutter(0) else 'закрыта / неизвестно'}"))
            if args.goto is not None:
                R.step(f"Переход на {args.goto} нм", lambda: mono.set_wavelength(args.goto, True, 600))
                R.step("Длина волны после перехода", lambda: R(f"    {mono.get_wavelength():.4f} нм"))

            def calib():
                wl = mono.get_wavelength()
                gr = mono.get_grating()
                cpx = inst.sensor.calib_center_pixel if inst.sensor else 1024
                pw = inst.sensor.calib_pixel_width if inst.sensor else 12
                for width in (pw, 12.0):
                    arr = mono.pixel_wavelengths(wl, cpx, width, 2048)
                    if arr is None:
                        R(f"    модель прибора (центр {cpx}, ширина {width}): недоступна")
                        continue
                    R(f"    модель прибора (центр {cpx}, ширина {width}): λ[0]={arr[0]:.3f}, λ[{int(cpx)}]="
                      f"{arr[int(cpx)]:.3f}, λ[2047]={arr[-1]:.3f}, дисперсия {(arr[-1] - arr[0]) / 2047:.5f} нм/пикс")
                geo = geometries_from_config(inst.mono).get(gr)
                if geo:
                    g = geo.wavelengths(wl, cpx, 12.0, 2048)
                    R(f"    геометрическая модель (шаг 12 мкм): λ[0]={g[0]:.3f}, λ[2047]={g[-1]:.3f}, "
                      f"дисперсия {(g[-1] - g[0]) / 2047:.5f} нм/пикс")
            R.step("Калибровка шкалы", calib)

    if not args.mono_only:
        from ..hw.psi_camera import PsiCamera
        if not inst.sensor or not inst.sensor.ip:
            R("IP камеры не найден в Sensor_*.xml")
        else:
            cam = PsiCamera(directory, inst.sensor.ip, args.dll)
            info = R.step(f"Камера: подключение к {inst.sensor.ip}", cam.open)
            if info:
                R(f"    {info}")
                R(f"    экспозиция в камере {cam.current_exposure_us:.0f} мкс, считывание {cam.current_readout_speed} кГц")
                R.step("Состояние", lambda: R(f"    {cam.get_state()}"))
                R.step("Области", lambda: R(f"    {cam.read_regions()}"))
                if args.cooling is not None:
                    R.step(f"Охлаждение до {args.cooling} °C", lambda: cam.set_cooling(True, args.cooling))
                if args.expose:
                    top, bottom = args.rows or (inst.stripe or (info.height // 2 - 15, info.height // 2 + 14))
                    speed = args.speed or (inst.sensor.readout_speed or cam.current_readout_speed)
                    acq = AcqSettings(int(args.expose * 1000), speed,
                                      (spectrum_region(info.width, info.height, top, bottom, True),))

                    def expose():
                        cam.configure(acq)
                        times = []
                        for _ in range(3):
                            t0 = time.monotonic()
                            frame = cam.acquire()
                            times.append(time.monotonic() - t0)
                        y = frame[0].astype(float)
                        R(f"    полоса {top}…{bottom}, 3 кадра по {args.expose:g} мс: время {', '.join(f'{t:.3f}' for t in times)} с")
                        R(f"    мин {y.min():.0f}, среднее {y.mean():.1f}, макс {y.max():.0f} (пиксель {int(y.argmax())})")
                        path = os.path.join(out_dir, f"probe_{stamp}_spectrum.txt")
                        save_spectrum(path, np.arange(y.size), y, {"exposure_ms": args.expose, "rows": f"{top}-{bottom}"},
                                      x_name="pixel")
                        R(f"    спектр сохранён: {path}")
                    R.step("Пробная экспозиция (аппаратное объединение строк)", expose)
                    if args.full_frame:
                        def full():
                            cam.configure(AcqSettings(int(args.expose * 1000), speed,
                                                      (full_frame_region(info.width, info.height),)))
                            t0 = time.monotonic()
                            img = cam.acquire()
                            R(f"    кадр {img.shape}, время {time.monotonic() - t0:.2f} с, макс {img.max()}")
                            path = os.path.join(out_dir, f"probe_{stamp}_frame.npy")
                            save_image(path, img)
                            R(f"    кадр сохранён: {path}")
                        R.step("Полный кадр", full)
                # холодную ПЗС (например, после --cooling) не оставляем: плавный отогрев до +2 °C
                R.step("Камера: отключение", lambda: cam.close(warm_up=True))
    if mono is not None and mono.is_open:
        R.step("Монохроматор: отключение", mono.close)
    R.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
