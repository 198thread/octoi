#!/bin/bash

ssh-keygen -f ~/.ssh/known_hosts -R '192.168.1.1' && ssh -o StrictHostKeyChecking=no root@192.168.1.1 'resize2fs /dev/loop0 && poweroff'