"""Прибор целиком: камера + монохроматор + обработка.

Класс не зависит от Qt. Методы блокирующие и потокобезопасные: интерфейс
вызывает их из фоновых задач. Каждый драйвер работает в своём выделенном
потоке (hw/worker.py), поэтому «зависание» прибора не блокирует окно, а
долгую экспозицию можно прервать.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import processing as P
from .calibration import WavelengthAxis, geometries_from_config
from .config import ACQ_IMAGE, ACQ_SPECTRUM, AppSettings
from .hw.base import (MECH_GRATING, MECH_MIRROR, MECH_SLIT, MECH_TITLES, MECH_TURRET, AcqSettings, Camera,
                      CameraInfo, CameraState, DeviceError, GratingInfo, Monochromator, MonoInfo,
                      full_frame_region, spectrum_region, warm_up_camera)
from .hw.worker import DeviceThread
from .spectrasp_config import InstalledSetup, find_spectrasp_dir, read_installation

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]


@dataclass
class Measurement:
    x: np.ndarray
    x_unit: str
    y: np.ndarray                         # итоговый спектр (с вычетом фона, если включён)
    raw: np.ndarray                       # до вычета фона
    image: Optional[np.ndarray] = None    # последний кадр (режим изображения)
    background_used: bool = False
    saturated: bool = False
    exposure_ms: float = 0.0
    accumulations: int = 1
    accumulate_mode: str = "average"
    acq_mode: str = ACQ_SPECTRUM
    rows: Tuple[int, int] = (0, 0)
    hardware_binning: bool = True
    center_wl: Optional[float] = None
    grating: Optional[int] = None
    grating_title: str = ""
    slit_um: Optional[float] = None
    temperature: Optional[float] = None
    timestamp: _dt.datetime = field(default_factory=_dt.datetime.now)
    duration_s: float = 0.0
    calibration_source: str = ""
    is_background: bool = False
    label: str = ""
    stitched: bool = False                # сшит из нескольких кадров (панорама): «пиксель» не имеет смысла

    def meta(self) -> Dict[str, object]:
        m: Dict[str, object] = {
            "measurement_time": self.timestamp.isoformat(timespec="seconds"),
            "type": "background" if self.is_background else "spectrum",
            "exposure_ms": f"{self.exposure_ms:g}",
            "accumulations": f"{self.accumulations} ({self.accumulate_mode})",
            "mode": self.acq_mode,
            "rows": f"{self.rows[0]}-{self.rows[1]} ({'аппаратное' if self.hardware_binning else 'программное'} объединение)",
            "background_subtracted": "yes" if self.background_used else "no",
            "saturated": "YES" if self.saturated else "no",
            "calibration": self.calibration_source,
        }
        if self.center_wl is not None:
            m["center_wavelength_nm"] = f"{self.center_wl:.4f}"
        if self.grating_title:
            m["grating"] = self.grating_title
        if self.slit_um is not None:
            m["entrance_slit_um"] = f"{self.slit_um:g}"
        if self.temperature is not None:
            m["ccd_temperature_C"] = f"{self.temperature:g}"
        if self.label:
            m["label"] = self.label
        return m


class Instrument:
    def __init__(self, settings: AppSettings, simulate: bool = False):
        self.settings = settings
        self.simulate = simulate
        self.setup: Optional[InstalledSetup] = None
        self.camera: Optional[Camera] = None
        self.mono: Optional[Monochromator] = None
        self.cam_thread = DeviceThread("camera")
        self.mono_thread = DeviceThread("monochromator")
        self._lock = threading.RLock()
        self._backgrounds: Dict[tuple, np.ndarray] = {}
        self._bg_images: Dict[tuple, np.ndarray] = {}
        self.mono_wl: Optional[float] = None
        self.mono_grating: Optional[int] = None
        self.mono_slit: Optional[float] = None
        self._abort = threading.Event()
        self.camera_stuck = False
        self.last_state: Optional[CameraState] = None
        self._mono_exclusive: Optional[Tuple[str, int]] = None     # (кто, поток) — см. mono_exclusive()
        self.axis = WavelengthAxis(settings.calibration, {})
        self.load_installation()

    # ================================================================ конфигурация
    def load_installation(self) -> Optional[InstalledSetup]:
        d = find_spectrasp_dir(self.settings.spectrasp_dir or None)
        if d:
            self.settings.spectrasp_dir = d
            self.setup = read_installation(d)
            self.axis.geometries = geometries_from_config(self.setup.mono)
            self._import_spectrasp_defaults()
            log.info("Каталог SpectraSP: %s", d)
        else:
            self.setup = None
            log.warning("Каталог SpectraSP не найден")
        if self.simulate and not self.axis.geometries:
            from .hw.simulated import DEFAULT_GEOMETRIES
            self.axis.geometries = dict(DEFAULT_GEOMETRIES)
        return self.setup

    def _import_spectrasp_defaults(self) -> None:
        """Однократно перенести из SpectraSP параметры, чтобы всё выглядело привычно."""
        s, st = self.settings, self.setup
        if st is None:
            return
        if not s.wl_presets and st.main.wl_presets:
            s.wl_presets = list(st.main.wl_presets)
        if not s.save_dir and st.main.last_dir:
            s.save_dir = st.main.last_dir
        if s.rows_top < 0 and st.stripe:
            s.rows_top, s.rows_bottom = st.stripe
        if st.sensor and not s.calibration_imported and not self.simulate:
            c = s.calibration
            if st.sensor.calib_center_pixel > 0:
                c.center_pixel = st.sensor.calib_center_pixel
            if st.sensor.calib_pixel_width > 0:
                c.device_pixel_width = st.sensor.calib_pixel_width
            s.calibration_imported = True
        if st.sensor and st.sensor.readout_speed > 0 and not s.readout_speed:
            s.readout_speed = st.sensor.readout_speed
        if st.sensor and st.sensor.exposure_us > 0 and s.exposure_ms <= 0:
            s.exposure_ms = st.sensor.exposure_us / 1000.0
        if st.sensor and st.sensor.flip.upper().startswith("HOR"):
            s.flip_x = True

    def camera_ip(self) -> str:
        if self.settings.camera_ip_override:
            return self.settings.camera_ip_override
        if self.setup and self.setup.sensor:
            return self.setup.sensor.ip
        return ""

    def mono_address(self) -> str:
        if self.setup:
            dc = self.setup.devctrl
            return f"{dc.address} ({dc.interface_text})" if dc.address else ""
        return ""

    # ================================================================ подключение
    def _make_devices(self) -> None:
        with self._lock:
            self._make_devices_locked()

    def _make_devices_locked(self) -> None:
        if self.simulate:
            from .hw.simulated import SimCamera, SimMonochromator
            if self.mono is None:
                self.mono = SimMonochromator(geometries=self.axis.geometries or None)
            if self.camera is None:
                self.camera = SimCamera(mono=self.mono, true_center_pixel=self.settings.calibration.center_pixel)
            return
        if not self.settings.spectrasp_dir or not self.setup:
            raise DeviceError("Не найден каталог установленной SpectraSP. Укажите его в настройках.")
        if self.camera is None:
            from .hw.psi_camera import PsiCamera
            ip = self.camera_ip()
            if not ip:
                raise DeviceError("В настройках SpectraSP не найден IP-адрес камеры (Sensor_*.xml)")
            self.camera = PsiCamera(self.settings.spectrasp_dir, ip, self.settings.camera_dll)
        if self.mono is None:
            from .hw.sspl_mono import SsplMonochromator
            self.mono = SsplMonochromator(self.settings.spectrasp_dir, self._mono_template())

    def _mono_template(self) -> MonoInfo:
        info = MonoInfo()
        if self.setup:
            mc = self.setup.mono
            info.model = mc.caption
            info.serial = mc.serial
            info.gratings = [GratingInfo(g.index, g.lines_per_mm) for g in mc.gratings]
            info.slits = list(mc.slits)
            info.mirror_positions = list(mc.mirror_positions)
            info.has_shutter = mc.has_shutter
            info.step_limits = dict(mc.step_limits)
            info.slit_max_um = list(mc.slit_max_um)
        return info

    def connect_camera(self) -> CameraInfo:
        self._make_devices()
        try:
            info = self.cam_thread.call(self.camera.open, timeout=180)
        except FutureTimeout:
            raise DeviceError("Камера не ответила за 3 минуты при подключении") from None
        self.camera_stuck = False
        self._fix_rows(info)
        try:
            self.last_state = self.cam_thread.call(self.camera.get_state)
        except DeviceError:
            pass
        if not self.settings.readout_speed and getattr(self.camera, "current_readout_speed", 0):
            self.settings.readout_speed = self.camera.current_readout_speed
        return info

    def _fix_rows(self, info: CameraInfo) -> None:
        s = self.settings
        if s.rows_top < 0 or s.rows_bottom < 0 or s.rows_bottom >= info.height:
            c = info.height // 2
            s.rows_top, s.rows_bottom = max(0, c - 15), min(info.height - 1, c + 14)

    def connect_mono(self) -> MonoInfo:
        self._make_devices()
        try:
            info = self.mono_thread.call(self.mono.open, timeout=self.settings.mono_timeout_s + 60)
        except FutureTimeout:
            raise DeviceError("Монохроматор не ответил при подключении") from None
        self.refresh_mono()
        return info

    def refresh_mono(self) -> None:
        if not self.mono_connected:
            return

        def read():
            wl = self.mono.get_wavelength()
            gr = self.mono.get_grating()
            try:
                slit = self.mono.get_slit(0)
            except DeviceError:
                slit = None
            return wl, gr, slit
        wl, gr, slit = self.mono_thread.call(read)
        with self._lock:
            self.mono_wl, self.mono_grating, self.mono_slit = wl, gr, slit
            self.axis.invalidate()

    @property
    def camera_connected(self) -> bool:
        cam = self.camera
        return cam is not None and cam.is_open

    @property
    def mono_connected(self) -> bool:
        m = self.mono
        return m is not None and m.is_open

    def disconnect(self, warm_up: Optional[bool] = None, say: Optional[Callable[[str], None]] = None,
                   cancel_warm_up: Optional[threading.Event] = None) -> None:
        """Безопасное отключение приборов.

        * идущая экспозиция прерывается;
        * охлаждённая ПЗС сначала плавно отогревается до +2 °C (если включено в настройках;
          ожидание можно прервать через cancel_warm_up — уставка +2 °C всё равно остаётся);
        * монохроматор закрывается только после окончания движения: иначе DevCtrl может
          не сохранить положение механизмов, и при следующем запуске оно будет неверным.
        """
        warm = self.settings.warm_up_on_close if warm_up is None else warm_up
        say = say or (lambda text: None)
        errors = []
        self.abort()
        # не меньше 5 минут: даже при коротком таймауте в настройках не закрываем DevCtrl посреди движения
        t = max(self.settings.mono_timeout_s, 300.0)
        mono_fut = self.mono_thread.submit(self._close_mono_safely, t, label="close") \
            if self.mono is not None else None
        if self.camera is not None:
            try:
                self.cam_thread.call(lambda: None, timeout=60)       # дождаться прерванной экспозиции
            except FutureTimeout:
                errors.append("камера: экспозиция не прервалась за 60 с")
            if warm and self.camera_connected and not self.camera_stuck and self.camera.info.cooling_type:
                try:
                    warm_up_camera(lambda: self.cam_thread.call(self.camera.get_state, timeout=15),
                                   lambda on, c: self.cam_thread.call(self.camera.set_cooling, on, c, timeout=15),
                                   say=say, cancel=cancel_warm_up)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"отогрев ПЗС: {e}")
            try:
                self.cam_thread.call(self.camera.close, False, timeout=60)
            except Exception as e:  # noqa: BLE001
                errors.append(f"камера: {e}")
        if mono_fut is not None:
            if not mono_fut.done():
                say("Ожидание окончания движения монохроматора…")
            try:
                mono_fut.result(timeout=t + 90)
            except Exception as e:  # noqa: BLE001
                errors.append(f"монохроматор: {e}")
        if errors:
            log.warning("При отключении: %s", "; ".join(errors))

    def _close_mono_safely(self, timeout_s: float) -> None:
        """В потоке монохроматора: дождаться конца движения и штатно закрыть DevCtrl."""
        m = self.mono
        if m.is_open:
            try:
                if m.is_busy():
                    log.info("Отключение: ожидание окончания движения монохроматора…")
                m.wait_idle(timeout_s)
            except DeviceError as e:
                log.warning("Монохроматор перед отключением: %s", e)
        m.close()

    def shutdown(self, say: Optional[Callable[[str], None]] = None,
                 cancel_warm_up: Optional[threading.Event] = None) -> None:
        try:
            self.disconnect(say=say, cancel_warm_up=cancel_warm_up)
        finally:
            self.cam_thread.stop(2)
            self.mono_thread.stop(2)

    # ================================================================ камера
    def camera_state(self) -> Optional[CameraState]:
        """Состояние камеры, если поток камеры свободен (иначе последнее известное)."""
        if not self.camera_connected:
            return None
        if self.cam_thread.busy_for > 0:
            return self.last_state
        try:
            self.last_state = self.cam_thread.call(self.camera.get_state, timeout=10)
        except FutureTimeout:
            pass
        return self.last_state

    def set_cooling(self, enabled: bool, target_c: int) -> None:
        self._require_camera()
        self.cam_thread.call(self.camera.set_cooling, enabled, int(target_c))

    def _require_camera(self) -> None:
        if not self.camera_connected:
            raise DeviceError("Камера не подключена")
        if self.camera_stuck:
            raise DeviceError("Камера не отвечает. Нажмите «Переподключить».")

    def acq_settings(self) -> AcqSettings:
        s = self.settings
        info = self.camera.info
        if s.acq_mode == ACQ_IMAGE:
            region = full_frame_region(info.width, info.height)
        else:
            region = spectrum_region(info.width, info.height, s.rows_top, s.rows_bottom, s.hardware_binning)
            if region.bin_y > info.max_bin_y:
                region = spectrum_region(info.width, info.height, s.rows_top, s.rows_bottom, False)
        speed = s.readout_speed or (info.readout_speeds[0] if info.readout_speeds else 0)
        return AcqSettings(exposure_us=int(round(s.exposure_ms * 1000)), readout_speed=speed, regions=(region,))

    def expected_frame_time(self, acq: AcqSettings) -> float:
        r = acq.regions[0]
        speed = max(1, acq.readout_speed or 125) * 1000.0
        return acq.exposure_us / 1e6 + r.out_pixels / speed + 0.3 + (r.out_pixels * 2) / 5e6

    def _frame(self, acq: AcqSettings) -> np.ndarray:
        """Один кадр с контролем времени: при зависании — прерывание."""
        self.cam_thread.call(self.camera.configure, acq, timeout=60)
        limit = self.expected_frame_time(acq) + self.settings.frame_timeout_s
        fut = self.cam_thread.submit(self.camera.acquire, label="acquire")
        try:
            return fut.result(timeout=limit)
        except FutureTimeout:
            log.error("Кадр не получен за %.0f с — прерывание экспозиции", limit)
            try:
                self.camera.abort()
            except Exception:  # noqa: BLE001
                log.exception("abort")
            try:
                return fut.result(timeout=15)
            except FutureTimeout:
                self.camera_stuck = True
                raise DeviceError("Камера не отвечает (экспозиция не завершилась). "
                                  "Проверьте питание и сеть камеры, затем переподключитесь.") from None
            except DeviceError:
                raise DeviceError(f"Кадр не получен за {limit:.0f} с, экспозиция прервана") from None

    def abort(self) -> None:
        """Прервать текущую экспозицию и серию."""
        self._abort.set()
        cam = self.camera
        if cam is not None and self.cam_thread.current_label == "acquire":
            try:
                cam.abort()
            except Exception:  # noqa: BLE001
                log.exception("abort")

    def _bg_key(self, acq: AcqSettings) -> tuple:
        return acq.exposure_us, acq.readout_speed, acq.regions, self.settings.flip_x

    def has_background(self) -> bool:
        if not self.camera_connected:
            return False
        return self._bg_key(self.acq_settings()) in self._backgrounds

    def clear_background(self) -> None:
        with self._lock:
            self._backgrounds.clear()
            self._bg_images.clear()

    def _frame_to_spectrum(self, frame: np.ndarray, acq: AcqSettings) -> np.ndarray:
        s = self.settings
        if s.acq_mode == ACQ_IMAGE:
            top = max(0, s.rows_top)
            return P.bin_rows(frame, top, s.rows_bottom)
        return P.to_spectrum(frame)

    def acquire(self, n: Optional[int] = None, progress: Optional[ProgressFn] = None,
                background: bool = False, label: str = "") -> Measurement:
        """Снять спектр (с накоплением n кадров). При background=True — снять и
        запомнить темновой фон (шторка закрывается автоматически, если включено)."""
        self._require_camera()
        s = self.settings
        n = max(1, int(n or s.accumulations))
        self._abort.clear()
        acq = self.acq_settings()
        t0 = time.monotonic()
        shutter_restore = None
        if background and s.close_shutter_for_background and self.mono_connected:
            try:
                prev = self.mono_thread.call(self.mono.get_shutter, 0, timeout=30)
                self.mono_thread.call(self.mono.set_shutter, False, 0, timeout=30)
                shutter_restore = prev if prev is not None else True
                time.sleep(0.2)
            except (DeviceError, FutureTimeout) as e:
                log.warning("Не удалось закрыть шторку для фона: %s", e)
        spectra: List[np.ndarray] = []
        frame = None
        sat = False
        try:
            for i in range(n):
                if self._abort.is_set():
                    raise DeviceError("Остановлено пользователем")
                if progress:
                    progress(i, n, "фон" if background else "экспозиция")
                frame = self._frame(acq)
                if s.flip_x:
                    frame = frame[:, ::-1]
                sat = sat or P.saturated(frame)
                spectra.append(self._frame_to_spectrum(frame, acq))
        finally:
            if shutter_restore is not None:
                try:
                    self.mono_thread.call(self.mono.set_shutter, shutter_restore, 0, timeout=30)
                except Exception as e:  # noqa: BLE001
                    log.warning("Не удалось вернуть шторку: %s", e)
        if progress:
            progress(n, n, "обработка")
        mode = "average" if background else s.accumulate_mode
        raw = P.combine_frames(spectra, mode=mode, reject_cosmics=s.remove_cosmics)
        if s.remove_cosmics and n < 3:
            raw = P.remove_spikes(raw)
        key = self._bg_key(acq)
        y = raw
        bg_used = False
        if background:
            with self._lock:
                self._backgrounds[key] = raw.copy()
                if frame is not None and s.acq_mode == ACQ_IMAGE:
                    self._bg_images[key] = frame.astype(np.float64)
        elif s.subtract_background:
            with self._lock:
                bg = self._backgrounds.get(key)
            if bg is not None and bg.shape == raw.shape:
                y = raw - bg * (n if mode == "sum" else 1)
                bg_used = True
        x, unit = self.wavelength_axis(raw.size)
        m = Measurement(x=x, x_unit=unit, y=y, raw=raw,
                        image=frame if s.acq_mode == ACQ_IMAGE else None,
                        background_used=bg_used, saturated=sat,
                        exposure_ms=acq.exposure_us / 1000.0, accumulations=n, accumulate_mode=mode,
                        acq_mode=s.acq_mode, rows=(max(0, s.rows_top), s.rows_bottom),
                        hardware_binning=acq.regions[0].bin_y > 1,
                        center_wl=self.mono_wl, grating=self.mono_grating,
                        grating_title=self.grating_title(self.mono_grating),
                        slit_um=self.mono_slit,
                        temperature=self.last_state.temperature if self.last_state else None,
                        duration_s=time.monotonic() - t0, calibration_source=self.axis.source,
                        is_background=background, label=label)
        return m

    def background_image(self) -> Optional[np.ndarray]:
        if not self.camera_connected:
            return None
        with self._lock:
            return self._bg_images.get(self._bg_key(self.acq_settings()))

    # ================================================================ шкала
    def wavelength_axis(self, n_pixels: int) -> Tuple[np.ndarray, str]:
        with self._lock:
            wl, gr = self.mono_wl, self.mono_grating
        use_device = self.mono_connected and self.mono_thread.busy_for == 0
        return self.axis.axis(n_pixels, wl, gr, self.device_model if use_device else None)

    def device_model(self, center: float, center_px: float, px_width: float, n: int):
        """Шкала по модели прибора (SSPL_IM.GetCalibrationByTurret) — вызов в потоке монохроматора."""
        return self.mono_thread.call(self.mono.pixel_wavelengths, center, center_px, px_width, n, timeout=10)

    def grating_title(self, index: Optional[int]) -> str:
        if index is None or self.mono is None:
            return ""
        for g in self.mono.info.gratings:
            if g.index == index:
                return g.title
        return f"№{index + 1}"

    # ================================================================ монохроматор
    def _require_mono(self, control: bool = False) -> None:
        """control=True — команда, которая что-то двигает (проверяется и монопольный режим)."""
        if not self.mono_connected:
            raise DeviceError("Монохроматор не подключён")
        if control:
            excl = self._mono_exclusive
            if excl is not None and excl[1] != threading.get_ident():
                raise DeviceError(f"Монохроматором сейчас управляет {excl[0]} — дождитесь окончания или нажмите «Стоп»")

    @contextmanager
    def mono_exclusive(self, who: str):
        """На время панорамы и т. п. ручные команды монохроматору из других потоков отклоняются."""
        self._mono_exclusive = (who, threading.get_ident())
        try:
            yield
        finally:
            self._mono_exclusive = None

    # --- «защита от дурака»: проверка команд до отправки в прибор
    def _check_wavelength(self, wl: float) -> None:
        if not math.isfinite(wl) or wl <= 0:
            raise DeviceError(f"Недопустимая длина волны: {wl}")
        lo, hi = self.mono.info.wl_range
        if hi > lo and not (lo <= wl <= hi):
            raise DeviceError(f"Длина волны {wl:g} нм вне диапазона текущей решётки {lo:g}…{hi:g} нм")

    def _check_grating(self, index: int) -> None:
        n = len(self.mono.info.gratings)
        if index < 0 or (n and index >= n):
            raise DeviceError(f"В приборе нет решётки №{index + 1}" + (f" (решёток: {n})" if n else ""))

    def _check_mirror(self, position: int) -> None:
        n = len(self.mono.info.mirror_positions) or 2
        if not 0 <= position < n:
            raise DeviceError(f"У выходного зеркала нет положения {position}")

    def slit_max(self, index: int) -> float:
        """Наибольшая допустимая ширина щели, мкм (0 — неизвестна)."""
        mx = self.mono.info.slit_max_um if self.mono is not None else []
        return float(mx[index]) if 0 <= index < len(mx) else 0.0

    def _check_slit(self, index: int, width_um: float) -> None:
        n = len(self.mono.info.slits)
        if index < 0 or (n and index >= n):
            raise DeviceError(f"В приборе нет щели №{index + 1}")
        if not math.isfinite(width_um) or width_um < 0:
            raise DeviceError(f"Недопустимая ширина щели: {width_um}")
        mx = self.slit_max(index)
        if mx and width_um > mx:
            raise DeviceError(f"Ширина {width_um:g} мкм больше допустимой для этой щели ({mx:g} мкм)")

    def _check_steps(self, mech: str, index: int, target: int) -> None:
        limit = self.mono.info.step_limits.get((mech, index))
        name = MECH_TITLES.get(mech, mech)
        if not limit:
            raise DeviceError(f"Для механизма «{name}» неизвестен предел хода (нет MaxMayStepCount в файле прибора) — "
                              "перемещение по шагам заблокировано. Доступен «Сброс» или штатное окно DevCtrl.")
        if not 0 <= target <= limit:
            raise DeviceError(f"Положение {target} шагов вне допустимого диапазона 0…{limit} для «{name}»")

    def goto_wavelength(self, wl: float) -> float:
        self._require_mono(control=True)
        self._check_wavelength(float(wl))
        t = self.settings.mono_timeout_s
        self.mono_thread.call(self.mono.set_wavelength, float(wl), True, t, timeout=t + 30)
        self.refresh_mono()
        return self.mono_wl

    def set_grating(self, index: int) -> None:
        self._require_mono(control=True)
        self._check_grating(int(index))
        t = self.settings.mono_timeout_s
        self.mono_thread.call(self.mono.set_grating, int(index), True, t, timeout=t + 30)
        self.refresh_mono()

    def set_slit(self, index: int, width_um: float) -> float:
        self._require_mono(control=True)
        self._check_slit(int(index), float(width_um))
        self.mono_thread.call(self.mono.set_slit, int(index), float(width_um), timeout=120)
        w = self.mono_thread.call(self.mono.get_slit, int(index), timeout=60)
        if index == 0:
            self.mono_slit = w
        return w

    def get_slit(self, index: int) -> float:
        self._require_mono()
        return self.mono_thread.call(self.mono.get_slit, int(index), timeout=60)

    def set_mirror(self, position: int, index: int = 0) -> None:
        self._require_mono(control=True)
        self._check_mirror(int(position))
        self.mono_thread.call(self.mono.set_mirror, index, int(position), timeout=120)

    def get_mirror(self, index: int = 0) -> int:
        self._require_mono()
        return self.mono_thread.call(self.mono.get_mirror, index, timeout=60)

    def set_shutter(self, is_open: bool) -> None:
        self._require_mono(control=True)
        self.mono_thread.call(self.mono.set_shutter, bool(is_open), 0, timeout=60)

    def get_shutter(self) -> Optional[bool]:
        self._require_mono()
        return self.mono_thread.call(self.mono.get_shutter, 0, timeout=60)

    # ---------------------------------------------------------------- сервис
    def service_mechanisms(self) -> List[Tuple[str, int, str]]:
        """Механизмы для сервисного окна: (механизм, номер, название)."""
        if self.mono is None:
            return []
        info = self.mono.info
        out = [(MECH_GRATING, 0, "Решётка (длина волны)"), (MECH_TURRET, 0, "Турель решёток")]
        out += [(MECH_SLIT, i, name) for i, name in enumerate(info.slits or ["Входная щель"])]
        if info.mirror_positions:
            out.append((MECH_MIRROR, 0, "Выходное зеркало"))
        return out

    def service_snapshot(self) -> Dict[str, object]:
        """Всё состояние монохроматора одним вызовом (для сервисного окна)."""
        self._require_mono()
        mechs = self.service_mechanisms()

        def read():
            m = self.mono
            snap: Dict[str, object] = {"busy": m.is_busy()}

            def safe(key, fn):
                try:
                    snap[key] = fn()
                except DeviceError as e:
                    snap[key] = None
                    log.debug("service %s: %s", key, e)
            safe("wl", m.get_wavelength)
            safe("grating", m.get_grating)
            safe("shutter", lambda: m.get_shutter(0))
            safe("mirror", lambda: m.get_mirror(0))
            for i in range(len(m.info.slits) or 1):
                safe(f"slit{i}", lambda i=i: m.get_slit(i))
            steps = {}
            for mech, idx, _name in mechs:
                try:
                    steps[(mech, idx)] = m.get_steps(mech, idx)
                except DeviceError:
                    steps[(mech, idx)] = None
            snap["steps"] = steps
            return snap
        snap = self.mono_thread.call(read, timeout=self.settings.mono_timeout_s + 30)
        with self._lock:
            if snap.get("wl") is not None:
                self.mono_wl = snap["wl"]
            if snap.get("grating") is not None:
                self.mono_grating = snap["grating"]
            if snap.get("slit0") is not None:
                self.mono_slit = snap["slit0"]
            self.axis.invalidate()
        return snap

    def service_set_steps(self, mech: str, index: int, position: int) -> None:
        self._require_mono(control=True)
        self._check_steps(mech, int(index), int(position))
        t = self.settings.mono_timeout_s
        self.mono_thread.call(self.mono.set_steps, mech, int(index), int(position), t, timeout=t + 30)

    def service_move_steps(self, mech: str, index: int, delta: int) -> None:
        self._require_mono(control=True)
        t = self.settings.mono_timeout_s

        def work():          # положение читается и проверяется в потоке прибора непосредственно перед движением
            self._check_steps(mech, int(index), self.mono.get_steps(mech, int(index)) + int(delta))
            self.mono.move_steps(mech, int(index), int(delta), t)
        self.mono_thread.call(work, timeout=t + 30)

    def service_reset(self, mech: str, index: int) -> None:
        self._require_mono(control=True)
        t = self.settings.mono_timeout_s
        self.mono_thread.call(self.mono.reset, mech, int(index), t, timeout=t + 30)

    def show_native_window(self) -> None:
        """Штатное окно DevCtrl (выполняется в потоке монохроматора — там живут окна драйвера)."""
        self._require_mono(control=True)
        self.mono_thread.call(self.mono.show_native_window, timeout=60)

    def mono_busy(self) -> bool:
        """Идёт ли движение (не блокирует, если поток занят долгой командой)."""
        if not self.mono_connected:
            return False
        if self.mono_thread.busy_for > 0:
            return True
        try:
            return bool(self.mono_thread.call(self.mono.is_busy, timeout=5))
        except (DeviceError, FutureTimeout):
            return False

    # ================================================================ панорама
    def panorama_plan(self, start_nm: float, end_nm: float, overlap: float = 0.15) -> List[float]:
        """Центральные длины волн для панорамы с заданным перекрытием."""
        if not self.camera_connected or self.mono_wl is None:
            raise DeviceError("Нужны подключённые камера и монохроматор")
        n_pix = self.camera.info.width
        lo, hi = sorted((start_nm, end_nm))
        centers: List[float] = []
        c = lo
        for _ in range(1000):
            x, unit = self.axis.axis(n_pix, c, self.mono_grating, None)
            if unit != "nm":
                raise DeviceError("Для панорамы нужна калибровка шкалы длин волн")
            width = abs(x[-1] - x[0])
            if not centers:
                c = lo + width / 2
                x, _ = self.axis.axis(n_pix, c, self.mono_grating, None)
            centers.append(round(float(c), 3))
            if c + width / 2 >= hi:
                break
            c += width * (1 - overlap)
        return centers
