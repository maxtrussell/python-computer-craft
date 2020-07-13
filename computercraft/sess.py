import asyncio
import string
import sys
from code import InteractiveConsole
from contextlib import contextmanager
from functools import partial
from importlib import import_module
from importlib.abc import MetaPathFinder, Loader
from importlib.machinery import ModuleSpec
from itertools import count
from traceback import format_exc
from types import ModuleType

from greenlet import greenlet, getcurrent as get_current_greenlet

from .lua import lua_string, lua_call, return_lua_call
from . import rproc


__all__ = (
    'CCSession',
    'get_current_session',
    'eval_lua',
    'lua_context_object',
)


def debug(*args):
    sys.__stdout__.write(' '.join(map(str, args)) + '\n')
    sys.__stdout__.flush()


DIGITS = string.digits + string.ascii_lowercase


def base36(n):
    r = ''
    while n:
        r += DIGITS[n % 36]
        n //= 36
    return r[::-1]


def _is_global_greenlet():
    return not hasattr(get_current_greenlet(), 'cc_greenlet')


def get_current_session():
    try:
        return get_current_greenlet().cc_greenlet._sess
    except AttributeError:
        raise RuntimeError('Computercraft function was called outside context')


class StdFileProxy:
    def __init__(self, native):
        self._native = native

    def read(self, size=-1):
        if _is_global_greenlet():
            return self._native.read(size)
        else:
            raise RuntimeError(
                "Computercraft environment doesn't support stdin read method")

    def readline(self, size=-1):
        if _is_global_greenlet():
            return self._native.readline(size)
        else:
            if size is not None and size >= 0:
                raise RuntimeError(
                    "Computercraft environment doesn't support "
                    "stdin readline method with parameter")
            return rproc.string(eval_lua(
                return_lua_call('io.read')
            )) + '\n'

    def write(self, s):
        if _is_global_greenlet():
            return self._native.write(s)
        else:
            return rproc.nil(eval_lua(
                lua_call('io.write', s)
            ))

    def fileno(self):
        if _is_global_greenlet():
            return self._native.fileno()
        else:
            # preventing use of gnu readline here
            # https://github.com/python/cpython/blob/master/Python/bltinmodule.c#L1970
            raise AttributeError

    def __getattr__(self, name):
        return getattr(self._native, name)


class ComputerCraftFinder(MetaPathFinder):
    @staticmethod
    def find_spec(fullname, path, target=None):
        if fullname == 'cc':
            return ModuleSpec(fullname, ComputerCraftLoader, is_package=True)
        if fullname.startswith('cc.'):
            return ModuleSpec(fullname, ComputerCraftLoader, is_package=False)


class ComputerCraftLoader(Loader):
    @staticmethod
    def create_module(spec):
        sn = spec.name.split('.', 1)
        assert sn[0] == 'cc'
        if len(sn) == 1:
            sn.append('_pkg')
        rawmod = import_module('.' + sn[1], 'computercraft.subapis')
        mod = ModuleType(spec.name)
        for k in rawmod.__all__:
            setattr(mod, k, getattr(rawmod, k))
        return mod

    @staticmethod
    def exec_module(module):
        pass


def install_import_hook():
    import sys
    sys.meta_path.append(ComputerCraftFinder)


install_import_hook()
sys.stdin = StdFileProxy(sys.__stdin__)
sys.stdout = StdFileProxy(sys.__stdout__)
sys.stderr = StdFileProxy(sys.__stderr__)


def eval_lua(lua_code, immediate=False):
    result = get_current_session()._server_greenlet.switch({
        'code': lua_code,
        'immediate': immediate,
    })
    # do not uncomment this, or use sys.__stdout__.write
    # print('{} → {}'.format(lua_code, repr(result)))
    if not immediate:
        result = rproc.coro(result)
    return result


@contextmanager
def lua_context_object(create_expr: str, finalizer_template: str = ''):
    sess = get_current_session()
    fid = sess.create_task_id()
    var = 'temp[{}]'.format(lua_string(fid))
    eval_lua('{} = {}'.format(var, create_expr))
    try:
        yield var
    finally:
        finalizer_template += '; {e} = nil'
        finalizer_template = finalizer_template.lstrip(' ;')
        eval_lua(finalizer_template.format(e=var))


def eval_lua_method_factory(obj):
    def method(name, *params):
        return eval_lua(return_lua_call(obj + name, *params))
    return method


class CCGreenlet:
    def __init__(self, body_fn, sess=None):
        if sess is None:
            self._sess = get_current_session()
        else:
            self._sess = sess

        self._task_id = self._sess.create_task_id()
        self._sess._greenlets[self._task_id] = self

        parent_g = get_current_greenlet()
        if parent_g is self._sess._server_greenlet:
            self._parent = None
            debug(self._task_id, 'G.__init__', 'parent None')
        else:
            self._parent = parent_g.cc_greenlet
            self._parent._children.add(self._task_id)
            debug(self._task_id, 'G.__init__', 'parent', self._parent._task_id)

        self._children = set()
        self._g = greenlet(body_fn)
        self._g.cc_greenlet = self

    def detach_children(self):
        if self._children:
            debug(self._task_id, 'G.detach_children', self._children)
            ch = list(self._children)
            self._children.clear()
            self._sess.drop(ch)

    def _on_death(self, error=None):
        debug(self._task_id, 'G._on_death', error)
        self._sess._greenlets.pop(self._task_id, None)
        self.detach_children()
        if error is not None:
            if error is True:
                error = {}
            self._sess._sender({'action': 'close', **error})
        if self._parent is not None:
            self._parent._children.discard(self._task_id)

    def defer_switch(self, *args, **kwargs):
        asyncio.get_running_loop().call_soon(
            partial(self.switch, *args, **kwargs))

    def switch(self, *args, **kwargs):
        # switch must be called from server greenlet
        assert get_current_greenlet() is self._sess._server_greenlet
        debug(self._task_id, 'G.switch', args, kwargs)
        try:
            task = self._g.switch(*args, **kwargs)
        except SystemExit:
            debug(self._task_id, 'G.switch: ', 'SystemExit')
            self._on_death(True)
            return
        except Exception as e:
            debug(self._task_id, 'G.switch: ', 'Exception', e, type(e), format_exc(limit=None, chain=False))
            self._on_death({'error': format_exc(limit=None, chain=False)})
            return

        debug(self._task_id, 'G.switch: result', repr(task))

        # lua_eval call or simply idle
        if isinstance(task, dict):
            x = self
            while x._g.dead:
                x = x._parent
            tid = x._task_id
            debug(self._task_id, 'G.switch: start task for', tid)
            self._sess._sender({
                'action': 'task',
                'task_id': tid,
                **task,
            })
        else:
            debug(self._task_id, f'G.switch: {"dead" if self._g.dead else "idle"}')

        if self._g.dead:
            if self._parent is None:
                self._on_death(True)
            else:
                self._on_death()


class CCSession:
    def __init__(self, computer_id, sender):
        # computer_id is unique identifier of a CCSession
        self._computer_id = computer_id
        self._tid_allocator = map(base36, count(start=1))
        self._sender = sender
        self._greenlets = {}
        self._server_greenlet = get_current_greenlet()
        self._program_greenlet = None

    def on_task_result(self, task_id, result):
        assert get_current_greenlet() is self._server_greenlet
        if task_id not in self._greenlets:
            # ignore for dropped tasks
            return
        debug('on_task_result', task_id, result)
        self._greenlets[task_id].switch(result)

    def create_task_id(self):
        return next(self._tid_allocator)

    def drop(self, task_ids):
        def collect(task_id):
            yield task_id
            g = self._greenlets.pop(task_id)
            for tid in g._children:
                yield from collect(tid)

        all_tids = []
        for task_id in task_ids:
            all_tids.extend(collect(task_id))

        debug('Sess.drop', all_tids)

        self._sender({
            'action': 'drop',
            'task_ids': all_tids,
        })

    def _run_sandboxed_greenlet(self, fn):
        self._program_greenlet = CCGreenlet(fn, sess=self)
        self._program_greenlet.switch()

    def run_program(self, program):
        def _run_program():
            p, code = eval_lua('''
local p = fs.combine(shell.dir(), {})
if not fs.exists(p) then return nil end
if fs.isDir(p) then return nil end
local f = fs.open(p, 'r')
local code = f.readAll()
f.close()
return p, code
'''.lstrip().format(lua_string(program)))
            cc = compile(code, p, 'exec')
            exec(cc, {'__file__': p})

        self._run_sandboxed_greenlet(_run_program)

    def run_repl(self):
        def _repl():
            InteractiveConsole(locals={}).interact()

        self._run_sandboxed_greenlet(_repl)
