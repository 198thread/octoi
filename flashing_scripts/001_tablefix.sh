#!/bin/bash

ssh-keygen -f ~/.ssh/known_hosts -R '192.168.1.1'

ssh -o StrictHostKeyChecking=no root@192.168.1.1 << 'EOF'
set -e

sleep 2

parted -s /dev/mmcblk0 --fix

PART_NUM=$(parted -s /dev/mmcblk0 print --fix | grep rootfs | awk '/\s+\d/ {last=$1} END {print last}')

parted -s /dev/mmcblk0 resizepart "$PART_NUM" 100%

sleep 2

reboot
EOF
