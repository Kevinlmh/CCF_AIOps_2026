"""Essential scan quality, without raw-field distributions or cardinality sketches."""
from collections import Counter
from aiops_v4.data.reader import is_missing


class EssentialProfile:
    def __init__(self,saved=None):
        saved=saved or {}
        self.accepted=saved.get('accepted',0);self.rejected=saved.get('rejected',0)
        self.flags=Counter(saved.get('flags',{}));self.missing=Counter(saved.get('missing',{}))
        self.start=saved.get('start');self.end=saved.get('end')

    def add(self,row):
        self.accepted+=1;self.flags.update(row['flags'])
        stamp=row['timestamp'];self.start=min(self.start,stamp) if self.start else stamp;self.end=max(self.end,stamp) if self.end else stamp
        self.missing.update(k for k,v in row['raw_values'].items() if v is None or (isinstance(v,str) and is_missing(v)))

    def report(self):
        return dict(profile_policy='essential',accepted=self.accepted,rejected=self.rejected,flags=dict(self.flags),
                    missing=dict(self.missing),start=self.start,end=self.end,raw_field_distributions='not_computed',
                    raw_field_cardinality='not_computed',recent_duplicate_diagnostics='not_computed')
