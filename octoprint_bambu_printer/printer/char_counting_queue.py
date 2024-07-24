import queue
import time


class CharCountingQueue(queue.Queue):
    def __init__(self, maxsize, name=None):
        queue.Queue.__init__(self, maxsize=maxsize)
        self._size = 0
        self._name = name

    def clear(self):
        with self.mutex:
            self.queue.clear()

    def put(self, item, block=True, timeout=None, partial=False) -> int:
        self.not_full.acquire()

        try:
            if not self._will_it_fit(item) and partial:
                space_left = self.maxsize - self._qsize()
                if space_left:
                    item = item[:space_left]

            if not block:
                if not self._will_it_fit(item):
                    raise queue.Full
            elif timeout is None:
                while not self._will_it_fit(item):
                    self.not_full.wait()
            elif timeout < 0:
                raise ValueError("'timeout' must be a positive number")
            else:
                endtime = time.monotonic() + timeout
                while not self._will_it_fit(item):
                    remaining = endtime - time.monotonic()
                    if remaining <= 0:
                        raise queue.Full
                    self.not_full.wait(remaining)

            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

            return self._len(item)
        finally:
            self.not_full.release()

    # noinspection PyMethodMayBeStatic
    def _len(self, item):
        return len(item)

    def _qsize(self, l=len):  # noqa: E741
        return self._size

    # Put a new item in the queue
    def _put(self, item):
        self.queue.append(item)
        self._size += self._len(item)

    # Get an item from the queue
    def _get(self):
        item = self.queue.popleft()
        self._size -= self._len(item)
        return item

    def _will_it_fit(self, item):
        return self.maxsize - self._qsize() >= self._len(item)
