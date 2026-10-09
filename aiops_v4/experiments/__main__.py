"""Explicit run / evaluate / ablate / compare commands; no label-bearing run options."""
import argparse
from pathlib import Path
import sys
from aiops_v4.agents.jsonio import dumps,loads
from .config import load_config
from .run import run_experiment
from .evaluation import evaluate_run
from .ablation import run_ablation,compare_runs,VARIANTS


def main(argv=None):
    parser=argparse.ArgumentParser(description='v4 hybrid diagnosis and isolated experiment evaluation')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('run','ablate'):
        command=commands.add_parser(name)
        command.add_argument('--config',type=Path,required=True)
        command.add_argument('--output-dir',type=Path,required=True)
        if name=='ablate':
            command.add_argument('--variants',nargs='+',choices=VARIANTS)
            command.add_argument('--replay-map',type=Path,help='JSON variant -> replay path, resolved relative to this map')
    command=commands.add_parser('evaluate')
    command.add_argument('--run-dir',type=Path,required=True)
    command.add_argument('--ground-truth',type=Path,required=True)
    command.add_argument('--batch',required=True)
    command.add_argument('--output-dir',type=Path,required=True)
    command.add_argument('--allow-partial',action='store_true')
    command=commands.add_parser('compare')
    command.add_argument('--run-dirs',nargs='+',type=Path,required=True)
    command.add_argument('--evaluation-dirs',nargs='+',type=Path,help='one sealed evaluation per run, in matching order')
    command.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='run':result=run_experiment(load_config(args.config),args.output_dir)
        elif args.command=='ablate':
            replays=None
            if args.replay_map is not None:
                path=args.replay_map.resolve(strict=True);mapping=loads(path.read_text(encoding='utf-8'))
                if not isinstance(mapping,dict) or any(not isinstance(v,str) or not v for v in mapping.values()):raise ValueError('invalid replay map')
                replays={k:str((path.parent/v).resolve()) for k,v in mapping.items()}
            result=run_ablation(load_config(args.config),args.output_dir,variants=args.variants,replays=replays)
        elif args.command=='evaluate':
            result=evaluate_run(args.run_dir,args.ground_truth,args.batch,args.output_dir,allow_partial=args.allow_partial)
        else:result=compare_runs(args.run_dirs,args.output_dir,evaluation_dirs=args.evaluation_dirs)
        print(dumps(result));return 0
    except (ValueError,TypeError,KeyError,OSError,RuntimeError,RecursionError) as error:
        # Never echo an exception message: provider text/config content may contain credentials.
        print('v4 experiment failed ('+type(error).__name__+'); inspect configuration or sanitized failure.json.',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
