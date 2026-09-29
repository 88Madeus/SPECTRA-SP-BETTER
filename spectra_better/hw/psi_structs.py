"""Структуры и прототипы функций драйвера камер PSI (ProScan Special Instruments).

Источник — расширенная RTTI (Delphi) в SpectraSP.exe: имена полей, типы и
смещения взяты оттуда без догадок. Все записи упакованы (packed record).
Прототипы функций восстановлены по местам вызова в SpectraSP.exe, размеры
аргументов сверены с инструкцией `ret N` в psi_ccd2_ver0/1/3.dll.
Подробности — docs/API_PSI_CCD.md.
"""
from __future__ import annotations

import ctypes
from ctypes import (POINTER, c_bool, c_char, c_char_p, c_double, c_int32,
                    c_uint16, c_uint32, c_void_p)


class _Packed(ctypes.Structure):
    _pack_ = 1

    def as_dict(self) -> dict:
        out = {}
        for name, _type in self._fields_:
            value = getattr(self, name)
            if isinstance(value, bytes):
                value = value.split(b"\0", 1)[0].decode("cp1251", "replace")
            elif hasattr(value, "__len__") and not isinstance(value, str):
                value = list(value)
            out[name] = value
        return out


class TPSICCDRegionV2(_Packed):          # 14 байт
    _fields_ = [
        ("wLeft", c_uint16),
        ("wTop", c_uint16),
        ("wRight", c_uint16),
        ("wBottom", c_uint16),
        ("wRegionNumber", c_uint16),
        ("wSUPixelHeight", c_uint16),     # биннинг по вертикали (порядок как в драйвере!)
        ("wSUPixelWidth", c_uint16),      # биннинг по горизонтали
    ]


class TPSIControllerInfoV2(_Packed):     # 88 байт
    _fields_ = [
        ("lOSMemory", c_int32),
        ("dwOSMajorVer", c_uint32),
        ("dwOSMinorVer", c_uint32),
        ("dwOSPlatform", c_uint32),
        ("wSoftMajorVer", c_uint16),
        ("wSoftMinorVer", c_uint16),
        ("wFPGAMajorVer", c_uint16),
        ("wFPGAMinorVer", c_uint16),
        ("szDeviceName", c_char * 32),
        ("szSerial", c_char * 32),
    ]


class TPSICameraInfoV2(_Packed):         # 130 байт
    _fields_ = [
        ("wWidth", c_uint16),
        ("wHeight", c_uint16),
        ("wReadoutSpeedCount", c_uint16),
        ("wCoolingType", c_uint16),
        ("lTempMin", c_int32),
        ("lTempMax", c_int32),
        ("ReadoutSpeeds", c_int32 * 8),   # кГц
        ("dPxlWidth", c_double),          # мкм
        ("dPxlHeight", c_double),
        ("wMaxHBinning", c_uint16),
        ("wMaxVBinning", c_uint16),
        ("wRegionsMode", c_uint16),
        ("wRegionsFlags", c_uint16),
        ("wMinRegionWidth", c_uint16),
        ("wMaxRegionWidth", c_uint16),
        ("wMinRegionHeight", c_uint16),
        ("wMaxRegionHeight", c_uint16),
        ("wFanCount", c_uint16),
        ("wSynchOutCount", c_uint16),
        ("wSynchModesCount", c_uint16),
        ("wSynchOut1ModesCount", c_uint16),
        ("wSynchOut2ModesCount", c_uint16),
        ("wCameraRAMSize", c_uint16),
        ("dwImagesToStore", c_uint32),
        ("dwMinExposure", c_uint32),
        ("dwMaxExposure", c_uint32),
        ("dwMinPrevDelay", c_uint32),
        ("dwMaxPrevDelay", c_uint32),
        ("dwMinPostDelay", c_uint32),
        ("dwMaxPostDelay", c_uint32),
        ("dwMinBetweenDelay", c_uint32),
        ("dwMaxBetweenDelay", c_uint32),
        ("wSensorType", c_uint16),
    ]


class TPSICameraParamsV2(_Packed):       # 88 байт, передаётся в SetParameters по значению
    _fields_ = [
        ("lRegNum", c_int32),
        ("lReadoutSpeed", c_int32),
        ("lFactReadoutSpeed", c_int32),
        ("wBits", c_uint16),
        ("wSynchMode", c_uint16),
        ("wSynchOut1Type", c_uint16),
        ("wSynchOut2Type", c_uint16),
        ("wExpUnit", c_uint16),           # 1 мкс, 2 мс, 3 с, 4 мин
        ("wExpValue", c_uint16),
        ("wPrevUnit", c_uint16),
        ("wPrevValue", c_uint16),
        ("wPostUnit", c_uint16),
        ("wPostValue", c_uint16),
        ("wBetweenUnit", c_uint16),
        ("wBetweenValue", c_uint16),
        ("wCCDStandardMode", c_uint16),
        ("wCCDAdvancedMode", c_uint16),
        ("wXReadMode", c_uint16),
        ("wYReadMode", c_uint16),
        ("wPrechargeBus", c_uint16),
        ("wXOptions", c_uint16),
    ] + [(f"wNDR_Frame{i}_{k}", c_uint16) for i in range(8) for k in ("Unit", "Value")] + [
        ("wDSreset_Unit", c_uint16),
        ("wDSreset_Value", c_uint16),
        ("wDAC", c_uint16),
        ("wGammaCorrection", c_uint16),
    ]


class TPSICameraStateV2(_Packed):        # 32 байта
    _fields_ = [
        ("lFPGAStatus", c_int32),
        ("lErrorCode", c_int32),
        ("lCoolingState", c_int32),       # 2 — температура установилась
        ("lSensor1Temp", c_int32),        # °C
        ("lSensor2Temp", c_int32),
        ("lSensor3Temp", c_int32),
        ("wFan1State", c_uint16),
        ("wFan2State", c_uint16),
        ("wFan3State", c_uint16),
        ("wOverheat", c_uint16),
    ]


class TPSIStepMotor(_Packed):            # 12 байт, драйвер заполняет 8 штук
    _fields_ = [
        ("lSensor", c_int32),
        ("wCurrPosition", c_uint16),
        ("wCurrSpeed", c_uint16),
        ("wMaxPosition", c_uint16),
        ("wLastDirection", c_uint16),
    ]


class TPSICameraScanParamsV2(_Packed):   # 8 байт
    _fields_ = [
        ("lScanMode", c_int32),
        ("lFramesToScan", c_int32),
    ]


EXPECTED_SIZES = {
    TPSICCDRegionV2: 14,
    TPSIControllerInfoV2: 88,
    TPSICameraInfoV2: 130,
    TPSICameraParamsV2: 88,
    TPSICameraStateV2: 32,
    TPSIStepMotor: 12,
    TPSICameraScanParamsV2: 8,
}

MOTOR_COUNT = 8
MAX_REGIONS = 16

# имя: (тип результата, типы аргументов, байт аргументов по `ret N` в DLL)
PROTOTYPES = {
    "psiccd2_Init": (c_bool, [c_char_p], 4),
    "psiccd2_GetDevCount": (c_int32, [POINTER(c_int32)], 4),
    "psiccd2_OpenDevice": (c_int32, [c_int32, POINTER(TPSIControllerInfoV2), POINTER(TPSICameraInfoV2)], 12),
    "psiccd2_CloseDevice": (c_bool, [c_int32], 4),
    "psiccd2_GetLastError": (c_char_p, [], 0),
    "psiccd2_GetCameraState": (c_int32, [c_int32, POINTER(TPSICameraStateV2), POINTER(TPSIStepMotor)], 12),
    "psiccd2_GetParameters": (c_int32, [c_int32, POINTER(TPSICameraParamsV2), POINTER(TPSICameraScanParamsV2)], 12),
    "psiccd2_SetParameters": (c_int32, [c_int32, TPSICameraParamsV2], 92),
    "psiccd2_GetRegions": (c_int32, [c_int32, POINTER(c_int32), POINTER(TPSICCDRegionV2)], 12),
    "psiccd2_SetRegions": (c_int32, [c_int32, c_int32, POINTER(TPSICCDRegionV2)], 12),
    "psiccd2_SetAcqMode": (c_int32, [c_int32, POINTER(c_int32), POINTER(c_int32)], 12),
    "psiccd2_StartWaitMeasure": (c_int32, [c_int32, POINTER(c_int32), POINTER(c_int32)], 12),
    "psiccd2_GetData": (c_int32, [c_int32, c_int32, c_int32, c_int32, POINTER(c_void_p)], 20),
    "psiccd2_AbortScan": (c_int32, [c_int32], 4),
    "psiccd2_SetCoolingOn": (c_int32, [c_int32, c_int32], 8),
    "psiccd2_SetCoolingOff": (c_int32, [c_int32], 4),
    "psiccd2_SetLoggingMode": (c_int32, [c_int32, c_bool], 8),
    "psiccd2_GetGain": (c_int32, [c_int32, POINTER(c_double)], 8),
    "psiccd2_GetOffset": (c_int32, [c_int32, POINTER(c_double)], 8),
}

# Порядок перебора библиотек как в SpectraSP (psi_ccd2_ver0, ver1, ...).
# ver2 пропущена: у неё другая сигнатура GetCameraState (5 аргументов).
DLL_CANDIDATES = ("psi_ccd2_ver0.dll", "psi_ccd2_ver1.dll", "psi_ccd2_ver3.dll")
CORE_DLL = "psi_ccd5.dll"
