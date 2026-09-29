# API драйвера ПЗС-камер PSI (psi_ccd2_verN.dll)

Документация восстановлена по SpectraSP.exe и самим DLL (дизассемблирование),
т. к. производитель её не публикует.

* Источник структур — расширенная RTTI Delphi в SpectraSP.exe: имена полей,
  типы и смещения взяты оттуда напрямую (`TPSICameraInfoV2` и др.).
* Сигнатуры функций — по местам вызова в SpectraSP.exe (`PSI_CCD_Ver3_*`),
  число байт аргументов — по инструкции `ret N` в DLL (все функции `stdcall`).
* Всё сведено в `spectra_better/hw/psi_structs.py`, соответствие проверяется
  тестами `tests/test_structs.py`.

## Выбор библиотеки

`psi_ccd2_ver0/1/2/3.dll` — оболочки над ядром `psi_ccd5.dll` (API `psiccd3_*`,
сеть через wsock32). SpectraSP перебирает `psi_ccd2_ver0.dll`, `ver1`, …
и берёт первую, с которой камера инициализировалась. По журналам это
`psi_ccd2_ver0.dll` (инициализация с первой попытки). `ver2` имеет другую
сигнатуру `GetCameraState` (5 аргументов) — пропускается.

Перед `psiccd2_Init` SpectraSP проверяет доступность камеры ICMP-запросом
(таймаут 3 с) и без ответа не подключается.

## Функции

Все возвращают `int` (0 — успех), если не указано иное. `h` — дескриптор из
`OpenDevice`. Драйвер работает с одним устройством (внутри используется
глобальный дескриптор).

| Функция | Аргументы | Байт | Примечание |
|---|---|---|---|
| `psiccd2_Init` | `const char* ip` | 4 | возвращает Boolean |
| `psiccd2_GetDevCount` | `int* count` | 4 | |
| `psiccd2_OpenDevice` | `int index, TPSIControllerInfoV2*, TPSICameraInfoV2*` | 12 | возвращает дескриптор ≥0 |
| `psiccd2_CloseDevice` | `int h` | 4 | Boolean |
| `psiccd2_GetLastError` | — | 0 | `char*` — текст ошибки |
| `psiccd2_GetCameraState` | `h, TPSICameraStateV2*, TPSIStepMotor[8]*` | 12 | |
| `psiccd2_GetParameters` | `h, TPSICameraParamsV2*, TPSICameraScanParamsV2*` | 12 | |
| `psiccd2_SetParameters` | `h, TPSICameraParamsV2` **по значению** | 92 | 88 байт записи на стеке |
| `psiccd2_GetRegions` | `h, int* count, TPSICCDRegionV2* regs` | 12 | |
| `psiccd2_SetRegions` | `h, int count, TPSICCDRegionV2* regs` | 12 | |
| `psiccd2_SetAcqMode` | `h, int* frames, int* mode` | 12 | frames=1, mode=0 |
| `psiccd2_StartWaitMeasure` | `h, int* frames, int* frameSize` | 12 | блокирует до конца экспозиции и считывания; на выходе frameSize — размер кадра в байтах |
| `psiccd2_GetData` | `h, int frame, int count, int bits, void** ppBuf` | 20 | копирует кадр в `*ppBuf`; для одного кадра `frame=0, count=0` |
| `psiccd2_AbortScan` | `h` | 4 | прерывает `StartWaitMeasure` (можно из другого потока: взводит флаг, который проверяет цикл ожидания) |
| `psiccd2_SetCoolingOn` | `h, int t°C` | 8 | уставка температуры |
| `psiccd2_SetCoolingOff` | `h` | 4 | |
| `psiccd2_SetLoggingMode` | `h, bool` | 8 | SpectraSP выключает |
| `psiccd2_GetGain/GetOffset` | `h, double*` | 8 | |

## Порядок работы (как в SpectraSP)

1. ping → `Init(ip)` → `GetDevCount` → `OpenDevice(0, &ctrl, &cam)` → `SetLoggingMode(h, false)`.
2. `GetCameraState`, `GetParameters`, `GetRegions`.
3. Настройка: заполнить `TPSICameraParamsV2` (экспозиция `wExpUnit/wExpValue`,
   `lReadoutSpeed`, `lRegNum`), затем
   `SetParameters → SetAcqMode(1, 0) → SetRegions → SetParameters → GetParameters`.
   SpectraSP записывает в `lFactReadoutSpeed` значение 166 (камера его игнорирует).
4. Кадр: `StartWaitMeasure(h, &frames, &size)` → `GetData(h, 0, 0, wBits, &pBuf)`.
   SpectraSP повторяет шаг 3 перед **каждым** кадром; SpectraSP Better — только
   при изменении параметров.
5. Закрытие: если ПЗС холоднее 0 °C — `SetCoolingOn(h, 2)` и ожидание, затем
   `CloseDevice`. SpectraSP ждёт `lCoolingState == 2` до 60 с, но сразу после
   смены уставки камера ещё сообщает «стабилизировано» на старой уставке, и
   ожидание может закончиться мгновенно. SpectraSP Better ждёт саму
   температуру (не ниже +1 °C) — до 3 минут, с возможностью не ждать.

### Экспозиция

`wExpUnit`: 1 — мкс, 2 — мс, 3 — с, 4 — мин; `wExpValue` — 16 бит.
SpectraSP выбирает самую мелкую единицу, в которой значение ≤ 65535
(1 с → 1000 мс). Реализовано в `exposure_to_unit_value()`.

### Выходы синхронизации (лазер)

У камеры два выхода синхронизации (`CameraInfoV2.wSynchOutCount = 2`, по 9 режимов;
`wSynchModesCount = 6` — режимы запуска). Режим выхода — поле
`TPSICameraParamsV2.wSynchOut1Type / wSynchOut2Type`, передаётся вместе с остальными
параметрами в `SetParameters`. SpectraSP при каждом `SetDevParam` записывает туда
`SynchroOut1/2` из `Sensor_*.xml` (в этой установке 0 и 6); после включения питания
камера сообщает 0 на обоих выходах. Названия режимов — из списка SpectraSP для
`ST_PSI_CCD_DETECTOR_VER3` (номер в списке = значение поля):

| Режим | В SpectraSP | Смысл |
|---|---|---|
| 0 | EXP polar negative | импульс на время экспозиции, отрицательный |
| 1 | EXP polar positive | импульс на время экспозиции, положительный |
| 2 | PRED DELAY polar negative | перед экспозицией |
| 3 | POST DELAY polar negative | после экспозиции |
| 4 | EXTERNAL SYNCH 0 | внешняя синхронизация |
| 5 | LOGICAL 1 | постоянно высокий уровень |
| 6 | LOGICAL 0 | постоянно низкий уровень |
| 7 | SOFT START SCAN | программный старт |

SpectraSP Better передаёт эти значения так же (при подключении и при каждой
настройке кадра). Кнопка «Лазер» переключает выбранный в настройках выход между
LOGICAL 1 и LOGICAL 0 (или наоборот, если лазер включается нулём).

### Области и объединение пикселей

`TPSICCDRegionV2`: `wLeft, wTop, wRight, wBottom` (включительно),
`wRegionNumber`, `wSUPixelHeight` (объединение по вертикали), `wSUPixelWidth`
(по горизонтали) — порядок полей именно такой. Размер кадра в драйвере:
`(ширина/binW) × (высота/binH) × 2 байта`.
Спектральный режим SpectraSP Better: одна область на всю ширину, строки полосы,
`wSUPixelHeight` = высота полосы → одна строка на кадр.

## Структуры (packed)

См. `psi_structs.py`. Размеры: `TPSICCDRegionV2` 14, `TPSIControllerInfoV2` 88,
`TPSICameraInfoV2` 130, `TPSICameraParamsV2` 88, `TPSICameraStateV2` 32,
`TPSIStepMotor` 12, `TPSICameraScanParamsV2` 8 байт.

Значения, прочитанные с камеры 160082 (из журнала): 2048×506, пиксель 12 мкм,
3 скорости считывания (в т. ч. 125 и 250 кГц), охлаждение −50…+35 °C,
`wBits = 4`, `wCCDStandardMode = 2`.
