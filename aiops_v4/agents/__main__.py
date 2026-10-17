"""Evidence index → independent LLM roles → audited official JSONL."""
import argparse
import os
from pathlib import Path
import re
from .backend import HTTPBackend, ReplayBackend
from .engine import Budget
from .jsonio import dumps, loads
from .pipeline import diagnose


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('diagnose')
    for name in ('database', 'batch', 'output-dir', 'backend', 'model'):
        run.add_argument('--' + name, required=True, **({'choices': ['replay', 'http']} if name == 'backend' else {}))
    run.add_argument('--replay-file'); run.add_argument('--base-url')
    run.add_argument('--api-key-env', default='AIOPS_LLM_API_KEY')
    run.add_argument('--timeout', type=float, default=60)
    run.add_argument('--max-completion-tokens', type=int, default=4096)
    run.add_argument('--export-abstentions', action='store_true',
                     help='export confirmed events whose later roles abstained, using delivered evidence priors')
    run.add_argument('--shard-index', type=int, default=0)
    run.add_argument('--shard-count', type=int, default=1)
    run.add_argument('--no-knowledge', action='store_true')
    run.add_argument('--state-explanation', action='store_true')
    run.add_argument('--unstructured', action='store_true')
    run.add_argument('--temperature', type=float, default=0.0)
    run.add_argument('--thinking', action='store_true')
    run.add_argument('--evidence-wire', default=None)
    selection = run.add_mutually_exclusive_group()
    selection.add_argument('--bundle-id'); selection.add_argument('--limit-bundles', type=int)
    for name, default in [('max-turns', 6), ('max-tool-calls', 12), ('max-prompt-chars', 120000),
                          ('max-response-chars', 32000), ('max-manifest-windows', 2000)]:
        run.add_argument('--' + name, type=int, default=default)
    args = parser.parse_args()
    try:
        budget = Budget(**{key: getattr(args, key) for key in ('max_turns', 'max_tool_calls', 'max_prompt_chars', 'max_response_chars', 'max_manifest_windows')})
        if args.backend == 'replay':
            if not args.replay_file or args.base_url:
                raise ValueError('replay requires --replay-file and no --base-url')
            with Path(args.replay_file).open(encoding='utf-8') as stream:
                entries = [loads(line) for line in stream if line.strip()]
            backend = ReplayBackend(args.model, entries, capture_requests=False)
        else:
            if args.replay_file or not args.base_url or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.api_key_env):
                raise ValueError('http requires --base-url and a valid --api-key-env, without replay file')
            key = os.environ.get(args.api_key_env)
            if not key:
                raise ValueError('configured API key environment variable is missing')
            backend = HTTPBackend(args.model, args.base_url, key, timeout=args.timeout,
                                  max_completion_tokens=args.max_completion_tokens, structured_roles=not args.unstructured,
                                  compact_evidence=not args.unstructured,
                                  enable_thinking=bool(args.thinking), temperature=args.temperature,
                                  evidence_wire_version=args.evidence_wire)
        from .prompts import COMPONENT_REVIEW_INSTRUCTION  # noqa: F401  (prompt import gate)
        from aiops_v4.experiments.roles import run_diagnosis
        summary = run_diagnosis(args.database, args.batch, args.output_dir, backend, mode='independent', review_mode='llm',
                                knowledge=not args.no_knowledge, state_explanation=args.state_explanation,
                                bundle_id=args.bundle_id, limit_bundles=args.limit_bundles, budget=budget,
                                export_abstentions=args.export_abstentions, shard_index=args.shard_index,
                                shard_count=args.shard_count)
    except (ValueError, OSError) as error:
        # Transport errors never include API/server bodies; configuration errors contain no key values.
        parser.exit(2, 'v4 roles: ' + str(error) + '\n')
    print(dumps(summary))


if __name__ == '__main__':
    main()
