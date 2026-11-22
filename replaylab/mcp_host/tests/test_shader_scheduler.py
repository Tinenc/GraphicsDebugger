import threading
import time
from contextvars import ContextVar
from src.shader_scheduler import ScheduledTask, is_current_task


def test_many_waiting_jobs_share_bounded_threads_and_stop_cleanly():
    stopped=threading.Event()
    ticks=set()
    def body(index):
        while not stopped.is_set():
            ticks.add(index)
            yield 0.01
    jobs=[ScheduledTask(lambda i=i:body(i),name='test-'+str(i),lane='watch') for i in range(80)]
    for job in jobs: job.start()
    try:
        deadline=time.monotonic()+3
        while len(ticks)<80 and time.monotonic()<deadline: time.sleep(0.01)
        assert len(ticks)==80
        workers=[t for t in threading.enumerate() if t.name.startswith('shader-shared-watch')]
        assert len(workers)<=4
    finally:
        stopped.set()
        for job in jobs: job.join(2)
    assert not any(job.is_alive() for job in jobs)


def test_pool_worker_does_not_confer_another_jobs_apply_ownership_or_context():
    marker=ContextVar('shader_test',default=None)
    results=[]
    handles=[]
    def body(index):
        assert is_current_task(handles[index])
        assert not is_current_task(handles[1-index])
        assert marker.get() is None
        marker.set(index)
        yield 0.01
        assert marker.get() is None
        results.append(index)
    handles.extend(ScheduledTask(lambda i=i:body(i),name='owned-'+str(i),lane='apply') for i in range(2))
    for handle in handles: handle.start()
    for handle in handles: handle.join(2)
    assert sorted(results)==[0,1]
