# API монохроматора: SSPL_IM.dll

`SSPL_IM.dll` — модуль самой SpectraSP (параметр `CtrlDllName` в
`SpectraSP.xml`, исходник `SSPL_IM_U.pas`). При `Init` он загружает
`DevCtrl.dll` из своего каталога; `DevCtrl.dll` (упакована UPX) читает
`DevCtrl.ini` и `MS7504_20126.cfg` **из каталога самой DLL** (путь берётся из
`GetModuleFileName(HInstance)`), связь с прибором — через `SOLCon.DLL`
(Indy TCP/UDP). Поэтому достаточно загрузить `SSPL_IM.dll` по полному пути из
каталога SpectraSP — будут использованы те же настройки и адрес прибора.

Все функции `stdcall`. Сигнатуры — по вызовам `ctrl_*` в SpectraSP.exe и телам
функций в DLL (`spectra_better/hw/sspl_mono.py`).

| Функция | Аргументы | Байт | Результат |
|---|---|---|---|
| `Init` | `HWND parent, int 0` | 8 | ≠0 — успех |
| `Close` | — | 0 | |
| `GetStatus` | — | 0 | 0 — готов, 1 — движение, 2 — не инициализирован |
| `GetDeviceTypeAndSerial` | `int* type, int* serial` | 8 | Boolean (для прибора: тип 5, № 20126) |
| `GetWaveLengthByTurret` | `int turret` | 4 | `double` (нм) |
| `SetWaveLengthByTurret` | `int turret, double nm, BOOL reset, int grating` | 20 | ≠0 — успех; SpectraSP передаёт `reset=false, grating=-1` |
| `GetActiveGratingByTurret` | `int turret` | 4 | номер решётки с 0 |
| `SetActiveGratingByTurret` | `int turret, int grating` | 8 | Boolean |
| `GetGratingCountByTurret` | `int turret` | 4 | число решёток |
| `GetWLRangeByTurret` | `int turret, int 0, double* min, double* max` | 16 | Boolean |
| `GetSlitWidth` / `SetSlitWidth` | `int slit, double* мкм` / `int slit, double мкм` | 8 / 12 | Boolean |
| `GetMirrorState` / `SetMirrorState` | `int mirror, int* pos` / `int mirror, int pos` | 8 / 8 | Boolean; pos 0 — «Top (CCD)», 1 — «Lateral (Slit)» |
| `GetSignalSourceState` | `int shutter` | 4 | 0 — неизвестно, 1 — открыта (SSS_ON), 2 — закрыта (SSS_OFF) |
| `SetSignalSourceState` | `int shutter, int state` | 8 | ≠0 — успех |
| `GetCalibrationByTurret` | `int turret, double centerWL, int centerPx, double pxWidth, int n, double* out` | 32 | ≠0 — успех; длины волн n пикселей по модели DevCtrl |

### Сервисные функции (шаговые двигатели и окно DevCtrl)

Используются окном «Сервис монохроматора». Сигнатуры проверены по телам
функций в `SSPL_IM.dll`; положения — в шагах двигателя.

| Функция | Аргументы | Байт | Результат |
|---|---|---|---|
| `GetGratingStepByTurret` | `int turret, int (не используется), int* steps` | 12 | Boolean |
| `SetGratingStepByTurret` | `int turret, int steps` | 8 | Boolean, абсолютное положение |
| `MoveWLByStepsByTurret` | `int turret, int delta` | 8 | Boolean, относительный сдвиг решётки |
| `ResetGratingByTurret` | `int turret` | 4 | ≠0 — успех; поиск нуля по датчику |
| `GetTurretStep` / `SetTurretStepByTurret` | `int turret, int* steps` / `int turret, int steps` | 8 / 8 | Boolean |
| `ResetTurret` | — | 0 | Boolean |
| `GetSlitStep` / `SetSlitStep` | `int slit, int* steps` / `int slit, int steps` | 8 / 8 | Boolean |
| `MoveSlitsBySteps` | `int slit, int delta` | 8 | Boolean |
| `ResetSlit` | `int slit` | 4 | Boolean |
| `GetMirrorStep` / `SetMirrorStep` | `int mirror, int* steps` / `int mirror, int steps` | 8 / 8 | Boolean |
| `ResetMirror` | `int mirror` | 4 | Boolean |
| `ShowWnd` | `int x, int y` | 8 | — ; вызывает `DevCtrl.ShowDeviceWindowEx(0)` |

Предел положения каждого механизма — `MaxMayStepCount` в соответствующей
секции cfg-файла прибора (`[Grating1]`, `[Slit1]`, `[Slit2]`, `[Mirror1]`);
для турели предел оценивается по `TurretPositionN`.

**Скрытое окно SpectraSP.** На главной форме SpectraSP есть невидимая кнопка
`btnSpcInstrument` (TSpeedButton) → действие `ViewSpcDevExecute` →
`ctrl_ShowWnd` → `SSPL_IM.ShowWnd(-1, -1)` → `DevCtrl.ShowDeviceWindowEx`.
Это и есть окно DevCtrl со шторкой, щелями, зеркалом, режимом «Test» и
разделом Configuration, которое в SpectraSP открывалось только сочетанием
клавиш. Окно создаётся в потоке, загрузившем DLL, поэтому `ShowWnd`
вызывается из потока монохроматора, который обрабатывает сообщения Windows.

Особенности:

* Большинство функций (включая `GetWaveLength…`) **внутри ждут** окончания
  предыдущего движения (`IsOperationFinished` в цикле). Состояние без
  блокировки — только `GetStatus`.
* Номер щели/зеркала/шторки SSPL переводит в маску устройства DevCtrl
  (щель n → `0x10 << n`, зеркало n → `0x100000 << n`, шторка 0 → `0x1000`).
* Турель 0 соответствует устройству `0x400` DevCtrl.
