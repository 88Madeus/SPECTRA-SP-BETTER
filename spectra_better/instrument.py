"""Прибор целиком: камера + монохроматор + обработка.

Класс не зависит от Qt. Методы блокирующие и потокобезопасные: интерфейс
вызывает их из фоновых задач. Каждый драйвер работает в своём выделенном
потоке (hw/worker.py), поэтому «зависание» прибора не блокирует окно, а
долгую экспозицию можно прервать.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import math
import threading
import time
from collections import deque
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
                      SYNC_EXPOSURE_MODES, SYNC_LOGICAL_0, SYNC_LOGICAL_1, SYNC_OUT_MODES, camera_exposure_us,
                      full_frame_region, spectrum_region, warm_up_camera)
from .hw.worker import DeviceThread
from .spectrasp_config import InstalledSetup, find_spectrasp_dir, read_installation

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]

# Съёмка запрещена, пока ПЗС выходит на новую уставку (как в SpectraSP: «Идет процесс изменения
# температуры камеры»). Температура считается установившейся в пределах TEMP_TOLERANCE_C от уставки
# или если она не меняется TEMP_STALL_S секунд (уставка недостижима — не блокировать навсегда).
TEMP_TOLERANCE_C = 1.0
TEMP_STALL_S = 120.0
# фон, снятый при другой температуре, не вычитается (темновой ток ~×2 на каждые 6–7 °C)
BG_TEMP_TOLERANCE_C = 2.0
# DevCtrl выполняет OpenShutter/CloseShutter асинхронно: команда уходит в очередь, а состояние
# меняется, когда прибор ответит. Поэтому после команды ждём подтверждения (SwitchTimeout шторки
# в MS7504_*.cfg — 2 с, плюс обмен по сети) и даём шторке доехать.
SHUTTER_CONFIRM_S = 6.0
SHUTTER_SETTLE_S = 0.3


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
    # несколько полос строк: спектр каждой (T×N, полоса 1 = y) и их строки ПЗС
    tracks: Optional[np.ndarray] = None
    track_rows: Tuple[Tuple[int, int], ...] = ()
    defects_fixed: int = 0                # исправлено дефектных пикселей
    intensity_corrected: str = ""         # как скорректирована интенсивность ("" — нет)
    comment: str = ""                     # комментарий пользователя (образец, условия)
    source_file: str = ""                 # кадр открыт из файла (не снят сейчас)

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
        if self.comment:
            m["comment"] = self.comment
        if self.track_rows and len(self.track_rows) > 1:
            m["stripes"] = ", ".join(f"{a}-{b}" for a, b in self.track_rows)
        if self.defects_fixed:
            m["defect_pixels_fixed"] = str(self.defects_fixed)
        if self.intensity_corrected:
            m["intensity_correction"] = self.intensity_corrected
        return m

    @property
    def n_tracks(self) -> int:
        return 1 if self.tracks is None else int(self.tracks.shape[0])

    def track(self, i: int) -> np.ndarray:
        """Спектр полосы i (0 — основная)."""
        return self.y if i == 0 or self.tracks is None else self.tracks[i]


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
        self.shutter_state: Optional[bool] = None     # последнее известное состояние шторки (True — открыта)
        self.shutter_ttl: Optional[bool] = None       # режим шторки в приборе: True — по экспозиции (TTL)
        self._corr_warned = False
        self._shutter_warned = False
        self.cooling_target: Optional[int] = None    # уставка, заданная в этом сеансе (None — выкл./неизвестна)
        self._temp_hist: deque = deque(maxlen=400)   # (время, температура ПЗС) — для «уставка недостижима»
        # команды, меняющие свет на ПЗС (движение монохроматора, шторка, лазер и выходы синхронизации):
        # кадр не снимается, пока такая команда выполняется, а кадр, во время которого она началась,
        # снимается заново — иначе в спектре смесь двух состояний под подписью одного из них
        self._light = threading.Condition()
        self._light_busy: Dict[int, str] = {}        # выполняющиеся команды: номер → что делается
        self._light_gen = 0                          # растёт при начале и конце каждой такой команды
        self._light_seq = 0
        self._state_time = 0.0
        self._bg_temps: Dict[tuple, float] = {}      # температура ПЗС при съёмке фона
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
        if st.sensor:
            for n, mode in enumerate(st.sensor.sync_out, start=1):
                if getattr(s, f"sync_out{n}") < 0 and mode in SYNC_OUT_MODES:
                    setattr(s, f"sync_out{n}", mode)

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

    # ================================================================ свет на ПЗС
    @contextmanager
    def changing_light(self, what: str):
        """Отметить команду, меняющую свет на ПЗС (см. _wait_light_steady)."""
        with self._light:
            self._light_seq += 1
            key = self._light_seq
            self._light_busy[key] = what
            self._light_gen += 1
        try:
            yield
        finally:
            with self._light:
                del self._light_busy[key]
                self._light_gen += 1
                self._light.notify_all()

    def light_changing(self) -> str:
        """Что сейчас меняет свет на ПЗС ("" — ничего)."""
        with self._light:
            return next(iter(self._light_busy.values()), "")

    def _wait_light_steady(self, progress: Optional[ProgressFn] = None) -> int:
        """Дождаться конца команд, меняющих свет на ПЗС; вернуть номер установившегося состояния."""
        limit = self.settings.mono_timeout_s + 60
        t0 = time.monotonic()
        told = False
        with self._light:
            while self._light_busy:
                if self._abort.is_set():
                    raise DeviceError("Остановлено пользователем")
                if time.monotonic() - t0 > limit:
                    raise DeviceError(f"Не дождались окончания команды монохроматора ({self.light_changing()}) "
                                      f"за {limit:.0f} с — съёмка не начата")
                if progress and not told:
                    progress(0, 0, f"ожидание: {next(iter(self._light_busy.values()))}")
                    told = True
                self._light.wait(0.2)
            return self._light_gen

    def _light_changed_since(self, gen: int) -> bool:
        with self._light:
            return bool(self._light_busy) or self._light_gen != gen

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
            info.shutter_ttl = mc.shutter_ttl
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
        # выходы синхронизации — сразу, как SpectraSP при запуске; лазер при подключении всегда выключен
        s = self.settings
        if s.laser_output in (1, 2):
            setattr(s, f"sync_out{s.laser_output}", self.laser_modes()[1])
        try:
            self.cam_thread.call(self.camera.configure, self.acq_settings(), timeout=60)
        except (DeviceError, FutureTimeout, ValueError) as e:
            log.warning("Параметры камеры (выходы синхронизации) не переданы: %s", e)
        try:
            self._note_state(self.cam_thread.call(self.camera.get_state))
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
        self._apply_shutter_mode()
        return info

    def _apply_shutter_mode(self) -> None:
        """Режим шторки (ручной / по экспозиции) хранится в самом приборе — после подключения
        выставляем выбранный в программе, чтобы он был известен наверняка."""
        self.shutter_ttl = None
        if not (self.mono.info.has_shutter and self.mono.info.shutter_ttl):
            self.shutter_ttl = False
            return
        want = bool(self.settings.shutter_ttl)
        try:
            self.mono_thread.call(self.mono.set_shutter_ttl, want, 0, timeout=60)
            self.shutter_ttl = want
            log.info("Шторка: %s", "по экспозиции (TTL)" if want else "ручное управление")
        except (DeviceError, FutureTimeout) as e:
            log.warning("Не удалось выставить режим шторки: %s", e)

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
            if self.camera_connected and not self.camera_stuck and self.laser_on:
                say("Выключение лазера…")
                try:
                    self.set_laser(False)                            # лазер не оставляем включённым
                except Exception as e:  # noqa: BLE001
                    errors.append(f"выключение лазера: {e}")
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
            self.cooling_target = None     # после отогрева уставка в камере уже другая
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
        self.shutter_state = None
        self.shutter_ttl = None
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
            self._note_state(self.cam_thread.call(self.camera.get_state, timeout=10))
        except FutureTimeout:
            pass
        return self.last_state

    def _note_state(self, st: Optional[CameraState]) -> None:
        self.last_state = st
        self._state_time = time.monotonic()
        if st is not None and st.temperature is not None:
            self._temp_hist.append((self._state_time, float(st.temperature)))

    def set_cooling(self, enabled: bool, target_c: int) -> None:
        self._require_camera()
        self.cam_thread.call(self.camera.set_cooling, enabled, int(target_c))
        self.cooling_target = int(target_c) if enabled else None
        self._temp_hist.clear()                    # отсчёт «температура не меняется» — с новой уставки

    def temperature_changing(self) -> str:
        """Почему сейчас нельзя снимать из-за температуры ПЗС ("" — можно)."""
        st, target = self.last_state, self.cooling_target
        if not self.camera_connected or target is None or st is None or st.temperature is None:
            return ""
        t = float(st.temperature)
        if abs(t - target) <= TEMP_TOLERANCE_C:
            return ""
        now = time.monotonic()
        recent = [T for ts, T in self._temp_hist if now - ts <= TEMP_STALL_S]
        oldest = next((ts for ts, _ in self._temp_hist if now - ts <= TEMP_STALL_S), now)
        if recent and now - oldest >= TEMP_STALL_S * 0.9 and max(recent) - min(recent) <= TEMP_TOLERANCE_C:
            return ""                              # стоит на месте 2 мин — уставка недостижима, не блокируем
        return f"ПЗС выходит на уставку: {t:+.0f} °C → {target:+d} °C"

    def temperature_stalled(self) -> bool:
        """Уставка задана, но недостижима (температура стоит, а до уставки далеко)."""
        st, target = self.last_state, self.cooling_target
        return (self.camera_connected and target is not None and st is not None and st.temperature is not None
                and abs(float(st.temperature) - target) > TEMP_TOLERANCE_C and not self.temperature_changing())

    def _require_camera(self) -> None:
        if not self.camera_connected:
            raise DeviceError("Камера не подключена")
        if self.camera_stuck:
            raise DeviceError("Камера не отвечает. Нажмите «Переподключить».")

    MAX_STRIPES = 8

    def stripes(self) -> List[Tuple[int, int]]:
        """Полосы строк ПЗС: [(верх, низ), …], первая — основная (rows_top…rows_bottom)."""
        s = self.settings
        h = self.camera.info.height if self.camera is not None and self.camera.info.height else 0
        out = []
        for t, b in [(s.rows_top, s.rows_bottom)] + [tuple(p) for p in s.extra_stripes]:
            t, b = int(round(t)), int(round(b))
            t = max(0, t)
            if h:
                t, b = min(t, h - 1), min(b, h - 1)
            out.append((t, max(t, b)))
        return out[:self.MAX_STRIPES]

    def set_stripes(self, stripes: List[Tuple[int, int]]) -> None:
        """Задать полосы (первая — основная). Не больше MAX_STRIPES."""
        if not stripes:
            raise DeviceError("Нужна хотя бы одна полоса строк")
        if len(stripes) > self.MAX_STRIPES:
            raise DeviceError(f"Не больше {self.MAX_STRIPES} полос")
        clean = [(int(min(a, b)), int(max(a, b))) for a, b in stripes]
        s = self.settings
        s.rows_top, s.rows_bottom = clean[0]
        s.extra_stripes = [[a, b] for a, b in clean[1:]]

    def acq_settings(self) -> AcqSettings:
        s = self.settings
        info = self.camera.info
        stripes = self.stripes()
        if s.acq_mode == ACQ_IMAGE:
            region = full_frame_region(info.width, info.height)
        elif len(stripes) > 1:
            # несколько полос: строки от верхней до нижней читаются построчно, полосы
            # суммируются программно (как в SpectraSP — там всегда читался полный кадр)
            region = spectrum_region(info.width, info.height, min(t for t, _ in stripes),
                                     max(b for _, b in stripes), False)
        else:
            region = spectrum_region(info.width, info.height, s.rows_top, s.rows_bottom, s.hardware_binning)
            if region.bin_y > info.max_bin_y:
                region = spectrum_region(info.width, info.height, s.rows_top, s.rows_bottom, False)
        speed = s.readout_speed or (info.readout_speeds[0] if info.readout_speeds else 0)
        sync = self._sync_modes()
        # в камеру уходит не любое число: не меньше её минимума и с точностью формата PSI
        exposure = camera_exposure_us(s.exposure_ms * 1000, info.min_exposure)
        return AcqSettings(exposure_us=exposure, readout_speed=speed, regions=(region,), sync_out=sync)

    def _sync_modes(self) -> Tuple[int, int]:
        s = self.settings
        return tuple(m if m in SYNC_OUT_MODES else -1 for m in (s.sync_out1, s.sync_out2))

    # ---------------------------------------------------------------- выходы синхронизации, лазер
    def laser_modes(self) -> Tuple[int, int]:
        """(режим «лазер включён», режим «выключен») для выхода, к которому подключён лазер."""
        if self.settings.laser_active_high:
            return SYNC_LOGICAL_1, SYNC_LOGICAL_0
        return SYNC_LOGICAL_0, SYNC_LOGICAL_1

    def sync_out_setting(self, n: int) -> int:
        return int(getattr(self.settings, f"sync_out{n}"))

    def set_sync_out(self, n: int, mode: int) -> None:
        """Режим выхода синхронизации n (1 или 2) — сразу передаётся в камеру."""
        if n not in (1, 2):
            raise DeviceError(f"У камеры нет выхода синхронизации {n}")
        if mode not in SYNC_OUT_MODES:
            raise DeviceError(f"Недопустимый режим выхода синхронизации: {mode}")
        self._require_camera()
        with self.changing_light(f"выход синхронизации {n}"):
            setattr(self.settings, f"sync_out{n}", int(mode))
            log.info("Камера: выход синхронизации %d — %s", n, SYNC_OUT_MODES[mode])
            self.cam_thread.call(self.camera.configure, self.acq_settings(),
                                 timeout=self.settings.frame_timeout_s + 60)

    @property
    def laser_on(self) -> bool:
        n = self.settings.laser_output
        return n in (1, 2) and self.sync_out_setting(n) == self.laser_modes()[0]

    def set_laser(self, on: bool) -> None:
        n = self.settings.laser_output
        if n not in (1, 2):
            raise DeviceError("Не указано, к какому выходу синхронизации камеры подключён лазер "
                              "(«Настройка → Настройки…»)")
        if not on and self.cam_thread.current_label == "acquire":
            self.abort()          # иначе новый режим выхода дойдёт до камеры только после кадра
        self.set_sync_out(n, self.laser_modes()[0 if on else 1])
        log.info("Лазер %s", "ВКЛЮЧЁН" if on else "выключен")

    def expected_frame_time(self, acq: AcqSettings) -> float:
        r = acq.regions[0]
        speed = max(1, acq.readout_speed or 125) * 1000.0
        return acq.exposure_us / 1e6 + r.out_pixels / speed + 0.3 + (r.out_pixels * 2) / 5e6

    def _frame(self, acq: AcqSettings) -> np.ndarray:
        """Один кадр с контролем времени: при зависании — прерывание."""
        # выходы синхронизации — текущие, а не на момент начала съёмки: иначе лазер, включённый или
        # выключенный между кадрами накопления, следующий кадр вернул бы в прежнее состояние
        acq = dataclasses.replace(acq, sync_out=self._sync_modes())
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

    def _bg_key(self, acq: AcqSettings, flip: Optional[bool] = None, stripes=None) -> tuple:
        # полосы входят в ключ: фон снимается и вычитается для каждой полосы отдельно
        flip = self.settings.flip_x if flip is None else flip
        return acq.exposure_us, acq.readout_speed, acq.regions, bool(flip), tuple(stripes or self.stripes())

    def _intensity_correct(self, y: np.ndarray, x: np.ndarray, unit: str) -> Tuple[np.ndarray, str]:
        """y × K: коррекция спектральной чувствительности по таблице из настроек."""
        s = self.settings
        if len(s.intensity_corr_points) < 2:
            return y, ""
        if s.intensity_corr_by == "wl":
            if unit != "nm":
                if not self._corr_warned:
                    log.warning("Коррекция интенсивности по длинам волн не применена: шкала только в пикселях")
                    self._corr_warned = True
                return y, ""
            k = P.correction_curve(x, s.intensity_corr_points)
            how = f"по длинам волн, {len(s.intensity_corr_points)} точек"
        else:
            k = P.correction_curve(np.arange(y.shape[-1]), s.intensity_corr_points)
            how = f"по пикселям, {len(s.intensity_corr_points)} точек"
        self._corr_warned = False
        return y * k, how

    def has_background(self) -> bool:
        return self.camera_connected and not self.background_problem() and \
            self._bg_key(self.acq_settings()) in self._backgrounds

    def background_problem(self, key: Optional[tuple] = None) -> str:
        """Почему снятый фон сейчас не подходит ("" — подходит или фона нет)."""
        if not self.camera_connected:
            return ""
        t_bg = self._bg_temps.get(key if key is not None else self._bg_key(self.acq_settings()))
        st = self.last_state
        if t_bg is None or st is None or st.temperature is None:
            return ""
        if abs(float(st.temperature) - t_bg) > BG_TEMP_TOLERANCE_C:
            return f"фон снят при {t_bg:+.0f} °C, сейчас {st.temperature:+.0f} °C — переснимите"
        return ""

    def clear_background(self) -> None:
        with self._lock:
            self._bg_temps.clear()
            self._backgrounds.clear()
            self._bg_images.clear()

    @staticmethod
    def _frame_to_tracks(frame: np.ndarray, acq: AcqSettings, stripes, image_mode: bool) -> np.ndarray:
        """Кадр → спектры полос (T×N). Полосы и режим — зафиксированные в начале съёмки."""
        r = acq.regions[0]
        if not image_mode and r.bin_y > 1:
            return np.asarray(frame, dtype=np.float64)[:1]          # аппаратное объединение: одна строка
        offset = 0 if image_mode else r.top
        return np.vstack([P.bin_rows(frame, t - offset, b - offset) for t, b in stripes])

    def acquire(self, n: Optional[int] = None, progress: Optional[ProgressFn] = None,
                background: bool = False, label: str = "") -> Measurement:
        """Снять спектр (с накоплением n кадров). При background=True — снять и
        запомнить темновой фон (шторка закрывается автоматически, если включено)."""
        self._require_camera()
        s = self.settings
        n = max(1, int(n or s.accumulations))
        if time.monotonic() - self._state_time > 3.0:      # свежая температура перед съёмкой
            try:
                self._note_state(self.cam_thread.call(self.camera.get_state, timeout=15))
            except (DeviceError, FutureTimeout) as e:
                log.debug("Состояние камеры перед съёмкой: %s", e)
        why = self.temperature_changing()
        if why:
            raise DeviceError(f"Идёт изменение температуры камеры ({why}). Съёмка будет доступна, "
                              "когда температура установится.")
        self._abort.clear()
        # шторку для фона трогаем и состояние прибора читаем, только когда закончились команды монохроматора
        self._wait_light_steady(progress)
        # всё, что можно поменять в интерфейсе во время непрерывной съёмки (перетащить полосу,
        # сменить режим, развернуть спектр), фиксируется на всю съёмку: кадр, полосы и фон
        # должны соответствовать друг другу
        image_mode = s.acq_mode == ACQ_IMAGE
        flip = bool(s.flip_x)
        mode = "average" if background else s.accumulate_mode
        stripes = self.stripes()
        acq = self.acq_settings()
        t0 = time.monotonic()
        if not background and self.mono_connected and not self.shutter_ttl and self.shutter_state is False:
            # закрытая шторка сохраняется между сеансами (и в SpectraSP) — сигнал тогда только темновой
            if not self._shutter_warned:
                log.warning("Входная шторка монохроматора закрыта — на ПЗС попадает только темновой "
                            "сигнал. Откройте шторку на панели «Монохроматор».")
                self._shutter_warned = True
        elif self.shutter_state:
            self._shutter_warned = False
        shutter_restore = None
        ttl_restore = False
        close_shutter = background and s.close_shutter_for_background and self.mono_connected
        if background and self.mono_connected and self.shutter_ttl:
            # по TTL шторка открылась бы на время каждого кадра фона — на фон переводим её на ручное
            # управление (закрытой), потом возвращаем режим по экспозиции
            try:
                self.mono_thread.call(self.mono.set_shutter_ttl, False, 0, timeout=60)
            except (DeviceError, FutureTimeout) as e:
                raise DeviceError(f"Не удалось перевести шторку на ручное управление для фона: {e}") from e
            self.shutter_ttl = False
            ttl_restore = True
            close_shutter = True
        if close_shutter:
            try:
                prev = self.mono_thread.call(self.mono.get_shutter, 0, timeout=30)
            except (DeviceError, FutureTimeout) as e:
                raise DeviceError(f"Не удалось прочитать состояние шторки перед съёмкой фона: {e}") from e
            shutter_restore = prev if prev is not None else True
        spectra: List[np.ndarray] = []
        frame = None
        sat = False
        try:
            if close_shutter:
                try:
                    self._shutter_to(False)
                except (DeviceError, FutureTimeout) as e:
                    # фон при открытой шторке испортил бы все спектры после вычитания — не снимаем
                    raise DeviceError(f"Шторка не закрылась, фон не снят: {e}. Закройте вход вручную и "
                                      "снимите фон без шторки (Обработка → «закрывать шторку при съёмке "
                                      "фона»).") from e
            gen = None
            while len(spectra) < n:
                if self._abort.is_set():
                    raise DeviceError("Остановлено пользователем")
                g = self._wait_light_steady(progress)
                if g != gen:
                    if spectra:
                        log.info("Во время накопления менялись монохроматор, шторка или лазер — "
                                 "накопление начато заново")
                        spectra, sat = [], False
                    gen = g
                if progress:
                    progress(len(spectra), n, "фон" if background else "экспозиция")
                f = self._frame(acq)
                if self._light_changed_since(gen):
                    log.info("Во время кадра менялись монохроматор, шторка или лазер — кадр снимается заново")
                    continue
                frame = f[:, ::-1] if flip else f
                sat = sat or P.saturated(frame)
                spectra.append(self._frame_to_tracks(frame, acq, stripes, image_mode))
            # состояние монохроматора — то, при котором сняты кадры (а не после следующей команды)
            with self._lock:
                wl, gr, slit = self.mono_wl, self.mono_grating, self.mono_slit
        finally:
            if ttl_restore:
                try:
                    self.mono_thread.call(self.mono.set_shutter_ttl, True, 0, timeout=60)
                    self.shutter_ttl = True
                except Exception as e:  # noqa: BLE001
                    log.warning("Не удалось вернуть шторку в режим по экспозиции: %s", e)
            elif shutter_restore is not None:
                try:
                    self._shutter_to(shutter_restore)
                except Exception as e:  # noqa: BLE001
                    log.warning("Не удалось вернуть шторку: %s", e)
        if progress:
            progress(n, n, "обработка")
        raw = P.combine_frames(spectra, mode=mode, reject_cosmics=s.remove_cosmics)     # T×N
        if s.remove_cosmics and n < 3:
            raw = np.vstack([P.remove_spikes(t) for t in raw])
        n_fixed = 0
        if s.defect_correction and s.defect_pixels:
            raw = P.fix_defects(raw, s.defect_pixels, flip=flip)
            n_fixed = len(s.defect_pixels)
        key = self._bg_key(acq, flip, stripes)
        y = raw
        bg_used = False
        t_now = self.last_state.temperature if self.last_state else None
        if background:
            with self._lock:
                self._backgrounds[key] = raw.copy()
                if t_now is not None:
                    self._bg_temps[key] = float(t_now)
                if frame is not None and image_mode:
                    self._bg_images[key] = frame.astype(np.float64)
        elif s.subtract_background:
            with self._lock:
                bg = self._backgrounds.get(key)
            problem = self.background_problem(key)
            if bg is not None and problem:
                log.warning("Фон не вычтен: %s", problem)
                bg = None
            if bg is not None and bg.shape == raw.shape:
                y = raw - bg * (n if mode == "sum" else 1)
                bg_used = True
        x, unit = self.wavelength_axis(raw.shape[-1], (wl, gr))
        corrected = ""
        if s.intensity_corr and not background:
            y, corrected = self._intensity_correct(y, x, unit)
        m = Measurement(x=x, x_unit=unit, y=y[0], raw=raw[0],
                        tracks=y if y.shape[0] > 1 else None, track_rows=tuple(stripes[:y.shape[0]]),
                        defects_fixed=n_fixed, intensity_corrected=corrected, comment=s.comment.strip(),
                        image=frame if image_mode else None,
                        background_used=bg_used, saturated=sat,
                        exposure_ms=acq.exposure_us / 1000.0, accumulations=n, accumulate_mode=mode,
                        acq_mode=ACQ_IMAGE if image_mode else s.acq_mode, rows=stripes[0],
                        hardware_binning=acq.regions[0].bin_y > 1,
                        center_wl=wl, grating=gr, grating_title=self.grating_title(gr), slit_um=slit,
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
    def wavelength_axis(self, n_pixels: int, state: Optional[Tuple[Optional[float], Optional[int]]] = None
                        ) -> Tuple[np.ndarray, str]:
        """Шкала для (длина волны, решётка) — по умолчанию для текущего состояния монохроматора."""
        if state is None:
            with self._lock:
                state = (self.mono_wl, self.mono_grating)
        wl, gr = state
        # модель прибора считается в его потоке; пока монохроматор выполняет долгую команду, ждать её
        # нельзя — тогда запасная модель (WavelengthAxis не запоминает её вместо модели прибора)
        use_device = self.mono_connected and not self.light_changing()
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
        with self.changing_light(f"переход на {float(wl):g} нм"):
            self.mono_thread.call(self.mono.set_wavelength, float(wl), True, t, timeout=t + 30)
            self.refresh_mono()
        return self.mono_wl

    def set_grating(self, index: int) -> None:
        self._require_mono(control=True)
        self._check_grating(int(index))
        t = self.settings.mono_timeout_s
        with self.changing_light("смена решётки"):
            self.mono_thread.call(self.mono.set_grating, int(index), True, t, timeout=t + 30)
            self.refresh_mono()

    def set_slit(self, index: int, width_um: float) -> float:
        """Установить ширину щели и дождаться окончания движения (DevCtrl выполняет команду
        асинхронно: сразу после неё прибор сообщает ещё прежнюю ширину)."""
        self._require_mono(control=True)
        self._check_slit(int(index), float(width_um))
        t = self.settings.mono_timeout_s

        def work():
            self.mono.set_slit(int(index), float(width_um))
            self.mono.wait_idle(t)
            return self.mono.get_slit(int(index))
        with self.changing_light(f"щель {int(index) + 1}"):
            w = self.mono_thread.call(work, timeout=t + 30)
            if index == 0:
                with self._lock:
                    self.mono_slit = w
        if abs(w - float(width_um)) > max(1.0, 0.01 * float(width_um)):
            log.warning("Щель %d: запрошено %.1f мкм, прибор сообщает %.1f мкм", index + 1, width_um, w)
        return w

    def get_slit(self, index: int) -> float:
        self._require_mono()
        return self.mono_thread.call(self.mono.get_slit, int(index), timeout=60)

    def set_mirror(self, position: int, index: int = 0) -> None:
        """Переключить выходное зеркало и дождаться окончания (команда DevCtrl асинхронная)."""
        self._require_mono(control=True)
        self._check_mirror(int(position))
        t = self.settings.mono_timeout_s

        def work():
            self.mono.set_mirror(index, int(position))
            self.mono.wait_idle(t)
            return self.mono.get_mirror(index)
        with self.changing_light("выходное зеркало"):
            got = self.mono_thread.call(work, timeout=t + 30)
        if got != int(position):
            raise DeviceError(f"Зеркало не переключилось: прибор сообщает положение {got} вместо {position}")

    def get_mirror(self, index: int = 0) -> int:
        self._require_mono()
        return self.mono_thread.call(self.mono.get_mirror, index, timeout=60)

    def set_shutter(self, is_open: bool) -> None:
        self._require_mono(control=True)
        if self.shutter_ttl:
            raise DeviceError("Шторка работает по экспозиции (TTL) — чтобы открыть или закрыть её вручную, "
                              "переключите режим шторки на «вручную»")
        with self.changing_light("шторка"):
            self._shutter_to(is_open)

    def set_shutter_ttl(self, on: bool) -> None:
        """Шторка по экспозиции: открывается импульсом с выхода синхронизации камеры только на время
        кадра (как кнопка «TTL» в окне DevCtrl). False — ручное управление («Internal»)."""
        self._require_mono(control=True)
        if not (self.mono.info.has_shutter and self.mono.info.shutter_ttl):
            raise DeviceError("Шторка этого монохроматора не управляется по TTL (в MS*.cfg нет TTL=1)")
        on = bool(on)
        with self.changing_light("режим шторки"):
            self.mono_thread.call(self.mono.set_shutter_ttl, on, 0, timeout=60)
        self.shutter_ttl = on
        self.settings.shutter_ttl = on
        self.shutter_state = None
        if on:
            log.info("Шторка: по экспозиции (TTL) — открывается только на время кадра")
            if self.camera_connected and not self.exposure_sync_output():
                log.warning("Ни один выход синхронизации камеры не выдаёт сигнал экспозиции (режим EXP). "
                            "Шторка по TTL откроется, только если на её вход заведён такой выход — "
                            "в SpectraSP это выход 1 в режиме 0 «EXP».")
        else:
            log.info("Шторка: ручное управление")

    def exposure_sync_output(self) -> int:
        """Номер выхода синхронизации камеры, выдающего импульс экспозиции (0 — такого нет)."""
        for n in (1, 2):
            if self.sync_out_setting(n) in SYNC_EXPOSURE_MODES:
                return n
        return 0

    def _shutter_to(self, is_open: bool) -> bool:
        """Переключить шторку и дождаться, пока прибор подтвердит новое состояние.

        DevCtrl лишь ставит команду в очередь, и сразу после неё прибор сообщает ещё
        прежнее состояние (из-за этого кнопка шторки «отставала» на одно нажатие, а фон
        мог сниматься при открытой шторке). Возвращает True, если состояние подтверждено,
        False — если прибор состояние не сообщает (тогда выждано SHUTTER_CONFIRM_S).
        """
        is_open = bool(is_open)
        self.mono_thread.call(self.mono.set_shutter, is_open, 0, timeout=60)
        deadline = time.monotonic() + SHUTTER_CONFIRM_S
        while True:
            time.sleep(0.05)            # поток монохроматора тем временем обрабатывает сообщения DevCtrl
            state = self.mono_thread.call(self.mono.get_shutter, 0, timeout=30)
            self.shutter_state = state
            if state is not None and state == is_open:
                time.sleep(SHUTTER_SETTLE_S)
                log.info("Шторка %s", "открыта" if is_open else "закрыта")
                return True
            if time.monotonic() > deadline:
                break
        if state is None:
            log.warning("Монохроматор не сообщает состояние шторки — команда «%s» не подтверждена",
                        "открыть" if is_open else "закрыть")
            return False
        raise DeviceError(f"шторка не {'открылась' if is_open else 'закрылась'} за {SHUTTER_CONFIRM_S:.0f} с "
                          f"(прибор сообщает: {'открыта' if state else 'закрыта'})")

    def get_shutter(self) -> Optional[bool]:
        self._require_mono()
        self.shutter_state = self.mono_thread.call(self.mono.get_shutter, 0, timeout=60)
        return self.shutter_state

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
        with self.changing_light(f"{MECH_TITLES.get(mech, mech)}: перемещение"):
            self.mono_thread.call(self.mono.set_steps, mech, int(index), int(position), t, timeout=t + 30)
            self._refresh_after_service()

    def service_move_steps(self, mech: str, index: int, delta: int) -> None:
        self._require_mono(control=True)
        t = self.settings.mono_timeout_s

        def work():          # положение читается и проверяется в потоке прибора непосредственно перед движением
            self._check_steps(mech, int(index), self.mono.get_steps(mech, int(index)) + int(delta))
            self.mono.move_steps(mech, int(index), int(delta), t)
        with self.changing_light(f"{MECH_TITLES.get(mech, mech)}: сдвиг"):
            self.mono_thread.call(work, timeout=t + 30)
            self._refresh_after_service()

    def service_reset(self, mech: str, index: int) -> None:
        self._require_mono(control=True)
        t = self.settings.mono_timeout_s
        with self.changing_light(f"{MECH_TITLES.get(mech, mech)}: сброс"):
            self.mono_thread.call(self.mono.reset, mech, int(index), t, timeout=t + 30)
            self._refresh_after_service()

    def _refresh_after_service(self) -> None:
        """После сервисной команды длина волны и щель могли измениться — перечитать до следующего кадра."""
        try:
            self.refresh_mono()
        except DeviceError as e:
            log.warning("Состояние монохроматора после сервисной команды не прочитано: %s", e)

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
    def panorama_plan(self, start_nm: float, end_nm: float, overlap: float = 0.15, trim: int = 0) -> List[float]:
        """Центральные длины волн для панорамы с заданным перекрытием (trim — отбрасываемые
        точки с каждого края кадра: рабочая ширина кадра меньше)."""
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
            width = abs(x[-1] - x[0]) * max(0.1, 1 - 2 * trim / max(1, n_pix))
            if not np.isfinite(width) or width <= 1e-6:
                raise DeviceError("Шкала длин волн вырождена (кадр нулевой ширины) — проверьте калибровку")
            if not centers:
                c = lo + width / 2
                x, _ = self.axis.axis(n_pix, c, self.mono_grating, None)
            centers.append(round(float(c), 3))
            if c + width / 2 >= hi:
                break
            c += width * (1 - overlap)
        return centers
