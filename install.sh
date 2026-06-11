#!/bin/bash

echo "[+] Updating package list..."
sudo apt-get update

echo "[+] Installing Python 3, pip, and build tools..."
sudo apt-get install -y python3 python3-pip python3-venv build-essential ffmpeg

echo "[+] Upgrading pip..."
pip install --upgrade pip

echo "[+] Installing Python requirements..."
pip install -r requirements.txt

echo "[+] Installation complete!"
echo "[+] To start the bot: python3 -m bot"
