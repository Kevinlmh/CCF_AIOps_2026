"""Stream a read-only evidence queue through roles into a new auditable run."""
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile

from aiops_v4.evidence.index import metadata, query_evidence, read_index
from .contracts import load_manifest, validate_confirmation
from .diagnosis import diagnose_event, review_event
from .engine import Budget, run_role
from .export import prediction_for
from .jsonio import dumps
from .prompts import PROMPT_VERSION
from .tools import EvidenceSession

ARTIFACTS = ('bundles.jsonl', 'roles.jsonl', 'diagnoses.jsonl', 'predictions.jsonl', 'summary.json')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            digest.update(block)
    return digest.hexdigest()


def _publish(scratch, output):
    output.mkdir(exist_ok=False)
    linked = []
    try:
        for name in ARTIFACTS:  # summary is the completion marker and is linked last.
            os.link(scratch / name, output / name)
            linked.append(name)
    except OSError:
        for name in linked:
            path = output / name
            if path.is_file() and os.path.samestat(path.stat(), (scratch / name).stat()):
                path.unlink()
        try:
            output.rmdir()
        except OSError:
            pass
        raise


def diagnose(database, batch, output_dir, backend, *, bundle_id=None, limit_bundles=None, budget=Budget(),
             context=None, diagnostic_mode='independent', review_mode='llm', state_explanation=False):
    if diagnostic_mode not in ('independent', 'joint') or review_mode not in ('llm','program_only') or type(state_explanation) is not bool:
        raise ValueError('invalid diagnostic experiment modes')
    from aiops_v4.experiments.roles import explain_state, role_session, diagnose_case, experiment_prediction_for
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise FileExistsError('output directory already exists')
    if limit_bundles is not None and (type(limit_bundles) is not int or limit_bundles < 1):
        raise ValueError('positive limit_bundles required')
    if bundle_id is not None and (not isinstance(bundle_id, str) or not bundle_id):
        raise ValueError('nonempty bundle_id required')
    if bundle_id is not None and limit_bundles is not None:
        raise ValueError('choose bundle_id or limit_bundles')
    database = Path(database).resolve(strict=True)
    output = output.resolve()
    with read_index(database, batch) as db:
        protected = [database.parent, Path(metadata(db, 'raw_root')).resolve(),
                     Path(metadata(db, 'window_summary')['input_root']).resolve(), Path(__file__).resolve().parents[2] / 'data']
        if any(output == root or root in output.parents for root in protected):
            raise ValueError('output must be outside evidence/raw/data input directories')
        available = db.execute('SELECT COUNT(*) FROM bundles').fetchone()[0]
        if bundle_id is not None and db.execute('SELECT 1 FROM bundles WHERE id=?', (bundle_id,)).fetchone() is None:
            raise ValueError('unknown bundle_id')
        state_summary = metadata(db, 'state_summary')
    before = sha256(database)
    summary = dict(schema_version=1, batch=batch, created_at=datetime.now(timezone.utc).isoformat(),
        database=str(database), output_dir=str(output), input_evidence_sha256=before,
        input_scope=state_summary['input_scope'], reference_scope=state_summary['reference_scope'],
        input_state_config=state_summary['config'], backend=backend.metadata(), simulated=backend.metadata()['simulated'],
        diagnostic_mode=diagnostic_mode, review_mode=review_mode, semantic_review=review_mode=='llm',
        experimental=diagnostic_mode!='independent' or review_mode!='llm', state_explanation=state_explanation,
        prompt_version=PROMPT_VERSION, budget=asdict(budget), selection=dict(bundle_id=bundle_id, limit_bundles=limit_bundles),
        bundles_available=available, bundles_selected=0, bundles_deferred=0, role_runs=0, role_failures=0, backend_calls=0, usage_reported_calls=0,
        assessments_confirmed=0, assessments_rejected=0, assessments_deferred=0,
        diagnoses_accepted=0, diagnoses_rejected=0, diagnoses_deferred=0, predictions=0, usage={})
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.v4-roles-', dir=output.parent) as temporary:
        scratch = Path(temporary)
        with ExitStack() as stack:
            streams = {name: stack.enter_context((scratch / name).open('w', encoding='utf-8')) for name in ARTIFACTS[:-1]}
            def write(name, value):
                streams[name].write(dumps(value) + '\n')
            def role_write(run):
                if run is None:
                    return
                write('roles.jsonl', run)
                summary['role_runs'] += 1
                summary['backend_calls'] += run['backend_calls']
                summary['usage_reported_calls'] += run['usage_reported_calls']
                summary['role_failures'] += int(run['status'] != 'completed')
                for key, count in run['usage'].items():
                    summary['usage'][key] = summary['usage'].get(key, 0) + count
            with read_index(database, batch) as db:
                sql, params = 'SELECT id FROM bundles', []
                if bundle_id is not None:
                    sql += ' WHERE id=?'; params.append(bundle_id)
                sql += ' ORDER BY entity,start,id'
                if limit_bundles is not None:
                    sql += ' LIMIT ?'; params.append(limit_bundles)
                prediction_ids = set()
                for (identifier,) in db.execute(sql, params):
                    packet = query_evidence(database, batch, 'bundle', bundle_id=identifier)[0]
                    bundle_record = dict(bundle_id=identifier, entity_id=packet['entity_id'], status='deferred',
                                         error_code=None, windows_assigned=0, assessments=[])
                    summary['bundles_selected'] += 1
                    try:
                        manifest = load_manifest(database, batch, identifier, budget.max_manifest_windows)
                    except ValueError as error:
                        bundle_record['error_code'] = 'manifest_budget' if str(error) == 'manifest_budget' else 'invalid_manifest'
                        summary['bundles_deferred'] += 1
                        write('bundles.jsonl', bundle_record)
                        continue
                    local_context = dict(context or {})
                    if state_explanation:
                        explanation_run, explanation = explain_state(backend, database, batch, packet, manifest, budget, local_context)
                        role_write(explanation_run)
                        if explanation is not None: local_context['state_explanation'] = explanation
                    session = role_session(database, batch, packet, local_context)
                    payload = dict(bundle=packet, manifest=manifest)
                    if context is not None or state_explanation: payload['context'] = local_context
                    run = run_role(backend, session, 'confirmation', identifier, payload, budget)
                    assessments = None
                    if run['status'] == 'completed':
                        try:
                            assessments = validate_confirmation(run['output'], manifest, session.seen)
                        except (ValueError, KeyError, TypeError):
                            run.update(status='deferred', error_code='invalid_role_contract', output=None)
                    role_write(run)
                    if assessments is None:
                        bundle_record['error_code'] = run['error_code']
                        summary['bundles_deferred'] += 1
                        write('bundles.jsonl', bundle_record)
                        continue
                    bundle_record.update(status='assessed', windows_assigned=len(manifest), assessments=[
                        dict(event_id=a['event_id'], decision=a['decision'], window_ids=a['window_ids']) for a in assessments])
                    for event in assessments:
                        summary['assessments_' + event['decision']] += 1
                        if event['decision'] != 'confirmed':
                            write('diagnoses.jsonl', dict(batch=batch, bundle_id=identifier, event=event, accepted=False,
                                  status=event['decision'], simulated=summary['simulated']))
                            continue
                        item = diagnose_case(backend, database, batch, packet, event, budget,
                            local_context if context is not None or state_explanation else None, diagnostic_mode, review_mode)
                        for role in ('localization', 'classification', 'joint', 'review'):
                            if role in item: role_write(item[role]['run'])
                        item['simulated'] = summary['simulated']
                        summary['diagnoses_' + item['status']] += 1
                        write('diagnoses.jsonl', item)
                        record = experiment_prediction_for(item) if summary['experimental'] else prediction_for(item)
                        if record is not None:
                            if record['prediction_id'] in prediction_ids:
                                raise ValueError('duplicate prediction_id')
                            prediction_ids.add(record['prediction_id'])
                            write('predictions.jsonl', record); summary['predictions'] += 1
                    write('bundles.jsonl', bundle_record)
        if sha256(database) != before:
            raise ValueError('evidence changed during run; no artifacts published')
        summary['unselected_bundles'] = available - summary['bundles_selected']
        summary['selection_complete'] = summary['unselected_bundles'] == 0
        summary['diagnosis_complete'] = summary['selection_complete'] and not any(summary[key] for key in (
            'bundles_deferred', 'assessments_deferred', 'diagnoses_deferred'))
        summary['usage_complete'] = summary['backend_calls'] == summary['usage_reported_calls']
        summary['artifact_sha256'] = {name: sha256(scratch / name) for name in ARTIFACTS[:-1]}
        (scratch / 'summary.json').write_text(dumps(summary) + '\n', encoding='utf-8')
        _publish(scratch, output)
    return summary
