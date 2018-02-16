import random
import string

from collections import defaultdict, namedtuple, OrderedDict
import gitlab
import pygit2
import gitorious2gitlab.gitorious as gitorious

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class RepositoryGroup(namedtuple('ParsedRepos', 'project_repo, wiki_repo, forks')):
    @staticmethod
    def from_project(gitorious_project):
        project_repos = [r for r in gitorious_project.repositories if not r.name.endswith('-gitorious-wiki') and r.parent is None]
        wiki_repos = dict((r.hashed_path[0:-15], r) for r in gitorious_project.repositories if r.name.endswith('-gitorious-wiki'))
        forks = [r for r in gitorious_project.repositories if r.parent is not None]
        for repo in project_repos:
            yield RepositoryGroup(repo,
                            wiki_repos[repo.hashed_path] if repo.hashed_path in wiki_repos else None,
                            [f for f in forks if f.parent is repo])

def randomword(length):
    return ''.join(random.choice(string.ascii_letters) for i in range(length))

class ImportSession(object):
    def __init__(self, gitorious_db_conn, gitorious_url, gitlab_url, gitlab_token):
        self._gitorious_session = gitorious.setup_session(gitorious_db_conn)
        self._gitorious_url = gitorious_url
        self._gitlab = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, api_version=4, ssl_verify=False)

        self.users = OrderedDict()
    
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
        gitorious_users = dict((u.login.upper(), u) for u in self.gitorious.query(gitorious.User))
        gitlab_users = dict((u.username, u) for u in self.gitlab.users.list(all=True))

        for username, user in gitorious_users.items():
            print('{} {}in gitlab'.format(username, '' if username in gitlab_users else 'not '))
            self.users[user] = gitlab_users[username] if username in gitlab_users else None

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
                    'username': user.login.upper(),
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
        # TODO clone repo
        # TODO clone wiki repo

        for fork in repo_group.forks:
            gl_fork = self.users[fork.user].projects.create({
                'visibility': 'public',
                'name': fork.name,
                'description':  None if fork.description is None else fork.description[0:255],
                'wiki_enabled': False
            })
            # TODO create fork relation
            # TODO clone repo
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
            self.gitlab.users.delete(obj)
    
    def _remove_gl(self, object_name):
        glo = getattr(self.gitlab, object_name)
        for obj in glo.list(all=True):
            glo.delete(obj.id)