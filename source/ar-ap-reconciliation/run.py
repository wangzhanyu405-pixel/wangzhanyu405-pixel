"""Start the local finance workbench from any working directory."""

import argparse
import os
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="启动财务智能对账平台")
    parser.add_argument("--port", type=int, default=8502, help="本机服务端口")
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parent)
    os.execv(sys.executable, [
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.address", "127.0.0.1", "--server.port", str(args.port),
        "--browser.gatherUsageStats", "false",
    ])


if __name__ == "__main__":
    main()
