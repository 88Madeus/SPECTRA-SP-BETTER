"""Драйвер ПЗС-камеры PSI (ProScan) через штатные DLL SpectraSP.

Последовательность вызовов повторяет SpectraSP (PSI_CCD_Ver3_*), но:
* параметры отправляются в камеру только при их изменении (SpectraSP перед
  каждым кадром делает ~10 сетевых запросов, это ~1,1 с лишнего времени);
* поддерживается аппаратное объединение строк (спектральный режим): вместо
  чтения всего кадра 2048×506 (≈4 с при 250 кГц) читается одна строка;
* ошибки драйвера превращаются в понятные исключения DeviceError.

Все методы, кроме abort(), должны вызываться из одного потока
(см. hw/worker.py) — того же, в котором была загружена DLL.
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from typing import List, Optional

import numpy as np

from . import psi_structs as S
from .base import (AcqSettings, Camera, CameraInfo, CameraState, DeviceError,
                   Region, exposure_to_unit_value, unit_value_to_us)
from .winutil import DllDirectory, free_dll, load_dll, ping, stdcall_function

log = logging.getLogger(__name__)

# SpectraSP всегда записывает это значение в lFactReadoutSpeed перед SetParameters
# (камера поле игнорирует и возвращает фактическую скорость). Повторяем как есть.
_SPECTRASP_FACT_READOUT = 0xA6


class PsiCamera(Camera):
    def __init__(self, spectrasp_dir: str, ip: str, dll_name: str = "auto",
                 device_index: int = 0):
        self.dir = spectrasp_dir
        self.ip = ip
        self.dll_name = dll_name
        self.device_index = device_index
        self.info = CameraInfo()
        self._dll = None
        self._core = None
        self._f = {}
        self._h = -1
        self._params = S.TPSICameraParamsV2()
        self._scan = S.TPSICameraScanParamsV2()
        self._applied: Optional[AcqSettings] = None
        self._bits = 16

    # ------------------------------------------------------------ служебное
    def _bind(self, dll) -> dict:
        funcs = {}
        for name, (restype, argtypes, nbytes) in S.PROTOTYPES.items():
            try:
                funcs[name] = stdcall_function(dll, name, restype, argtypes, nbytes)
            except AttributeError:
                log.warning("В %s нет функции %s", self.info.dll_name, name)
        missing = [n for n in ("psiccd2_Init", "psiccd2_OpenDevice", "psiccd2_StartWaitMeasure",
                               "psiccd2_GetData", "psiccd2_SetParameters") if n not in funcs]
        if missing:
            raise DeviceError(f"Библиотека камеры не содержит функций: {', '.join(missing)}")
        return funcs

    def _last_error(self) -> str:
        fn = self._f.get("psiccd2_GetLastError")
        if not fn:
            return ""
        try:
            raw = fn()
            return raw.decode("cp1251", "replace").strip() if raw else ""
        except Exception:  # noqa: BLE001
            return ""

    def _check(self, rc: int, what: str) -> None:
        """Большинство функций драйвера возвращают 0 при успехе."""
        if rc != 0:
            err = self._last_error()
            raise DeviceError(f"Камера: ошибка {what} (код {rc}){': ' + err if err else ''}")

    @property
    def is_open(self) -> bool:
        return self._h >= 0

    def _require_open(self) -> None:
        if self._h < 0:
            raise DeviceError("Камера не подключена")

    # ------------------------------------------------------------ открытие
    def open(self) -> CameraInfo:
        if self._h >= 0:
            return self.info
        names = list(S.DLL_CANDIDATES) if self.dll_name in ("", "auto") else [self.dll_name]
        if ping(self.ip, 3000) is False:
            raise DeviceError(f"Камера не отвечает на ping по адресу {self.ip}. Проверьте питание камеры, "
                              "сетевой кабель и настройки сетевого адаптера (как для SpectraSP).")
        errors: List[str] = []
        with DllDirectory(self.dir):
            core_path = os.path.join(self.dir, S.CORE_DLL)
            if self._core is None and os.path.isfile(core_path):
                # Предзагрузка ядра по полному пути: оболочки грузят его по короткому имени.
                self._core = load_dll(core_path)
            for name in names:
                path = os.path.join(self.dir, name)
                if not os.path.isfile(path):
                    continue
                try:
                    self._open_with(path)
                    return self.info
                except DeviceError as e:
                    errors.append(f"{name}: {e}")
                    log.info("Камера через %s не открылась: %s", name, e)
                    self._unload()
        if not errors:
            raise DeviceError(f"В каталоге {self.dir} не найдены библиотеки камеры psi_ccd2_ver*.dll")
        raise DeviceError("Не удалось подключиться к камере " + self.ip + ":\n" + "\n".join(errors))

    def _open_with(self, path: str) -> None:
        self.info = CameraInfo(dll_name=os.path.basename(path))
        self._dll = load_dll(path)
        self._f = self._bind(self._dll)
        f = self._f
        log.info("Камера: %s, инициализация по адресу %s", self.info.dll_name, self.ip)
        if not f["psiccd2_Init"](self.ip.encode("ascii")):
            raise DeviceError(f"инициализация не удалась: {self._last_error() or 'нет ответа'}")
        count = ctypes.c_int32(0)
        self._check(f["psiccd2_GetDevCount"](ctypes.byref(count)), "подсчёта устройств")
        if count.value <= self.device_index:
            raise DeviceError(f"камера не найдена (устройств: {count.value})")
        ctrl = S.TPSIControllerInfoV2()
        cam = S.TPSICameraInfoV2()
        h = f["psiccd2_OpenDevice"](self.device_index, ctypes.byref(ctrl), ctypes.byref(cam))
        if h < 0:
            raise DeviceError(f"не удалось открыть камеру: {self._last_error() or h}")
        self._h = h
        if "psiccd2_SetLoggingMode" in f:
            f["psiccd2_SetLoggingMode"](h, False)     # как в SpectraSP: журнал драйвера выключен
        self.info = self._make_info(ctrl, cam)
        self._read_params()
        self._applied = None
        log.info("Камера открыта: %s", self.info)

    def _make_info(self, ctrl: S.TPSIControllerInfoV2, cam: S.TPSICameraInfoV2) -> CameraInfo:
        speeds = [int(s) for s in cam.ReadoutSpeeds[:max(0, min(8, cam.wReadoutSpeedCount))] if s > 0]
        name = ctrl.szDeviceName.split(b"\0", 1)[0].decode("cp1251", "replace").strip()
        serial = ctrl.szSerial.split(b"\0", 1)[0].decode("cp1251", "replace").strip()
        return CameraInfo(
            model=name or "PSI CCD", serial=serial,
            width=cam.wWidth, height=cam.wHeight,
            pixel_width_um=cam.dPxlWidth, pixel_height_um=cam.dPxlHeight,
            readout_speeds=speeds, temp_min=cam.lTempMin, temp_max=cam.lTempMax,
            cooling_type=cam.wCoolingType,
            max_bin_x=max(1, cam.wMaxHBinning), max_bin_y=max(1, cam.wMaxVBinning),
            min_exposure=cam.dwMinExposure, max_exposure=cam.dwMaxExposure,
            firmware=f"ПО {ctrl.wSoftMajorVer}.{ctrl.wSoftMinorVer}, FPGA {ctrl.wFPGAMajorVer}.{ctrl.wFPGAMinorVer}",
            dll_name=self.info.dll_name)

    def _read_params(self) -> None:
        p = S.TPSICameraParamsV2()
        sp = S.TPSICameraScanParamsV2()
        self._check(self._f["psiccd2_GetParameters"](self._h, ctypes.byref(p), ctypes.byref(sp)),
                    "чтения параметров")
        self._params, self._scan = p, sp

    @property
    def current_exposure_us(self) -> float:
        return unit_value_to_us(self._params.wExpUnit, self._params.wExpValue)

    @property
    def current_readout_speed(self) -> int:
        return int(self._params.lReadoutSpeed)

    def read_regions(self) -> List[Region]:
        self._require_open()
        count = ctypes.c_int32(0)
        regs = (S.TPSICCDRegionV2 * S.MAX_REGIONS)()
        self._check(self._f["psiccd2_GetRegions"](self._h, ctypes.byref(count), regs), "чтения областей")
        return [Region(r.wLeft, r.wTop, r.wRight, r.wBottom, max(1, r.wSUPixelWidth), max(1, r.wSUPixelHeight))
                for r in regs[:max(0, min(count.value, S.MAX_REGIONS))]]

    # ------------------------------------------------------------ закрытие
    def close(self, warm_up: bool = True) -> None:
        if self._h >= 0:
            try:
                if warm_up:
                    self._warm_up_before_close()
            except Exception as e:  # noqa: BLE001
                log.warning("Отогрев перед отключением не выполнен: %s", e)
            try:
                self._f["psiccd2_CloseDevice"](self._h)
            except Exception:  # noqa: BLE001
                log.warning("CloseDevice завершился с ошибкой", exc_info=True)
            self._h = -1
        self._unload()

    def _warm_up_before_close(self, timeout_s: float = 60.0) -> None:
        """Как SpectraSP: если ПЗС охлаждена ниже 0 °C, поднять уставку до +2 °C
        и дождаться стабилизации (защита от конденсата)."""
        if not self.info.cooling_type:
            return
        st = self.get_state()
        if st.temperature is None or st.temperature > 0:
            return
        log.info("Отогрев ПЗС до +2 °C перед отключением...")
        self.set_cooling(True, 2)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            time.sleep(1.0)
            if self.get_state().cooling_state == 2:
                break

    def _unload(self) -> None:
        free_dll(self._dll)
        self._dll = None
        self._f = {}
        self._h = -1

    # ------------------------------------------------------------ состояние
    def get_state(self) -> CameraState:
        self._require_open()
        st = S.TPSICameraStateV2()
        motors = (S.TPSIStepMotor * S.MOTOR_COUNT)()
        self._check(self._f["psiccd2_GetCameraState"](self._h, ctypes.byref(st), motors), "чтения состояния")
        return CameraState(temperature=float(st.lSensor1Temp),
                           temperature2=float(st.lSensor2Temp),
                           cooling_state=int(st.lCoolingState),
                           fan_on=bool(st.wFan1State), overheat=bool(st.wOverheat),
                           error_code=int(st.lErrorCode), fpga_status=int(st.lFPGAStatus))

    def set_cooling(self, enabled: bool, target_c: int) -> None:
        self._require_open()
        if enabled:
            t = int(max(self.info.temp_min, min(self.info.temp_max, target_c)))
            self._check(self._f["psiccd2_SetCoolingOn"](self._h, t), "включения охлаждения")
        else:
            self._check(self._f["psiccd2_SetCoolingOff"](self._h), "выключения охлаждения")

    # ------------------------------------------------------------ экспозиция
    def configure(self, settings: AcqSettings) -> None:
        self._require_open()
        if settings == self._applied:
            return
        if not settings.regions:
            raise DeviceError("Не задана область считывания")
        for r in settings.regions:
            r.validate(self.info.width, self.info.height)
        f = self._f
        p = S.TPSICameraParamsV2.from_buffer_copy(self._params)
        unit, value = exposure_to_unit_value(settings.exposure_us)
        p.wExpUnit, p.wExpValue = unit, value
        if settings.readout_speed:
            p.lReadoutSpeed = int(settings.readout_speed)
        p.lFactReadoutSpeed = _SPECTRASP_FACT_READOUT
        p.lRegNum = len(settings.regions)
        p.wSynchMode = 0                      # внутренний запуск
        regs = (S.TPSICCDRegionV2 * len(settings.regions))()
        for i, r in enumerate(settings.regions):
            regs[i].wLeft, regs[i].wTop, regs[i].wRight, regs[i].wBottom = r.left, r.top, r.right, r.bottom
            regs[i].wRegionNumber = i
            regs[i].wSUPixelWidth, regs[i].wSUPixelHeight = r.bin_x, r.bin_y
        frames = ctypes.c_int32(1)
        mode = ctypes.c_int32(0)
        # Порядок как в SpectraSP SetDevParam: параметры → режим → области → параметры.
        self._check(f["psiccd2_SetParameters"](self._h, p), "записи параметров")
        self._check(f["psiccd2_SetAcqMode"](self._h, ctypes.byref(frames), ctypes.byref(mode)), "установки режима")
        self._check(f["psiccd2_SetRegions"](self._h, len(settings.regions), regs), "установки областей")
        self._check(f["psiccd2_SetParameters"](self._h, p), "записи параметров")
        self._read_params()
        self._applied = settings
        log.info("Камера: экспозиция %.0f мкс (%d×%d), %d кГц, область %s",
                 self.current_exposure_us, value, unit, self._params.lReadoutSpeed, settings.regions[0])

    def invalidate(self) -> None:
        """Забыть применённые параметры: при следующем configure() они уйдут заново."""
        self._applied = None

    def acquire(self) -> np.ndarray:
        self._require_open()
        if self._applied is None:
            raise DeviceError("Параметры экспозиции не заданы")
        rows, cols = self._applied.out_shape
        n_pixels = rows * cols
        frames = ctypes.c_int32(1)
        frame_size = ctypes.c_int32(n_pixels)
        self._check(self._f["psiccd2_StartWaitMeasure"](self._h, ctypes.byref(frames), ctypes.byref(frame_size)),
                    "экспозиции")
        # Драйвер сообщает размер кадра в байтах (16 бит на пиксель) и копирует
        # в буфер ровно столько. Буфер берём с запасом, чтобы при расхождении
        # областей не испортить память, а само расхождение считаем ошибкой.
        expected_bytes = n_pixels * 2
        reported = frame_size.value
        if reported <= 0 or reported > 64 * 1024 * 1024:
            raise DeviceError(f"Камера сообщила неверный размер кадра: {reported}")
        buf = np.zeros(max(n_pixels, (reported + 1) // 2), dtype=np.uint16)
        ptr = ctypes.c_void_p(buf.ctypes.data)
        self._check(self._f["psiccd2_GetData"](self._h, 0, 0, self._params.wBits, ctypes.byref(ptr)),
                    "чтения данных")
        if reported != expected_bytes:
            self._applied = None
            raise DeviceError(f"Размер кадра {reported} байт не совпадает с ожидаемым {expected_bytes}; "
                              "параметры будут переданы в камеру заново")
        return buf[:n_pixels].reshape(rows, cols).copy()

    def abort(self) -> None:
        fn = self._f.get("psiccd2_AbortScan")
        if fn and self._h >= 0:
            rc = fn(self._h)
            log.info("Камера: прерывание экспозиции, код %s", rc)
