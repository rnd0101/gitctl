# -*- encoding: utf-8 -*-
"""Command handlers."""
import os
import sys
import git
import logging

import gitctl.utils

LOG = logging.getLogger('gitctl')

def gitctl_create(args):
    """Handles the 'gitctl create' command"""
    project_path = os.path.realpath(os.path.join(os.getcwd(), args.project[0]))
    project_name = os.path.basename(project_path)
    config = gitctl.utils.parse_config(args.config)
    
    if not os.path.exists(project_path):
        LOG.critical('Project path does not exist!', project_path)
        sys.exit(1)
    
    project_url = '%s:%s.git' % (config['upstream-url'], project_name)

    # Make sure that the remote repository does not exist already.
    retcode = gitctl.utils.run('ssh %s test ! -d %s.git' % (config['upstream-url'], project_name))
    if retcode != 0:
        LOG.error('Remote repository ``%s`` already exists. Aborting.', project_url)
        sys.exit(1)
    
    # Set up the remote bare repository
    initialize_remote = """\
    ssh %(upstream)s
    "mkdir -p %(project)s.git && 
     cd %(project)s.git && 
     git --bare init && 
     echo %(project)s > description && 
     echo '. /usr/share/doc/git-core/contrib/hooks/post-receive-email' > hooks/post-receive &&
     chmod a+x hooks/post-receive && 
     git config hooks.mailinglist %(commit_email)s && 
     git config hooks.emailprefix \\"%(commit_email_prefix)s \\" &&
     git config hooks.emaildiff true"
    """ % { 'upstream' : config['upstream-url'],
            'project' : project_name,
            'commit_email' : config['commit-email'],
            'commit_email_prefix' : config['commit-email-prefix'] }
    
    gitctl.utils.run(' '.join([l.strip() for l in initialize_remote.splitlines()]))
    LOG.info('Created new remote repository: %s', project_url)
    
    # Initialize the local directory.
    repository = git.Git(project_path)
    repository.init()

    # Create the initial commit
    repository.add('.')
    repository.commit('-m', args.message)
    
    # Create local branches
    for remote, local in config['branches']:
        repository.branch(local)

    # Push the initial structure to upstream
    repository.remote('add', config['upstream'], project_url)
    for remote, local in config['branches']:
        repository.push(config['upstream'], local)
    repository.fetch(config['upstream'])
    
    LOG.info('Created new local repository: %s', project_path)
    
    # Set up the local branches to track the remote ones
    for remote, local in config['branches']:
        repository.branch('-f', '--track', local, remote)
        LOG.info('Branch ``%s`` is tracking ``%s``', local, remote)
        
    # Checkout the development branch 
    repository.checkout(config['development-branch'])
    # Get rid of the default master branch
    repository.branch('-d', 'master')
    
    # Fix the HEAD ref in the upstream repo so cloning does not give an error
    gitctl.utils.run('ssh %(upstream)s "echo ref: refs/heads/%(devbranch)s > %(project)s.git/HEAD"' % {
        'upstream' : config['upstream-url'],
        'devbranch' : config['development-branch'],
        'project' : project_name,
        })
    
    LOG.info('Checked out development branch ``%s``', config['development-branch'])

def gitctl_fetch(args):
    """Fetches all projects."""
    projects = gitctl.utils.parse_externals(args.externals)
    config = gitctl.utils.parse_config(args.config)
    
    for proj in gitctl.utils.filter_projects(projects, set(args.project)):
        repository = git.Git(gitctl.utils.project_path(proj))
        repository.fetch(config['upstream'])
        LOG.info('%s Fetched', gitctl.utils.pretty(proj['name']))

def gitctl_branch(args):
    """Operates on the project branches."""
    projects = gitctl.utils.parse_externals(args.externals)
    config = gitctl.utils.parse_config(args.config)
    
    for proj in gitctl.utils.filter_projects(projects, set(args.project)):
        repository = git.Repo(gitctl.utils.project_path(proj))
        if not args.checkout and args.list:
            LOG.info('%s %s' % (gitctl.utils.pretty(proj['name']),
                                repository.active_branch))
        
        if args.checkout:
            branch = args.checkout[0]
            if repository.is_dirty:
                LOG.info('%s Dirty working directory. Please commit or stash and try again.' % gitctl.utils.pretty(proj['name']))
            else:
                branches = set([b.name for b in repository.branches])
                if branch not in branches:
                    LOG.warning('%s No such branch: ``%s``' % (gitctl.utils.pretty(proj['name']), branch))
                elif branch == repository.active_branch and args.verbose:
                    LOG.info('%s Already at ``%s``' % (gitctl.utils.pretty(proj['name']), branch))
                else:
                    repository.git.checkout(branch)
                    LOG.info('%s Checked out ``%s``' % (gitctl.utils.pretty(proj['name']), branch))

def gitctl_update(args):
    """Updates the external projects.
    
    If the project already exists locally, it will be pulled (or rebased).
    Otherwise it will cloned.
    """
    config = gitctl.utils.parse_config(args.config)
    projects = gitctl.utils.parse_externals(args.externals)
    
    for proj in gitctl.utils.filter_projects(projects, set(args.project)):
        path = gitctl.utils.project_path(proj)
        if os.path.exists(path):
            repository = git.Repo(path)
            repository.git.fetch()
            
            if repository.is_dirty:
                LOG.info('%s Dirty working directory. Please commit or stash and try again.', gitctl.utils.pretty(proj['name']))
                continue

            ok = True
            updated = False

            if gitctl.utils.is_sha1(proj['treeish']):
                # We're dealing with an explicit version pin.
                pinned_at = repository.git.rev_parse('HEAD').strip()
                treeish = proj['treeish']
                # Simply do a hard reset to the requested revision
                repository.git.reset('--hard', treeish)
            else:
                # We're dealing with a dynamic branch pointer
                pinned_at = None
                treeish = repository.active_branch

                remote_branches = set(repository.git.branch('-r').split())
                local_branches = set(repository.git.branch().split())

                for remote, local in config['branches']:
                    if remote in remote_branches and local in local_branches:
                        if repository.git.rev_parse(remote) == repository.git.rev_parse(local):
                            # Skip branches that have not changed.
                            continue

                        # Switch to the branch to avoid implicit merge commits
                        repository.git.checkout(local)

                        # Use a remote:local refspec to pull the given branch. We omit the + from the
                        # refspec to attempt a fast-forward merge.
                        status, stdout, stderr = repository.git.pull(
                            config['upstream'],
                            '%s:%s' % (local, local),
                            with_exceptions=False,
                            with_extended_output=True)

                        if status != 0:
                            ok = False
                            if 'non fast forward' in stderr.lower():
                                # Fast-forward merge was not possible, we'll
                                # bail out for now. We could attempt a normal 'git pull' operation but that
                                # might leave multiple branch in an inconsistent state at the same time.
                                LOG.warning('%s Fast forward merge not possible for branch ``%s``. Try syncing with upstream manually (pull, push or merge).', gitctl.utils.pretty(proj['name']), local)
                            else:
                                # Some other kind of error.
                                LOG.critical('%s Update failure: %s', gitctl.utils.pretty(proj['name']), stderr)
                        else:
                            updated = True

                repository.git.checkout(treeish)

            if ok:
                if gitctl.utils.is_sha1(treeish) and pinned_at is not None:
                    # If we're using pinned down revisions we only report changes when the
                    # explicit revision was changed, even if the branches were updated.
                    if pinned_at == proj['treeish']:
                        if args.verbose:
                            LOG.info('%s OK', gitctl.utils.pretty(proj['name']))
                    else:
                        LOG.info('%s Checked out revision ``%s``', gitctl.utils.pretty(proj['name']), treeish)
                elif updated:
                    LOG.info('%s Updated', gitctl.utils.pretty(proj['name']))
                elif args.verbose:
                    LOG.info('%s OK', gitctl.utils.pretty(proj['name']))

        else:
            # Clone the repository
            temp = git.Git('/tmp')
            temp.clone('--no-checkout', '--origin', config['upstream'],  proj['url'], path)

            # Set up the local tracking branches
            repository = git.Git(path)
            remote_branches = set(repository.branch('-r').split())
            local_branches = set(repository.branch().split())
            for remote, local in config['branches']:
                if remote in remote_branches and local not in local_branches:
                    repository.branch('-f', '--track', local, remote)
            # Check out the given treeish
            repository.checkout(proj['treeish'])
            LOG.info('%s Cloned and checked out ``%s``', gitctl.utils.pretty(proj['name']), proj['treeish'])

def gitctl_status(args):
    """Checks the status of all external projects."""
    config = gitctl.utils.parse_config(args.config)
    projects = gitctl.utils.parse_externals(args.externals)

    for proj in gitctl.utils.filter_projects(projects, set(args.project)):
        repository = git.Repo(gitctl.utils.project_path(proj))
        if not args.no_fetch:
            # Fetch upstream
            repository.git.fetch(config['upstream'])

        if repository.is_dirty:
            LOG.info('%s Uncommitted local changes', gitctl.utils.pretty(proj['name']))
            continue
            
        remote_branches = set(repository.git.branch('-r').split())
        
        uptodate = True
        for remote, local in config['branches']:
            if remote in remote_branches:
                if len(repository.diff(remote, local).strip()) > 0:
                    LOG.info('%s Branch ``%s`` out of sync with upstream', gitctl.utils.pretty(proj['name']), local)
                    uptodate = False
        if uptodate and args.verbose:
            LOG.info('%s OK', gitctl.utils.pretty(proj['name']))

def gitctl_pending(args):
    """Checks for pending changes between two consecutive states in our
    workflow.
    """
    projects = gitctl.utils.parse_externals(args.externals)
    config = gitctl.utils.parse_config(args.config)

    for proj in projects:
        repository = git.Repo(gitctl.utils.project_path(proj))
        
        local_branches = set(repository.git.branch().split())
        remote_branches = set(repository.git.branch('-r').split())
        
        def assert_branch(branch, quiet=False):
            if branch in local_branches:
                return True
            else:
                if not quiet:
                    LOG.warning('%s Branch %s does not exist', gitctl.utils.pretty(proj['name']), branch)
                return False
                
        
        if not assert_branch(config['development-branch'], quiet=True):
            # This looks to be a package that does not share our common repository layout
            # which is possible with 3rd party packages etc. We can safely ignore it.
            if not args.show_config and args.verbose:
                LOG.info('%s Skipping.', gitctl.utils.pretty(proj['name']))
            continue

        # Check for dirty working directory
        if repository.is_dirty:
            LOG.info('%s Uncommitted local changes.', gitctl.utils.pretty(proj['name']))
            continue
        
        # Update the remotes
        if not args.no_fetch:
            repository.git.fetch(config['upstream'])

        # Check for out-of-sync remote branches in the staging / development mode.
        # For production mode it doesn't make sense since we're using HEADless
        # checkouts and the branches are not kept in sync with upstream.
        if not args.production:
            skip_project = False
            for remote, local in config['branches']:
                if remote in remote_branches:
                    if len(repository.git.diff(remote, local).strip()) > 0:
                        LOG.warning('%s Branch ``%s`` out of sync with upstream. Run "gitcl update" or pull manually.',
                                    gitctl.utils.pretty(proj['name']), local)
                        skip_project = True
            if skip_project:
                continue
        
        # Get actual versions of the trees to be compared.
        if args.production:
            # Compare the the pinned down version against the HEAD of the
            # production branch
            if not assert_branch(config['production-branch']):
                continue
                
            if not gitctl.utils.is_sha1(proj['treeish']):
                LOG.warning('%s Treeish is not a SHA1 revision: %s', gitctl.utils.pretty(proj['name']), proj['treeish'])
                continue
            
            from_ = repository.git.rev_parse(proj['treeish'])
            to = repository.git.rev_parse('%s/%s' % (config['upstream'], config['production-branch']))
        elif args.staging:

            if not assert_branch(config['production-branch']):
                continue
            if not assert_branch(config['staging-branch']):
                continue

            from_ = repository.git.rev_parse(config['production-branch'])
            to = repository.git.rev_parse(config['staging-branch'])
        elif args.dev:

            if not assert_branch(config['development-branch']):
                continue
            if not assert_branch(config['staging-branch']):
                continue

            from_ = repository.git.rev_parse(config['staging-branch'])
            to = repository.git.rev_parse(config['development-branch'])
        
        if from_ != to:
            # The comparison branch has advanced.
            if args.show_config and args.production:
                # Update the treeish to the latest version in the comparison branch.
                proj['treeish'] = to
            else:
                commits = len(repository.git.log('--pretty=oneline', '%s..%s' % (from_, to)).splitlines())
                if args.production:
                    LOG.info('%s Branch ``%s`` is %s commit(s) ahead of the pinned down version at revision %s',
                             gitctl.utils.pretty(proj['name']), config['production-branch'], commits, to)
                else:
                    if args.staging:
                        b1 = config['staging-branch']
                        b2 = config['production-branch']
                    elif args.dev:
                        b1 = config['development-branch']
                        b2 = config['staging-branch']
                        
                    LOG.info('%s Branch ``%s`` is %s commit(s) ahead of ``%s``',
                             gitctl.utils.pretty(proj['name']), b1, commits, b2)
                    
                if args.diff:
                    LOG.info(repository.git.log('--stat', '--summary', '-p', from_, to))
        else:
            if args.verbose and not args.show_config:
                LOG.info('%s OK', gitctl.utils.pretty(proj['name']))
        
    if args.show_config and args.production:
        LOG.info(gitctl.utils.generate_externals(projects))

__all__ = ['gitctl_create', 'gitctl_fetch', 'gitctl_update', 'gitctl_status',
           'gitctl_pending', 'gitctl_branch']
