#!/usr/bin/env bash
set -euo pipefail

actual="$(python3 hello_world.py)"
expected="Hello, World!"

if [ "$actual" != "$expected" ]; then
  echo "FAIL: expected '$expected', got '$actual'" >&2
  exit 1
fi

echo "PASS: hello world"
