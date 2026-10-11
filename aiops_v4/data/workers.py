"""Bounded deterministic process scheduling and cooperative cancellation."""
from collections import deque
_STOP=None


class WorkerCancelled(RuntimeError):pass


def initialize(stop):
    global _STOP
    _STOP=stop


def cancelled():return _STOP is not None and _STOP.is_set()


def check_cancel():
    if cancelled():raise WorkerCancelled('data build cancelled; committed checkpoints retained')


def bounded_results(pool,function,tasks,limit):
    tasks=iter(tasks);pending=deque()
    for _ in range(limit):
        task=next(tasks,None)
        if task is None:break
        pending.append((task,pool.submit(function,task)))
    while pending:
        task,future=pending.popleft();result=future.result()
        following=next(tasks,None)
        if following is not None:pending.append((following,pool.submit(function,following)))
        yield task,result
