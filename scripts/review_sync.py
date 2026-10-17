#!/usr/bin/env python3
"""Backward-compatible entry for the removed GitCode review sync.

Publishing goes through scripts/sync_reviews.py, which stages the review files
into the cloud warehouse and pushes them. The old analysis-hub path and the
local /home/agentuser/stockdb path are retired.
"""
import os
import runpy
import sys

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_reviews.py")
sys.argv = [TARGET, *sys.argv[1:]]
runpy.run_path(TARGET, run_name="__main__")
