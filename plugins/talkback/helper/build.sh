#!/bin/sh
# Build talkback-helper: build.sh <output path>
set -e
mkdir -p "$(dirname "$1")"
out="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
cd "$(dirname "$0")"
swiftc -O main.swift -o "$out" -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist
codesign -s - -f --identifier dev.talkback.helper "$out" 2>/dev/null
