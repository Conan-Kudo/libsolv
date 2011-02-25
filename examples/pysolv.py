#!/usr/bin/python

import sys
import os
import glob
import solv
import re
import tempfile
import time
import subprocess
import fnmatch
import rpm
from stat import *
from solv import Pool, Repo, Dataiterator, Job, Solver, Transaction
from iniparse import INIConfig
from optparse import OptionParser

def calc_checksum_stat(stat, type=solv.REPOKEY_TYPE_SHA256):
    chksum = solv.Chksum(type)
    chksum.add("1.1")
    chksum.add(str(stat[ST_DEV]))
    chksum.add(str(stat[ST_INO]))
    chksum.add(str(stat[ST_SIZE]))
    chksum.add(str(stat[ST_MTIME]))
    return chksum.raw()

def calc_checksum_fp(fp, type=solv.REPOKEY_TYPE_SHA256):
    chksum = solv.Chksum(type)
    chksum.addfp(fp)
    return chksum.raw()

def calccachepath(repo, repoext = None):
    path = re.sub(r'^\.', '_', repo['alias'])
    if repoext:
	path += "_" + repoext + ".solvx"
    else:
	path += ".solv"
    return "/var/cache/solv/" + re.sub(r'[/]', '_', path)
    
def usecachedrepo(repo, repoext, mark=False):
    if not repoext:
	cookie = repo['cookie']
    else:
	cookie = repo['extcookie']
    handle = repo['handle']
    try: 
        repopath = calccachepath(repo, repoext)
        f = open(repopath, 'r')
	f.seek(-32, os.SEEK_END)
	fcookie = f.read(32)
	if len(fcookie) != 32:
	    return False
	if cookie and fcookie != cookie:
	    return False
	if repo['alias'] != '@System' and not repoext:
	    f.seek(-32 * 2, os.SEEK_END)
	    fextcookie = f.read(32)
	    if len(fextcookie) != 32:
		return False
        f.seek(0)
        flags = 0
        if repoext:
            flags = Repo.REPO_USE_LOADING|Repo.REPO_EXTEND_SOLVABLES
            if repoext != 'DL':
		flags |= Repo.REPO_LOCALPOOL
        if not repo['handle'].add_solv(f, flags):
	    return False
	if repo['alias'] != '@System' and not repoext:
	    repo['cookie'] = fcookie
	    repo['extcookie'] = fextcookie
	if mark:
	    # no futimes in python?
	    try:
		os.utime(repopath, None)
	    except Exception, e:
		pass
    except IOError, e:
	return False
    return True

def writecachedrepo(repo, repoext, info=None):
    try:
	if not os.path.isdir("/var/cache/solv"):
	    os.mkdir("/var/cache/solv", 0755);
	(fd, tmpname) = tempfile.mkstemp(prefix='.newsolv-', dir='/var/cache/solv')
	os.fchmod(fd, 0444)
        f = os.fdopen(fd, 'w+')
	if not info:
	    repo['handle'].write(f)
	elif repoext:
	    info.write(f)
	else:
	    repo['handle'].write_first_repodata(f)
	if repo['alias'] != '@System' and not repoext:
	    if 'extcookie' not in repo:
		# create unique id
		extcookie = calc_checksum_stat(os.fstat(f.fileno()))
		extcookie = ''.join(chr(ord(s)^ord(c)) for s,c in zip(extcookie, repo['cookie']))
		if ord(extcookie[0]) == 0:
		    extcookie[0] = chr(1)
		repo['extcookie'] = extcookie
	    f.write(repo['extcookie'])
	if not repoext:
	    f.write(repo['cookie'])
	else:
	    f.write(repo['extcookie'])
	f.close()
	os.rename(tmpname, calccachepath(repo, repoext))
    except IOError, e:
	if tmpname:
	    os.unlink(tmpname)

def curlfopen(repo, file, uncompress, chksum, chksumtype, badchecksum=None):
    baseurl = repo['baseurl']
    url = re.sub(r'/$', '', baseurl) + '/' + file;
    f = tempfile.TemporaryFile()
    st = subprocess.call(['curl', '-f', '-s', '-L', url], stdout=f.fileno())
    if os.lseek(f.fileno(), 0, os.SEEK_CUR) == 0 and (st == 0 or not chksumtype):
	return None
    os.lseek(f.fileno(), 0, os.SEEK_SET)
    if st:
	print "%s: download error %d" % (file, st)
	if badchecksum:
	    badchecksum['True'] = 'True'
        return None
    if chksumtype:
	fchksum = solv.Chksum(chksumtype)
	if not fchksum:
	    print "%s: unknown checksum type" % file
	    if badchecksum:
		badchecksum['True'] = 'True'
	    return None
	fchksum.addfd(f.fileno())
	if not fchksum.matches(chksum):
	    print "%s: checksum mismatch" % file
	    if badchecksum:
		badchecksum['True'] = 'True'
	    return None
    if uncompress:
	return solv.xfopen_fd(file, os.dup(f.fileno()))
    return solv.xfopen_fd("", os.dup(f.fileno()))

def repomd_find(repo, what):
    di = repo['handle'].dataiterator_new(solv.SOLVID_META, solv.REPOSITORY_REPOMD_TYPE, what, Dataiterator.SEARCH_STRING)
    di.prepend_keyname(solv.REPOSITORY_REPOMD);
    for d in di:
        d.setpos_parent()
        filename = d.pool.lookup_str(solv.SOLVID_POS, solv.REPOSITORY_REPOMD_LOCATION);
        chksum, chksumtype = d.pool.lookup_bin_checksum(solv.SOLVID_POS, solv.REPOSITORY_REPOMD_CHECKSUM);
        if filename and not chksumtype:
	    print "no %s file checksum!" % filename
	    filename = None
	    chksum = None
        if filename:
            return (filename, chksum, chksumtype)
    return (None, None, None)

def repomd_add_ext(repo, repodata, what):
    filename, chksum, chksumtype = repomd_find(repo, what)
    if not filename:
	return False
    if what == 'prestodelta':
	what = 'deltainfo'
    handle = repodata.new_handle()
    repodata.set_poolstr(handle, solv.REPOSITORY_REPOMD_TYPE, what)
    repodata.set_str(handle, solv.REPOSITORY_REPOMD_LOCATION, filename)
    repodata.set_bin_checksum(handle, solv.REPOSITORY_REPOMD_CHECKSUM, chksumtype, chksum)
    if what == 'deltainfo':
	repodata.add_idarray(handle, solv.REPOSITORY_KEYS, solv.REPOSITORY_DELTAINFO)
	repodata.add_idarray(handle, solv.REPOSITORY_KEYS, solv.REPOKEY_TYPE_FLEXARRAY)
    elif what == 'filelists':
	repodata.add_idarray(handle, solv.REPOSITORY_KEYS, solv.SOLVABLE_FILELIST)
	repodata.add_idarray(handle, solv.REPOSITORY_KEYS, solv.REPOKEY_TYPE_DIRSTRARRAY)
    repodata.add_flexarray(solv.SOLVID_META, solv.REPOSITORY_EXTERNAL, handle)
    return True
    
def repomd_load_ext(repo, repodata):
    repomdtype = repodata.lookup_str(solv.SOLVID_META, solv.REPOSITORY_REPOMD_TYPE)
    if repomdtype == 'filelists':
	ext = 'FL'
    elif repomdtype == 'deltainfo':
	ext = 'DL'
    else:
	return False
    sys.stdout.write("[%s:%s" % (repo['alias'], ext))
    if usecachedrepo(repo, ext):
	sys.stdout.write(" cached]\n")
	sys.stdout.flush()
	return True
    sys.stdout.write(" fetching]\n")
    sys.stdout.flush()
    filename = repodata.lookup_str(solv.SOLVID_META, solv.REPOSITORY_REPOMD_LOCATION)
    filechksum, filechksumtype = repodata.lookup_bin_checksum(solv.SOLVID_META, solv.REPOSITORY_REPOMD_CHECKSUM)
    f = curlfopen(repo, filename, True, filechksum, filechksumtype)
    if not f:
	return False
    if ext == 'FL':
	repo['handle'].add_rpmmd(f, 'FL', Repo.REPO_USE_LOADING|Repo.REPO_EXTEND_SOLVABLES)
    elif ext == 'DL':
	repo['handle'].add_deltainfoxml(f, Repo.REPO_USE_LOADING)
    solv.xfclose(f)
    writecachedrepo(repo, ext, repodata)
    return True

def susetags_find(repo, what):
    di = repo['handle'].dataiterator_new(solv.SOLVID_META, solv.SUSETAGS_FILE_NAME, what, Dataiterator.SEARCH_STRING)
    di.prepend_keyname(solv.SUSETAGS_FILE);
    for d in di:
        d.setpos_parent()
        chksum, chksumtype = d.pool.lookup_bin_checksum(solv.SOLVID_POS, solv.SUSETAGS_FILE_CHECKSUM);
	return (what, chksum, chksumtype)
    return (None, None, None)

def susetags_load_ext(repo, repodata):
    return False

def validarch(pool, arch):
    if not arch:
	return False
    id = pool.str2id(arch, False)
    if not id:
	return False
    return pool.isknownarch(id)

def limitjobs(pool, jobs, flags, evr):
    njobs = []
    for j in jobs:
	how = j.how;
	sel = how & Job.SOLVER_SELECTMASK
	what = pool.rel2id(j.what, evr, flags)
        if flags == solv.REL_ARCH:
	    how |= Job.SOLVER_SETARCH
	if flags == solv.REL_EQ and sel == Job.SOLVER_SOLVABLE_NAME:
	    if pool.id2str(evr).find('-') >= 0:
		how |= Job.SOLVER_SETEVR
	    else:
		how |= Job.SOLVER_SETEV
	njobs.append(pool.Job(how, what))
    return njobs

def limitjobs_arch(pool, jobs, flags, evr):
    m = re.match(r'(.+)\.(.+?)$', evr)
    if m and validarch(pool, m.group(2)):
	jobs = limitjobs(pool, jobs, solv.REL_ARCH, pool.str2id(m.group(2)))
	return limitjobs(pool, jobs, flags, pool.str2id(m.group(1)))
    else:
	return limitjobs(pool, jobs, flags, pool.str2id(evr))

def mkjobs(pool, cmd, arg):
    if len(arg) and arg[0] == '/':
        if re.search(r'[[*?]', arg):
	    type = Dataiterator.SEARCH_GLOB
	else:
	    type = Dataiterator.SEARCH_STRING
        if cmd == 'rm' or cmd == 'erase':
	    di = pool.installed.dataiterator_new(0, solv.SOLVABLE_FILELIST, arg, type | Dataiterator.SEARCH_FILES|Dataiterator.SEARCH_COMPLETE_FILELIST)
	else:
	    di = pool.dataiterator_new(0, solv.SOLVABLE_FILELIST, arg, type | Dataiterator.SEARCH_FILES|Dataiterator.SEARCH_COMPLETE_FILELIST)
        matches = []
	for d in di:
	    s = d.solvable
	    if s and s.installable():
		matches.append(s.id)
		di.skip_solvable()	# one match is enough
	if len(matches):
	    print "[using file list match for '%s']" % arg
	    if len(matches) > 1:
		return [ pool.Job(Job.SOLVER_SOLVABLE_ONE_OF, pool.towhatprovides(matches)) ]
	    else:
		return [ pool.Job(Job.SOLVER_SOLVABLE | Job.SOLVER_NOAUTOSET, matches[0]) ]
    m = re.match(r'(.+?)\s*([<=>]+)\s*(.+?)$', arg)
    if m:
	(name, rel, evr) = m.group(1, 2, 3);
	flags = 0
	if rel.find('<') >= 0: flags |= solv.REL_LT
	if rel.find('=') >= 0: flags |= solv.REL_EQ 
	if rel.find('>') >= 0: flags |= solv.REL_GT
	jobs = depglob(pool, name, True, True)
	if len(jobs):
	    return limitjobs(pool, jobs, flags, pool.str2id(evr))
	m = re.match(r'(.+)\.(.+?)$', name)
	if m and validarch(pool, m.group(2)):
	    jobs = depglob(pool, m.group(1), True, True)
	    if len(jobs):
		jobs = limitjobs(pool, jobs, solv.REL_ARCH, pool.str2id(m.group(2)))
		return limitjobs(pool, jobs, flags, pool.str2id(evr))
    else:
	jobs = depglob(pool, arg, True, True)
        if len(jobs):
	    return jobs
	m = re.match(r'(.+)\.(.+?)$', arg)
	if m and validarch(pool, m.group(2)):
	    jobs = depglob(pool, m.group(1), True, True)
	    if len(jobs):
		return limitjobs(pool, jobs, solv.REL_ARCH, pool.str2id(m.group(2)))
	m = re.match(r'(.+)-(.+?)$', arg)
	if m:
	    jobs = depglob(pool, m.group(1), True, False)
	    if len(jobs):
		return limitjobs_arch(pool, jobs, solv.REL_EQ, m.group(2))
	m = re.match(r'(.+)-(.+?-.+?)$', arg)
	if m:
	    jobs = depglob(pool, m.group(1), True, False)
	    if len(jobs):
		return limitjobs_arch(pool, jobs, solv.REL_EQ, m.group(2))
    return []
    
	    
def depglob(pool, name, globname, globdep):
    id = pool.str2id(name, False)
    if id:
	match = False
	for s in pool.providers(id):
	    if globname and s.nameid == id:
		return [ pool.Job(Job.SOLVER_SOLVABLE_NAME, id) ]
	    match = True
	if match:
	    if globname and globdep:
		print "[using capability match for '%s']" % name
	    return [ pool.Job(Job.SOLVER_SOLVABLE_PROVIDES, id) ]
    if not re.search(r'[[*?]', name):
	return []
    if globname:
	# try name glob
	idmatches = {}
	for s in pool.solvables:
	    if s.installable() and fnmatch.fnmatch(s.name, name):
		idmatches[s.nameid] = True
	if len(idmatches):
	    return [ pool.Job(Job.SOLVER_SOLVABLE_NAME, id) for id in sorted(idmatches.keys()) ]
    if globdep:
	# try dependency glob
	idmatches = {}
	for id in pool.allprovidingids():
	    if fnmatch.fnmatch(pool.id2str(id), name):
		idmatches[id] = True
	if len(idmatches):
	    print "[using capability match for '%s']" % name
	    return [ pool.Job(Job.SOLVER_SOLVABLE_PROVIDES, id) for id in sorted(idmatches.keys()) ]
    return []
    
def load_stub(repodata):
    if repodata.lookup_str(solv.SOLVID_META, solv.REPOSITORY_REPOMD_TYPE):
	return repomd_load_ext(repodata.repo.appdata, repodata)
    if repodata.lookup_str(solv.SOLVID_META, solv.SUSETAGS_FILE_NAME):
	return susetags_load_ext(repodata.repo.appdata, repodata)
    return False
    

parser = OptionParser(usage="usage: solv.py [options] COMMAND")
(options, args) = parser.parse_args()
if not args:
    parser.print_help(sys.stderr)
    sys.exit(1)

cmd = args[0]
args = args[1:]

pool = solv.Pool()
pool.setarch(os.uname()[4])
pool.set_loadcallback(load_stub)

repos = []
for reposdir in ["/etc/zypp/repos.d"]:
    if not os.path.isdir(reposdir):
	continue
    for reponame in sorted(glob.glob('%s/*.repo' % reposdir)):
	cfg = INIConfig(open(reponame))
	for alias in cfg:
	    repo = cfg[alias]
	    repo['alias'] = alias
	    if 'baseurl' not in repo:
		print "repo %s has no baseurl" % alias
		continue
	    if 'priority' not in repo:
		repo['priority'] = 99
	    if 'autorefresh' not in repo:
		repo['autorefresh'] = 1
	    if 'type' not in repo:
		repo['type'] = 'rpm-md'
	    repo['metadata_expire'] = 900
	    repos.append(repo)

print "rpm database:",
sysrepo = { 'alias': '@System' }
sysrepo['handle'] = pool.add_repo(sysrepo['alias'])
sysrepo['handle'].appdata = sysrepo
pool.installed = sysrepo['handle']
sysrepostat = os.stat("/var/lib/rpm/Packages")
sysrepocookie = calc_checksum_stat(sysrepostat)
sysrepo['cookie'] = sysrepocookie
if usecachedrepo(sysrepo, None):
    print "cached"
else:
    print "reading"
    sysrepo['handle'].add_products("/etc/products.d", Repo.REPO_NO_INTERNALIZE);
    sysrepo['handle'].add_rpmdb(None)
    writecachedrepo(sysrepo, None)

for repo in repos:
    if not int(repo.enabled):
	continue
    repo['handle'] = pool.add_repo(repo['alias'])
    repo['handle'].appdata = repo
    repo['handle'].priority = 99 - repo['priority']
    if repo['autorefresh']:
	dorefresh = True
    if dorefresh:
	try:
	    st = os.stat(calccachepath(repo))
	    if time.time() - st[ST_MTIME] < repo['metadata_expire']:
		dorefresh = False
	except OSError, e:
	    pass
    repo['cookie'] = None
    if not dorefresh and usecachedrepo(repo, None):
	print "repo: '%s': cached" % repo['alias']
	continue

    badchecksum = {}

    if repo['type'] == 'rpm-md':
	print "rpmmd repo '%s':" % repo['alias'],
	sys.stdout.flush()
	f = curlfopen(repo, "repodata/repomd.xml", False, None, None)
	if not f:
	    print "no repomd.xml file, skipped"
	    repo['handle'].free(True)
	    del repo['handle']
	    continue
	repo['cookie'] = calc_checksum_fp(f)
	if usecachedrepo(repo, None, True):
	    print "cached"
	    solv.xfclose(f)
	    continue
	repo['handle'].add_repomdxml(f, 0)
	solv.xfclose(f)
	print "fetching"
	(filename, filechksum, filechksumtype) = repomd_find(repo, 'primary')
	if filename:
	    f = curlfopen(repo, filename, True, filechksum, filechksumtype, badchecksum)
	    if f:
		repo['handle'].add_rpmmd(f, None, 0)
		solv.xfclose(f)
	    if badchecksum:
		continue	# hopeless, need good primary
	(filename, filechksum, filechksumtype) = repomd_find(repo, 'updateinfo')
	if filename:
	    f = curlfopen(repo, filename, True, filechksum, filechksumtype, badchecksum)
	    if f:
		repo['handle'].add_updateinfoxml(f, 0)
		solv.xfclose(f)
	repodata = repo['handle'].add_repodata(0)
	if not repomd_add_ext(repo, repodata, 'deltainfo'):
	    repomd_add_ext(repo, repodata, 'prestodelta')
	repomd_add_ext(repo, repodata, 'filelists')
	repodata.internalize()
    elif repo['type'] == 'yast2':
	print "susetags repo '%s':" % repo['alias'],
	sys.stdout.flush()
	f = curlfopen(repo, "content", False, None, None)
        if not f:
	    print "no content file, skipped"
	    repo['handle'].free(True)
	    del repo['handle']
	    continue
	repo['cookie'] = calc_checksum_fp(f)
	if usecachedrepo(repo, None, True):
	    print "cached"
	    solv.xfclose(f)
	    continue
	repo['handle'].add_content(f, 0)
	solv.xfclose(f)
	print "fetching"
	defvendorid = repo['handle'].lookup_id(solv.SOLVID_META, solv.SUSETAGS_DEFAULTVENDOR);
	descrdir = repo['handle'].lookup_str(solv.SOLVID_META, solv.SUSETAGS_DESCRDIR);
	if not descrdir:
	    descrdir = "suse/setup/descr"
	(filename, filechksum, filechksumtype) = susetags_find(repo, 'packages.gz')
	if not filename:
	    (filename, filechksum, filechksumtype) = susetags_find(repo, 'packages')
	if filename:
	    f = curlfopen(repo, descrdir + '/' + filename, True, filechksum, filechksumtype, badchecksum)
	    if f:
		repo['handle'].add_susetags(f, defvendorid, None, Repo.REPO_NO_INTERNALIZE|Repo.SUSETAGS_RECORD_SHARES)
		solv.xfclose(f)
		(filename, filechksum, filechksumtype) = susetags_find(repo, 'packages.en.gz')
		if not filename:
		    (filename, filechksum, filechksumtype) = susetags_find(repo, 'packages.en')
		if filename:
		    f = curlfopen(repo, descrdir + '/' + filename, True, filechksum, filechksumtype, badchecksum)
		    if f:
			repo['handle'].add_susetags(f, defvendorid, None, Repo.REPO_NO_INTERNALIZE|Repo.REPO_REUSE_REPODATA|Repo.REPO_EXTEND_SOLVABLES)
			solv.xfclose(f)
		repo['handle'].internalize()
    else:
	print "unsupported repo '%s': skipped" % repo['alias']
	repo['handle'].free(True)
	del repo['handle']
	continue

    # if the checksum was bad we work with the data we got, but don't cache it
    if 'True' not in badchecksum:
	writecachedrepo(repo, None)
    # must be called after writing the repo
    repo['handle'].create_stubs()
    
if cmd == 'se' or cmd == 'search':
    matches = {}
    di = pool.dataiterator_new(0, solv.SOLVABLE_NAME, args[0], Dataiterator.SEARCH_SUBSTRING|Dataiterator.SEARCH_NOCASE)
    for d in di:
	matches[di.solvid] = True
    for solvid in sorted(matches.keys()):
	print " - %s [%s]: %s" % (pool.solvid2str(solvid), pool.solvables[solvid].repo.name, pool.lookup_str(solvid, solv.SOLVABLE_SUMMARY))
    sys.exit(0)

# XXX: insert rewrite_repos function

pool.addfileprovides()
pool.createwhatprovides()

jobs = []
for arg in args:
    argjob = mkjobs(pool, cmd, arg)
    jobs += argjob

if cmd == 'li' or cmd == 'list' or cmd == 'info':
    if not jobs:
	print "no package matched."
	sys.exit(1)
    for job in jobs:
	for s in pool.jobsolvables(job):
	    if cmd == 'info':
		print "Name:        %s" % s.str()
		print "Repo:        %s" % s.repo.name
		print "Summary:     %s" % s.lookup_str(solv.SOLVABLE_SUMMARY)
		str = s.lookup_str(solv.SOLVABLE_URL)
		if str:
		    print "Url:         %s" % str
		str = s.lookup_str(solv.SOLVABLE_LICENSE)
		if str:
		    print "License:     %s" % str
		print "Description:\n%s" % s.lookup_str(solv.SOLVABLE_DESCRIPTION)
		print
	    else:
		print "  - %s [%s]" % (s.str(), s.repo.name)
		print "    %s" % s.lookup_str(solv.SOLVABLE_SUMMARY)
    sys.exit(0)

if cmd == 'in' or cmd == 'install' or cmd == 'rm' or cmd == 'erase' or cmd == 'up':
    if cmd == 'up' and not jobs:
	jobs = [ pool.Job(Job.SOLVER_SOLVABLE_ALL, 0) ]
    if not jobs:
	print "no package matched."
	sys.exit(1)
    for job in jobs:
	if cmd == 'up':
	    if job.how == Job.SOLVER_SOLVABLE_ALL or filter(lambda s: s.isinstalled(), pool.jobsolvables(job)):
		job.how |= Job.SOLVER_UPDATE
	    else:
		job.how |= Job.SOLVER_INSTALL
	if cmd == 'in' or cmd == 'install':
	    job.how |= Job.SOLVER_INSTALL
	elif cmd == 'rm' or cmd == 'erase':
	    job.how |= Job.SOLVER_ERASE

    #pool.set_debuglevel(2)
    solver = pool.create_solver()
    solver.ignorealreadyrecommended = True
    problems = solver.solve(jobs)
    if problems:
	for problem in problems:
	    print "Problem %d:" % problem.id
	    r = problem.findproblemrule()
	    type, source, target, dep = r.info()
	    if type == Solver.SOLVER_RULE_DISTUPGRADE:
		print "%s does not belong to a distupgrade repository" % source.str()
	    elif type == Solver.SOLVER_RULE_INFARCH:
		print "%s has inferiour architecture" % source.str()
	    elif type == Solver.SOLVER_RULE_UPDATE:
		print "problem with installed package %s" % source.str()
	    elif type == Solver.SOLVER_RULE_JOB:
		print "conflicting requests"
	    elif type == Solver.SOLVER_RULE_JOB_NOTHING_PROVIDES_DEP:
		print "nothing provides requested %s" % pool.dep2str(dep)
	    elif type == Solver.SOLVER_RULE_RPM:
		print "some dependency problem"
	    elif type == Solver.SOLVER_RULE_RPM_NOT_INSTALLABLE:
		print "package %s is not installable" % source.str()
	    elif type == Solver.SOLVER_RULE_RPM_NOTHING_PROVIDES_DEP:
		print "nothing provides %s needed by %s" % (pool.dep2str(dep), source.str())
	    elif type == Solver.SOLVER_RULE_RPM_SAME_NAME:
		print "cannot install both %s and %s" % (source.str(), target.str())
	    elif type == Solver.SOLVER_RULE_RPM_PACKAGE_CONFLICT:
		print "package %s conflicts with %s provided by %s" % (source.str(), pool.dep2str(dep), target.str())
	    elif type == Solver.SOLVER_RULE_RPM_PACKAGE_OBSOLETES:
		print "package %s obsoletes %s provided by %s" % (source.str(), pool.dep2str(dep), target.str())
	    elif type == Solver.SOLVER_RULE_RPM_INSTALLEDPKG_OBSOLETES:
		print "installed package %s obsoletes %s provided by %s" % (source.str(), pool.dep2str(dep), target.str())
	    elif type == Solver.SOLVER_RULE_RPM_IMPLICIT_OBSOLETES:
		print "package %s implicitely obsoletes %s provided by %s" % (source.str(), pool.dep2str(dep), target.str())
	    elif type == Solver.SOLVER_RULE_RPM_PACKAGE_REQUIRES:
		print "package %s requires %s, but none of the providers can be installed" % (source.str(), pool.dep2str(dep))
	    elif type == Solver.SOLVER_RULE_RPM_SELF_CONFLICT:
		print "package %s conflicts with %s provided by itself" % (source.str(), pool.dep2str(dep))
	    else:
		print "bad rule type", type
	    solutions = problem.solutions()
	    for solution in solutions:
	        print "  Solution %d:" % solution.id
	        elements = solution.elements()
                for element in elements:
		    etype = element.type
		    if etype == Solver.SOLVER_SOLUTION_JOB:
			print "  - remove job %d" % element.jobidx
		    elif etype == Solver.SOLVER_SOLUTION_INFARCH:
			if element.solvable.isinstalled():
			    print "  - keep %s despite the inferior architecture" % element.solvable.str()
			else:
			    print "  - install %s despite the inferior architecture" % element.solvable.str()
		    elif etype == Solver.SOLVER_SOLUTION_DISTUPGRADE:
			if element.solvable.isinstalled():
			    print "  - keep obsolete %s" % element.solvable.str()
			else:
			    print "  - install %s from excluded repository" % element.solvable.str()
		    elif etype == Solver.SOLVER_SOLUTION_REPLACE:
			print "  - allow replacement of %s with %s" % (element.solvable.str(), element.replacement.str())
		    elif etype == Solver.SOLVER_SOLUTION_DEINSTALL:
			print "  - allow deinstallation of %s" % element.solvable.str()
	sys.exit(1)
    # no problems, show transaction
    trans = solver.transaction()
    del solver
    if trans.isempty():
        print "Nothing to do."
        sys.exit(0)
    print
    print "Transaction summary:"
    print
    for ctype, pkgs, fromid, toid in trans.classify():
	if ctype == Transaction.SOLVER_TRANSACTION_ERASE:
	    print "%d erased packages:" % len(pkgs)
	elif ctype == Transaction.SOLVER_TRANSACTION_INSTALL:
	    print "%d installed packages:" % len(pkgs)
	elif ctype == Transaction.SOLVER_TRANSACTION_REINSTALLED:
	    print "%d reinstalled packages:" % len(pkgs)
	elif ctype == Transaction.SOLVER_TRANSACTION_DOWNGRADED:
	    print "%d downgraded packages:" % len(pkgs)
	elif ctype == Transaction.SOLVER_TRANSACTION_CHANGED:
	    print "%d changed packages:" % len(pkgs)
	elif ctype == Transaction.SOLVER_TRANSACTION_UPGRADED:
	    print "%d upgraded packages:" % len(pkgs)
	elif ctype == Transaction.SOLVER_TRANSACTION_VENDORCHANGE:
	    print "%d vendor changes from '%s' to '%s':" % (len(pkgs), pool.id2str(fromid), pool.id2str(toid))
	elif ctype == Transaction.SOLVER_TRANSACTION_ARCHCHANGE:
	    print "%d arch changes from '%s' to '%s':" % (len(pkgs), pool.id2str(fromid), pool.id2str(toid))
	else:
	    continue
	for p in pkgs:
	    if ctype == Transaction.SOLVER_TRANSACTION_UPGRADED or ctype == Transaction.SOLVER_TRANSACTION_DOWNGRADED:
		op = trans.othersolvable(p)
		print "  - %s -> %s" % (p.str(), op.str())
	    else:
		print "  - %s" % p.str()
        print
    print "install size change: %d K" % trans.calc_installsizechange()
    print
    
# vim: sw=4 et
    while True:
	sys.stdout.write("OK to continue (y/n)? ")
	sys.stdout.flush()
	yn = sys.stdin.readline().strip()
	if yn == 'y': break
	if yn == 'n': sys.exit(1)
    newpkgs, keptpkgs = trans.installedresult()
    newpkgsfp = {}
    if newpkgs:
	downloadsize = 0
	for p in newpkgs:
	    downloadsize += p.lookup_num(solv.SOLVABLE_DOWNLOADSIZE);
	print "Downloading %d packages, %d K" % (len(newpkgs), downloadsize)
	for p in newpkgs:
	    repo = p.repo.appdata
	    location, medianr = p.lookup_location()
	    if not location:
		continue
	    if sysrepo['handle'].nsolvables:
		pname = p.name
		di = p.repo.dataiterator_new(solv.SOLVID_META, solv.DELTA_PACKAGE_NAME, pname, Dataiterator.SEARCH_STRING)
		di.prepend_keyname(solv.REPOSITORY_DELTAINFO);
		for d in di:
		    d.setpos_parent()
		    if pool.lookup_id(solv.SOLVID_POS, solv.DELTA_PACKAGE_EVR) != p.evrid or pool.lookup_id(solv.SOLVID_POS, solv.DELTA_PACKAGE_ARCH) != p.archid:
			continue
		    baseevrid = pool.lookup_id(solv.SOLVID_POS, solv.DELTA_BASE_EVR)
		    candidate = None
		    for installedp in pool.providers(p.nameid):
			if installedp.isinstalled() and installedp.nameid == p.nameid and installedp.archid == p.archid and installedp.evrid == baseevrid:
			    candidate = installedp
		    if candidate:
			# add applydeltarpm code here...
			print "PKG", p.str(), "CANDIDATE", candidate.str()
	    chksum, chksumtype = p.lookup_bin_checksum(solv.SOLVABLE_CHECKSUM);
	    f = curlfopen(repo, location, False, chksum, chksumtype)
	    if not f:
		sys.exit("\n%s: %s not found in repository" % (repo['alias'], location))
	    newpkgsfp[p.id] = f
	    sys.stdout.write(".")
	    sys.stdout.flush()
	print
    print "Committing transaction:"
    print
    ts = rpm.TransactionSet('/')
    erasenamehelper = {}
    for p in trans.steps():
	type = trans.steptype(p, Transaction.SOLVER_TRANSACTION_RPM_ONLY)
	if type == Transaction.SOLVER_TRANSACTION_ERASE:
	    rpmdbid = p.lookup_num(solv.RPM_RPMDBID)
	    erasenamehelper[p.name] = p
	    if not rpmdbid:
		sys.exit("\ninternal error: installed package %s has no rpmdbid\n" % p.str())
	    ts.addErase(rpmdbid)
	elif type == Transaction.SOLVER_TRANSACTION_INSTALL:
	    f = newpkgsfp[p.id]
	    h = ts.hdrFromFdno(solv.xfileno(f))
	    os.lseek(solv.xfileno(f), 0, os.SEEK_SET)
	    ts.addInstall(h, p, 'u')
	elif type == Transaction.SOLVER_TRANSACTION_MULTIINSTALL:
	    f = newpkgsfp[p.id]
	    h = ts.hdrFromFdno(solv.xfileno(f))
	    os.lseek(solv.xfileno(f), 0, os.SEEK_SET)
	    ts.addInstall(h, p, 'i')
    checkproblems = ts.check()
    if checkproblems:
	print checkproblems
	sys.exit("Sorry.")
    ts.order()
    def runCallback(reason, amount, total, p, d):
	if reason == rpm.RPMCALLBACK_INST_OPEN_FILE:
	    return solv.xfileno(newpkgsfp[p.id])
	if reason == rpm.RPMCALLBACK_INST_START:
	    print "install", p.str()
	if reason == rpm.RPMCALLBACK_UNINST_START:
	    # argh, p is just the name of the package
	    if p in erasenamehelper:
		p = erasenamehelper[p]
		print "erase", p.str()
    runproblems = ts.run(runCallback, '')
    if runproblems:
	print runproblems
	sys.exit(1)