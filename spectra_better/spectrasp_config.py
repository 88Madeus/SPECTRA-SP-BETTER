"""Чтение конфигурации установленной SpectraSP (только чтение!).

Отсюда берутся адреса устройств (IP камеры, параметры связи монохроматора),
параметры решёток и калибровки. Файлы SpectraSP программа не изменяет —
отдельная утилита tools/tune_spectrasp.py правит лишь список опрашиваемых
камер и делает резервные копии.
"""
from __future__ import annotations

import configparser
import glob
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_DIRS = (
    r"C:\ProgramData\SpectraSP for MS7504i N020126",
)


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin1")


def _xml_root(path: str) -> Optional[ET.Element]:
    try:
        return ET.fromstring(_read_text(path).lstrip("\ufeff"))
    except (OSError, ET.ParseError) as e:
        log.warning("Не удалось прочитать %s: %s", path, e)
        return None


def _num(text: Optional[str], default: float = 0.0) -> float:
    if text is None:
        return default
    try:
        return float(str(text).strip().replace(",", "."))
    except ValueError:
        return default


def looks_like_spectrasp_dir(path: str) -> bool:
    return os.path.isdir(path) and (os.path.isfile(os.path.join(path, "SSPL_IM.dll"))
                                    or os.path.isfile(os.path.join(path, "SpectraSP.exe")))


def find_spectrasp_dir(preferred: Optional[str] = None) -> Optional[str]:
    candidates: List[str] = []
    if preferred:
        candidates.append(preferred)
    candidates += list(DEFAULT_DIRS)
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    candidates += sorted(glob.glob(os.path.join(program_data, "SpectraSP*")))
    for p in candidates:
        if looks_like_spectrasp_dir(p):
            return os.path.abspath(p)
    return None


# ------------------------------------------------------------ SpectraSP.xml

@dataclass
class SpectraSPSettings:
    ctrl_dll: str = "SSPL_IM.dll"
    used_sensors: Dict[str, bool] = field(default_factory=dict)
    wl_presets: List[float] = field(default_factory=list)
    panorama: Tuple[float, float] = (0.0, 0.0)
    last_dir: str = ""
    mirror_names: List[str] = field(default_factory=list)
    slit_names: Dict[int, Tuple[str, bool]] = field(default_factory=dict)


def read_spectrasp_xml(directory: str) -> SpectraSPSettings:
    s = SpectraSPSettings()
    root = _xml_root(os.path.join(directory, "SpectraSP.xml"))
    if root is None:
        return s
    cfg_idx = root.findtext("General/CurrentConfig", "0").strip() or "0"
    cfg = root.find(f"Config_{cfg_idx}")
    if cfg is None:
        cfg = root.find("Config_0")
    if cfg is None:
        return s
    sp = cfg.find("SysParams")
    if sp is None:
        return s
    s.ctrl_dll = (sp.findtext("General/CtrlDllName") or s.ctrl_dll).strip()
    s.last_dir = (sp.findtext("General/LastSpcDir") or "").strip()
    for el in sp.findall("UsedSensors/*"):
        s.used_sensors[el.tag] = el.get("Used", "false").lower() == "true"
    wl = sp.find("WLList")
    if wl is not None:
        s.wl_presets = [v for v in (_num(e.text, float("nan")) for e in wl) if v == v]
    s.panorama = (_num(sp.findtext("Panoraming/BegWL")), _num(sp.findtext("Panoraming/EndWL")))
    m1 = sp.find("DeviceControl/Mirror1")
    if m1 is not None:
        s.mirror_names = [(m1.findtext(f"StateName_{i}") or "").strip() for i in range(2)]
    for i in range(1, 5):
        el = sp.find(f"DeviceControl/Slit{i}")
        if el is not None:
            s.slit_names[i - 1] = ((el.findtext("Name") or f"Щель {i}").strip(),
                                   (el.findtext("Enable") or "").strip().lower() == "true")
    return s


# ------------------------------------------------------------ Sensor_*.xml

@dataclass
class SensorSettings:
    path: str = ""
    sensor_type: str = ""
    sensor_id: str = ""
    ip: str = ""
    mac: str = ""
    exposure_us: float = 1e6
    readout_speed: int = 0
    calib_mode: str = "Static"
    calib_center_pixel: float = 1024.0
    calib_pixel_width: float = 0.0
    calib_center_wl: float = 0.0
    region: Tuple[int, int, int, int] = (0, 0, 0, 0)
    region_bin: Tuple[int, int] = (1, 1)
    flip: str = "NONE"


def read_sensor_xml(path: str) -> Optional[SensorSettings]:
    root = _xml_root(path)
    if root is None:
        return None
    s = root.find("Sensor")
    if s is None:
        return None
    ch = s.find("SensorChannel_0")
    reg = s.find("Regions/Region_0")
    out = SensorSettings(
        path=path,
        sensor_type=(s.findtext("SensorType") or "").strip(),
        sensor_id=(s.findtext("SensorID") or "").strip(),
        ip=(s.findtext("IP_Address") or "").strip(),
        mac=(s.findtext("MAC_Address") or "").strip(),
        flip=(s.findtext("Flip") or "NONE").strip(),
    )
    if ch is not None:
        out.exposure_us = _num(ch.findtext("ExpTime"), out.exposure_us)
        out.readout_speed = int(_num(ch.findtext("SensorReadOutSpeed"), 0))
    cal = s.find("Calibration")
    if cal is not None:
        out.calib_mode = (cal.findtext("ClbrMode") or "Static").strip()
        out.calib_center_pixel = _num(cal.findtext("CentrPxl"), 1024)
        out.calib_pixel_width = _num(cal.findtext("PxlWidth"), 0)
        out.calib_center_wl = _num(cal.findtext("CentrWL"), 0)
    if reg is not None:
        out.region = tuple(int(_num(reg.findtext(k))) for k in
                           ("RegionsLeft", "RegionsTop", "RegionsRight", "RegionsBottom"))
        out.region_bin = (int(_num(reg.findtext("RegionsBinningWidth"), 1)) or 1,
                          int(_num(reg.findtext("RegionsBinningHeight"), 1)) or 1)
    return out


def find_psi_sensor(directory: str) -> Optional[SensorSettings]:
    """Файл настроек камеры PSI (SensorType PSI_VER3) из каталога SpectraSP."""
    found = []
    for path in sorted(glob.glob(os.path.join(directory, "Sensor_*.xml"))):
        s = read_sensor_xml(path)
        if s and s.sensor_type.upper().startswith("PSI") and s.ip:
            found.append(s)
    # приоритет PSI_VER3 (используемая камера), затем остальные PSI
    found.sort(key=lambda s: (s.sensor_type.upper() != "PSI_VER3", s.path))
    return found[0] if found else None


# ------------------------------------------------------------ DevCtrl.ini

@dataclass
class DevCtrlSettings:
    address: str = ""
    interface_type: str = ""
    protocol_version: str = ""

    @property
    def interface_text(self) -> str:
        return {"0": "USB/COM", "1": "Ethernet"}.get(self.interface_type, f"тип {self.interface_type}")


def _ini(path: str) -> Optional[configparser.ConfigParser]:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read_string(_read_text(path))
    except (OSError, configparser.Error) as e:
        log.warning("Не удалось прочитать %s: %s", path, e)
        return None
    return cp


def read_devctrl_ini(directory: str) -> DevCtrlSettings:
    cp = _ini(os.path.join(directory, "DevCtrl.ini"))
    if cp is None or not cp.has_section("General"):
        return DevCtrlSettings()
    g = cp["General"]
    return DevCtrlSettings(address=g.get("CurrentAddress", g.get("CurrentIPAddress", "")).strip(),
                           interface_type=g.get("InterfaceType", "").strip(),
                           protocol_version=g.get("ProtocolVersion", "").strip())


# ------------------------------------------------------------ MSxxxx_nnnnn.cfg

@dataclass
class GratingParams:
    index: int
    lines_per_mm: float
    focus_m: float
    theta2: float
    fc_angle: float
    sin_alpha: float = 0.0


@dataclass
class MonoConfig:
    path: str = ""
    caption: str = ""
    serial: str = ""
    gratings: List[GratingParams] = field(default_factory=list)
    slits: List[str] = field(default_factory=list)
    mirror_positions: List[str] = field(default_factory=list)
    has_shutter: bool = False
    current_grating: int = -1


def find_mono_cfg(directory: str) -> Optional[str]:
    for path in sorted(glob.glob(os.path.join(directory, "MS*_*.cfg"))):
        if "_old" not in os.path.basename(path).lower():
            return path
    return None


def read_mono_cfg(directory: str) -> MonoConfig:
    path = find_mono_cfg(directory)
    if not path:
        return MonoConfig()
    cp = _ini(path)
    if cp is None:
        return MonoConfig(path=path)
    dev = cp["Device"] if cp.has_section("Device") else {}
    mc = MonoConfig(path=path, caption=dev.get("Caption", "").strip(), serial=dev.get("SerialNumber", "").strip())
    n = int(_num(dev.get("GratingCount"), 0))
    for i in range(1, n + 1):
        sec = f"Grating{i}"
        if not cp.has_section(sec):
            continue
        g = cp[sec]
        mc.gratings.append(GratingParams(
            index=i - 1,
            lines_per_mm=_num(g.get("LineCount"), 0) / 1000.0,
            focus_m=_num(g.get("Focus"), 0.75),
            theta2=_num(g.get("Theta2"), 0.0),
            fc_angle=_num(g.get("FCAngle"), 0.0),
            sin_alpha=_num(g.get("SinAlpha"), 0.0)))
    for i in range(1, int(_num(dev.get("SlitCount"), 0)) + 1):
        sec = f"Slit{i}"
        name = cp[sec].get("CaptionWork", f"Щель {i}").strip() if cp.has_section(sec) else f"Щель {i}"
        mc.slits.append(name)
    if cp.has_section("Mirror1"):
        m = cp["Mirror1"]
        # SSPL: положение 0 = DevCtrl-позиция 2, положение 1 = DevCtrl-позиция 1
        mc.mirror_positions = [m.get("PosCaption2", "Положение 2").strip(), m.get("PosCaption1", "Положение 1").strip()]
    mc.has_shutter = int(_num(dev.get("ShutterCount"), 0)) > 0
    if cp.has_section("Turret1"):
        mc.current_grating = int(_num(cp["Turret1"].get("CurGrating"), 0)) - 1
    return mc


# ------------------------------------------------------------ ImgWndConfig.cfg

def read_stripe(directory: str) -> Optional[Tuple[int, int]]:
    """Полоса интегрирования, заданная в SpectraSP на изображении (строки top, bottom)."""
    root = _xml_root(os.path.join(directory, "ImgWndConfig.cfg"))
    if root is None:
        return None
    for tool in root.iter():
        if not tool.tag.startswith("ImgToolInd_"):
            continue
        thick = _num(tool.findtext("Thickness"), 0)
        if thick >= 2:
            cy = _num(tool.findtext("CenterY"), 0)
            top = int(round(cy - thick / 2))
            return max(0, top), max(0, top + int(thick) - 1)
    return None


@dataclass
class InstalledSetup:
    directory: str
    main: SpectraSPSettings
    sensor: Optional[SensorSettings]
    devctrl: DevCtrlSettings
    mono: MonoConfig
    stripe: Optional[Tuple[int, int]]


def read_installation(directory: str) -> InstalledSetup:
    return InstalledSetup(directory=directory,
                          main=read_spectrasp_xml(directory),
                          sensor=find_psi_sensor(directory),
                          devctrl=read_devctrl_ini(directory),
                          mono=read_mono_cfg(directory),
                          stripe=read_stripe(directory))
