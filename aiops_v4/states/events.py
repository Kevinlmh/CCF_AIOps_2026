"""Merge only observed, adjacent triggered windows within one native series."""
from collections import Counter
import hashlib


class EventBuilder:
    def __init__(self):
        self.current = None
        self.reference_ids = set()

    def finish(self):
        if self.current is None:
            return []
        event, self.current = self.current, None
        self.reference_ids.clear()
        return [event]

    def push(self, row, state):
        completed = []
        if self.current is not None and (not state['candidate'] or self.current['end_time'] != row['start_time'] or self.current['group_id'] != row['group_id']):
            completed = self.finish()
        if not state['candidate']:
            return completed
        if self.current is None:
            key = row['group_id'] + '|' + row['start_time'] + '|' + row['vector_id']
            self.current = {key: row[key] for key in ('batch', 'group_id', 'source', 'view', 'identity', 'dimensions', 'start_time', 'end_time')}
            self.current.update(schema_version=1, event_id=hashlib.sha256(key.encode()).hexdigest(),
                                needs_confirmation=True, window_count=0, window_ids=[], window_ids_truncated=False,
                                references=[], references_truncated=False, trigger_counts={},
                                peak_stat_score=None, peak_cluster_distance=None)
        event = self.current
        event['end_time'] = row['end_time']
        event['window_count'] += 1
        if len(event['window_ids']) < 64:
            event['window_ids'].append(row['vector_id'])
        else:
            event['window_ids_truncated'] = True
        counts = Counter(event['trigger_counts'])
        counts.update(state['triggers'])
        event['trigger_counts'] = dict(sorted(counts.items()))
        for key, field in [('peak_stat_score', 'stat_score'), ('peak_cluster_distance', 'cluster_distance')]:
            value = state[field]
            if value is not None and (event[key] is None or value > event[key]):
                event[key] = value
        event['references_truncated'] |= row['references_truncated']
        for ref in row['references']:
            if ref['record_id'] in self.reference_ids:
                continue
            if len(event['references']) < 16:
                event['references'].append(ref)
                self.reference_ids.add(ref['record_id'])
            else:
                event['references_truncated'] = True
        return completed
