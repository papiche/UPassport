#!/bin/bash
set -euo pipefail
[ $(id -u) -eq 0 ] && echo "LANCEMENT root INTERDIT (use sudo user). " && exit 1

PYTHON="$(which python)"
cat upassport.service.tpl \
    | sed "s~_USER_~$USER~g" \
    | sed "s~_PYTHON_~$PYTHON~g" \
    | sed "s~_MY_PATH_~$(pwd)~" > /tmp/upassport.service

cat /tmp/upassport.service
sudo cp /tmp/upassport.service /etc/systemd/system/upassport.service

sudo systemctl daemon-reload
sudo systemctl enable upassport
sudo systemctl restart upassport
