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
            print('{} wiki{}not migrated'.format(num_wikis, 's ' if num_wikis > 1 else ' '))

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
    def gitlab(self):
        return self._gitlab
    
    def map_existing_users(self):
        gitorious_users = dict((self.format_username(u.login), u) for u in self.gitorious.query(gitorious.User))
        gitlab_users = dict((u.username, u) for u in self.gitlab.users.list(all=True))

        for username, user in gitorious_users.items():
            print('{} {}in gitlab'.format(username, '' if username in gitlab_users else 'not '))
            self.users[user] = gitlab_users[username] if username in gitlab_users else None

    def make_authenticated_url(self, repo_url, token, username='gitlab-ci-token'):
        parsed_url = urlparse(repo_url)._asdict()
        parsed_url['netloc'] = '{}:{}@{}'.format(username, token, parsed_url['netloc'])

        return ParseResult(**parsed_url).geturl()

    def mirror(self, gitorious_repository, gitlab_project):
            if gitlab_project.namespace['kind'] == 'user':
                gl_owner = self.gitlab.users.get(gitlab_project.owner['id'])
            else: # kind is group
                group = self.gitlab.groups.get(gitlab_project.namespace['id'])
                gl_owner = [self.gitlab.users.get(m.id) for m in group.members.list(access_level=gitlab.OWNER_ACCESS, all=True) if m.id > 1][0]
            
            if gl_owner not in self.gl_tokens:
                self.gl_tokens[gl_owner] = gl_owner.impersonationtokens.create({
                    'name': 'import token',
                    'scopes': ['api', 'read_user']
                })
            
            repo_path = path.join('exported_repositories', gitlab_project.namespace['path'], gitlab_project.path)
        repo = Repository(gitorious_repository.clone_url(self.gitorious_url), repo_path)
        repo.configure('http', proxy='', sslVerify=False)

        push_url = self.make_authenticated_url(gitlab_project.http_url_to_repo, self.gl_tokens[gl_owner].token)
        repo.mirror('gitlab', push_url)
            
    def create_users(self):
        self.map_existing_users()
        for user in self.users:
            if self.users[user] is not None:
                print('{} already exists as {}'.format(user.login, self.users[user].username))
                continue
            try:
                print(user)
                self.users[user] = self.gitlab.users.create({
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
        gl_project = gitlab_project_root.create(kwargs)
        self.mirror(repo_group.project_repo, gl_project)
        # TODO clone wiki repo

        for fork in repo_group.forks:
            gl_fork = self.users[fork.user].projects.create({
                'visibility': 'public',
                'name': fork.name,
                'description':  None if fork.description is None else fork.description[0:255],
                'wiki_enabled': False
            })
            fork_project = self.gitlab.projects.get(gl_fork.id)
            fork_project.create_fork_relation(gl_project.id)

            self.mirror(fork, gl_fork)
        return gl_project

    def create_group(self, project):
        gitlab_group = self.gitlab.groups.create({
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
        
        return gitlab_group

    def migrate_projects(self):
        for project in self.gitorious.query(gitorious.Project):
            print(project)
            repo_groups = list(RepositoryGroup.from_project(project))
            
            if type(project.owner) is gitorious.User and len(repo_groups) == 1:
                gitlab_user = self.users[project.owner]
                self.create_project(repo_groups[0], gitlab_user.projects)
            else: # create a group
                # 1. create parent group
                gitlab_group = self.create_group(project)
                
                for repository in repo_groups:
                    self.create_project(repository, self.gitlab.projects, namespace_id=gitlab_group.id)

    def cleanup(self):
        self.remove_gitlab_projects()
        self.remove_gitlab_groups()

    def remove_gitlab_projects(self):
        self._remove_gl('projects')
    
    def remove_gitlab_groups(self):
        self._remove_gl('groups')
    
    def remove_gitlab_users(self):
        for obj in filter(lambda x: x.id > 1, self.gitlab.users.list(all=True)):
            self.gitlab.users.delete(obj.id)
    
    def _remove_gl(self, object_name):
        glo = getattr(self.gitlab, object_name)
        for obj in glo.list(all=True):
            glo.delete(obj.id)