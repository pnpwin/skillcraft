#!/usr/bin/env python3
"""Stable standalone entrypoint for the Codex learning hook."""

from pathlib import Path
import runpy


if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'src/codex/learning_capture.py'), run_name='__main__')
