#!/bin/bash
# Double-click to start ffscan in its own window. Closing the window stops it; this Terminal window shows progress.
cd "$(dirname "$0")" && exec ./run.sh --window
