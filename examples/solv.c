#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <dirent.h>
#include <unistd.h>
#include <zlib.h>
#include <sys/utsname.h>
#include <sys/types.h>
#include <sys/wait.h>

#include "pool.h"
#include "poolarch.h"
#include "repo.h"
#include "util.h"
#include "solver.h"
#include "solverdebug.h"

#include "repo_rpmdb.h"
#include "repo_rpmmd.h"
#include "repo_susetags.h"
#include "repo_repomdxml.h"
#include "pool_fileconflicts.h"

/* solv, a little demo application */

struct repoinfo {
  Repo *repo;

  char *alias;
  char *name;
  int enabled;
  int autorefresh;
  char *baseurl;
  char *path;
  int type;
  int priority;
  int keeppackages;
};

#define TYPE_UNKNOWN 0
#define TYPE_SUSETAGS 1
#define TYPE_RPMMD 2

struct repoinfo *
read_repoinfos(Pool *pool, const char *reposdir, int *nrepoinfosp)
{
  char buf[4096];
  char buf2[4096], *kp, *vp, *kpe;
  DIR *dir;
  FILE *fp;
  struct dirent *ent;
  int l, rdlen;
  struct repoinfo *repoinfos = 0, *cinfo;
  int nrepoinfos = 0;

  rdlen = strlen(reposdir);
  dir = opendir(reposdir);
  if (!dir)
    {
      *nrepoinfosp = 0;
      return 0;
    }
  while ((ent = readdir(dir)) != 0)
    {
      l = strlen(ent->d_name);
      if (l < 6 || rdlen + 2 + l >= sizeof(buf) || strcmp(ent->d_name + l - 5, ".repo") != 0)
	continue;
      snprintf(buf, sizeof(buf), "%s/%s", reposdir, ent->d_name);
      if ((fp = fopen(buf, "r")) == 0)
	{
	  perror(buf);
	  continue;
	}
      cinfo = 0;
      while(fgets(buf2, sizeof(buf2), fp))
	{
	  l = strlen(buf2);
	  if (l == 0)
	    continue;
	  while (buf2[l - 1] == '\n' || buf2[l - 1] == ' ' || buf2[l - 1] == '\t')
	    buf2[--l] = 0;
	  kp = buf2;
	  while (*kp == ' ' || *kp == '\t')
	    kp++;
	  if (!*kp || *kp == '#')
	    continue;
	  if (!cinfo)
	    {
	      if (*kp != '[')
		continue;
	      vp = strrchr(kp, ']');
	      if (!vp)
		continue;
	      *vp = 0;
	      repoinfos = sat_extend(repoinfos, nrepoinfos, 1, sizeof(*repoinfos), 15);
	      cinfo = repoinfos + nrepoinfos++;
	      memset(cinfo, 0, sizeof(*cinfo));
	      cinfo->alias = strdup(kp + 1);
	      continue;
	    }
          vp = strchr(kp, '=');
	  if (!vp)
	    continue;
	  for (kpe = vp - 1; kpe >= kp; kpe--)
	    if (*kpe != ' ' && *kpe != '\t')
	      break;
	  if (kpe == kp)
	    continue;
	  vp++;
	  while (*vp == ' ' || *vp == '\t')
	    vp++;
	  kpe[1] = 0;
	  if (!strcmp(kp, "name"))
	    cinfo->name = strdup(vp);
	  else if (!strcmp(kp, "enabled"))
	    cinfo->enabled = *vp == '0' ? 0 : 1;
	  else if (!strcmp(kp, "autorefresh"))
	    cinfo->autorefresh = *vp == '0' ? 0 : 1;
	  else if (!strcmp(kp, "baseurl"))
	    cinfo->baseurl = strdup(vp);
	  else if (!strcmp(kp, "path"))
	    cinfo->path = strdup(vp);
	  else if (!strcmp(kp, "type"))
	    {
	      if (!strcmp(vp, "yast2"))
	        cinfo->type = TYPE_SUSETAGS;
	      else if (!strcmp(vp, "rpm-md"))
	        cinfo->type = TYPE_RPMMD;
	      else
	        cinfo->type = TYPE_UNKNOWN;
	    }
	  else if (!strcmp(kp, "priority"))
	    cinfo->priority = atoi(vp);
	  else if (!strcmp(kp, "keeppackages"))
	    cinfo->keeppackages = *vp == '0' ? 0 : 1;
	}
      fclose(fp);
      if (cinfo->type == TYPE_SUSETAGS && cinfo->baseurl)
	{
	  char *old = cinfo->baseurl;
	  cinfo->baseurl = sat_malloc(5 + strlen(old) + 1);
	  sprintf(cinfo->baseurl, "%s/suse", old);
	  free(old);
	}
      cinfo = 0;
    }
  closedir(dir);
  *nrepoinfosp = nrepoinfos;
  return repoinfos;
}

static ssize_t
cookie_gzread(void *cookie, char *buf, size_t nbytes)
{
  return gzread((gzFile *)cookie, buf, nbytes);
}

static int
cookie_gzclose(void *cookie)
{
  return gzclose((gzFile *)cookie);
}

FILE *
myfopen(const char *fn)
{
  cookie_io_functions_t cio;
  char *suf;
  gzFile *gzf;

  if (!fn)
    return 0;
  suf = strrchr(fn, '.');
  if (!suf || strcmp(suf, ".gz") != 0)
    return fopen(fn, "r");
  gzf = gzopen(fn, "r");
  if (!gzf)
    return 0;
  memset(&cio, 0, sizeof(cio));
  cio.read = cookie_gzread;
  cio.close = cookie_gzclose;
  return  fopencookie(gzf, "r", cio);
}

FILE *
curlfopen(char *baseurl, char *file, int uncompress)
{
  pid_t pid;
  int fd, l;
  int status;
  char tmpl[100];
  char url[4096];

  l = strlen(baseurl);
  if (l && baseurl[l - 1] == '/')
    snprintf(url, sizeof(url), "%s%s", baseurl, file);
  else
    snprintf(url, sizeof(url), "%s/%s", baseurl, file);
  strcpy(tmpl, "/var/tmp/solvXXXXXX");
  fd = mkstemp(tmpl);
  if (fd < 0)
    {
      perror("mkstemp");
      exit(1);
    }
  unlink(tmpl);
  if ((pid = fork()) == (pid_t)-1)
    {
      perror("fork");
      exit(1);
    }
  if (pid == 0)
    {
      if (fd != 1)
	{
          dup2(fd, 1);
	  close(fd);
	}
      execlp("curl", "curl", "-s", "-L", url, (char *)0);
      perror("curl");
      _exit(0);
    }
  while (waitpid(pid, &status, 0) != pid)
    ;
  lseek(fd, 0, SEEK_SET);
  if (uncompress)
    {
      cookie_io_functions_t cio;
      gzFile *gzf;

      sprintf(tmpl, "/dev/fd/%d", fd);
      gzf = gzopen(tmpl, "r");
      close(fd);
      if (!gzf)
	return 0;
      memset(&cio, 0, sizeof(cio));
      cio.read = cookie_gzread;
      cio.close = cookie_gzclose;
      return fopencookie(gzf, "r", cio);
    }
  return fdopen(fd, "r");
}

void
setarch(Pool *pool)
{
  struct utsname un;
  if (uname(&un))
    {
      perror("uname");
      exit(1);
    }
  pool_setarch(pool, un.machine);
}

void
read_repos(Pool *pool, struct repoinfo *repoinfos, int nrepoinfos)
{
  Repo *repo;
  struct repoinfo *cinfo;
  int i;
  FILE *fp;

  printf("reading rpm database\n");
  repo = repo_create(pool, "@System");
  repo_add_rpmdb(repo, 0, 0, 0);
  pool_set_installed(pool, repo);
  for (i = 0; i < nrepoinfos; i++)
    {
      cinfo = repoinfos + i;
      if (!cinfo->enabled)
	continue;
      switch (cinfo->type)
	{
        case TYPE_RPMMD:
	  printf("reading rpmmd repo '%s'\n", cinfo->alias);
	  if ((fp = curlfopen(cinfo->baseurl, "repodata/repomd.xml", 0)) == 0)
	    break;
	  repo = repo_create(pool, cinfo->alias);
	  cinfo->repo = repo;
	  repo_add_repomdxml(repo, fp, 0);
	  fclose(fp);
	  if ((fp = curlfopen(cinfo->baseurl, "repodata/primary.xml.gz", 1)) == 0)
	    continue;
	  repo_add_rpmmd(repo, fp, 0, 0);
	  fclose(fp);
	  break;
        case TYPE_SUSETAGS:
	  printf("reading susetags repo '%s'\n", cinfo->alias);
	  if ((fp = curlfopen(cinfo->baseurl, "setup/descr/packages.gz", 1)) == 0)
	    break;
	  repo = repo_create(pool, cinfo->alias);
	  cinfo->repo = repo;
	  repo_add_susetags(repo, fp, 0, 0, 0);
	  fclose(fp);
	  break;
	default:
	  printf("skipping unknown repo '%s'\n", cinfo->alias);
	  break;
	}
    }
}

void
mkselect(Pool *pool, const char *arg, int flags, Queue *out)
{
  Id id, p, pp;
  Id type = 0;
  const char *r, *r2;

  id = str2id(pool, arg, 0);
  if (id)
    {
      FOR_PROVIDES(p, pp, id)
	{
	  Solvable *s = pool_id2solvable(pool, p);
	  if (s->name == id)
	    {
	      type = SOLVER_SOLVABLE_NAME;
	      break;
	    }
	  type = SOLVER_SOLVABLE_PROVIDES;
	}
    }
  if (!type)
    {
      /* did not find a solvable, see if it's a relation */
      if ((r = strpbrk(arg, "<=>")) != 0)
	{
	  Id rid, rname, revr;
	  int rflags = 0;
	  for (r2 = r; r2 > arg && (r2[-1] == ' ' || r2[-1] == '\t'); )
	    r2--;
	  rname = r2 > arg ? strn2id(pool, arg, r2 - arg, 1) : 0;
	  for (; *r; r++)
	    {
	      if (*r == '<')
		rflags |= REL_LT;
	      else if (*r == '=')
		rflags |= REL_EQ;
	      else if (*r == '>')
		rflags |= REL_GT;
	      else
		break;
	    }
	  while (*r == ' ' || *r == '\t')
	    r++;
	  revr = *r ? str2id(pool, r, 1) : 0;
	  rid = rname && revr ? rel2id(pool, rname, revr, rflags, 1) : 0;
	  if (rid)
	    {
	      FOR_PROVIDES(p, pp, rid)
		{
		  Solvable *s = pool_id2solvable(pool, p);
		  if (pool_match_nevr(pool, s, rid))
		    {
		      type = SOLVER_SOLVABLE_NAME;
		      break;
		    }
		  type = SOLVER_SOLVABLE_PROVIDES;
		}
	    }
	  if (type)
	    id = rid;
	}
    }
  if (type)
    {
      queue_push(out, type);
      queue_push(out, id);
    }
}

int
yesno(const char *str)
{
  char inbuf[128], *ip;

  for (;;)
    {
      printf("%s", str);
      fflush(stdout);
      *inbuf = 0;
      if (!(ip = fgets(inbuf, sizeof(inbuf), stdin)))
	{
	  printf("Abort.\n");
	  exit(1);
	}
      while (*ip == ' ' || *ip == '\t')
	ip++;
      if (*ip == 'q')
	{
	  printf("Abort.\n");
	  exit(1);
	}
      if (*ip == 'y' || *ip == 'n')
	return *ip == 'y' ? 1 : 0;
    }
}

struct fcstate {
  FILE **newpkgsfps;
  Id *newpkgsps;
  int newpkgscnt;
  void *rpmdbstate;
};

static void *
fc_cb(Pool *pool, Id p, void *cbdata)
{
  struct fcstate *fcstate = cbdata;
  Solvable *s;
  Id rpmdbid;
  int i;
  FILE *fp;

  if (!p)
    {
      rpm_byrpmdbid(0, 0, &fcstate->rpmdbstate);
      return 0;
    }
  s = pool_id2solvable(pool, p);
  if (pool->installed && s->repo == pool->installed)
    {
      if (!s->repo->rpmdbid)
	return 0;
      rpmdbid = s->repo->rpmdbid[p - s->repo->start];
      if (!rpmdbid)
	return 0;
       return rpm_byrpmdbid(rpmdbid, 0, &fcstate->rpmdbstate);
    }
  for (i = 0; i < fcstate->newpkgscnt; i++)
    if (fcstate->newpkgsps[i] == p)
      break;
  if (i == fcstate->newpkgscnt)
    return 0;
  fp = fcstate->newpkgsfps[i];
  rewind(fp);
  return rpm_byfp(fp, solvable2str(pool, s), &fcstate->rpmdbstate);
}

void
runrpm(const char *arg, const char *name)
{
  pid_t pid;
  int status;

  if ((pid = fork()) == (pid_t)-1)
    {
      perror("fork");
      exit(1);
    }
  if (pid == 0)
    {
      if (strcmp(arg, "-e") == 0)
        execlp("rpm", "rpm", arg, "--nodeps", "--nodigest", "--nosignature", name, (char *)0);
      else
        execlp("rpm", "rpm", arg, "--force", "--nodeps", "--nodigest", "--nosignature", name, (char *)0);
      perror("rpm");
      _exit(0);
    }
  while (waitpid(pid, &status, 0) != pid)
    ;
  if (status)
    {
      printf("rpm failed\n");
      exit(1);
    }
}

int
main(int argc, char **argv)
{
  Pool *pool;
  Id p, pp;
  struct repoinfo *repoinfos;
  int nrepoinfos = 0;
  int i, mode, newpkgs;
  Queue job, checkq;
  Solver *solv = 0;
  Transaction *trans;
  char inbuf[128], *ip;
  int updateall = 0;
  FILE **newpkgsfps = 0;
  Id *newpkgsps = 0;
  struct fcstate fcstate;

  pool = pool_create();
  // pool_setdebuglevel(pool, 2);
  setarch(pool);
  repoinfos = read_repoinfos(pool, "/etc/zypp/repos.d", &nrepoinfos);
  read_repos(pool, repoinfos, nrepoinfos);
  // FOR_REPOS(i, repo)
  //   printf("%s: %d solvables\n", repo->name, repo->nsolvables);
  pool_addfileprovides(pool);
  pool_createwhatprovides(pool);
  if (!strcmp(argv[1], "install") || !strcmp(argv[1], "in"))
    mode = SOLVER_INSTALL;
  else if (!strcmp(argv[1], "erase") || !strcmp(argv[1], "rm"))
    mode = SOLVER_ERASE;
  else if (!strcmp(argv[1], "show"))
    mode = 0;
  else if (!strcmp(argv[1], "update") || !strcmp(argv[1], "up"))
    mode = SOLVER_UPDATE;
  else
    {
      fprintf(stderr, "Usage: solv install|erase|update|show <select>\n");
      exit(1);
    }
  queue_init(&job);
  for (i = 2; i < argc; i++)
    mkselect(pool, argv[i], 0, &job);
  if (!job.count && mode == SOLVER_UPDATE)
    updateall = 1;
  else if (!job.count)
    {
      printf("nothing matched\n");
      exit(1);
    }
  if (!mode)
    {
      for (i = 0; i < job.count; i += 2)
	{
	  FOR_JOB_SELECT(p, pp, job.elements[i], job.elements[i + 1])
	    printf("  - %s\n", solvid2str(pool, p));
	}
      exit(0);
    }
  // add mode
  for (i = 0; i < job.count; i += 2)
    job.elements[i] |= mode;

rerunsolver:
  for (;;)
    {
      Id problem, solution;
      int pcnt, scnt;

      solv = solver_create(pool);
      solv->ignorealreadyrecommended = 1;
      solv->updatesystem = updateall;
      solver_solve(solv, &job);
      if (!solv->problems.count)
	break;
      pcnt = solver_problem_count(solv);
      printf("Found %d problems:\n", pcnt);
      for (problem = 1; problem <= pcnt; problem++)
	{
	  int take = 0;
	  printf("Problem %d:\n", problem);
	  solver_printprobleminfo(solv, problem);
	  printf("\n");
	  scnt = solver_solution_count(solv, problem);
	  for (solution = 1; solution <= pcnt; solution++)
	    {
	      printf("Solution %d:\n", solution);
	      solver_printsolution(solv, problem, solution);
	      printf("\n");
	    }
	  for (;;)
	    {
	      printf("Please choose a solution: ");
	      fflush(stdout);
	      *inbuf = 0;
	      if (!(ip = fgets(inbuf, sizeof(inbuf), stdin)))
		{
		  printf("Abort.\n");
		  exit(1);
		}
	      while (*ip == ' ' || *ip == '\t')
		ip++;
	      if (*ip >= '0' && *ip <= '9')
		{
		  take = atoi(ip);
		  if (take >= 1 && take <= solution)
		    break;
		}
	      if (*ip == 's')
		{
		  take = 0;
		  break;
		}
	      if (*ip == 'q')
		{
		  printf("Abort.\n");
		  exit(1);
		}
	    }
	  if (!take)
	    continue;
	  solver_take_solution(solv, problem, take, &job);
	}
      solver_free(solv);
      solv = 0;
    }
  if (!solv->trans.steps.count)
    {
      printf("Nothing to do.\n");
      exit(1);
    }
  printf("\n");
  printf("Transaction summary:\n\n");
  solver_printtransaction(solv);
  if (!yesno("OK to continue (y/n)? "))
    {
      printf("Abort.\n");
      exit(1);
    }

  trans = &solv->trans;
  queue_init(&checkq);
  newpkgs = transaction_installedresult(trans, &checkq);
  if (newpkgs)
    {
      Queue conflicts;
      printf("Downloading %d packages\n", newpkgs);
      newpkgsfps = sat_calloc(newpkgs, sizeof(*newpkgsfps));
      newpkgsps = sat_calloc(newpkgs, sizeof(Id));
      for (i = 0; i < newpkgs; i++)
	{
	  int j;
	  unsigned int medianr;
	  char *loc;
	  Solvable *s;
	  struct repoinfo *cinfo;

	  p = checkq.elements[i];
	  newpkgsps[i] = p;
	  s = pool_id2solvable(pool, p);
	  loc = solvable_get_location(s, &medianr);
	  cinfo = 0;
	  for (j = 0; j < nrepoinfos; j++)
	    if (s->repo == repoinfos[j].repo)
	      {
		cinfo = repoinfos + j;
		break;
	      }
	  if ((newpkgsfps[i] = curlfopen(cinfo->baseurl, loc, 0)) == 0)
	    {
	      printf("%s: %s not found\n", cinfo->alias, loc);
	      exit(1);
	    }
	}
      printf("Searching for file conflicts\n");
      queue_init(&conflicts);
      fcstate.rpmdbstate = 0;
      fcstate.newpkgscnt = newpkgs;
      fcstate.newpkgsfps = newpkgsfps;
      fcstate.newpkgsps = newpkgsps;
      pool_findfileconflicts(pool, &checkq, newpkgs, &conflicts, &fc_cb, &fcstate);
      if (conflicts.count)
	{
	  for (i = 0; i < newpkgs; i++)
	    fclose(newpkgsfps[i]);
	  sat_free(newpkgsfps);
	  sat_free(newpkgsps);
	  solver_free(solv);
	  printf("\n");
	  for (i = 0; i < conflicts.count; i += 5)
	    printf("file %s of package %s conflicts with package %s\n", id2str(pool, conflicts.elements[i]), solvid2str(pool, conflicts.elements[i + 1]), solvid2str(pool, conflicts.elements[i + 3]));
	  printf("\n");
	  if (!yesno("Re-run solver (y/n)? "))
	    {
	      printf("Abort.\n");
	      exit(1);
	    }
	  pool_add_fileconflicts_deps(pool, &conflicts);
	  pool_createwhatprovides(pool);
	  goto rerunsolver;
	}
      queue_free(&conflicts);
    }
  transaction_order(trans, 0);

  printf("Committing transaction:\n");
  for (i = 0; i < trans->steps.count; i++)
    {
      char rpmname[256];
      const char *evr, *evrp;
      Solvable *s;
      int j;
      FILE *fp;

      p = trans->steps.elements[i];
      s = pool_id2solvable(pool, p);
      Id type = transaction_type(trans, p, SOLVER_TRANSACTION_RPM_ONLY);
      switch(type)
	{
	case SOLVER_TRANSACTION_ERASE:
	  printf("erase %s\n", solvid2str(pool, p));
	  if (strlen(solvid2str(pool, p)) > 255)
	    continue;
	  evr = evrp = id2str(pool, s->evr);
	  while (*evrp >= '0' && *evrp <= '9')
	    evrp++;
	  if (evrp > evr && evrp[0] == ':' && evrp[1])
	    evr = evrp + 1;
	  sprintf(rpmname, "%s-%s.%s", id2str(pool, s->name), evr, id2str(pool, s->arch));
	  runrpm("-e", rpmname);
	  break;
	case SOLVER_TRANSACTION_INSTALL:
	case SOLVER_TRANSACTION_MULTIINSTALL:
	  printf("install %s\n", solvid2str(pool, p));
	  for (j = 0; j < newpkgs; j++)
	    if (newpkgsps[j] == p)
	      break;
	  fp = j < newpkgs ? newpkgsfps[j] : 0;
	  rewind(fp);
	  lseek(fileno(fp), 0, SEEK_SET);
	  sprintf(rpmname, "/dev/fd/%d", fileno(fp));
	  runrpm(type == SOLVER_TRANSACTION_MULTIINSTALL ? "-i" : "-U", rpmname);
	  break;
	default:
	  break;
	}
    }
  for (i = 0; i < newpkgs; i++)
    fclose(newpkgsfps[i]);
  sat_free(newpkgsfps);
  sat_free(newpkgsps);
  solver_free(solv);
  pool_free(pool);
  exit(0);
}
