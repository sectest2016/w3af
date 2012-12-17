'''
base_consumer.py

Copyright 2012 Andres Riancho

This file is part of w3af, w3af.sourceforge.net .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

'''
import Queue
import sys

from multiprocessing.dummy import Process

from core.controllers.exception_handling.helpers import pprint_plugins
from core.controllers.core_helpers.exception_handler import ExceptionData
from core.controllers.core_helpers.status import w3af_core_status
from core.controllers.core_helpers.consumers.constants import POISON_PILL
from core.controllers.threads.threadpool import Pool


class BaseConsumer(Process):
    '''
    Consumer thread that takes fuzzable requests from a Queue that's populated
    by the crawl plugins and identified vulnerabilities by performing various
    requests.
    '''

    def __init__(self, consumer_plugins, w3af_core, thread_name='Consumer'):
        '''
        @param in_queue: The input queue that will feed the base_consumer plugins
        @param base_consumer_plugins: Instances of base_consumer plugins in a list
        @param w3af_core: The w3af core that we'll use for status reporting
        '''
        super(BaseConsumer, self).__init__()

        self.in_queue = Queue.Queue()
        # See documentation in the property below
        self._out_queue = Queue.Queue()
        self._consumer_plugins = consumer_plugins
        self._w3af_core = w3af_core
        self._threadpool = Pool(10, worker_names=thread_name)
        self._tasks_in_progress_counter = 0

    def run(self):
        '''
        Consume the queue items, sending them to the plugins which are then going
        to find vulnerabilities, new URLs, etc.

        TODO: Report progress to w3afCore somehow.
        '''

        while True:

            work_unit = self.in_queue.get()

            if work_unit == POISON_PILL:

                # Close the pool and wait for everyone to finish
                self._threadpool.terminate()
                self._threadpool.join()
                del self._threadpool
                
                self._teardown()

                # Finish this consumer and everyone consuming the output
                self._out_queue.put(POISON_PILL)
                self.in_queue.task_done()
                break

            else:

                self._consume(work_unit)
                self.in_queue.task_done()

    def _teardown(self):
        raise NotImplementedError

    def _consume(self, work_unit):
        raise NotImplementedError

    def _task_done(self, result):
        '''
        The task_in_progress_counter is needed because we want to know if the
        consumer is processing something and let it finish. It is mainly used
        in the has_pending_work().

        For example:

            * You can have pending work if there are items in the input_queue

            * You can have pending work if there are still items to be read from
            the output_queue by one of the consumers that reads our output.

            * You can have pending work when there are no items in input_queue
            and no items in output_queue but the threadpool inside the consumer
            is processing something. This situation is handled by the
            self._tasks_in_progress_counter attribute and the _add_task and
            _task_done methods.

        So, for each _add_task() there has to be a _task_done() even if the
        task ends in an error or exception.
        
        Recommendation: Do NOT set the callback for apply_async to call
        _task_done, the Python2.7 pool implementation won't call it if the
        function raised an exception and you'll end up with tasks in progress
        that finished with an exception.
        '''
        self._tasks_in_progress_counter -= 1
        assert self._tasks_in_progress_counter >= 0, 'You can not _task_done()' \
                                                     ' more than you _add_task().'

    def _add_task(self):
        '''
        @see: _task_done()'s documentation.
        '''
        self._tasks_in_progress_counter += 1

    def in_queue_put(self, work):
        if work is not None:
            return self.in_queue.put(work)

    def in_queue_put_iter(self, work_iter):
        if work_iter is not None:
            for work in work_iter:
                self.in_queue_put(work)

    def has_pending_work(self):
        '''
        @see: _task_done() documentation

        @return: True if the in_queue_size is != 0 OR if one of the pool workers
                 is still doing something that might impact on out_queue.
        '''
        if self.in_queue_size() > 0 \
        or self.out_queue.qsize() > 0:
            return True

        if self._tasks_in_progress_counter > 0:
            return True

        return False

    @property
    def out_queue(self):
        #
        #    This output queue can contain one of the following:
        #        * POISON_PILL
        #        * (plugin_name, fuzzable_request, AsyncResult)
        return self._out_queue

    def in_queue_size(self):
        return self.in_queue.qsize()

    def join(self):
        '''
        Poison the loop and wait for all queued work to finish this might take
        some time to process.
        '''
        self.in_queue_put(POISON_PILL)
        self.in_queue.join()

    def terminate(self):
        '''
        Remove all queued work from in_queue and poison the loop so the consumer
        exits. Should be very fast and called only if we don't care about the
        queued work anymore (ie. user clicked stop in the UI).
        '''
        while not self.in_queue.empty():
            self.in_queue.get()
            self.in_queue.task_done()

        self.in_queue_put(POISON_PILL)
        self.in_queue.join()

    def get_result(self, timeout=0.5):
        '''
        @return: The first result from the output Queue.
        '''
        return self._out_queue.get(timeout=timeout)

    def handle_exception(self, phase, plugin_name, fuzzable_request, _exception):
        '''
        Get the exception information, and put it into the output queue
        then, the strategy will get the items from the output queue and
        handle the exceptions.

        @param plugin_name: The plugin that generated the exception
        @param fuzzable_request: The fuzzable request that was sent as input to
                                 the plugin when the exception was raised
        @param _exception: The exception object
        '''
        except_type, except_class, tb = sys.exc_info()
        enabled_plugins = pprint_plugins(self._w3af_core)

        status = w3af_core_status()
        status.set_running_plugin(plugin_name, log=False)
        status.set_phase(phase)
        status.set_current_fuzzable_request(fuzzable_request)

        exception_data = ExceptionData(status, _exception, tb,
                                       enabled_plugins)
        self._out_queue.put(exception_data)
