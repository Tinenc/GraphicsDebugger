"""GUI-owned single-flight queue with latest pending value per reflected field."""

import collections
import threading


class MaterialEditQueue(object):
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.pending = collections.OrderedDict()
        self.inflight = None
        self.epoch = 0
        self.completed = 0

    @property
    def busy(self):
        return self.inflight is not None or bool(self.pending)

    def submit(self, key, job, observer, owner):
        self.pending[key] = {"job": job, "observer": observer, "owner": owner,
                             "epoch": self.epoch, "cancel": threading.Event()}
        self._pump()

    def remove_pending(self, predicate):
        for key in list(self.pending):
            if predicate(self.pending[key]["job"]):
                del self.pending[key]

    def cancel_owner(self, owner):
        for key in list(self.pending):
            if self.pending[key]["owner"] is owner:
                del self.pending[key]
        if self.inflight is not None and self.inflight["owner"] is owner:
            self.inflight["cancel"].set()

    def reset(self):
        self.epoch += 1
        self.pending.clear()
        if self.inflight is not None:
            self.inflight["cancel"].set()
            # CloseThread may discard a native callback altogether. Detach its
            # epoch now; a late completion cannot release a newer in-flight job.
            self.inflight = None

    def _pump(self):
        if self.inflight is not None or not self.pending:
            return
        unused_key, item = self.pending.popitem(last=False)
        self.inflight = item

        def finished(result=None, error=None):
            if self.inflight is not item:
                return
            self.inflight = None
            self.completed += 1
            try:
                if item["epoch"] == self.epoch and not item["cancel"].is_set():
                    item["observer"](result, error)
            finally:
                self._pump()

        try:
            self.dispatch(item["job"], item["cancel"], finished)
        except Exception as exc:
            finished(None, exc)
