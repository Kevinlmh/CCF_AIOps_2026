"""Build a review queue or query it without mutating its database."""
import argparse
from pathlib import Path
import sqlite3
import sys
from aiops_v4.states.pipeline import _dump, _load
from .pipeline import build_evidence
from .index import query_evidence, query_raw


def main(argv=None):
    parser = argparse.ArgumentParser(description='v4 traceable review evidence')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--states-dir', type=Path, required=True)
    build.add_argument('--batch', required=True)
    build.add_argument('--output-dir', type=Path, required=True)
    build.add_argument('--raw-root', type=Path)
    build.add_argument('--skip-raw', action='store_true')
    build.add_argument('--topology', type=Path)
    build.add_argument('--context-seconds', type=int, default=120)
    build.add_argument('--observations-per-kind', type=int, default=8)
    query = sub.add_parser('query')
    query.add_argument('--database', type=Path, required=True)
    query.add_argument('--batch', required=True)
    query.add_argument('--kind', required=True, choices=('bundle', 'members', 'states', 'model', 'relations'))
    for name in ('bundle-id', 'entity-id', 'source', 'group-id', 'start', 'end'):
        query.add_argument('--' + name)
    query.add_argument('--limit', type=int, default=100)
    query.add_argument('--offset', type=int, default=0)
    raw = sub.add_parser('query-raw')
    raw.add_argument('--database', type=Path, required=True)
    raw.add_argument('--batch', required=True)
    choose = raw.add_mutually_exclusive_group(required=True)
    choose.add_argument('--record-id')
    choose.add_argument('--reference-json', help='full registered reference as a JSON object')
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            result = build_evidence(args.states_dir, args.batch, args.output_dir, raw_root=args.raw_root,
                skip_raw=args.skip_raw, topology=args.topology, context_seconds=args.context_seconds,
                observations_per_kind=args.observations_per_kind)
        elif args.command == 'query':
            result = query_evidence(args.database, args.batch, args.kind, bundle_id=args.bundle_id,
                entity_id=args.entity_id, source=args.source, group_id=args.group_id, start=args.start,
                end=args.end, limit=args.limit, offset=args.offset)
        else:
            result = query_raw(args.database, args.batch, args.record_id,
                               reference=_load(args.reference_json) if args.reference_json is not None else None)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(_dump(result))
    return 0
