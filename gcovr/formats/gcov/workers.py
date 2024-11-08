# -*- coding:utf-8 -*-

#  ************************** Copyrights and license ***************************
#
# This file is part of gcovr 8.2+main, a parsing and reporting tool for gcov.
# https://gcovr.com/en/main
#
# _____________________________________________________________________________
#
# Copyright (c) 2013-2024 the gcovr authors
# Copyright (c) 2013 Sandia Corporation.
# Under the terms of Contract DE-AC04-94AL85000 with Sandia Corporation,
# the U.S. Government retains certain rights in this software.
#
# This software is distributed under the 3-clause BSD License.
# For more information, see the README.rst file.
#
# ****************************************************************************

import logging
from sys import exc_info
from threading import Thread, Condition, RLock
from traceback import format_exception
from contextlib import contextmanager
from queue import Queue, Empty
from typing import Any, Callable, Dict, List

LOGGER = logging.getLogger("gcovr")


class LockedDirectories:
    """
    Class that keeps a list of locked directories
    """

    def __init__(self):
        self.dirs = set()
        self.cv = Condition()

    def run_in(self, dir_):
        """
        Start running in the directory and lock it
        """
        self.cv.acquire()
        while dir_ in self.dirs:
            self.cv.wait()
        self.dirs.add(dir_)
        self.cv.release()

    def done(self, dir_):
        """
        Finished with the directory, unlock it
        """
        self.cv.acquire()
        self.dirs.remove(dir_)
        self.cv.notify_all()
        self.cv.release()


locked_directory_global_object = LockedDirectories()


@contextmanager
def locked_directory(dir_):
    """
    Context for doing something in a locked directory
    """
    locked_directory_global_object.run_in(dir_)
    try:
        yield
    finally:
        locked_directory_global_object.done(dir_)


def worker(queue: Queue, context: Callable[[], Dict[str, Any]], pool: "Workers"):
    """
    Run work items from the queue until the sentinel
    None value is hit
    """
    while True:
        work, args, kwargs = queue.get(True)
        if not work:
            break
        kwargs.update(context)
        try:
            work(*args, **kwargs)
        except:  # noqa: E722 # pylint: disable=bare-except
            pool.stop_with_exception()
            break


class Workers:
    """
    Create a thread-pool which can be given work via an
    add method and will run until work is complete
    """

    def __init__(self, number: int, context: Callable[[], Dict[str, Any]]):
        if number < 1:
            raise AssertionError("At least one executer is needed.")
        self.q: Queue = Queue()
        self.lock = RLock()
        self.exceptions: List[str] = []
        self.contexts = [context() for _ in range(0, number)]
        self.workers = [
            Thread(target=worker, args=(self.q, c, self)) for c in self.contexts
        ]
        for w in self.workers:
            w.start()

    def add(self, work, *args, **kwargs):
        """
        Add in a method and the arguments to be used
        when running it
        """
        with self.lock:
            # Do not push additional items if there is already an exception
            if self.exceptions:  # pragma: no cover
                return
            self.q.put((work, args, kwargs))

    def add_sentinels(self):
        """
        Add the sentinels to the end of the queue so
        the threads know to stop
        """
        with self.lock:
            for _ in self.workers:
                self.q.put((None, [], {}))

    def drain(self):
        """
        Drain the queue
        """
        with self.lock:
            while True:
                try:
                    self.q.get(False)
                except Empty:
                    break
            self.add_sentinels()

    def stop_with_exception(self):
        """
        A thread has failed and needs to raise an exception.
        """
        with self.lock:
            self.drain()
            self.exceptions.append("".join(format_exception(*exc_info())))

    def size(self):
        """
        Run the size of the thread pool
        """
        return len(self.workers)

    def wait(self):
        """
        Wait until all work is complete
        """
        self.add_sentinels()
        for w in self.workers:
            # Allow interrupts in Thread.join
            while w.is_alive():
                w.join(timeout=1)
        self.workers = None

        for traceback in self.exceptions:
            LOGGER.error(traceback)

        if self.exceptions:
            raise RuntimeError(
                "Worker thread raised exception, workers canceled."
            ) from None
        return self.contexts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.workers is not None:
            raise AssertionError(
                "Sanity check, you must call wait on the contextmanager to get the context of the workers."
            )
