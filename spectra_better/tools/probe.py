"""Диагностика оборудования по шагам (запускать на компьютере с приборами).

    py -3.12-32 -m spectra_better.tools.probe
    py -3.12-32 -m spectra_better.tools.probe --expose 100
    py -3.12-32 -m spectra_better.tools.probe --mono-only
    py -3.12-32 -m spectra_better.tools.probe --open-shutter --expose 200

По умолчанию выполняются только безопасные операции чтения: подключение,
чтение параметров, температуры, длины волны и т. п. Экспозиция (--expose),
перемещение монохроматора (--goto), открытие шторки (--open-shutter) и охлаждение
(--cooling) — только по ключам.
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
from .. import processing as P
from ..hw.base import AcqSettings, DeviceError, camera_exposure_us, full_frame_region, spectrum_region
from ..hw.winutil import IS_WINDOWS, PYTHON_BITS, present_as_spectrasp, pump_messages, spectrasp_is_running
from ..spectrasp_config import find_spectrasp_dir, read_installation
from ..storage import save_image, save_spectrum


def open_shutter(mono) -> float:
    """Открыть шторку и дождаться подтверждения (DevCtrl выполняет команду не сразу). Возвращает, за
    сколько секунд прибор подтвердил открытие."""
    mono.set_shutter(True, 0)
    t0 = time.monotonic()
    state = None
    while time.monotonic() - t0 < 6:
        pump_messages()
        time.sleep(0.02)
        state = mono.get_shutter(0)
        if state:
            return time.monotonic() - t0
    raise DeviceError("прибор не подтвердил открытие шторки за 6 с (сообщает: "
                      + {False: "закрыта", None: "неизвестно"}[state] + ")")


LINEARITY_MS = (0.01, 0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500)
LINEARITY_FRAMES = 3
LINE_HALF_WIDTH = 5         # сигнал — сумма ±5 пикселей вокруг линии над медианой спектра


def linearity_sweep(R, cam, info, region, speed: int, ttl: bool, out_path: str) -> None:
    """Снимки линии при экспозициях LINEARITY_MS (по LINEARITY_FRAMES кадров) и разбор пропорциональности.

    Сигнал — сумма отсчётов в окне вокруг самой яркой линии за вычетом медианы спектра (темновой ток,
    смещение, фон): какая доля линии попала в окно, неважно — она одна и та же при всех экспозициях.
    Шум точки — по разбросу между кадрами; по нему же — погрешность «лишнего времени»."""
    sync = (0, -1) if ttl else (-1, -1)        # TTL: выход 1 камеры — импульс экспозиции («EXP», как в SpectraSP)
    rows = []
    for e in LINEARITY_MS:
        us = camera_exposure_us(e * 1000, info.min_exposure)
        cam.configure(AcqSettings(us, speed, (region,), sync_out=sync))
        frames, times = [], []
        for _ in range(LINEARITY_FRAMES):
            t0 = time.monotonic()
            frames.append(cam.acquire()[0].astype(float))
            times.append(time.monotonic() - t0)
        sat = max(float(f.max()) for f in frames) >= 65535
        rows.append((us / 1000.0, frames, sat, float(np.mean(times))))
    # медиана по кадрам: космическая частица в окне линии не испортит точку
    ref = next((np.median(fr, axis=0) for _, fr, sat, _ in reversed(rows) if not sat), np.median(rows[-1][1], axis=0))
    i = int(np.argmax(ref))
    w = slice(max(0, i - LINE_HALF_WIDTH), i + LINE_HALF_WIDTH + 1)

    def window_sum(y):
        return float(np.sum(y[w] - np.median(y)))
    sig, err = [], []
    for _, frames, sat, _ in rows:
        if sat:
            sig.append(float("nan"))
            err.append(float("nan"))
            continue
        sig.append(window_sum(np.median(frames, axis=0)))
        sums = [window_sum(f) for f in frames]
        err.append(float(np.std(sums, ddof=1) / np.sqrt(len(sums))) if len(sums) > 1 else float("nan"))
    exps = [r[0] for r in rows]
    R(f"    строки {region.top}…{region.bottom}; линия: пиксель {i}; "
      f"шторка {'по экспозиции (TTL)' if ttl else 'открыта вручную'}")
    R("    экспозиция, мс | сигнал, отсч. | шум, отсч. | отсч./мс | время кадра, с")
    for (e, _, sat, t), s_, n_ in zip(rows, sig, err):
        R(f"    {e:>12g} | {('НАСЫЩЕНИЕ' if sat else f'{s_:.0f}'):>12} | {n_:>10.0f} | "
          f"{(s_ / e if np.isfinite(s_) else float('nan')):>8.1f} | {t:.3f}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("exposure_ms;signal;noise;saturated;frame_time_s\n")
        for (e, _, sat, t), s_, n_ in zip(rows, sig, err):
            f.write(f"{e:g};{s_:.1f};{n_:.1f};{int(sat)};{t:.4f}\n")
    noise = float(np.nanmedian(err)) if np.isfinite(err).any() else float("nan")
    top = max((s_ for s_ in sig if np.isfinite(s_)), default=float("nan"))
    hint = ("Поставьте полосу строк на свет (--rows ВЕРХ НИЗ, как в программе на изображении ПЗС), "
            "откройте щель или усильте свет и повторите.")
    try:
        res = P.linearity_fit(exps, sig, sigmas=err)
    except ValueError as e:
        R(f"    не удалось оценить: {e}")
        R(f"    СИГНАЛ СЛАБЫЙ или его нет. {hint}")
        R(f"    таблица: {out_path}")
        return
    R(f"    подгонка по экспозициям ≥ 20 мс: сигнал ≈ {res.rate:.1f} отсч./мс × (t {res.t0_ms:+.2f} мс)")
    if np.isfinite(res.t0_err_ms):
        R(f"    погрешность «лишнего времени»: ±{res.t0_err_ms:.1f} мс")
    weak = not (np.isfinite(noise) and noise > 0 and top / noise >= 100) or \
        (np.isfinite(res.t0_err_ms) and res.t0_err_ms > 3)
    if weak:
        R(f"    СИГНАЛ СЛАБЫЙ: при самой длинной экспозиции он лишь в {top / noise if noise else 0:.0f} раз выше "
          f"шума — оценке «лишнего времени» верить нельзя. {hint}")
    significant = np.isfinite(res.t0_err_ms) and abs(res.t0_ms) > 2 * res.t0_err_ms
    if weak:
        pass
    elif not significant:
        R("    «лишнего времени» не обнаружено (в пределах погрешности)")
    elif ttl and res.t0_ms < 0:
        R(f"    шторка «съедает» ≈ {-res.t0_ms:.1f} мс каждой экспозиции (открывается позже/закрывается раньше)")
    elif res.t0_ms > 0:
        R(f"    свет добавляется ≈ {res.t0_ms:.1f} мс сверх экспозиции (пока идёт сдвиг строк при считывании)")
    if np.isfinite(res.min_ok_ms) and not weak:
        R(f"    сигнал пропорционален экспозиции с точностью 5 % начиная с {res.min_ok_ms:g} мс")
    R(f"    таблица: {out_path}")


def default_rows(args, inst, height: int):
    """Полоса строк для пробных снимков: --rows, иначе — как в программе (где пользователь поставил её
    на свет), иначе — из SpectraSP, иначе — середина ПЗС. Возвращает ((верх, низ), откуда)."""
    if args.rows:
        rows, src = tuple(args.rows), "--rows"
    else:
        rows, src = None, ""
        try:
            from ..config import AppSettings
            s = AppSettings.load()
            if 0 <= s.rows_top < s.rows_bottom:
                rows, src = (s.rows_top, s.rows_bottom), "настройки SpectraSP Better"
        except Exception:  # noqa: BLE001 — нет файла настроек: берём дальше
            pass
        if rows is None and inst.stripe:
            rows, src = tuple(inst.stripe), "настройки SpectraSP"
        if rows is None:
            rows, src = (height // 2 - 15, height // 2 + 14), "середина ПЗС"
    top, bottom = sorted(int(v) for v in rows)
    return (max(0, top), min(height - 1, bottom)), src


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
    ap.add_argument("--open-shutter", action="store_true",
                    help="открыть входную шторку монохроматора (с подтверждением от прибора)")
    ap.add_argument("--linearity", action="store_true",
                    help="проверка линейности: снимки линии лампы при экспозициях 0,01…500 мс — с какой "
                         "экспозиции сигнал пропорционален времени и сколько «съедает» шторка/считывание")
    ap.add_argument("--ttl", action="store_true",
                    help="с --linearity: шторка по экспозиции (TTL) — измерить её запаздывание")
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
    if IS_WINDOWS:
        # драйверы ищут psi_ccd5.dll и DevCtrl.dll рядом с «программой» — она должна быть в каталоге SpectraSP
        ok = present_as_spectrasp(directory)
        R(f"  путь программы для драйверов: {os.path.join(directory, 'SpectraSP.exe') if ok else 'НЕ ПОДМЕНЁН'}")
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
            R.step("Шторка", lambda: R("    " + {True: "открыта", False: "закрыта"}.get(mono.get_shutter(0),
                                                                              "прибор не сообщает")))
            if args.open_shutter or (args.linearity and not args.ttl):
                def manual_open():
                    try:
                        mono.set_shutter_ttl(False, 0)   # шторка могла остаться «по экспозиции» с прошлого сеанса
                    except DeviceError:
                        pass
                    R(f"    открыта (подтверждено через {open_shutter(mono):.2f} с)")
                R.step("Открыть шторку", manual_open)
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
                if args.expose or args.linearity:
                    (top, bottom), rows_src = default_rows(args, inst, info.height)
                    R(f"    полоса строк {top}…{bottom} ({rows_src})")
                if args.expose:
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
                if args.linearity:
                    speed = args.speed or (inst.sensor.readout_speed or cam.current_readout_speed)
                    region = spectrum_region(info.width, info.height, top, bottom, True)
                    ttl_on = False
                    if args.ttl:
                        if mono is not None and mono.is_open:
                            ttl_on = R.step("Шторка по экспозиции (TTL)",
                                            lambda: mono.set_shutter_ttl(True, 0) or True) is True
                        else:
                            R("--ttl: монохроматор не подключён — шторкой по TTL управлять нельзя")
                    try:
                        R.step("Линейность по экспозиции", lambda: linearity_sweep(
                            R, cam, info, region, speed, args.ttl,
                            os.path.join(out_dir, f"probe_{stamp}_linearity.csv")))
                    finally:
                        if ttl_on:
                            R.step("Шторка: ручное управление", lambda: mono.set_shutter_ttl(False, 0))
                # холодную ПЗС (например, после --cooling) не оставляем: плавный отогрев до +2 °C
                R.step("Камера: отключение", lambda: cam.close(warm_up=True))
    if mono is not None and mono.is_open:
        R.step("Монохроматор: отключение", mono.close)
    R.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
