# cRIO RT target (no LabVIEW RT)

## Role
- Open FPGA locally (`RIO0`)
- Run condition 1 / 2 and write TTL on the cRIO
- Stream raw EEG/EMG + movement + theta/delta + flags to PC over TCP

## PC
- Configure groups, start/stop
- Display raw + delta/theta energy
- Record CSV under `python/recordings/`

## Run (develop on PC first)
```bat
py -3.11 python\rt_target\main.py --simulate
py -3.11 python\pc_ui_tk\pc_ui.py
```
Connect to `127.0.0.1` port `7001`.

## Replay real mouse TDMS (offline test)
1. Put the `.tdms` under e.g. `python/testdata/`
2. Inspect channels:
```bat
py -3.11 python\tdms_replay.py path\to\file.tdms
```
3. Start RT with replay (10x speed example):
```bat
py -3.11 python\rt_target\main.py --tdms path\to\file.tdms --tdms-speed 10 --tdms-channels "EEG,EMG"
```
4. Open `python/pc_ui_tk/pc_ui.py`, connect `127.0.0.1:7001`, confirm groups so EEG/EMG AI indices match mapped channels (first listed channel → AI0).

## Run on cRIO
1. Disable LabVIEW `startup.rtexe` in NI MAX
2. SSH as admin, install Python3 + nifpga
3. Copy `python/` and the `.lvbitx` to the cRIO
4. `python3 rt_target/main.py --resource RIO0 --bitfile /path/to.lvbitx`
5. On PC: `python/pc_ui_tk/pc_ui.py` (or `python/pc_ui_qt/qt_pc_ui.py`), set RT IP to the cRIO, Connect
