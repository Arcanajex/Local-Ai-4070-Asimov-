#!/bin/sh
set -eu
cd "$(dirname "$0")"
out=$(sh greet.sh)
test "$out" = "hello"
