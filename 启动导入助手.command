#!/bin/zsh
cd -- "${0:A:h}" || exit 1
exec /usr/bin/python3 -I marketplace_helper.py
