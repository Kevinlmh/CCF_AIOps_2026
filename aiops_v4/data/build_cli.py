"""Explicit canonical build, verification and optional logical-window export."""
import argparse
import json
from pathlib import Path
import sys
from .cache import atomic_json,verify_layer,dependency_path,sha256
from .pipeline import build_data_layers
from aiops_v4.experiments.config import load_config
from aiops_v4.features.store import export_windows


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('build');p.add_argument('--config',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--resume',action='store_true');p.add_argument('--progress',action='store_true')
    p=commands.add_parser('verify');p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--raw',action='store_true')
    p=commands.add_parser('export');p.add_argument('--feature-store',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='build':
            root=args.output_dir.resolve()
            if root.exists() and not args.resume:raise FileExistsError('choose a new dataset directory or --resume')
            c=load_config(args.config)
            result=build_data_layers(c,root,resume=args.resume,progress=(lambda p:print(json.dumps(dict(progress=p)),flush=True)) if args.progress else None)
            record=dict(format='canonical_dataset_v1',layers={k:str(Path(v).relative_to(root)) for k,v in result['layers'].items()},
                        seal_sha256={k:sha256(Path(v)/'seal.json') for k,v in result['layers'].items()})
            atomic_json(root/'dataset.json',record)
        elif args.command=='verify':
            root=args.data_dir.resolve(strict=True);record=json.loads((root/'dataset.json').read_text())
            if record['format']!='canonical_dataset_v1' or set(record['layers'])!={'features','states','evidence'}:raise ValueError('invalid dataset manifest')
            for name,relative in record['layers'].items():
                seal=root/relative/'seal.json'
                # All three selected layer directories belong to this dataset root.
                if Path(relative).is_absolute() or '..' in Path(relative).parts or root not in seal.resolve(strict=True).parents or seal.is_symlink():raise ValueError('unsafe dataset layer')
                if sha256(seal)!=record['seal_sha256'][name]:raise ValueError('dataset seal changed')
                verify_layer(seal.parent,verify_raw=args.raw and name=='features')
            result=dict(verified=True,layers=record['layers'],raw_verified=args.raw)
        else:
            export_windows(args.feature_store,args.output);result=dict(export=str(args.output.resolve()))
        print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0
    except (ValueError,OSError,RuntimeError,KeyError,TypeError) as error:
        print('canonical data failed ('+type(error).__name__+'); inspect the retained checkpoint and input policy.',file=sys.stderr);return 2
