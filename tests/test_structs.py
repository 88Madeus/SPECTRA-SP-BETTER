"""Проверка структур и прототипов, восстановленных из SpectraSP.exe и DLL.

Смещения полей сверены с расширенной RTTI Delphi в SpectraSP.exe,
размеры аргументов — с инструкцией `ret N` в DLL.
"""
import ctypes

from spectra_better.hw import psi_structs as S
from spectra_better.hw import sspl_mono
from spectra_better.hw.winutil import stack_bytes


def test_struct_sizes():
    for cls, size in S.EXPECTED_SIZES.items():
        assert ctypes.sizeof(cls) == size, cls.__name__


def test_field_offsets_from_rtti():
    assert S.TPSICCDRegionV2.wSUPixelHeight.offset == 10
    assert S.TPSICCDRegionV2.wSUPixelWidth.offset == 12
    assert S.TPSIControllerInfoV2.szDeviceName.offset == 24
    assert S.TPSIControllerInfoV2.szSerial.offset == 56
    assert S.TPSICameraInfoV2.ReadoutSpeeds.offset == 16
    assert S.TPSICameraInfoV2.dPxlWidth.offset == 48
    assert S.TPSICameraInfoV2.wMaxHBinning.offset == 64
    assert S.TPSICameraInfoV2.dwImagesToStore.offset == 92
    assert S.TPSICameraInfoV2.wSensorType.offset == 128
    assert S.TPSICameraParamsV2.wBits.offset == 12
    assert S.TPSICameraParamsV2.wExpUnit.offset == 20
    assert S.TPSICameraParamsV2.wExpValue.offset == 22
    assert S.TPSICameraParamsV2.wCCDStandardMode.offset == 36
    assert S.TPSICameraParamsV2.wDSreset_Unit.offset == 80
    assert S.TPSICameraParamsV2.wGammaCorrection.offset == 86
    assert S.TPSICameraStateV2.lSensor1Temp.offset == 12
    assert S.TPSICameraStateV2.wOverheat.offset == 30


def test_psi_prototypes_stack_size():
    for name, (_res, args, nbytes) in S.PROTOTYPES.items():
        assert stack_bytes(args) == nbytes, name


def test_sspl_prototypes_stack_size():
    for name, (_res, args, nbytes) in sspl_mono.PROTOTYPES.items():
        assert stack_bytes(args) == nbytes, name


def test_params_by_value_is_92_bytes_with_handle():
    # SetParameters(h, TPSICameraParamsV2) снимает со стека 0x5C байт
    assert stack_bytes([ctypes.c_int32, S.TPSICameraParamsV2]) == 0x5C
