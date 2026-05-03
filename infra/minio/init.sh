#!/bin/sh
mc alias set local http://minio:9000 minioadmin minioadmin
mc mb --ignore-existing local/kbchat-dev
