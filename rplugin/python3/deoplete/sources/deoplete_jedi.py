import os
import re
import sys
import json
import time
import queue

sys.path.insert(1, os.path.dirname(__file__))

from deoplete_jedi import cache, worker, profiler
from deoplete.sources.base import Base


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)
        self.name = 'jedi'
        self.mark = '[jedi]'
        self.rank = 500
        self.filetypes = ['python']
        self.input_pattern = (r'[^. \t0-9]\.\w*$|'
                              r'^\s*@\w*$|'
                              r'^\s*from\s.+import \w*|'
                              r'^\s*from \w*|'
                              r'^\s*import \w*')

        self.debug_enabled = \
            self.vim.vars['deoplete#sources#jedi#debug_enabled']

        self.description_length = \
            self.vim.vars['deoplete#sources#jedi#statement_length']

        self.use_short_types = \
            self.vim.vars['deoplete#sources#jedi#short_types']

        self.show_docstring = \
            self.vim.vars['deoplete#sources#jedi#show_docstring']

        self.complete_min_length = \
            self.vim.vars['deoplete#auto_complete_start_length']

        self.worker_threads = \
            self.vim.vars['deoplete#sources#jedi#worker_threads']

        self.workers_started = False
        self.boilerplate = []  # Completions that are included in all results

    def get_complete_position(self, context):
        m = re.search(r'\w*$', context['input'])
        return m.start() if m else -1

    def mix_boilerplate(self, completions):
        seen = set()
        for item in sorted(self.boilerplate + completions, key=lambda x: x['word'].lower()):
            if item['word'] in seen:
                continue
            yield item

    def process_result_queue(self):
        """Process completion results

        This should be called before new completions begin.
        """
        while True:
            try:
                compl = worker.comp_queue.get(block=False, timeout=0.05)
                cache_key = compl.get('cache_key')
                cached = cache.retrieve(cache_key)
                # Ensure that the incoming completion is actually newer than
                # the current one.
                if cached is None or cached.time <= compl.get('time'):
                    cache.store(cache_key, compl)
            except queue.Empty:
                break

    @profiler.profile
    def gather_candidates(self, context):
        if not self.workers_started:
            cache.start_reaper()
            worker.start(max(1, self.worker_threads), self.description_length,
                         self.use_short_types, self.show_docstring,
                         self.debug_enabled)
            self.workers_started = True

        if not self.boilerplate:
            bp = cache.retrieve(('boilerplate~',))
            if bp:
                self.boilerplate = bp.completions[:]
            else:
                # This should be the first time any completion happened, so
                # `wait` will be True.
                worker.work_queue.put((('boilerplate~',), [], '', 1, 0, ''))

        self.process_result_queue()

        line = context['position'][1]
        col = context['complete_position']
        buf = self.vim.current.buffer
        src = buf[:]

        extra_modules = []
        cache_key = None
        cached = None
        refresh = True
        wait = False

        # Inclusion filters for the results
        filters = []

        if re.match('^\s*(from|import)\s+', context['input']) \
                and not re.match('^\s*from\s+\S+\s+', context['input']):
            # If starting an import, only show module results
            filters.append('module')

        cache_key, extra_modules = cache.cache_context(buf.name, context, src)
        cached = cache.retrieve(cache_key)
        if cached:
            modules = cached.modules
            if all([filename in modules for filename in extra_modules]) \
                    and all([int(os.path.getmtime(filename)) == mtime
                             for filename, mtime in modules.items()]):
                # The cache is still valid
                refresh = False

        if cache_key and (cache_key[-1] in ('vars', 'import~') or
                          (cached and len(cache_key) == 1 and
                           not len(cached.modules))):
            # Always refresh scoped variables and module imports.  Additionally
            # refresh cached items that did not have associated module files.
            refresh = True

        if cached is None:
            wait = True

        self.debug('Key: %r, Refresh: %r, Wait: %r', cache_key, refresh, wait)
        if cache_key and (not cached or refresh):
            n = time.time()
            worker.work_queue.put((cache_key, extra_modules, '\n'.join(src),
                                   line, col, str(buf.name)))
            while wait and time.time() - n < 2:
                self.process_result_queue()
                cached = cache.retrieve(cache_key)
                if cached and cached.time >= n:
                    break
                time.sleep(0.01)

        if cached:
            cached.touch()
            if cached.completions is None:
                return list(self.mix_boilerplate([]))

            if cache_key[-1] == 'vars':
                out = self.mix_boilerplate(cached.completions)
            else:
                out = cached.completions

            if filters:
                return [x for x in out if x['$type'] in filters]
            return list(out)
        return []
