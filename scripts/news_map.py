#!/usr/bin/env python3
"""Backward-compatible entry for the retired news-to-theme mapper.

The current chain is collect_news_extra/collect_hotnews -> build_news_maps
-> news_impact -> news_transmission. This entry only rebuilds the three map
files. It no longer calls AkShare or writes to the retired local stockdb.
"""
import os
import runpy
import sys

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_news_maps.py")
sys.argv = [TARGET]
runpy.run_path(TARGET, run_name="__main__")
