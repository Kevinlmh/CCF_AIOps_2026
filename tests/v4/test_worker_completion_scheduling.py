"""Real bounded workers replenish completed slots while preserving ordered callers."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from aiops_v4.data.workers import bounded_results


def test_completion_driven_scheduler_refills_fast_slot_while_first_task_is_blocked():
    release=Event();first_started=Event();lock=Lock();active=[0];peak=[0]
    def work(name):
        with lock:
            active[0]+=1;peak[0]=max(peak[0],active[0])
        try:
            if name=='slow':
                first_started.set()
                assert release.wait(2), 'completed slot was not replenished'
            elif name=='fast':
                assert first_started.wait(2)
            else:
                release.set()
            return name.upper()
        finally:
            with lock:active[0]-=1
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=bounded_results(pool,work,['slow','fast','release'],2,ordered=False)
            assert next(results)==('fast','FAST')
            assert sorted(results)==[('release','RELEASE'),('slow','SLOW')]
        assert peak[0]<=2
    finally:release.set()


def test_default_scheduler_emits_submission_order_even_if_workers_finish_in_reverse():
    release=Event()
    def work(name):
        if name=='slow':assert release.wait(2)
        else:release.set()
        return name.upper()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(bounded_results(pool,work,['slow','fast','later'],2))==[
            ('slow','SLOW'),('fast','FAST'),('later','LATER')]


@pytest.mark.parametrize('limit',[0,-1,True])
def test_invalid_scheduler_bound_is_rejected(limit):
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError,match='limit'):
            list(bounded_results(pool,lambda value:value,[1],limit))
