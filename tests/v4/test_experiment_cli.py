import json
import subprocess
import sys
from test_evidence_pipeline import inputs


def cli(args,cwd):
    return subprocess.run([sys.executable,'-m','aiops_v4.experiments',*args],cwd=cwd,capture_output=True,text=True)


def test_cli_relative_config_runs_from_elsewhere_and_refuses_overwrite(tmp_path):
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    config=dict(schema_version=1,batch='stage2',raw_root='raw',naive_timezone='UTC',
        agents={'discovery_only':True},backend={'kind':'replay','model':'fixture-model','replay_file':'empty.jsonl'})
    path=tmp_path/'config.json';path.write_text(json.dumps(config));out=tmp_path/'result'
    result=cli(['run','--config',str(path),'--output-dir',str(out)],tmp_path.parent)
    assert result.returncode==0,result.stderr
    summary=json.loads(result.stdout);assert summary['discovery_only'] and summary['status']=='completed'
    second=cli(['run','--config',str(path),'--output-dir',str(out)],tmp_path.parent)
    assert second.returncode==2 and 'FileExistsError' in second.stderr
    assert (out/'summary.json').is_file()


def test_cli_config_error_is_sanitized_and_never_echoes_secret(tmp_path):
    p=tmp_path/'secret.json';p.write_text(json.dumps({'api_key':'do-not-echo-this-value'}))
    result=cli(['run','--config',str(p),'--output-dir',str(tmp_path/'out')],tmp_path)
    assert result.returncode==2 and 'do-not-echo-this-value' not in result.stderr+result.stdout
    assert not (tmp_path/'out').exists()
    result=cli(['evaluate','--run-dir','none'],tmp_path)
    assert result.returncode==2


def test_cli_ablate_and_compare_are_separate_from_evaluation(tmp_path):
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    (tmp_path/'empty.jsonl').write_text('')
    c=dict(schema_version=1,batch='stage2',raw_root='raw',naive_timezone='UTC',
        backend=dict(kind='replay',model='fixture-model',replay_file='empty.jsonl'))
    p=tmp_path/'config.json';p.write_text(json.dumps(c));out=tmp_path/'ablations'
    result=cli(['ablate','--config',str(p),'--output-dir',str(out),'--variants','full','no_rules'],tmp_path)
    assert result.returncode==0,result.stderr
    result=cli(['compare','--run-dirs',str(out/'full'),str(out/'no_rules'),'--output-dir',str(tmp_path/'comparison')],tmp_path)
    assert result.returncode==0 and json.loads(result.stdout)['input_comparable']
    result=cli(['evaluate','--run-dir',str(out/'full'),'--ground-truth','none','--batch','wrong','--output-dir',str(tmp_path/'evaluation')],tmp_path)
    assert result.returncode==2 and not (tmp_path/'evaluation').exists()
