#!/usr/bin/env python
#
# Minimal support for git commands on an hg repository
#
# Copyright 2005 Chris Mason <mason@suse.com>
#
# This software may be used and distributed according to the terms
# of the GNU General Public License, incorporated herein by reference.

import time, sys, signal, os
from mercurial import hg, mdiff, fancyopts, commands, ui, util

def dodiff(fp, ui, repo, node1, node2, files=None, match=util.always,
           changes=None, text=False):
    def date(c):
        return time.asctime(time.gmtime(float(c[2].split(' ')[0])))

    if not changes:
        (c, a, d, u) = repo.changes(node1, node2, files, match=match)
    else:
        (c, a, d, u) = changes
    if files:
        c, a, d = map(lambda x: filterfiles(files, x), (c, a, d))

    if not c and not a and not d:
        return

    if node2:
        change = repo.changelog.read(node2)
        mmap2 = repo.manifest.read(change[0])
        date2 = date(change)
        def read(f):
            return repo.file(f).read(mmap2[f])
    else:
        date2 = time.asctime()
        if not node1:
            node1 = repo.dirstate.parents()[0]
        def read(f):
            return repo.wfile(f).read()

    change = repo.changelog.read(node1)
    mmap = repo.manifest.read(change[0])
    date1 = date(change)

    for f in c:
        to = None
        if f in mmap:
            to = repo.file(f).read(mmap[f])
        tn = read(f)
        fp.write("diff --git a/%s b/%s\n" % (f, f))
        fp.write(mdiff.unidiff(to, date1, tn, date2, f, None, text=text))
    for f in a:
        to = None
        tn = read(f)
        fp.write("diff --git /dev/null b/%s\n" % (f))
        fp.write(mdiff.unidiff(to, date1, tn, date2, f, None, text=text))
    for f in d:
        to = repo.file(f).read(mmap[f])
        tn = None
        fp.write("diff --git a/%s /dev/null\n" % (f))
        fp.write(mdiff.unidiff(to, date1, tn, date2, f, None, text=text))

def difftree(ui, repo, node1=None, node2=None, **opts):
    """diff trees from two commits"""
    def __difftree(repo, node1, node2):
        def date(c):
            return time.asctime(time.gmtime(float(c[2].split(' ')[0])))

        if node2:
            change = repo.changelog.read(node2)
            mmap2 = repo.manifest.read(change[0])
            (c, a, d, u) = repo.changes(node1, node2)
            def read(f): return repo.file(f).read(mmap2[f])
            date2 = date(change)
        else:
            date2 = time.asctime()
            (c, a, d, u) = repo.changes(node1, None)
            if not node1:
                node1 = repo.dirstate.parents()[0]
            def read(f): return file(os.path.join(repo.root, f)).read()

        change = repo.changelog.read(node1)
        mmap = repo.manifest.read(change[0])
        date1 = date(change)
        empty = "0" * 40;

        for f in c:
            # TODO get file permissions
            print ":100664 100664 %s %s M\t%s\t%s" % (hg.hex(mmap[f]), 
                                                      hg.hex(mmap2[f]), f, f)
        for f in a:
            print ":000000 100664 %s %s N\t%s\t%s" % (empty, hg.hex(mmap2[f]), f, f)
        for f in d:
            print ":100664 000000 %s %s D\t%s\t%s" % (hg.hex(mmap[f]), empty, f, f)
    ##

    while True:
        if opts['stdin']:
            try:
                line = raw_input().split(' ')
                node1 = line[0]
                if len(line) > 1:
                    node2 = line[1]
                else:
                    node2 = None
            except EOFError:
                break
        node1 = repo.lookup(node1)
        if node2:
            node2 = repo.lookup(node2)
        else:
            node2 = node1
            node1 = repo.changelog.parents(node1)[0]
        if opts['patch']:
            if opts['pretty']:
                catcommit(repo, node2, "")
            dodiff(sys.stdout, ui, repo, node1, node2)
        else:
            __difftree(repo, node1, node2)
        if not opts['stdin']:
            break

def catcommit(repo, n, prefix, changes=None):
    nlprefix = '\n' + prefix;
    (p1, p2) = repo.changelog.parents(n)
    (h, h1, h2) = map(hg.hex, (n, p1, p2))
    (i1, i2) = map(repo.changelog.rev, (p1, p2))
    if not changes:
        changes = repo.changelog.read(n)
    print "tree %s" % (hg.hex(changes[0]))
    if i1 != -1: print "parent %s" % (h1)
    if i2 != -1: print "parent %s" % (h2)
    date_ar = changes[2].split(' ')
    date = int(float(date_ar[0]))
    lines = changes[4].splitlines()
    if lines[-1].startswith('committer:'):
        committer = lines[-1].split(': ')[1].rstrip()
    else:
        committer = "%s %s %s" % (changes[1], date, date_ar[1])
        
    print "author %s %s %s" % (changes[1], date, date_ar[1])
    print "committer %s" % (committer)
    print ""
    if prefix != "":
        print "%s%s" % (prefix, changes[4].replace('\n', nlprefix).strip())
    else:
        print changes[4]
    if prefix:
        sys.stdout.write('\0')

def base(ui, repo, node1, node2):
    """Output common ancestor information"""
    node1 = repo.lookup(node1)
    node2 = repo.lookup(node2)
    n = repo.changelog.ancestor(node1, node2)
    print hg.hex(n)

def catfile(ui, repo, type=None, r=None, **opts):
    """cat a specific revision"""
    # in stdin mode, every line except the commit is prefixed with two
    # spaces.  This way the our caller can find the commit without magic
    # strings
    #
    prefix = ""
    if opts['stdin']:
        try:
            (type, r) = raw_input().split(' ');
            prefix = "    "
        except EOFError:
            return

    else:
        if not type or not r:
            ui.warn("cat-file: type or revision not supplied\n")
            commands.help_(ui, 'cat-file')

    while r:
        if type != "commit":
            sys.stderr.write("aborting hg cat-file only understands commits\n")
            sys.exit(1);
        n = repo.lookup(r)
        catcommit(repo, n, prefix)
        if opts['stdin']:
            try:
                (type, r) = raw_input().split(' ');
            except EOFError:
                break
        else:
            break

# git rev-tree is a confusing thing.  You can supply a number of
# commit sha1s on the command line, and it walks the commit history
# telling you which commits are reachable from the supplied ones via
# a bitmask based on arg position.
# you can specify a commit to stop at by starting the sha1 with ^
def revtree(args, repo, full="tree", maxnr=0, parents=False):
    def chlogwalk():
        ch = repo.changelog
        count = ch.count()
        i = count
        l = [0] * 100
        chunk = 100
        while True:
            if chunk > i:
                chunk = i
                i = 0
            else:
                i -= chunk

            for x in xrange(0, chunk):
                if i + x >= count:
                    l[chunk - x:] = [0] * (chunk - x)
                    break
                if full != None:
                    l[x] = ch.read(ch.node(i + x))
                else:
                    l[x] = 1
            for x in xrange(chunk-1, -1, -1):
                if l[x] != 0:
                    yield (i + x, full != None and l[x] or None)
            if i == 0:
                break
            
    # calculate and return the reachability bitmask for sha
    def is_reachable(ar, reachable, sha):
        if len(ar) == 0:
            return 1
        mask = 0
        for i in range(len(ar)):
            if sha in reachable[i]:
                mask |= 1 << i

        return mask

    reachable = []
    stop_sha1 = []
    want_sha1 = []
    count = 0

    # figure out which commits they are asking for and which ones they
    # want us to stop on
    for i in range(len(args)):
        if args[i].startswith('^'):
            s = repo.lookup(args[i][1:])
            stop_sha1.append(s)
            want_sha1.append(s)
        elif args[i] != 'HEAD':
            want_sha1.append(repo.lookup(args[i]))

    # calculate the graph for the supplied commits
    for i in range(len(want_sha1)):
        reachable.append({});
        n = want_sha1[i];
        visit = [n];
        reachable[i][n] = 1
        while visit:
            n = visit.pop(0)
            if n in stop_sha1:
                continue
            for p in repo.changelog.parents(n):
                if p not in reachable[i]:
                    reachable[i][p] = 1
                    visit.append(p)
                if p in stop_sha1:
                    continue

    # walk the repository looking for commits that are in our
    # reachability graph
    #for i in range(repo.changelog.count()-1, -1, -1):
    for i, changes in chlogwalk():
        n = repo.changelog.node(i)
        mask = is_reachable(want_sha1, reachable, n)
        if mask:
            parentstr = ""
            if parents:
                pp = repo.changelog.parents(n)
                if pp[0] != hg.nullid:
                    parentstr += " " + hg.hex(pp[0])
                if pp[1] != hg.nullid:
                    parentstr += " " + hg.hex(pp[1])
            if not full:
                print hg.hex(n) + parentstr
            elif full is "commit":
                print hg.hex(n) + parentstr
                catcommit(repo, n, '    ', changes)
            else:
                (p1, p2) = repo.changelog.parents(n)
                (h, h1, h2) = map(hg.hex, (n, p1, p2))
                (i1, i2) = map(repo.changelog.rev, (p1, p2))

                date = changes[2].split(' ')[0]
                print "%s %s:%s" % (date, h, mask),
                mask = is_reachable(want_sha1, reachable, p1)
                if i1 != -1 and mask > 0:
                    print "%s:%s " % (h1, mask),
                mask = is_reachable(want_sha1, reachable, p2)
                if i2 != -1 and mask > 0:
                    print "%s:%s " % (h2, mask),
                print ""
            if maxnr and count >= maxnr:
                break
            count += 1

# git rev-list tries to order things by date, and has the ability to stop
# at a given commit without walking the whole repo.  TODO add the stop
# parameter
def revlist(ui, repo, *revs, **opts):
    """print revisions"""
    if opts['header']:
        full = "commit"
    else:
        full = None
    copy = [x for x in revs]
    revtree(copy, repo, full, opts['max_count'], opts['parents'])

def view(ui, repo, *etc):
    "start interactive history viewer"
    os.chdir(repo.root)
    os.system("hgk " + " ".join(etc))

cmdtable = {
    "view": (view, [], 'hg view'),
    "debug-diff-tree": (difftree, [('p', 'patch', None, 'generate patch'),
                            ('r', 'recursive', None, 'recursive'),
                            ('P', 'pretty', None, 'pretty'),
                            ('s', 'stdin', None, 'stdin'),
                            ('C', 'copy', None, 'detect copies'),
                            ('S', 'search', "", 'search')],
                            "hg git-diff-tree [options] node1 node2"),
    "debug-cat-file": (catfile, [('s', 'stdin', None, 'stdin')],
                 "hg debug-cat-file [options] type file"),
    "debug-merge-base": (base, [], "hg debug-merge-base node node"),
    "debug-rev-list": (revlist, [('H', 'header', None, 'header'),
                           ('t', 'topo-order', None, 'topo-order'),
                           ('p', 'parents', None, 'parents'),
                           ('n', 'max-count', 0, 'max-count')],
                 "hg debug-rev-list [options] revs"),
}

def reposetup(ui, repo):
    pass
