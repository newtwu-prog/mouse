@echo off
REM Local simulate RT agent (no cRIO / no FPGA)
py -3.11 "%~dp0rt_target\main.py" --simulate %*
