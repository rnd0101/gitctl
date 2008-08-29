# -*- encoding: utf-8 -*-
"""Command handlers."""
import os
import sys
import git
import logging

import gitctl.utils

logging.basicConfig(level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


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
    retcode = gitctl.utils.run('ssh %s "test ! -d %s.git"' % (config['upstream-url'], project_name))
    if retcode != 0:
        LOG.error('Remote repository ``%s`` already exists. Aborting.', project_url)
        sys.exit(1)
    
    # Set up the remote bare repository
    initialize_remote = (
        'ssh', config['upstream-url'],
        'mkdir -p %(project)s.git && '
        'cd %(project)s.git && '
        'git --bare init && '
        'echo %(project)s > description && '
        'echo \'. /usr/share/doc/git-core/contrib/hooks/post-receive-email\' > hooks/post-receive && '
        'chmod a+x hooks/post-receive && '
        'git config hooks.mailinglist %(commit_email)s && '
        'git config hooks.emailprefix "%(commit_email_prefix)s "' % {
            'project' : project_name,
            'commit_email' : config['commit-email'],
            'commit_email_prefix' : config['commit-email-prefix'] },
        )
    gitctl.utils.run(initialize_remote)
    LOG.info('Created new remote repository: %s', project_url)
    
    # Initialize the local directory.
    repository = git.Git(project_path)
    repository.init()

    # Create the initial commit
    repository.add('.')
    repository.commit('-m', 'gitctl: project initialization')
    
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
    
    LOG.info('Checked out development branch ``%s``', config['development-branch'])

def gitctl_fetch(args):
    """Fetches all projects."""
    projects = gitctl.utils.parse_externals(args.externals)
    config = gitctl.utils.parse_config(args.config)
    
    for proj in projects:
        repository = git.Git(gitctl.utils.project_path(proj))
        repository.fetch(config['upstream'])
        LOG.info('%s Fetched', gitctl.utils.pretty(proj['name']))

def gitctl_update(args):
    """Updates the external projects.
    
    If the project already exists locally, it will be pulled (or rebased).
    Otherwise it will cloned.
    """
    projects = gitctl.utils.parse_externals(args.externals)
    config = gitctl.utils.parse_config(args.config)
    
    for proj in projects:
        path = gitctl.utils.project_path(proj)
        if os.path.exists(path):
            repository = git.Git(path)
            if gitctl.utils.is_dirty(repository):
                LOG.warning('%s Dirty working directory. Please commit or stash and try again.', gitctl.utils.pretty(proj['name']))
                continue

            if args.rebase:
                repository.pull('--rebase')
                LOG.info('%s Rebased', gitctl.utils.pretty(proj['name']))
            else:
                repository.pull()
                LOG.info('%s Pulled', gitctl.utils.pretty(proj['name']))
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

    LOG.info('Checking status..')
    for proj in projects:
        repository = git.Repo(gitctl.utils.project_path(proj))
        if not args.no_fetch:
            # Fetch upstream
            repository.git.fetch(config['upstream'])

        if gitctl.utils.is_dirty(repository):
            LOG.info('%s Uncommitted local changes', gitctl.utils.pretty(proj['name']))
            continue
            
        remote_branches = set([name.strip()
                               for name
                               in repository.git.branch('-r').splitlines()])
        
        uptodate = True
        for remote, local in config['branches']:
            if remote in remote_branches:
                if len(repository.diff(remote, local).strip()) > 0:
                    LOG.info('%s Branch ``%s`` out of sync with upstream', gitctl.utils.pretty(proj['name']), local)
                    uptodate = False
        if uptodate:
            LOG.info('%s OK', gitctl.utils.pretty(proj['name']))

def gitctl_changes(args):
    """Checks for changes between the stating branch and the currently pinned
    versions in the externals configuration.
    
    It makes most sense when executed in the production buildout when the
    reported differences are the ones most likely pending to be pushed into
    production.
    """
    projects = gitctl.utils.parse_externals(args.externals)
    config = gitctl.utils.parse_config(args.config)

    for proj in projects:
        repository = git.Git(gitctl.utils.project_path(proj))
        
        remote_branches = set([name.strip()
                               for name
                               in repository.branch('-r').splitlines()])
        
        if config['staging-branch'] not in remote_branches:
            # This looks to be a package that does not share our common repository layout
            # which is possible with 3rd party packages etc. We can safely ignore it.
            if not args.show_config:
                LOG.info('%s Skipping.', gitctl.utils.pretty(proj['name']))
            continue
        
        # Fetch from upstream so that we compare against the latest version
        repository.fetch(config['upstream'])
        # Get actual versions of both objects
        pinned_at = repository.rev_parse(proj['treeish'])
        staging_at = repository.rev_parse(config['staging-branch'])
        
        if pinned_at != staging_at:
            # The demo branch has advanced.
            if args.show_config:
                # Update the treeish to the latest version in the demo branch.
                proj['treeish'] = staging_at
            else:
                LOG.info('%s Latest staged revision at %s', gitctl.utils.pretty(proj['name']), demo_at)
                if args.diff:
                    print repository.log('--stat', '--summary', '-p', pinned_at, demo_at)
        else:
            if not args.show_config:
                LOG.info('%s OK', gitctl.utils.pretty(proj['name']))
        
    if args.show_config:
        print gitctl.utils.generate_externals(projects)

__all__ = ['gitctl_create', 'gitctl_fetch', 'gitctl_update', 'gitctl_status',
           'gitctl_changes',]
