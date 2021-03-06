import os
import os.path as path
import random
import string
import urllib3

from collections import defaultdict, namedtuple, OrderedDict
from urllib.parse import urlparse, ParseResult

import gitlab
import git

import gitorious2gitlab.gitorious as gitorious

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class UnmigratedRepositoryError(Exception):
    pass

MigrationError = namedtuple('MigrationError', 'project, exception')
MigrationResult = namedtuple('MigrationResult', 'migrated_project_count, unmigrated_projects')

class Repository(object):
    def __init__(self, origin_url, local_path):
        self._origin_url = origin_url
        self._path = local_path
        self._create_repo()

    @property
    def repository(self):
        return self._repo

    def update(self):
        self.repository.remotes['origin'].fetch('+refs/heads/*:refs/heads/*')

    def _create_repo(self):
        try:
            self._repo = git.Repo(self._path)
        except:
            if not path.exists(self._path):
                os.makedirs(self._path)
            self._repo = git.Repo.clone_from(self._origin_url, self._path, bare=True)
    
    def configure(self, section, **kwargs):
        section_exists = False
        with self.repository.config_reader() as r:
            section_exists = r.has_section(section)
        
        with self.repository.config_writer() as cw:
            if not section_exists:
                cw.add_section(section)
            for (prop,value) in kwargs.items():
                cw.set(section, prop, value)

    def mirror(self, remote_name, remote_url):
        try:
            remote = self.repository.remotes[remote_name]
        except IndexError:
            remote = self.repository.create_remote(remote_name, remote_url)
        
        with remote.config_writer as cw:
            cw.set('pushurl', remote_url)
            cw.set('mirror', True)
        
        remote.push()

class RepositoryGroup(namedtuple('ParsedRepos', 'project_repo, wiki_repo, forks')):
    @staticmethod
    def from_project(gitorious_project: gitorious.Project):
        project_repos = [r for r in gitorious_project.repositories if not r.name.endswith('-gitorious-wiki') and r.parent is None]
        wiki_repos = OrderedDict((r.hashed_path[0:-15], r) for r in gitorious_project.repositories if r.name.endswith('-gitorious-wiki'))
        forks = [r for r in gitorious_project.repositories if r.parent is not None]
        
        if len(project_repos) > 0:
            num_wikis = len(wiki_repos)
            if num_wikis > 1:
                print('{} has {} wikis!'.format(gitorious_project.slug, num_wikis))
            
            wiki_is_mapped = all(any(w==r.hashed_path for r in project_repos) for w in wiki_repos.keys())
            
            for repo in project_repos:
                selected_wiki = None
                if wiki_is_mapped:
                    selected_wiki = wiki_repos.pop(repo.hashed_path) if repo.hashed_path in wiki_repos else None
                elif len(wiki_repos) > 0:
                    (key, selected_wiki) = wiki_repos.popitem(last=False)
                
                if selected_wiki is not None:
                    num_wikis -= 1
                
                yield RepositoryGroup(repo, selected_wiki, [f for f in forks if f.parent is repo])
            if num_wikis > 0:
                raise UnmigratedRepositoryError('{} wiki{}not migrated'.format(num_wikis, 's ' if num_wikis > 1 else ' '))
        elif len(forks) > 0:
            # this means that there are no project repos, but forks exist
            # The most likely explanation is that the original repository was deleted, but the forks remained
            raise UnmigratedRepositoryError('{} forks{}not migrated'.format(len(forks), 's ' if len(forks) > 0 else ' '))
        elif len(wiki_repos) > 0:
            # we have a wiki, no project repos, and no forks
            # the most likely explanation is that the project exists, but the initial repositories were deleted
            for repo in wiki_repos.values():
                yield RepositoryGroup(repo, None, [])
            


def randomword(length):
    return ''.join(random.choice(string.ascii_letters) for i in range(length))

class ImportSession(object):
    def __init__(self, gitorious_db_conn, gitorious_url, gitlab_url, gitlab_token, username_formatter=str):
        self._gitorious_session = gitorious.setup_session(gitorious_db_conn)
        self._gitorious_url = gitorious_url
        self._gitlab = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, api_version=4, ssl_verify=False)
        self.format_username = username_formatter
        self.users = OrderedDict()
        self.gl_tokens = dict()
    
    @property
    def gitorious(self):
        return self._gitorious_session
    
    @property
    def gitorious_url(self):
        return self._gitorious_url
    
    @property
    def gl(self):
        return self._gitlab
    
    def map_existing_users(self):
        gitorious_users = dict((self.format_username(u.login), u) for u in self.gitorious.query(gitorious.User))
        gitlab_users = dict((u.username, u) for u in self.gl.users.list(all=True))

        for username, user in gitorious_users.items():
            self.users[user] = gitlab_users[username] if username in gitlab_users else None

    def wiki_url_for_project(self, project: gitlab.Project, include_auth=True) -> str:
        url = project.http_url_to_repo[0:-3] + 'wiki.git'
        if include_auth:
            return self.make_authenticated_url(url, self.token(project))
        return url

    def make_authenticated_url(self, repo_url, token, username='gitlab-ci-token'):
        parsed_url = urlparse(repo_url)._asdict()
        parsed_url['netloc'] = '{}:{}@{}'.format(username, token, parsed_url['netloc'])

        return ParseResult(**parsed_url).geturl()

    def make_local_path(self, project: gitlab.Project):
        return path.join('exported_repositories', project.namespace['path'], project.path)
    
    def mirror(self, local_path, source_url, target_url):
        repo = Repository(source_url, local_path)
        repo.configure('http', proxy='', sslVerify=False)

        repo.mirror('gitlab', target_url)
    
    def create_users(self):
        self.map_existing_users()
        unmapped_users = [k for (k,v) in self.users.items() if v is None]
        print('{} users already mapped'.format(len([k for (k,v) in self.users.items() if v is not None])))
        for user in unmapped_users:
            try:
                print(user)
                self.users[user] = self.gl.users.create({
                    'email': user.email,
                    'username': self.format_username(user.login),
                    'name': user.fullname,
                    'password': randomword(12),
                    'skip_confirmation': True
                })
                
                i = 0
                for key in user.ssh_keys:
                    try:
                        i+=1
                        self.users[user].keys.create({
                            'title': 'key {}'.format(i),
                            'key': key.key
                        })
                    except gitlab.GitlabCreateError as key_error:
                        print('\tproblem with key {}: {}'.format(i, key_error))
            except gitlab.GitlabCreateError as user_error:
                print('\tproblem with user {}: {}'.format(user.login, user_error))


    def create_project(self, repo_group, gitlab_project_root, **kwargs):
        kwargs.update({
            'visibility': 'public',
            'name': repo_group.project_repo.name,
            'description': None if repo_group.project_repo.description is None else repo_group.project_repo.description[0:255],
            'wiki_enabled': repo_group.wiki_repo is not None,
            'tag_list': [t.name for t in repo_group.project_repo.project.tags]
        })
        project = gitlab_project_root.create(kwargs)
        gl_project = self.gl.projects.get(project.id)

        i = 0
        for committer in (c.committer for c in repo_group.project_repo.committerships):
            owner = repo_group.project_repo.owner if type(repo_group.project_repo.owner) is gitorious.User else repo_group.project_repo.owner.admin

            if type(committer) is gitorious.User and owner is not committer:
                gl_project.members.create({
                    'user_id': self.users[committer].id,
                    'access_level': gitlab.DEVELOPER_ACCESS
                })
                i += 1

        if i > 0:
            print('\tAdded {} committers'.format(i))
        
        # migrate the project repo
        self.mirror(self.make_local_path(gl_project),
                    repo_group.project_repo.clone_url(self.gitorious_url),
                               self.make_authenticated_url(gl_project.http_url_to_repo, self.token(gl_project)))

        # migrate the wiki
        if repo_group.wiki_repo is not None:
            self.mirror(self.make_local_path(gl_project) + '.wiki',
                        repo_group.wiki_repo.clone_url(self.gitorious_url),
                        self.wiki_url_for_project(gl_project))

        for fork in repo_group.forks:
            gl_fork = self.users[fork.user].projects.create({
                'visibility': 'public',
                'name': fork.name,
                'description':  None if fork.description is None else fork.description[0:255],
                'wiki_enabled': False
            })
            fork_project = self.gl.projects.get(gl_fork.id)
            fork_project.create_fork_relation(gl_project.id)

            self.mirror(self.make_local_path(fork_project),
                        fork.clone_url(self.gitorious_url),
                        self.make_authenticated_url(fork_project.http_url_to_repo, self.token(fork_project)))
        return gl_project

    def create_group(self, project):
        gitlab_group = self.gl.groups.create({
            'visibility': 'public',
            'name': project.title.replace('#', 'S'),
            'path': project.slug,
            'description': None if project.description is None else project.description[0:255]
        })

        owner = project.owner
        if type(owner) is gitorious.Group:
            for member in project.owner.members:
                gitlab_group.members.create({
                    'user_id': self.users[member].id,
                    'access_level': gitlab.OWNER_ACCESS if member is owner.admin else gitlab.DEVELOPER_ACCESS
                })
        else: # owner is a user
            gitlab_group.members.create({
                'user_id': self.users[owner].id,
                'access_level': gitlab.OWNER_ACCESS
            })
        
        return gitlab_group

    def migrate_projects(self):
        unmigrated_projects = []
        
        migrated_projects_count = 0
        for project in self.gitorious.query(gitorious.Project):
            try:
                print(repr(project))
                repo_groups = list(RepositoryGroup.from_project(project))
                if type(project.owner) is gitorious.User and len(repo_groups) == 1:
                    print('\t{} {} {} forks'.format(repo_groups[0].project_repo.hashed_path,
                        'NO WIKI' if repo_groups[0].wiki_repo is None else repo_groups[0].wiki_repo.hashed_path,
                        len(repo_groups[0].forks)))
                    gitlab_user = self.users[project.owner]
                    self.create_project(repo_groups[0], gitlab_user.projects)
                else: # create a group
                    # 1. create parent group
                    gitlab_group = self.create_group(project)
                    
                    for repository in repo_groups:
                        print('\t{} {} {} forks'.format(repository.project_repo.hashed_path,
                                                'NO WIKI' if repository.wiki_repo is None else repository.wiki_repo.hashed_path,
                                                len(repository.forks)))
                        self.create_project(repository, self.gl.projects, namespace_id=gitlab_group.id)
                migrated_projects_count += 1
            except Exception as ex:
                print('ERROR: ' + repr(project) + str(ex))
                unmigrated_projects.append(MigrationError(project, ex))
        return MigrationResult(migrated_projects_count, unmigrated_projects)

    def cleanup(self):
        self.remove_gitlab_projects()
        self.remove_gitlab_groups()

    def remove_gitlab_projects(self):
        self._remove_gl('projects')
    
    def remove_gitlab_groups(self):
        self._remove_gl('groups')
    
    def remove_gitlab_users(self):
        for obj in filter(lambda x: x.id > 1, self.gl.users.list(all=True)):
            self.gl.users.delete(obj.id)
    
    def run(self, cleanup=False):
        self.create_users()
        if cleanup:
            self.cleanup()
        return self.migrate_projects()

    def token(self, project: gitlab.Project) -> str:
        owner = self._get_project_owner(project)
        if owner not in self.gl_tokens:
            self.gl_tokens[owner] = owner.impersonationtokens.create({
                'name': 'import token',
                'scopes': ['api', 'read_user']
            })
        return self.gl_tokens[owner].token
    
    def _get_project_owner(self, project: gitlab.Project) -> gitlab.User:
        if project.namespace['kind'] == 'user':
            owner = self.gl.users.get(project.owner['id'])
        else:# otherwise, project is owned by a group
            group = self.gl.groups.get(project.namespace['id'])
            owner = [self.gl.users.get(m.id) for m in group.members.list(access_level=gitlab.OWNER_ACCESS, all=True) if m.id > 1][0]
        
        return owner 
    def _remove_gl(self, object_name):
        glo = getattr(self.gl, object_name)
        for obj in glo.list(all=True):
            glo.delete(obj.id)
