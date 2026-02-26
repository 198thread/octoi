#!/bin/bash

# ── helpers ────────────────────────────────────────────────────────────
dbug="${dbug:-0}"

msg() {
  echo -e "\n\033[1;32m▶ $*\033[0m"
}

dbg() {
  [[ "$dbug" == "1" ]] && echo -e "  \033[1;34m[debug]\033[0m $*"
}

err() {
  echo -e "\n\033[1;31m✖ $*\033[0m" >&2
}

# ── step 0 : clear terminal & locate the .bin image ───────────────────
clear

dbg "Searching for openwrt*.bin files in the current directory ($(pwd))"

# Gather matching files into an array
shopt -s nullglob
bin_files=(openwrt*.bin)
shopt -u nullglob

dbg "Found ${#bin_files[@]} matching file(s)"

if [[ ${#bin_files[@]} -eq 0 ]]; then
  err "No openwrt*.bin file found in $(pwd)"
  err "Please run this script from the directory that contains the .bin image."
  exit 1
elif [[ ${#bin_files[@]} -eq 1 ]]; then
  BIN="${bin_files[0]}"
  msg "Auto-selected image: ${BIN}"
else
  msg "Multiple openwrt*.bin images found:"
  echo
  for i in "${!bin_files[@]}"; do
    printf "  \033[1;33m%d)\033[0m %s\n" $((i + 1)) "${bin_files[$i]}"
  done
  echo
  while true; do
    read -rp "Pick a file [1-${#bin_files[@]}]: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#bin_files[@]} )); then
      BIN="${bin_files[$((choice - 1))]}"
      break
    fi
    echo "  Invalid selection – try again."
  done
  msg "Selected image: ${BIN}"
fi

dbg "BIN = ${BIN}"

# ── step 1 : remove old host key for 192.168.1.1 ─────────────────────
msg "Step 1/5 — Removing old SSH host key for 192.168.1.1 …"
dbg "Running: ssh-keygen -f ~/.ssh/known_hosts -R '192.168.1.1'"

ssh-keygen -f ~/.ssh/known_hosts -R '192.168.1.1' 2>/dev/null

sleep 2

# ── step 2 : scp the image to the device ─────────────────────────────
msg "Step 2/5 — Copying ${BIN} to root@192.168.1.1:/tmp/ …"
dbg "Running: scp -o StrictHostKeyChecking=accept-new -O \"${BIN}\" root@192.168.1.1:/tmp/"

scp -o StrictHostKeyChecking=accept-new -O "${BIN}" root@192.168.1.1:/tmp/
if [[ $? -ne 0 ]]; then
  err "scp failed – aborting."
  exit 1
fi

sleep 2

# ── step 3 : zero-out & flash via dd ─────────────────────────────────
REMOTE_BIN="/tmp/${BIN}"
DD_CMD="dd if=/dev/zero bs=512 seek=7634911 of=/dev/mmcblk0 count=33 && dd if=${REMOTE_BIN} of=/dev/mmcblk0"

msg "Step 3/5 — Flashing image on device …"
dbg "Running remotely: ${DD_CMD}"

ssh root@192.168.1.1 -C "${DD_CMD}"
if [[ $? -ne 0 ]]; then
  err "Remote dd command failed – aborting."
  exit 1
fi

sleep 2

# ── step 4 : clean host key again (new install will regenerate it) ────
msg "Step 4/5 — Removing SSH host key again (device will regenerate on next boot) …"
dbg "Running: ssh-keygen -f ~/.ssh/known_hosts -R '192.168.1.1'"

ssh-keygen -f ~/.ssh/known_hosts -R '192.168.1.1' 2>/dev/null

# ── step 5 : done ────────────────────────────────────────────────────
msg "Step 5/5 — All done! Power-cycle the device when ready."
dbg "Script finished successfully."
