# util.py - Mercurial utility functions and platform specific implementations
#
#  Copyright 2005 K. Thananchayan <thananck@yahoo.com>
#  Copyright 2005-2007 Matt Mackall <mpm@selenic.com>
#  Copyright 2006 Vadim Gelfer <vadim.gelfer@gmail.com>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

"""Mercurial utility functions and platform specific implementations.

This contains helper routines that are independent of the SCM core and
hide platform-specific details from the core.
"""

import i18n
_ = i18n._
import error, osutil, encoding, parsers
import errno, shutil, sys, tempfile, traceback
import re as remod
import os, time, datetime, calendar, textwrap, signal, collections
import stat
import imp, socket, urllib
import gc
import bz2
import zlib

if os.name == 'nt':
    import windows as platform
else:
    import posix as platform

cachestat = platform.cachestat
checkexec = platform.checkexec
checklink = platform.checklink
copymode = platform.copymode
executablepath = platform.executablepath
expandglobs = platform.expandglobs
explainexit = platform.explainexit
findexe = platform.findexe
gethgcmd = platform.gethgcmd
getuser = platform.getuser
groupmembers = platform.groupmembers
groupname = platform.groupname
hidewindow = platform.hidewindow
isexec = platform.isexec
isowner = platform.isowner
localpath = platform.localpath
lookupreg = platform.lookupreg
makedir = platform.makedir
nlinks = platform.nlinks
normpath = platform.normpath
normcase = platform.normcase
normcasespec = platform.normcasespec
normcasefallback = platform.normcasefallback
openhardlinks = platform.openhardlinks
oslink = platform.oslink
parsepatchoutput = platform.parsepatchoutput
pconvert = platform.pconvert
poll = platform.poll
popen = platform.popen
posixfile = platform.posixfile
quotecommand = platform.quotecommand
readpipe = platform.readpipe
rename = platform.rename
removedirs = platform.removedirs
samedevice = platform.samedevice
samefile = platform.samefile
samestat = platform.samestat
setbinary = platform.setbinary
setflags = platform.setflags
setsignalhandler = platform.setsignalhandler
shellquote = platform.shellquote
spawndetached = platform.spawndetached
split = platform.split
sshargs = platform.sshargs
statfiles = getattr(osutil, 'statfiles', platform.statfiles)
statisexec = platform.statisexec
statislink = platform.statislink
termwidth = platform.termwidth
testpid = platform.testpid
umask = platform.umask
unlink = platform.unlink
unlinkpath = platform.unlinkpath
username = platform.username

# Python compatibility

_notset = object()

def safehasattr(thing, attr):
    return getattr(thing, attr, _notset) is not _notset

from hashlib import md5, sha1

DIGESTS = {
    'md5': md5,
    'sha1': sha1,
}
# List of digest types from strongest to weakest
DIGESTS_BY_STRENGTH = ['sha1', 'md5']

try:
    import hashlib
    DIGESTS.update({
        'sha512': hashlib.sha512,
    })
    DIGESTS_BY_STRENGTH.insert(0, 'sha512')
except ImportError:
    pass

for k in DIGESTS_BY_STRENGTH:
    assert k in DIGESTS

class digester(object):
    """helper to compute digests.

    This helper can be used to compute one or more digests given their name.

    >>> d = digester(['md5', 'sha1'])
    >>> d.update('foo')
    >>> [k for k in sorted(d)]
    ['md5', 'sha1']
    >>> d['md5']
    'acbd18db4cc2f85cedef654fccc4a4d8'
    >>> d['sha1']
    '0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33'
    >>> digester.preferred(['md5', 'sha1'])
    'sha1'
    """

    def __init__(self, digests, s=''):
        self._hashes = {}
        for k in digests:
            if k not in DIGESTS:
                raise Abort(_('unknown digest type: %s') % k)
            self._hashes[k] = DIGESTS[k]()
        if s:
            self.update(s)

    def update(self, data):
        for h in self._hashes.values():
            h.update(data)

    def __getitem__(self, key):
        if key not in DIGESTS:
            raise Abort(_('unknown digest type: %s') % k)
        return self._hashes[key].hexdigest()

    def __iter__(self):
        return iter(self._hashes)

    @staticmethod
    def preferred(supported):
        """returns the strongest digest type in both supported and DIGESTS."""

        for k in DIGESTS_BY_STRENGTH:
            if k in supported:
                return k
        return None

class digestchecker(object):
    """file handle wrapper that additionally checks content against a given
    size and digests.

        d = digestchecker(fh, size, {'md5': '...'})

    When multiple digests are given, all of them are validated.
    """

    def __init__(self, fh, size, digests):
        self._fh = fh
        self._size = size
        self._got = 0
        self._digests = dict(digests)
        self._digester = digester(self._digests.keys())

    def read(self, length=-1):
        content = self._fh.read(length)
        self._digester.update(content)
        self._got += len(content)
        return content

    def validate(self):
        if self._size != self._got:
            raise Abort(_('size mismatch: expected %d, got %d') %
                (self._size, self._got))
        for k, v in self._digests.items():
            if v != self._digester[k]:
                # i18n: first parameter is a digest name
                raise Abort(_('%s mismatch: expected %s, got %s') %
                    (k, v, self._digester[k]))

try:
    buffer = buffer
except NameError:
    if sys.version_info[0] < 3:
        def buffer(sliceable, offset=0):
            return sliceable[offset:]
    else:
        def buffer(sliceable, offset=0):
            return memoryview(sliceable)[offset:]

import subprocess
closefds = os.name == 'posix'

_chunksize = 4096

class bufferedinputpipe(object):
    """a manually buffered input pipe

    Python will not let us use buffered IO and lazy reading with 'polling' at
    the same time. We cannot probe the buffer state and select will not detect
    that data are ready to read if they are already buffered.

    This class let us work around that by implementing its own buffering
    (allowing efficient readline) while offering a way to know if the buffer is
    empty from the output (allowing collaboration of the buffer with polling).

    This class lives in the 'util' module because it makes use of the 'os'
    module from the python stdlib.
    """

    def __init__(self, input):
        self._input = input
        self._buffer = []
        self._eof = False
        self._lenbuf = 0

    @property
    def hasbuffer(self):
        """True is any data is currently buffered

        This will be used externally a pre-step for polling IO. If there is
        already data then no polling should be set in place."""
        return bool(self._buffer)

    @property
    def closed(self):
        return self._input.closed

    def fileno(self):
        return self._input.fileno()

    def close(self):
        return self._input.close()

    def read(self, size):
        while (not self._eof) and (self._lenbuf < size):
            self._fillbuffer()
        return self._frombuffer(size)

    def readline(self, *args, **kwargs):
        if 1 < len(self._buffer):
            # this should not happen because both read and readline end with a
            # _frombuffer call that collapse it.
            self._buffer = [''.join(self._buffer)]
            self._lenbuf = len(self._buffer[0])
        lfi = -1
        if self._buffer:
            lfi = self._buffer[-1].find('\n')
        while (not self._eof) and lfi < 0:
            self._fillbuffer()
            if self._buffer:
                lfi = self._buffer[-1].find('\n')
        size = lfi + 1
        if lfi < 0: # end of file
            size = self._lenbuf
        elif 1 < len(self._buffer):
            # we need to take previous chunks into account
            size += self._lenbuf - len(self._buffer[-1])
        return self._frombuffer(size)

    def _frombuffer(self, size):
        """return at most 'size' data from the buffer

        The data are removed from the buffer."""
        if size == 0 or not self._buffer:
            return ''
        buf = self._buffer[0]
        if 1 < len(self._buffer):
            buf = ''.join(self._buffer)

        data = buf[:size]
        buf = buf[len(data):]
        if buf:
            self._buffer = [buf]
            self._lenbuf = len(buf)
        else:
            self._buffer = []
            self._lenbuf = 0
        return data

    def _fillbuffer(self):
        """read data to the buffer"""
        data = os.read(self._input.fileno(), _chunksize)
        if not data:
            self._eof = True
        else:
            self._lenbuf += len(data)
            self._buffer.append(data)

def popen2(cmd, env=None, newlines=False):
    # Setting bufsize to -1 lets the system decide the buffer size.
    # The default for bufsize is 0, meaning unbuffered. This leads to
    # poor performance on Mac OS X: http://bugs.python.org/issue4194
    p = subprocess.Popen(cmd, shell=True, bufsize=-1,
                         close_fds=closefds,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         universal_newlines=newlines,
                         env=env)
    return p.stdin, p.stdout

def popen3(cmd, env=None, newlines=False):
    stdin, stdout, stderr, p = popen4(cmd, env, newlines)
    return stdin, stdout, stderr

def popen4(cmd, env=None, newlines=False, bufsize=-1):
    p = subprocess.Popen(cmd, shell=True, bufsize=bufsize,
                         close_fds=closefds,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         universal_newlines=newlines,
                         env=env)
    return p.stdin, p.stdout, p.stderr, p

def version():
    """Return version information if available."""
    try:
        import __version__
        return __version__.version
    except ImportError:
        return 'unknown'

# used by parsedate
defaultdateformats = (
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %I:%M:%S%p',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d %I:%M%p',
    '%Y-%m-%d',
    '%m-%d',
    '%m/%d',
    '%m/%d/%y',
    '%m/%d/%Y',
    '%a %b %d %H:%M:%S %Y',
    '%a %b %d %I:%M:%S%p %Y',
    '%a, %d %b %Y %H:%M:%S',        #  GNU coreutils "/bin/date --rfc-2822"
    '%b %d %H:%M:%S %Y',
    '%b %d %I:%M:%S%p %Y',
    '%b %d %H:%M:%S',
    '%b %d %I:%M:%S%p',
    '%b %d %H:%M',
    '%b %d %I:%M%p',
    '%b %d %Y',
    '%b %d',
    '%H:%M:%S',
    '%I:%M:%S%p',
    '%H:%M',
    '%I:%M%p',
)

extendeddateformats = defaultdateformats + (
    "%Y",
    "%Y-%m",
    "%b",
    "%b %Y",
    )

def cachefunc(func):
    '''cache the result of function calls'''
    # XXX doesn't handle keywords args
    if func.func_code.co_argcount == 0:
        cache = []
        def f():
            if len(cache) == 0:
                cache.append(func())
            return cache[0]
        return f
    cache = {}
    if func.func_code.co_argcount == 1:
        # we gain a small amount of time because
        # we don't need to pack/unpack the list
        def f(arg):
            if arg not in cache:
                cache[arg] = func(arg)
            return cache[arg]
    else:
        def f(*args):
            if args not in cache:
                cache[args] = func(*args)
            return cache[args]

    return f

class sortdict(dict):
    '''a simple sorted dictionary'''
    def __init__(self, data=None):
        self._list = []
        if data:
            self.update(data)
    def copy(self):
        return sortdict(self)
    def __setitem__(self, key, val):
        if key in self:
            self._list.remove(key)
        self._list.append(key)
        dict.__setitem__(self, key, val)
    def __iter__(self):
        return self._list.__iter__()
    def update(self, src):
        if isinstance(src, dict):
            src = src.iteritems()
        for k, v in src:
            self[k] = v
    def clear(self):
        dict.clear(self)
        self._list = []
    def items(self):
        return [(k, self[k]) for k in self._list]
    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._list.remove(key)
    def pop(self, key, *args, **kwargs):
        dict.pop(self, key, *args, **kwargs)
        try:
            self._list.remove(key)
        except ValueError:
            pass
    def keys(self):
        return self._list
    def iterkeys(self):
        return self._list.__iter__()
    def iteritems(self):
        for k in self._list:
            yield k, self[k]
    def insert(self, index, key, val):
        self._list.insert(index, key)
        dict.__setitem__(self, key, val)

class lrucachedict(object):
    '''cache most recent gets from or sets to this dictionary'''
    def __init__(self, maxsize):
        self._cache = {}
        self._maxsize = maxsize
        self._order = collections.deque()

    def __getitem__(self, key):
        value = self._cache[key]
        self._order.remove(key)
        self._order.append(key)
        return value

    def __setitem__(self, key, value):
        if key not in self._cache:
            if len(self._cache) >= self._maxsize:
                del self._cache[self._order.popleft()]
        else:
            self._order.remove(key)
        self._cache[key] = value
        self._order.append(key)

    def __contains__(self, key):
        return key in self._cache

    def clear(self):
        self._cache.clear()
        self._order = collections.deque()

def lrucachefunc(func):
    '''cache most recent results of function calls'''
    cache = {}
    order = collections.deque()
    if func.func_code.co_argcount == 1:
        def f(arg):
            if arg not in cache:
                if len(cache) > 20:
                    del cache[order.popleft()]
                cache[arg] = func(arg)
            else:
                order.remove(arg)
            order.append(arg)
            return cache[arg]
    else:
        def f(*args):
            if args not in cache:
                if len(cache) > 20:
                    del cache[order.popleft()]
                cache[args] = func(*args)
            else:
                order.remove(args)
            order.append(args)
            return cache[args]

    return f

class propertycache(object):
    def __init__(self, func):
        self.func = func
        self.name = func.__name__
    def __get__(self, obj, type=None):
        result = self.func(obj)
        self.cachevalue(obj, result)
        return result

    def cachevalue(self, obj, value):
        # __dict__ assignment required to bypass __setattr__ (eg: repoview)
        obj.__dict__[self.name] = value

def pipefilter(s, cmd):
    '''filter string S through command CMD, returning its output'''
    p = subprocess.Popen(cmd, shell=True, close_fds=closefds,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    pout, perr = p.communicate(s)
    return pout

def tempfilter(s, cmd):
    '''filter string S through a pair of temporary files with CMD.
    CMD is used as a template to create the real command to be run,
    with the strings INFILE and OUTFILE replaced by the real names of
    the temporary files generated.'''
    inname, outname = None, None
    try:
        infd, inname = tempfile.mkstemp(prefix='hg-filter-in-')
        fp = os.fdopen(infd, 'wb')
        fp.write(s)
        fp.close()
        outfd, outname = tempfile.mkstemp(prefix='hg-filter-out-')
        os.close(outfd)
        cmd = cmd.replace('INFILE', inname)
        cmd = cmd.replace('OUTFILE', outname)
        code = os.system(cmd)
        if sys.platform == 'OpenVMS' and code & 1:
            code = 0
        if code:
            raise Abort(_("command '%s' failed: %s") %
                        (cmd, explainexit(code)))
        fp = open(outname, 'rb')
        r = fp.read()
        fp.close()
        return r
    finally:
        try:
            if inname:
                os.unlink(inname)
        except OSError:
            pass
        try:
            if outname:
                os.unlink(outname)
        except OSError:
            pass

filtertable = {
    'tempfile:': tempfilter,
    'pipe:': pipefilter,
    }

def filter(s, cmd):
    "filter a string through a command that transforms its input to its output"
    for name, fn in filtertable.iteritems():
        if cmd.startswith(name):
            return fn(s, cmd[len(name):].lstrip())
    return pipefilter(s, cmd)

def binary(s):
    """return true if a string is binary data"""
    return bool(s and '\0' in s)

def increasingchunks(source, min=1024, max=65536):
    '''return no less than min bytes per chunk while data remains,
    doubling min after each chunk until it reaches max'''
    def log2(x):
        if not x:
            return 0
        i = 0
        while x:
            x >>= 1
            i += 1
        return i - 1

    buf = []
    blen = 0
    for chunk in source:
        buf.append(chunk)
        blen += len(chunk)
        if blen >= min:
            if min < max:
                min = min << 1
                nmin = 1 << log2(blen)
                if nmin > min:
                    min = nmin
                if min > max:
                    min = max
            yield ''.join(buf)
            blen = 0
            buf = []
    if buf:
        yield ''.join(buf)

Abort = error.Abort

def always(fn):
    return True

def never(fn):
    return False

def nogc(func):
    """disable garbage collector

    Python's garbage collector triggers a GC each time a certain number of
    container objects (the number being defined by gc.get_threshold()) are
    allocated even when marked not to be tracked by the collector. Tracking has
    no effect on when GCs are triggered, only on what objects the GC looks
    into. As a workaround, disable GC while building complex (huge)
    containers.

    This garbage collector issue have been fixed in 2.7.
    """
    def wrapper(*args, **kwargs):
        gcenabled = gc.isenabled()
        gc.disable()
        try:
            return func(*args, **kwargs)
        finally:
            if gcenabled:
                gc.enable()
    return wrapper

def pathto(root, n1, n2):
    '''return the relative path from one place to another.
    root should use os.sep to separate directories
    n1 should use os.sep to separate directories
    n2 should use "/" to separate directories
    returns an os.sep-separated path.

    If n1 is a relative path, it's assumed it's
    relative to root.
    n2 should always be relative to root.
    '''
    if not n1:
        return localpath(n2)
    if os.path.isabs(n1):
        if os.path.splitdrive(root)[0] != os.path.splitdrive(n1)[0]:
            return os.path.join(root, localpath(n2))
        n2 = '/'.join((pconvert(root), n2))
    a, b = splitpath(n1), n2.split('/')
    a.reverse()
    b.reverse()
    while a and b and a[-1] == b[-1]:
        a.pop()
        b.pop()
    b.reverse()
    return os.sep.join((['..'] * len(a)) + b) or '.'

def mainfrozen():
    """return True if we are a frozen executable.

    The code supports py2exe (most common, Windows only) and tools/freeze
    (portable, not much used).
    """
    return (safehasattr(sys, "frozen") or # new py2exe
            safehasattr(sys, "importers") or # old py2exe
            imp.is_frozen("__main__")) # tools/freeze

# the location of data files matching the source code
if mainfrozen():
    # executable version (py2exe) doesn't support __file__
    datapath = os.path.dirname(sys.executable)
else:
    datapath = os.path.dirname(__file__)

i18n.setdatapath(datapath)

_hgexecutable = None

def hgexecutable():
    """return location of the 'hg' executable.

    Defaults to $HG or 'hg' in the search path.
    """
    if _hgexecutable is None:
        hg = os.environ.get('HG')
        mainmod = sys.modules['__main__']
        if hg:
            _sethgexecutable(hg)
        elif mainfrozen():
            _sethgexecutable(sys.executable)
        elif os.path.basename(getattr(mainmod, '__file__', '')) == 'hg':
            _sethgexecutable(mainmod.__file__)
        else:
            exe = findexe('hg') or os.path.basename(sys.argv[0])
            _sethgexecutable(exe)
    return _hgexecutable

def _sethgexecutable(path):
    """set location of the 'hg' executable"""
    global _hgexecutable
    _hgexecutable = path

def _isstdout(f):
    fileno = getattr(f, 'fileno', None)
    return fileno and fileno() == sys.__stdout__.fileno()

def system(cmd, environ=None, cwd=None, onerr=None, errprefix=None, out=None):
    '''enhanced shell command execution.
    run with environment maybe modified, maybe in different dir.

    if command fails and onerr is None, return status, else raise onerr
    object as exception.

    if out is specified, it is assumed to be a file-like object that has a
    write() method. stdout and stderr will be redirected to out.'''
    if environ is None:
        environ = {}
    try:
        sys.stdout.flush()
    except Exception:
        pass
    def py2shell(val):
        'convert python object into string that is useful to shell'
        if val is None or val is False:
            return '0'
        if val is True:
            return '1'
        return str(val)
    origcmd = cmd
    cmd = quotecommand(cmd)
    if sys.platform == 'plan9' and (sys.version_info[0] == 2
                                    and sys.version_info[1] < 7):
        # subprocess kludge to work around issues in half-baked Python
        # ports, notably bichued/python:
        if not cwd is None:
            os.chdir(cwd)
        rc = os.system(cmd)
    else:
        env = dict(os.environ)
        env.update((k, py2shell(v)) for k, v in environ.iteritems())
        env['HG'] = hgexecutable()
        if out is None or _isstdout(out):
            rc = subprocess.call(cmd, shell=True, close_fds=closefds,
                                 env=env, cwd=cwd)
        else:
            proc = subprocess.Popen(cmd, shell=True, close_fds=closefds,
                                    env=env, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                out.write(line)
            proc.wait()
            rc = proc.returncode
        if sys.platform == 'OpenVMS' and rc & 1:
            rc = 0
    if rc and onerr:
        errmsg = '%s %s' % (os.path.basename(origcmd.split(None, 1)[0]),
                            explainexit(rc)[0])
        if errprefix:
            errmsg = '%s: %s' % (errprefix, errmsg)
        raise onerr(errmsg)
    return rc

def checksignature(func):
    '''wrap a function with code to check for calling errors'''
    def check(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except TypeError:
            if len(traceback.extract_tb(sys.exc_info()[2])) == 1:
                raise error.SignatureError
            raise

    return check

def copyfile(src, dest, hardlink=False):
    "copy a file, preserving mode and atime/mtime"
    if os.path.lexists(dest):
        unlink(dest)
    # hardlinks are problematic on CIFS, quietly ignore this flag
    # until we find a way to work around it cleanly (issue4546)
    if False and hardlink:
        try:
            oslink(src, dest)
            return
        except (IOError, OSError):
            pass # fall back to normal copy
    if os.path.islink(src):
        os.symlink(os.readlink(src), dest)
    else:
        try:
            shutil.copyfile(src, dest)
            shutil.copymode(src, dest)
        except shutil.Error as inst:
            raise Abort(str(inst))

def copyfiles(src, dst, hardlink=None, progress=lambda t, pos: None):
    """Copy a directory tree using hardlinks if possible."""
    num = 0

    if hardlink is None:
        hardlink = (os.stat(src).st_dev ==
                    os.stat(os.path.dirname(dst)).st_dev)
    if hardlink:
        topic = _('linking')
    else:
        topic = _('copying')

    if os.path.isdir(src):
        os.mkdir(dst)
        for name, kind in osutil.listdir(src):
            srcname = os.path.join(src, name)
            dstname = os.path.join(dst, name)
            def nprog(t, pos):
                if pos is not None:
                    return progress(t, pos + num)
            hardlink, n = copyfiles(srcname, dstname, hardlink, progress=nprog)
            num += n
    else:
        if hardlink:
            try:
                oslink(src, dst)
            except (IOError, OSError):
                hardlink = False
                shutil.copy(src, dst)
        else:
            shutil.copy(src, dst)
        num += 1
        progress(topic, num)
    progress(topic, None)

    return hardlink, num

_winreservednames = '''con prn aux nul
    com1 com2 com3 com4 com5 com6 com7 com8 com9
    lpt1 lpt2 lpt3 lpt4 lpt5 lpt6 lpt7 lpt8 lpt9'''.split()
_winreservedchars = ':*?"<>|'
def checkwinfilename(path):
    r'''Check that the base-relative path is a valid filename on Windows.
    Returns None if the path is ok, or a UI string describing the problem.

    >>> checkwinfilename("just/a/normal/path")
    >>> checkwinfilename("foo/bar/con.xml")
    "filename contains 'con', which is reserved on Windows"
    >>> checkwinfilename("foo/con.xml/bar")
    "filename contains 'con', which is reserved on Windows"
    >>> checkwinfilename("foo/bar/xml.con")
    >>> checkwinfilename("foo/bar/AUX/bla.txt")
    "filename contains 'AUX', which is reserved on Windows"
    >>> checkwinfilename("foo/bar/bla:.txt")
    "filename contains ':', which is reserved on Windows"
    >>> checkwinfilename("foo/bar/b\07la.txt")
    "filename contains '\\x07', which is invalid on Windows"
    >>> checkwinfilename("foo/bar/bla ")
    "filename ends with ' ', which is not allowed on Windows"
    >>> checkwinfilename("../bar")
    >>> checkwinfilename("foo\\")
    "filename ends with '\\', which is invalid on Windows"
    >>> checkwinfilename("foo\\/bar")
    "directory name ends with '\\', which is invalid on Windows"
    '''
    if path.endswith('\\'):
        return _("filename ends with '\\', which is invalid on Windows")
    if '\\/' in path:
        return _("directory name ends with '\\', which is invalid on Windows")
    for n in path.replace('\\', '/').split('/'):
        if not n:
            continue
        for c in n:
            if c in _winreservedchars:
                return _("filename contains '%s', which is reserved "
                         "on Windows") % c
            if ord(c) <= 31:
                return _("filename contains %r, which is invalid "
                         "on Windows") % c
        base = n.split('.')[0]
        if base and base.lower() in _winreservednames:
            return _("filename contains '%s', which is reserved "
                     "on Windows") % base
        t = n[-1]
        if t in '. ' and n not in '..':
            return _("filename ends with '%s', which is not allowed "
                     "on Windows") % t

if os.name == 'nt':
    checkosfilename = checkwinfilename
else:
    checkosfilename = platform.checkosfilename

def makelock(info, pathname):
    try:
        return os.symlink(info, pathname)
    except OSError as why:
        if why.errno == errno.EEXIST:
            raise
    except AttributeError: # no symlink in os
        pass

    ld = os.open(pathname, os.O_CREAT | os.O_WRONLY | os.O_EXCL)
    os.write(ld, info)
    os.close(ld)

def readlock(pathname):
    try:
        return os.readlink(pathname)
    except OSError as why:
        if why.errno not in (errno.EINVAL, errno.ENOSYS):
            raise
    except AttributeError: # no symlink in os
        pass
    fp = posixfile(pathname)
    r = fp.read()
    fp.close()
    return r

def fstat(fp):
    '''stat file object that may not have fileno method.'''
    try:
        return os.fstat(fp.fileno())
    except AttributeError:
        return os.stat(fp.name)

def statmtimesec(st):
    """Get mtime as integer of seconds

    'int(st.st_mtime)' cannot be used because st.st_mtime is computed as
    'sec + 1e-9 * nsec' and double-precision floating-point type is too narrow
    to represent nanoseconds. If 'nsec' is close to 1 sec, 'int(st.st_mtime)'
    can be 'sec + 1'. (issue4836)
    """
    try:
        return st[stat.ST_MTIME]
    except (TypeError, IndexError):
        # osutil.stat doesn't allow index access and its st_mtime is int
        return st.st_mtime

# File system features

def checkcase(path):
    """
    Return true if the given path is on a case-sensitive filesystem

    Requires a path (like /foo/.hg) ending with a foldable final
    directory component.
    """
    s1 = os.lstat(path)
    d, b = os.path.split(path)
    b2 = b.upper()
    if b == b2:
        b2 = b.lower()
        if b == b2:
            return True # no evidence against case sensitivity
    p2 = os.path.join(d, b2)
    try:
        s2 = os.lstat(p2)
        if s2 == s1:
            return False
        return True
    except OSError:
        return True

try:
    import re2
    _re2 = None
except ImportError:
    _re2 = False

class _re(object):
    def _checkre2(self):
        global _re2
        try:
            # check if match works, see issue3964
            _re2 = bool(re2.match(r'\[([^\[]+)\]', '[ui]'))
        except ImportError:
            _re2 = False

    def compile(self, pat, flags=0):
        '''Compile a regular expression, using re2 if possible

        For best performance, use only re2-compatible regexp features. The
        only flags from the re module that are re2-compatible are
        IGNORECASE and MULTILINE.'''
        if _re2 is None:
            self._checkre2()
        if _re2 and (flags & ~(remod.IGNORECASE | remod.MULTILINE)) == 0:
            if flags & remod.IGNORECASE:
                pat = '(?i)' + pat
            if flags & remod.MULTILINE:
                pat = '(?m)' + pat
            try:
                return re2.compile(pat)
            except re2.error:
                pass
        return remod.compile(pat, flags)

    @propertycache
    def escape(self):
        '''Return the version of escape corresponding to self.compile.

        This is imperfect because whether re2 or re is used for a particular
        function depends on the flags, etc, but it's the best we can do.
        '''
        global _re2
        if _re2 is None:
            self._checkre2()
        if _re2:
            return re2.escape
        else:
            return remod.escape

re = _re()

_fspathcache = {}
def fspath(name, root):
    '''Get name in the case stored in the filesystem

    The name should be relative to root, and be normcase-ed for efficiency.

    Note that this function is unnecessary, and should not be
    called, for case-sensitive filesystems (simply because it's expensive).

    The root should be normcase-ed, too.
    '''
    def _makefspathcacheentry(dir):
        return dict((normcase(n), n) for n in os.listdir(dir))

    seps = os.sep
    if os.altsep:
        seps = seps + os.altsep
    # Protect backslashes. This gets silly very quickly.
    seps.replace('\\','\\\\')
    pattern = remod.compile(r'([^%s]+)|([%s]+)' % (seps, seps))
    dir = os.path.normpath(root)
    result = []
    for part, sep in pattern.findall(name):
        if sep:
            result.append(sep)
            continue

        if dir not in _fspathcache:
            _fspathcache[dir] = _makefspathcacheentry(dir)
        contents = _fspathcache[dir]

        found = contents.get(part)
        if not found:
            # retry "once per directory" per "dirstate.walk" which
            # may take place for each patches of "hg qpush", for example
            _fspathcache[dir] = contents = _makefspathcacheentry(dir)
            found = contents.get(part)

        result.append(found or part)
        dir = os.path.join(dir, part)

    return ''.join(result)

def checknlink(testfile):
    '''check whether hardlink count reporting works properly'''

    # testfile may be open, so we need a separate file for checking to
    # work around issue2543 (or testfile may get lost on Samba shares)
    f1 = testfile + ".hgtmp1"
    if os.path.lexists(f1):
        return False
    try:
        posixfile(f1, 'w').close()
    except IOError:
        return False

    f2 = testfile + ".hgtmp2"
    fd = None
    try:
        oslink(f1, f2)
        # nlinks() may behave differently for files on Windows shares if
        # the file is open.
        fd = posixfile(f2)
        return nlinks(f2) > 1
    except OSError:
        return False
    finally:
        if fd is not None:
            fd.close()
        for f in (f1, f2):
            try:
                os.unlink(f)
            except OSError:
                pass

def endswithsep(path):
    '''Check path ends with os.sep or os.altsep.'''
    return path.endswith(os.sep) or os.altsep and path.endswith(os.altsep)

def splitpath(path):
    '''Split path by os.sep.
    Note that this function does not use os.altsep because this is
    an alternative of simple "xxx.split(os.sep)".
    It is recommended to use os.path.normpath() before using this
    function if need.'''
    return path.split(os.sep)

def gui():
    '''Are we running in a GUI?'''
    if sys.platform == 'darwin':
        if 'SSH_CONNECTION' in os.environ:
            # handle SSH access to a box where the user is logged in
            return False
        elif getattr(osutil, 'isgui', None):
            # check if a CoreGraphics session is available
            return osutil.isgui()
        else:
            # pure build; use a safe default
            return True
    else:
        return os.name == "nt" or os.environ.get("DISPLAY")

def mktempcopy(name, emptyok=False, createmode=None):
    """Create a temporary file with the same contents from name

    The permission bits are copied from the original file.

    If the temporary file is going to be truncated immediately, you
    can use emptyok=True as an optimization.

    Returns the name of the temporary file.
    """
    d, fn = os.path.split(name)
    fd, temp = tempfile.mkstemp(prefix='.%s-' % fn, dir=d)
    os.close(fd)
    # Temporary files are created with mode 0600, which is usually not
    # what we want.  If the original file already exists, just copy
    # its mode.  Otherwise, manually obey umask.
    copymode(name, temp, createmode)
    if emptyok:
        return temp
    try:
        try:
            ifp = posixfile(name, "rb")
        except IOError as inst:
            if inst.errno == errno.ENOENT:
                return temp
            if not getattr(inst, 'filename', None):
                inst.filename = name
            raise
        ofp = posixfile(temp, "wb")
        for chunk in filechunkiter(ifp):
            ofp.write(chunk)
        ifp.close()
        ofp.close()
    except: # re-raises
        try: os.unlink(temp)
        except OSError: pass
        raise
    return temp

class atomictempfile(object):
    '''writable file object that atomically updates a file

    All writes will go to a temporary copy of the original file. Call
    close() when you are done writing, and atomictempfile will rename
    the temporary copy to the original name, making the changes
    visible. If the object is destroyed without being closed, all your
    writes are discarded.
    '''
    def __init__(self, name, mode='w+b', createmode=None):
        self.__name = name      # permanent name
        self._tempname = mktempcopy(name, emptyok=('w' in mode),
                                    createmode=createmode)
        self._fp = posixfile(self._tempname, mode)

        # delegated methods
        self.write = self._fp.write
        self.seek = self._fp.seek
        self.tell = self._fp.tell
        self.fileno = self._fp.fileno

    def close(self):
        if not self._fp.closed:
            self._fp.close()
            rename(self._tempname, localpath(self.__name))

    def discard(self):
        if not self._fp.closed:
            try:
                os.unlink(self._tempname)
            except OSError:
                pass
            self._fp.close()

    def __del__(self):
        if safehasattr(self, '_fp'): # constructor actually did something
            self.discard()

def makedirs(name, mode=None, notindexed=False):
    """recursive directory creation with parent mode inheritance"""
    try:
        makedir(name, notindexed)
    except OSError as err:
        if err.errno == errno.EEXIST:
            return
        if err.errno != errno.ENOENT or not name:
            raise
        parent = os.path.dirname(os.path.abspath(name))
        if parent == name:
            raise
        makedirs(parent, mode, notindexed)
        makedir(name, notindexed)
    if mode is not None:
        os.chmod(name, mode)

def ensuredirs(name, mode=None, notindexed=False):
    """race-safe recursive directory creation

    Newly created directories are marked as "not to be indexed by
    the content indexing service", if ``notindexed`` is specified
    for "write" mode access.
    """
    if os.path.isdir(name):
        return
    parent = os.path.dirname(os.path.abspath(name))
    if parent != name:
        ensuredirs(parent, mode, notindexed)
    try:
        makedir(name, notindexed)
    except OSError as err:
        if err.errno == errno.EEXIST and os.path.isdir(name):
            # someone else seems to have won a directory creation race
            return
        raise
    if mode is not None:
        os.chmod(name, mode)

def readfile(path):
    fp = open(path, 'rb')
    try:
        return fp.read()
    finally:
        fp.close()

def writefile(path, text):
    fp = open(path, 'wb')
    try:
        fp.write(text)
    finally:
        fp.close()

def appendfile(path, text):
    fp = open(path, 'ab')
    try:
        fp.write(text)
    finally:
        fp.close()

class chunkbuffer(object):
    """Allow arbitrary sized chunks of data to be efficiently read from an
    iterator over chunks of arbitrary size."""

    def __init__(self, in_iter):
        """in_iter is the iterator that's iterating over the input chunks.
        targetsize is how big a buffer to try to maintain."""
        def splitbig(chunks):
            for chunk in chunks:
                if len(chunk) > 2**20:
                    pos = 0
                    while pos < len(chunk):
                        end = pos + 2 ** 18
                        yield chunk[pos:end]
                        pos = end
                else:
                    yield chunk
        self.iter = splitbig(in_iter)
        self._queue = collections.deque()
        self._chunkoffset = 0

    def read(self, l=None):
        """Read L bytes of data from the iterator of chunks of data.
        Returns less than L bytes if the iterator runs dry.

        If size parameter is omitted, read everything"""
        if l is None:
            return ''.join(self.iter)

        left = l
        buf = []
        queue = self._queue
        while left > 0:
            # refill the queue
            if not queue:
                target = 2**18
                for chunk in self.iter:
                    queue.append(chunk)
                    target -= len(chunk)
                    if target <= 0:
                        break
                if not queue:
                    break

            # The easy way to do this would be to queue.popleft(), modify the
            # chunk (if necessary), then queue.appendleft(). However, for cases
            # where we read partial chunk content, this incurs 2 dequeue
            # mutations and creates a new str for the remaining chunk in the
            # queue. Our code below avoids this overhead.

            chunk = queue[0]
            chunkl = len(chunk)
            offset = self._chunkoffset

            # Use full chunk.
            if offset == 0 and left >= chunkl:
                left -= chunkl
                queue.popleft()
                buf.append(chunk)
                # self._chunkoffset remains at 0.
                continue

            chunkremaining = chunkl - offset

            # Use all of unconsumed part of chunk.
            if left >= chunkremaining:
                left -= chunkremaining
                queue.popleft()
                # offset == 0 is enabled by block above, so this won't merely
                # copy via ``chunk[0:]``.
                buf.append(chunk[offset:])
                self._chunkoffset = 0

            # Partial chunk needed.
            else:
                buf.append(chunk[offset:offset + left])
                self._chunkoffset += left
                left -= chunkremaining

        return ''.join(buf)

def filechunkiter(f, size=65536, limit=None):
    """Create a generator that produces the data in the file size
    (default 65536) bytes at a time, up to optional limit (default is
    to read all data).  Chunks may be less than size bytes if the
    chunk is the last chunk in the file, or the file is a socket or
    some other type of file that sometimes reads less data than is
    requested."""
    assert size >= 0
    assert limit is None or limit >= 0
    while True:
        if limit is None:
            nbytes = size
        else:
            nbytes = min(limit, size)
        s = nbytes and f.read(nbytes)
        if not s:
            break
        if limit:
            limit -= len(s)
        yield s

def makedate(timestamp=None):
    '''Return a unix timestamp (or the current time) as a (unixtime,
    offset) tuple based off the local timezone.'''
    if timestamp is None:
        timestamp = time.time()
    if timestamp < 0:
        hint = _("check your clock")
        raise Abort(_("negative timestamp: %d") % timestamp, hint=hint)
    delta = (datetime.datetime.utcfromtimestamp(timestamp) -
             datetime.datetime.fromtimestamp(timestamp))
    tz = delta.days * 86400 + delta.seconds
    return timestamp, tz

def datestr(date=None, format='%a %b %d %H:%M:%S %Y %1%2'):
    """represent a (unixtime, offset) tuple as a localized time.
    unixtime is seconds since the epoch, and offset is the time zone's
    number of seconds away from UTC. if timezone is false, do not
    append time zone to string."""
    t, tz = date or makedate()
    if t < 0:
        t = 0   # time.gmtime(lt) fails on Windows for lt < -43200
        tz = 0
    if "%1" in format or "%2" in format or "%z" in format:
        sign = (tz > 0) and "-" or "+"
        minutes = abs(tz) // 60
        format = format.replace("%z", "%1%2")
        format = format.replace("%1", "%c%02d" % (sign, minutes // 60))
        format = format.replace("%2", "%02d" % (minutes % 60))
    try:
        t = time.gmtime(float(t) - tz)
    except ValueError:
        # time was out of range
        t = time.gmtime(sys.maxint)
    s = time.strftime(format, t)
    return s

def shortdate(date=None):
    """turn (timestamp, tzoff) tuple into iso 8631 date."""
    return datestr(date, format='%Y-%m-%d')

def parsetimezone(tz):
    """parse a timezone string and return an offset integer"""
    if tz[0] in "+-" and len(tz) == 5 and tz[1:].isdigit():
        sign = (tz[0] == "+") and 1 or -1
        hours = int(tz[1:3])
        minutes = int(tz[3:5])
        return -sign * (hours * 60 + minutes) * 60
    if tz == "GMT" or tz == "UTC":
        return 0
    return None

def strdate(string, format, defaults=[]):
    """parse a localized time string and return a (unixtime, offset) tuple.
    if the string cannot be parsed, ValueError is raised."""
    # NOTE: unixtime = localunixtime + offset
    offset, date = parsetimezone(string.split()[-1]), string
    if offset is not None:
        date = " ".join(string.split()[:-1])

    # add missing elements from defaults
    usenow = False # default to using biased defaults
    for part in ("S", "M", "HI", "d", "mb", "yY"): # decreasing specificity
        found = [True for p in part if ("%"+p) in format]
        if not found:
            date += "@" + defaults[part][usenow]
            format += "@%" + part[0]
        else:
            # We've found a specific time element, less specific time
            # elements are relative to today
            usenow = True

    timetuple = time.strptime(date, format)
    localunixtime = int(calendar.timegm(timetuple))
    if offset is None:
        # local timezone
        unixtime = int(time.mktime(timetuple))
        offset = unixtime - localunixtime
    else:
        unixtime = localunixtime + offset
    return unixtime, offset

def parsedate(date, formats=None, bias=None):
    """parse a localized date/time and return a (unixtime, offset) tuple.

    The date may be a "unixtime offset" string or in one of the specified
    formats. If the date already is a (unixtime, offset) tuple, it is returned.

    >>> parsedate(' today ') == parsedate(\
                                  datetime.date.today().strftime('%b %d'))
    True
    >>> parsedate( 'yesterday ') == parsedate((datetime.date.today() -\
                                               datetime.timedelta(days=1)\
                                              ).strftime('%b %d'))
    True
    >>> now, tz = makedate()
    >>> strnow, strtz = parsedate('now')
    >>> (strnow - now) < 1
    True
    >>> tz == strtz
    True
    """
    if bias is None:
        bias = {}
    if not date:
        return 0, 0
    if isinstance(date, tuple) and len(date) == 2:
        return date
    if not formats:
        formats = defaultdateformats
    date = date.strip()

    if date == 'now' or date == _('now'):
        return makedate()
    if date == 'today' or date == _('today'):
        date = datetime.date.today().strftime('%b %d')
    elif date == 'yesterday' or date == _('yesterday'):
        date = (datetime.date.today() -
                datetime.timedelta(days=1)).strftime('%b %d')

    try:
        when, offset = map(int, date.split(' '))
    except ValueError:
        # fill out defaults
        now = makedate()
        defaults = {}
        for part in ("d", "mb", "yY", "HI", "M", "S"):
            # this piece is for rounding the specific end of unknowns
            b = bias.get(part)
            if b is None:
                if part[0] in "HMS":
                    b = "00"
                else:
                    b = "0"

            # this piece is for matching the generic end to today's date
            n = datestr(now, "%" + part[0])

            defaults[part] = (b, n)

        for format in formats:
            try:
                when, offset = strdate(date, format, defaults)
            except (ValueError, OverflowError):
                pass
            else:
                break
        else:
            raise Abort(_('invalid date: %r') % date)
    # validate explicit (probably user-specified) date and
    # time zone offset. values must fit in signed 32 bits for
    # current 32-bit linux runtimes. timezones go from UTC-12
    # to UTC+14
    if abs(when) > 0x7fffffff:
        raise Abort(_('date exceeds 32 bits: %d') % when)
    if when < 0:
        raise Abort(_('negative date value: %d') % when)
    if offset < -50400 or offset > 43200:
        raise Abort(_('impossible time zone offset: %d') % offset)
    return when, offset

def matchdate(date):
    """Return a function that matches a given date match specifier

    Formats include:

    '{date}' match a given date to the accuracy provided

    '<{date}' on or before a given date

    '>{date}' on or after a given date

    >>> p1 = parsedate("10:29:59")
    >>> p2 = parsedate("10:30:00")
    >>> p3 = parsedate("10:30:59")
    >>> p4 = parsedate("10:31:00")
    >>> p5 = parsedate("Sep 15 10:30:00 1999")
    >>> f = matchdate("10:30")
    >>> f(p1[0])
    False
    >>> f(p2[0])
    True
    >>> f(p3[0])
    True
    >>> f(p4[0])
    False
    >>> f(p5[0])
    False
    """

    def lower(date):
        d = {'mb': "1", 'd': "1"}
        return parsedate(date, extendeddateformats, d)[0]

    def upper(date):
        d = {'mb': "12", 'HI': "23", 'M': "59", 'S': "59"}
        for days in ("31", "30", "29"):
            try:
                d["d"] = days
                return parsedate(date, extendeddateformats, d)[0]
            except Abort:
                pass
        d["d"] = "28"
        return parsedate(date, extendeddateformats, d)[0]

    date = date.strip()

    if not date:
        raise Abort(_("dates cannot consist entirely of whitespace"))
    elif date[0] == "<":
        if not date[1:]:
            raise Abort(_("invalid day spec, use '<DATE'"))
        when = upper(date[1:])
        return lambda x: x <= when
    elif date[0] == ">":
        if not date[1:]:
            raise Abort(_("invalid day spec, use '>DATE'"))
        when = lower(date[1:])
        return lambda x: x >= when
    elif date[0] == "-":
        try:
            days = int(date[1:])
        except ValueError:
            raise Abort(_("invalid day spec: %s") % date[1:])
        if days < 0:
            raise Abort(_('%s must be nonnegative (see "hg help dates")')
                % date[1:])
        when = makedate()[0] - days * 3600 * 24
        return lambda x: x >= when
    elif " to " in date:
        a, b = date.split(" to ")
        start, stop = lower(a), upper(b)
        return lambda x: x >= start and x <= stop
    else:
        start, stop = lower(date), upper(date)
        return lambda x: x >= start and x <= stop

def stringmatcher(pattern):
    """
    accepts a string, possibly starting with 're:' or 'literal:' prefix.
    returns the matcher name, pattern, and matcher function.
    missing or unknown prefixes are treated as literal matches.

    helper for tests:
    >>> def test(pattern, *tests):
    ...     kind, pattern, matcher = stringmatcher(pattern)
    ...     return (kind, pattern, [bool(matcher(t)) for t in tests])

    exact matching (no prefix):
    >>> test('abcdefg', 'abc', 'def', 'abcdefg')
    ('literal', 'abcdefg', [False, False, True])

    regex matching ('re:' prefix)
    >>> test('re:a.+b', 'nomatch', 'fooadef', 'fooadefbar')
    ('re', 'a.+b', [False, False, True])

    force exact matches ('literal:' prefix)
    >>> test('literal:re:foobar', 'foobar', 're:foobar')
    ('literal', 're:foobar', [False, True])

    unknown prefixes are ignored and treated as literals
    >>> test('foo:bar', 'foo', 'bar', 'foo:bar')
    ('literal', 'foo:bar', [False, False, True])
    """
    if pattern.startswith('re:'):
        pattern = pattern[3:]
        try:
            regex = remod.compile(pattern)
        except remod.error as e:
            raise error.ParseError(_('invalid regular expression: %s')
                                   % e)
        return 're', pattern, regex.search
    elif pattern.startswith('literal:'):
        pattern = pattern[8:]
    return 'literal', pattern, pattern.__eq__

def shortuser(user):
    """Return a short representation of a user name or email address."""
    f = user.find('@')
    if f >= 0:
        user = user[:f]
    f = user.find('<')
    if f >= 0:
        user = user[f + 1:]
    f = user.find(' ')
    if f >= 0:
        user = user[:f]
    f = user.find('.')
    if f >= 0:
        user = user[:f]
    return user

def emailuser(user):
    """Return the user portion of an email address."""
    f = user.find('@')
    if f >= 0:
        user = user[:f]
    f = user.find('<')
    if f >= 0:
        user = user[f + 1:]
    return user

def email(author):
    '''get email of author.'''
    r = author.find('>')
    if r == -1:
        r = None
    return author[author.find('<') + 1:r]

def ellipsis(text, maxlength=400):
    """Trim string to at most maxlength (default: 400) columns in display."""
    return encoding.trim(text, maxlength, ellipsis='...')

def unitcountfn(*unittable):
    '''return a function that renders a readable count of some quantity'''

    def go(count):
        for multiplier, divisor, format in unittable:
            if count >= divisor * multiplier:
                return format % (count / float(divisor))
        return unittable[-1][2] % count

    return go

bytecount = unitcountfn(
    (100, 1 << 30, _('%.0f GB')),
    (10, 1 << 30, _('%.1f GB')),
    (1, 1 << 30, _('%.2f GB')),
    (100, 1 << 20, _('%.0f MB')),
    (10, 1 << 20, _('%.1f MB')),
    (1, 1 << 20, _('%.2f MB')),
    (100, 1 << 10, _('%.0f KB')),
    (10, 1 << 10, _('%.1f KB')),
    (1, 1 << 10, _('%.2f KB')),
    (1, 1, _('%.0f bytes')),
    )

def uirepr(s):
    # Avoid double backslash in Windows path repr()
    return repr(s).replace('\\\\', '\\')

# delay import of textwrap
def MBTextWrapper(**kwargs):
    class tw(textwrap.TextWrapper):
        """
        Extend TextWrapper for width-awareness.

        Neither number of 'bytes' in any encoding nor 'characters' is
        appropriate to calculate terminal columns for specified string.

        Original TextWrapper implementation uses built-in 'len()' directly,
        so overriding is needed to use width information of each characters.

        In addition, characters classified into 'ambiguous' width are
        treated as wide in East Asian area, but as narrow in other.

        This requires use decision to determine width of such characters.
        """
        def _cutdown(self, ucstr, space_left):
            l = 0
            colwidth = encoding.ucolwidth
            for i in xrange(len(ucstr)):
                l += colwidth(ucstr[i])
                if space_left < l:
                    return (ucstr[:i], ucstr[i:])
            return ucstr, ''

        # overriding of base class
        def _handle_long_word(self, reversed_chunks, cur_line, cur_len, width):
            space_left = max(width - cur_len, 1)

            if self.break_long_words:
                cut, res = self._cutdown(reversed_chunks[-1], space_left)
                cur_line.append(cut)
                reversed_chunks[-1] = res
            elif not cur_line:
                cur_line.append(reversed_chunks.pop())

        # this overriding code is imported from TextWrapper of Python 2.6
        # to calculate columns of string by 'encoding.ucolwidth()'
        def _wrap_chunks(self, chunks):
            colwidth = encoding.ucolwidth

            lines = []
            if self.width <= 0:
                raise ValueError("invalid width %r (must be > 0)" % self.width)

            # Arrange in reverse order so items can be efficiently popped
            # from a stack of chucks.
            chunks.reverse()

            while chunks:

                # Start the list of chunks that will make up the current line.
                # cur_len is just the length of all the chunks in cur_line.
                cur_line = []
                cur_len = 0

                # Figure out which static string will prefix this line.
                if lines:
                    indent = self.subsequent_indent
                else:
                    indent = self.initial_indent

                # Maximum width for this line.
                width = self.width - len(indent)

                # First chunk on line is whitespace -- drop it, unless this
                # is the very beginning of the text (i.e. no lines started yet).
                if self.drop_whitespace and chunks[-1].strip() == '' and lines:
                    del chunks[-1]

                while chunks:
                    l = colwidth(chunks[-1])

                    # Can at least squeeze this chunk onto the current line.
                    if cur_len + l <= width:
                        cur_line.append(chunks.pop())
                        cur_len += l

                    # Nope, this line is full.
                    else:
                        break

                # The current line is full, and the next chunk is too big to
                # fit on *any* line (not just this one).
                if chunks and colwidth(chunks[-1]) > width:
                    self._handle_long_word(chunks, cur_line, cur_len, width)

                # If the last chunk on this line is all whitespace, drop it.
                if (self.drop_whitespace and
                    cur_line and cur_line[-1].strip() == ''):
                    del cur_line[-1]

                # Convert current line back to a string and store it in list
                # of all lines (return value).
                if cur_line:
                    lines.append(indent + ''.join(cur_line))

            return lines

    global MBTextWrapper
    MBTextWrapper = tw
    return tw(**kwargs)

def wrap(line, width, initindent='', hangindent=''):
    maxindent = max(len(hangindent), len(initindent))
    if width <= maxindent:
        # adjust for weird terminal size
        width = max(78, maxindent + 1)
    line = line.decode(encoding.encoding, encoding.encodingmode)
    initindent = initindent.decode(encoding.encoding, encoding.encodingmode)
    hangindent = hangindent.decode(encoding.encoding, encoding.encodingmode)
    wrapper = MBTextWrapper(width=width,
                            initial_indent=initindent,
                            subsequent_indent=hangindent)
    return wrapper.fill(line).encode(encoding.encoding)

def iterlines(iterator):
    for chunk in iterator:
        for line in chunk.splitlines():
            yield line

def expandpath(path):
    return os.path.expanduser(os.path.expandvars(path))

def hgcmd():
    """Return the command used to execute current hg

    This is different from hgexecutable() because on Windows we want
    to avoid things opening new shell windows like batch files, so we
    get either the python call or current executable.
    """
    if mainfrozen():
        return [sys.executable]
    return gethgcmd()

def rundetached(args, condfn):
    """Execute the argument list in a detached process.

    condfn is a callable which is called repeatedly and should return
    True once the child process is known to have started successfully.
    At this point, the child process PID is returned. If the child
    process fails to start or finishes before condfn() evaluates to
    True, return -1.
    """
    # Windows case is easier because the child process is either
    # successfully starting and validating the condition or exiting
    # on failure. We just poll on its PID. On Unix, if the child
    # process fails to start, it will be left in a zombie state until
    # the parent wait on it, which we cannot do since we expect a long
    # running process on success. Instead we listen for SIGCHLD telling
    # us our child process terminated.
    terminated = set()
    def handler(signum, frame):
        terminated.add(os.wait())
    prevhandler = None
    SIGCHLD = getattr(signal, 'SIGCHLD', None)
    if SIGCHLD is not None:
        prevhandler = signal.signal(SIGCHLD, handler)
    try:
        pid = spawndetached(args)
        while not condfn():
            if ((pid in terminated or not testpid(pid))
                and not condfn()):
                return -1
            time.sleep(0.1)
        return pid
    finally:
        if prevhandler is not None:
            signal.signal(signal.SIGCHLD, prevhandler)

def interpolate(prefix, mapping, s, fn=None, escape_prefix=False):
    """Return the result of interpolating items in the mapping into string s.

    prefix is a single character string, or a two character string with
    a backslash as the first character if the prefix needs to be escaped in
    a regular expression.

    fn is an optional function that will be applied to the replacement text
    just before replacement.

    escape_prefix is an optional flag that allows using doubled prefix for
    its escaping.
    """
    fn = fn or (lambda s: s)
    patterns = '|'.join(mapping.keys())
    if escape_prefix:
        patterns += '|' + prefix
        if len(prefix) > 1:
            prefix_char = prefix[1:]
        else:
            prefix_char = prefix
        mapping[prefix_char] = prefix_char
    r = remod.compile(r'%s(%s)' % (prefix, patterns))
    return r.sub(lambda x: fn(mapping[x.group()[1:]]), s)

def getport(port):
    """Return the port for a given network service.

    If port is an integer, it's returned as is. If it's a string, it's
    looked up using socket.getservbyname(). If there's no matching
    service, error.Abort is raised.
    """
    try:
        return int(port)
    except ValueError:
        pass

    try:
        return socket.getservbyname(port)
    except socket.error:
        raise Abort(_("no port number associated with service '%s'") % port)

_booleans = {'1': True, 'yes': True, 'true': True, 'on': True, 'always': True,
             '0': False, 'no': False, 'false': False, 'off': False,
             'never': False}

def parsebool(s):
    """Parse s into a boolean.

    If s is not a valid boolean, returns None.
    """
    return _booleans.get(s.lower(), None)

_hexdig = '0123456789ABCDEFabcdef'
_hextochr = dict((a + b, chr(int(a + b, 16)))
                 for a in _hexdig for b in _hexdig)

def _urlunquote(s):
    """Decode HTTP/HTML % encoding.

    >>> _urlunquote('abc%20def')
    'abc def'
    """
    res = s.split('%')
    # fastpath
    if len(res) == 1:
        return s
    s = res[0]
    for item in res[1:]:
        try:
            s += _hextochr[item[:2]] + item[2:]
        except KeyError:
            s += '%' + item
        except UnicodeDecodeError:
            s += unichr(int(item[:2], 16)) + item[2:]
    return s

class url(object):
    r"""Reliable URL parser.

    This parses URLs and provides attributes for the following
    components:

    <scheme>://<user>:<passwd>@<host>:<port>/<path>?<query>#<fragment>

    Missing components are set to None. The only exception is
    fragment, which is set to '' if present but empty.

    If parsefragment is False, fragment is included in query. If
    parsequery is False, query is included in path. If both are
    False, both fragment and query are included in path.

    See http://www.ietf.org/rfc/rfc2396.txt for more information.

    Note that for backward compatibility reasons, bundle URLs do not
    take host names. That means 'bundle://../' has a path of '../'.

    Examples:

    >>> url('http://www.ietf.org/rfc/rfc2396.txt')
    <url scheme: 'http', host: 'www.ietf.org', path: 'rfc/rfc2396.txt'>
    >>> url('ssh://[::1]:2200//home/joe/repo')
    <url scheme: 'ssh', host: '[::1]', port: '2200', path: '/home/joe/repo'>
    >>> url('file:///home/joe/repo')
    <url scheme: 'file', path: '/home/joe/repo'>
    >>> url('file:///c:/temp/foo/')
    <url scheme: 'file', path: 'c:/temp/foo/'>
    >>> url('bundle:foo')
    <url scheme: 'bundle', path: 'foo'>
    >>> url('bundle://../foo')
    <url scheme: 'bundle', path: '../foo'>
    >>> url(r'c:\foo\bar')
    <url path: 'c:\\foo\\bar'>
    >>> url(r'\\blah\blah\blah')
    <url path: '\\\\blah\\blah\\blah'>
    >>> url(r'\\blah\blah\blah#baz')
    <url path: '\\\\blah\\blah\\blah', fragment: 'baz'>
    >>> url(r'file:///C:\users\me')
    <url scheme: 'file', path: 'C:\\users\\me'>

    Authentication credentials:

    >>> url('ssh://joe:xyz@x/repo')
    <url scheme: 'ssh', user: 'joe', passwd: 'xyz', host: 'x', path: 'repo'>
    >>> url('ssh://joe@x/repo')
    <url scheme: 'ssh', user: 'joe', host: 'x', path: 'repo'>

    Query strings and fragments:

    >>> url('http://host/a?b#c')
    <url scheme: 'http', host: 'host', path: 'a', query: 'b', fragment: 'c'>
    >>> url('http://host/a?b#c', parsequery=False, parsefragment=False)
    <url scheme: 'http', host: 'host', path: 'a?b#c'>
    """

    _safechars = "!~*'()+"
    _safepchars = "/!~*'()+:\\"
    _matchscheme = remod.compile(r'^[a-zA-Z0-9+.\-]+:').match

    def __init__(self, path, parsequery=True, parsefragment=True):
        # We slowly chomp away at path until we have only the path left
        self.scheme = self.user = self.passwd = self.host = None
        self.port = self.path = self.query = self.fragment = None
        self._localpath = True
        self._hostport = ''
        self._origpath = path

        if parsefragment and '#' in path:
            path, self.fragment = path.split('#', 1)
            if not path:
                path = None

        # special case for Windows drive letters and UNC paths
        if hasdriveletter(path) or path.startswith(r'\\'):
            self.path = path
            return

        # For compatibility reasons, we can't handle bundle paths as
        # normal URLS
        if path.startswith('bundle:'):
            self.scheme = 'bundle'
            path = path[7:]
            if path.startswith('//'):
                path = path[2:]
            self.path = path
            return

        if self._matchscheme(path):
            parts = path.split(':', 1)
            if parts[0]:
                self.scheme, path = parts
                self._localpath = False

        if not path:
            path = None
            if self._localpath:
                self.path = ''
                return
        else:
            if self._localpath:
                self.path = path
                return

            if parsequery and '?' in path:
                path, self.query = path.split('?', 1)
                if not path:
                    path = None
                if not self.query:
                    self.query = None

            # // is required to specify a host/authority
            if path and path.startswith('//'):
                parts = path[2:].split('/', 1)
                if len(parts) > 1:
                    self.host, path = parts
                else:
                    self.host = parts[0]
                    path = None
                if not self.host:
                    self.host = None
                    # path of file:///d is /d
                    # path of file:///d:/ is d:/, not /d:/
                    if path and not hasdriveletter(path):
                        path = '/' + path

            if self.host and '@' in self.host:
                self.user, self.host = self.host.rsplit('@', 1)
                if ':' in self.user:
                    self.user, self.passwd = self.user.split(':', 1)
                if not self.host:
                    self.host = None

            # Don't split on colons in IPv6 addresses without ports
            if (self.host and ':' in self.host and
                not (self.host.startswith('[') and self.host.endswith(']'))):
                self._hostport = self.host
                self.host, self.port = self.host.rsplit(':', 1)
                if not self.host:
                    self.host = None

            if (self.host and self.scheme == 'file' and
                self.host not in ('localhost', '127.0.0.1', '[::1]')):
                raise Abort(_('file:// URLs can only refer to localhost'))

        self.path = path

        # leave the query string escaped
        for a in ('user', 'passwd', 'host', 'port',
                  'path', 'fragment'):
            v = getattr(self, a)
            if v is not None:
                setattr(self, a, _urlunquote(v))

    def __repr__(self):
        attrs = []
        for a in ('scheme', 'user', 'passwd', 'host', 'port', 'path',
                  'query', 'fragment'):
            v = getattr(self, a)
            if v is not None:
                attrs.append('%s: %r' % (a, v))
        return '<url %s>' % ', '.join(attrs)

    def __str__(self):
        r"""Join the URL's components back into a URL string.

        Examples:

        >>> str(url('http://user:pw@host:80/c:/bob?fo:oo#ba:ar'))
        'http://user:pw@host:80/c:/bob?fo:oo#ba:ar'
        >>> str(url('http://user:pw@host:80/?foo=bar&baz=42'))
        'http://user:pw@host:80/?foo=bar&baz=42'
        >>> str(url('http://user:pw@host:80/?foo=bar%3dbaz'))
        'http://user:pw@host:80/?foo=bar%3dbaz'
        >>> str(url('ssh://user:pw@[::1]:2200//home/joe#'))
        'ssh://user:pw@[::1]:2200//home/joe#'
        >>> str(url('http://localhost:80//'))
        'http://localhost:80//'
        >>> str(url('http://localhost:80/'))
        'http://localhost:80/'
        >>> str(url('http://localhost:80'))
        'http://localhost:80/'
        >>> str(url('bundle:foo'))
        'bundle:foo'
        >>> str(url('bundle://../foo'))
        'bundle:../foo'
        >>> str(url('path'))
        'path'
        >>> str(url('file:///tmp/foo/bar'))
        'file:///tmp/foo/bar'
        >>> str(url('file:///c:/tmp/foo/bar'))
        'file:///c:/tmp/foo/bar'
        >>> print url(r'bundle:foo\bar')
        bundle:foo\bar
        >>> print url(r'file:///D:\data\hg')
        file:///D:\data\hg
        """
        if self._localpath:
            s = self.path
            if self.scheme == 'bundle':
                s = 'bundle:' + s
            if self.fragment:
                s += '#' + self.fragment
            return s

        s = self.scheme + ':'
        if self.user or self.passwd or self.host:
            s += '//'
        elif self.scheme and (not self.path or self.path.startswith('/')
                              or hasdriveletter(self.path)):
            s += '//'
            if hasdriveletter(self.path):
                s += '/'
        if self.user:
            s += urllib.quote(self.user, safe=self._safechars)
        if self.passwd:
            s += ':' + urllib.quote(self.passwd, safe=self._safechars)
        if self.user or self.passwd:
            s += '@'
        if self.host:
            if not (self.host.startswith('[') and self.host.endswith(']')):
                s += urllib.quote(self.host)
            else:
                s += self.host
        if self.port:
            s += ':' + urllib.quote(self.port)
        if self.host:
            s += '/'
        if self.path:
            # TODO: similar to the query string, we should not unescape the
            # path when we store it, the path might contain '%2f' = '/',
            # which we should *not* escape.
            s += urllib.quote(self.path, safe=self._safepchars)
        if self.query:
            # we store the query in escaped form.
            s += '?' + self.query
        if self.fragment is not None:
            s += '#' + urllib.quote(self.fragment, safe=self._safepchars)
        return s

    def authinfo(self):
        user, passwd = self.user, self.passwd
        try:
            self.user, self.passwd = None, None
            s = str(self)
        finally:
            self.user, self.passwd = user, passwd
        if not self.user:
            return (s, None)
        # authinfo[1] is passed to urllib2 password manager, and its
        # URIs must not contain credentials. The host is passed in the
        # URIs list because Python < 2.4.3 uses only that to search for
        # a password.
        return (s, (None, (s, self.host),
                    self.user, self.passwd or ''))

    def isabs(self):
        if self.scheme and self.scheme != 'file':
            return True # remote URL
        if hasdriveletter(self.path):
            return True # absolute for our purposes - can't be joined()
        if self.path.startswith(r'\\'):
            return True # Windows UNC path
        if self.path.startswith('/'):
            return True # POSIX-style
        return False

    def localpath(self):
        if self.scheme == 'file' or self.scheme == 'bundle':
            path = self.path or '/'
            # For Windows, we need to promote hosts containing drive
            # letters to paths with drive letters.
            if hasdriveletter(self._hostport):
                path = self._hostport + '/' + self.path
            elif (self.host is not None and self.path
                  and not hasdriveletter(path)):
                path = '/' + path
            return path
        return self._origpath

    def islocal(self):
        '''whether localpath will return something that posixfile can open'''
        return (not self.scheme or self.scheme == 'file'
                or self.scheme == 'bundle')

def hasscheme(path):
    return bool(url(path).scheme)

def hasdriveletter(path):
    return path and path[1:2] == ':' and path[0:1].isalpha()

def urllocalpath(path):
    return url(path, parsequery=False, parsefragment=False).localpath()

def hidepassword(u):
    '''hide user credential in a url string'''
    u = url(u)
    if u.passwd:
        u.passwd = '***'
    return str(u)

def removeauth(u):
    '''remove all authentication information from a url string'''
    u = url(u)
    u.user = u.passwd = None
    return str(u)

def isatty(fd):
    try:
        return fd.isatty()
    except AttributeError:
        return False

timecount = unitcountfn(
    (1, 1e3, _('%.0f s')),
    (100, 1, _('%.1f s')),
    (10, 1, _('%.2f s')),
    (1, 1, _('%.3f s')),
    (100, 0.001, _('%.1f ms')),
    (10, 0.001, _('%.2f ms')),
    (1, 0.001, _('%.3f ms')),
    (100, 0.000001, _('%.1f us')),
    (10, 0.000001, _('%.2f us')),
    (1, 0.000001, _('%.3f us')),
    (100, 0.000000001, _('%.1f ns')),
    (10, 0.000000001, _('%.2f ns')),
    (1, 0.000000001, _('%.3f ns')),
    )

_timenesting = [0]

def timed(func):
    '''Report the execution time of a function call to stderr.

    During development, use as a decorator when you need to measure
    the cost of a function, e.g. as follows:

    @util.timed
    def foo(a, b, c):
        pass
    '''

    def wrapper(*args, **kwargs):
        start = time.time()
        indent = 2
        _timenesting[0] += indent
        try:
            return func(*args, **kwargs)
        finally:
            elapsed = time.time() - start
            _timenesting[0] -= indent
            sys.stderr.write('%s%s: %s\n' %
                             (' ' * _timenesting[0], func.__name__,
                              timecount(elapsed)))
    return wrapper

_sizeunits = (('m', 2**20), ('k', 2**10), ('g', 2**30),
              ('kb', 2**10), ('mb', 2**20), ('gb', 2**30), ('b', 1))

def sizetoint(s):
    '''Convert a space specifier to a byte count.

    >>> sizetoint('30')
    30
    >>> sizetoint('2.2kb')
    2252
    >>> sizetoint('6M')
    6291456
    '''
    t = s.strip().lower()
    try:
        for k, u in _sizeunits:
            if t.endswith(k):
                return int(float(t[:-len(k)]) * u)
        return int(t)
    except ValueError:
        raise error.ParseError(_("couldn't parse size: %s") % s)

class hooks(object):
    '''A collection of hook functions that can be used to extend a
    function's behavior. Hooks are called in lexicographic order,
    based on the names of their sources.'''

    def __init__(self):
        self._hooks = []

    def add(self, source, hook):
        self._hooks.append((source, hook))

    def __call__(self, *args):
        self._hooks.sort(key=lambda x: x[0])
        results = []
        for source, hook in self._hooks:
            results.append(hook(*args))
        return results

def debugstacktrace(msg='stacktrace', skip=0, f=sys.stderr, otherf=sys.stdout):
    '''Writes a message to f (stderr) with a nicely formatted stacktrace.
    Skips the 'skip' last entries. By default it will flush stdout first.
    It can be used everywhere and do intentionally not require an ui object.
    Not be used in production code but very convenient while developing.
    '''
    if otherf:
        otherf.flush()
    f.write('%s at:\n' % msg)
    entries = [('%s:%s' % (fn, ln), func)
        for fn, ln, func, _text in traceback.extract_stack()[:-skip - 1]]
    if entries:
        fnmax = max(len(entry[0]) for entry in entries)
        for fnln, func in entries:
            f.write(' %-*s in %s\n' % (fnmax, fnln, func))
    f.flush()

class dirs(object):
    '''a multiset of directory names from a dirstate or manifest'''

    def __init__(self, map, skip=None):
        self._dirs = {}
        addpath = self.addpath
        if safehasattr(map, 'iteritems') and skip is not None:
            for f, s in map.iteritems():
                if s[0] != skip:
                    addpath(f)
        else:
            for f in map:
                addpath(f)

    def addpath(self, path):
        dirs = self._dirs
        for base in finddirs(path):
            if base in dirs:
                dirs[base] += 1
                return
            dirs[base] = 1

    def delpath(self, path):
        dirs = self._dirs
        for base in finddirs(path):
            if dirs[base] > 1:
                dirs[base] -= 1
                return
            del dirs[base]

    def __iter__(self):
        return self._dirs.iterkeys()

    def __contains__(self, d):
        return d in self._dirs

if safehasattr(parsers, 'dirs'):
    dirs = parsers.dirs

def finddirs(path):
    pos = path.rfind('/')
    while pos != -1:
        yield path[:pos]
        pos = path.rfind('/', 0, pos)

# compression utility

class nocompress(object):
    def compress(self, x):
        return x
    def flush(self):
        return ""

compressors = {
    None: nocompress,
    # lambda to prevent early import
    'BZ': lambda: bz2.BZ2Compressor(),
    'GZ': lambda: zlib.compressobj(),
    }
# also support the old form by courtesies
compressors['UN'] = compressors[None]

def _makedecompressor(decompcls):
    def generator(f):
        d = decompcls()
        for chunk in filechunkiter(f):
            yield d.decompress(chunk)
    def func(fh):
        return chunkbuffer(generator(fh))
    return func

def _bz2():
    d = bz2.BZ2Decompressor()
    # Bzip2 stream start with BZ, but we stripped it.
    # we put it back for good measure.
    d.decompress('BZ')
    return d

decompressors = {None: lambda fh: fh,
                 '_truncatedBZ': _makedecompressor(_bz2),
                 'BZ': _makedecompressor(lambda: bz2.BZ2Decompressor()),
                 'GZ': _makedecompressor(lambda: zlib.decompressobj()),
                 }
# also support the old form by courtesies
decompressors['UN'] = decompressors[None]

# convenient shortcut
dst = debugstacktrace
