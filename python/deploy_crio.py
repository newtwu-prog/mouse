"""Deploy python RT agent + bitfile to cRIO and optionally start it.

Usage (from PC):
  py -3.11 python/deploy_crio.py
  py -3.11 python/deploy_crio.py --start
  py -3.11 python/deploy_crio.py --stop-labview --start
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "-q"])
    import paramiko

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DEFAULT_BITFILE = PROJECT / "FPGA Bitfiles" / "untitledproject1_FPGATarget_FPGADAQ6_GFVDrwkaPc4.lvbitx"
REMOTE_ROOT = "/home/admin/eeg_mice"
REMOTE_BIT = f"{REMOTE_ROOT}/bitfiles/FPGA_DAQ6.lvbitx"

# Packages needed on RT (not full PC requirements)
PIP_PKGS = ["nifpga", "numpy", "scipy"]

SKIP_NAMES = {
    "__pycache__",
    "recordings",
    "testdata",
    ".git",
    "_ssh_check_rt.py",
    "_ssh_check_bitfile.py",
    "_ssh_bit2.py",
    "deploy_crio.py",
}


def connect(host: str, user: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
        auth_timeout=20,
    )
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    print(f"  $ {cmd}")
    _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip())
    return code, out, err


def sftp_mkdirs(sftp: paramiko.SFTPClient, remote: str) -> None:
    parts = remote.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except OSError:
            sftp.mkdir(cur)


def should_skip(path: Path) -> bool:
    return any(part in SKIP_NAMES for part in path.parts)


def upload_tree(sftp: paramiko.SFTPClient, local: Path, remote: str) -> int:
    n = 0
    for path in local.rglob("*"):
        if should_skip(path.relative_to(local)):
            continue
        if path.is_dir():
            continue
        rel = path.relative_to(local).as_posix()
        rpath = f"{remote}/{rel}"
        sftp_mkdirs(sftp, str(Path(rpath).parent).replace("\\", "/"))
        sftp.put(str(path), rpath)
        n += 1
        if n % 20 == 0:
            print(f"  uploaded {n} files…")
    return n


def ensure_pip(client: paramiko.SSHClient) -> None:
    code, _, _ = run(client, "python3 -m pip --version")
    if code == 0:
        return
    print("Installing pip…")
    run(client, "opkg update", timeout=180)
    run(client, "opkg install python3-pip python3-misc", timeout=180)
    code, _, _ = run(client, "python3 -m pip --version")
    if code != 0:
        # bootstrap get-pip
        run(
            client,
            "python3 -c \"import urllib.request; urllib.request.urlretrieve("
            "'https://bootstrap.pypa.io/get-pip.py','/tmp/get-pip.py')\"",
            timeout=120,
        )
        run(client, "python3 /tmp/get-pip.py", timeout=180)


def install_pkgs(client: paramiko.SSHClient) -> None:
    ensure_pip(client)
    pkgs = " ".join(PIP_PKGS)
    run(client, f"python3 -m pip install --upgrade {pkgs}", timeout=600)


def stop_labview(client: paramiko.SSHClient) -> None:
    print("Stopping LabVIEW RT (lvrt) so FPGA can be opened by Python…")
    run(client, "/etc/init.d/lvrt stop || true")
    run(client, "killall lvrt 2>/dev/null || true")
    time.sleep(2)
    run(client, "ps -ef | grep -i lvrt | grep -v grep || echo lvrt_stopped")


def start_agent(client: paramiko.SSHClient, bitfile: str, port: int) -> None:
    # kill previous agent
    run(client, "pkill -f 'rt_target/main.py' 2>/dev/null || true")
    time.sleep(1)
    log = f"{REMOTE_ROOT}/rt_agent.log"
    cmd = (
        f"cd {REMOTE_ROOT}/python && "
        f"nohup python3 -u rt_target/main.py "
        f"--resource RIO0 --bitfile {bitfile} --host 0.0.0.0 --port {port} "
        f"> {log} 2>&1 & echo $!"
    )
    run(client, cmd)
    time.sleep(3)
    run(client, f"tail -n 40 {log} || true")
    run(client, f"ss -lnt | grep {port} || echo PORT_{port}_NOT_LISTENING")
    run(client, "ps -ef | grep rt_target/main.py | grep -v grep || echo NO_AGENT")


def main() -> int:
    p = argparse.ArgumentParser(description="Deploy EEG mice RT agent to cRIO")
    p.add_argument("--host", default="192.168.0.110")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="")
    p.add_argument("--bitfile", type=Path, default=DEFAULT_BITFILE)
    p.add_argument("--port", type=int, default=7001)
    p.add_argument("--skip-upload", action="store_true")
    p.add_argument("--skip-pip", action="store_true")
    p.add_argument("--stop-labview", action="store_true", help="stop lvrt before start")
    p.add_argument("--start", action="store_true", help="start rt_target after deploy")
    args = p.parse_args()

    if not args.bitfile.is_file():
        print(f"bitfile missing: {args.bitfile}")
        return 1

    print(f"Connecting {args.user}@{args.host}…")
    client = connect(args.host, args.user, args.password)
    print("SSH OK")

    run(client, "uname -a")
    run(client, "python3 --version")

    if not args.skip_upload:
        print(f"Creating {REMOTE_ROOT}…")
        run(client, f"mkdir -p {REMOTE_ROOT}/python {REMOTE_ROOT}/bitfiles")
        sftp = client.open_sftp()
        print(f"Uploading {ROOT} → {REMOTE_ROOT}/python")
        n = upload_tree(sftp, ROOT, f"{REMOTE_ROOT}/python")
        print(f"  uploaded {n} python files")
        print(f"Uploading bitfile → {REMOTE_BIT}")
        sftp.put(str(args.bitfile), REMOTE_BIT)
        sftp.close()
        print("Upload done")

    if not args.skip_pip:
        print("Installing RT Python packages…")
        install_pkgs(client)
        run(client, "python3 -c \"import nifpga, numpy, scipy; print('imports OK', nifpga.__file__)\"")

    if args.stop_labview:
        stop_labview(client)

    if args.start:
        if not args.stop_labview:
            print("Note: if FPGA open fails, re-run with --stop-labview")
        start_agent(client, REMOTE_BIT, args.port)

    client.close()
    print("\nDone.")
    if args.start:
        print(f"On PC: py -3.11 python/pc_ui_tk/pc_ui.py  → connect {args.host}:{args.port}")
    else:
        print("Uploaded only. To start: py -3.11 python/deploy_crio.py --skip-upload --skip-pip --stop-labview --start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
