"""Build reproducible multiview window features."""

import argparse
import csv
import json
from pathlib import Path
import sqlite3
import sys

from .pipeline import build_features


def main(argv=None):
    parser=argparse.ArgumentParser(description='v4 explicit semantic policies and window features')
    sub=parser.add_subparsers(dest='command',required=True)
    build=sub.add_parser('build')
    build.add_argument('--root',type=Path,required=True)
    build.add_argument('--batch',required=True)
    build.add_argument('--output-dir',type=Path,required=True)
    build.add_argument('--naive-timezone',required=True)
    build.add_argument('--window-seconds',type=int,default=60)
    build.add_argument('--expected-step-seconds',type=int)
    build.add_argument('--counter-max-gap-seconds',type=int,default=180)
    build.add_argument('--max-rows-per-file',type=int)
    build.add_argument('--verbose',action='store_true')
    arguments=parser.parse_args(argv)

    def progress(event):
        if arguments.verbose:
            print(json.dumps(event,ensure_ascii=False),file=sys.stderr,flush=True)

    try:
        summary=build_features(arguments.root,arguments.batch,arguments.output_dir,
                               naive_timezone=arguments.naive_timezone,window_seconds=arguments.window_seconds,
                               expected_step_seconds=arguments.expected_step_seconds,
                               counter_max_gap_seconds=arguments.counter_max_gap_seconds,
                               max_rows_per_file=arguments.max_rows_per_file,progress=progress)
        print(json.dumps({key:summary[key] for key in ['batch','mode','complete','rows_scanned','rows_rejected','window_count','output_dir']},ensure_ascii=False))
        return 0
    except (OSError,ValueError,csv.Error,sqlite3.DatabaseError) as error:
        print(f'error: {error}',file=sys.stderr)
        return 2
