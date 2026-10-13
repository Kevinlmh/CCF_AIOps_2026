"""Bounded deterministic process scheduling and cooperative cancellation."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
_STOP=None


class WorkerCancelled(RuntimeError):pass


def initialize(stop):
    global _STOP
    _STOP=stop


def cancelled():return _STOP is not None and _STOP.is_set()


def check_cancel():
    if cancelled():raise WorkerCancelled('data build cancelled; committed checkpoints retained')


def bounded_results(pool,function,tasks,limit,*,ordered=True):
    """Keep at most limit futures; optionally replenish whichever file finishes.

    Ordered consumers keep submission order. Completion-driven file parsing can
    opt out while its caller restores discovery order for manifests and merges.
    """
    if type(limit) is not int or limit < 1:raise ValueError('positive integer limit required')
    if type(ordered) is not bool:raise ValueError('ordered must be boolean')
    tasks=iter(tasks);sentinel=object()
    if ordered:
        pending=deque()
        for _ in range(limit):
            task=next(tasks,sentinel)
            if task is sentinel:break
            pending.append((task,pool.submit(function,task)))
        while pending:
            task,future=pending.popleft();result=future.result()
            following=next(tasks,sentinel)
            if following is not sentinel:pending.append((following,pool.submit(function,following)))
            yield task,result
    else:
        pending={};position=0
        def submit():
            nonlocal position
            task=next(tasks,sentinel)
            if task is sentinel:return
            pending[pool.submit(function,task)]=(position,task);position+=1
        for _ in range(limit):submit()
        while pending:
            ready,_=wait(pending,return_when=FIRST_COMPLETED)
            # Simultaneously ready tasks use their submission order for ties.
            for future in sorted(ready,key=lambda value:pending[value][0]):
                _,task=pending.pop(future);result=future.result()
                submit()
                yield task,result
