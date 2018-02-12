from sqlalchemy import create_engine, ForeignKey
from sqlalchemy.orm import relationship, sessionmaker

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, DateTime, Integer, String, Table

Base = declarative_base()

def setup_session(path_to_db):
    engine = create_engine('sqlite:///' + path_to_db)
    Session = sessionmaker()
    Session.configure(bind=engine)
    return Session()


group_memberships = Table('memberships', Base.metadata,
    Column('id', Integer, primary_key=True),
    Column('group_id', Integer, ForeignKey('groups.id')),
    Column('user_id', Integer, ForeignKey('users.id')),
    Column('role_id', Integer, ForeignKey('roles.id'))
)

class Role(Base):
    __tablename__ = 'roles'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    kind = Column(Integer)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    login = Column(String)
    email = Column(String)
    fullname = Column(String)

    groups = relationship('Group', secondary=group_memberships)

    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    def __str__(self):
        return '{} <{}>'.format(self.fullname, self.email)


class Site(Base):
    __tablename__ = 'sites'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    subdomain = Column(String)
    
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    wiki_git_path = Column(String)


class Group(Base):
    __tablename__ = 'groups'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    description = Column(String)

    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    user_id = Column(Integer, ForeignKey('users.id'))
    admin = relationship('User', back_populates='owned_groups')

    members = relationship('User', secondary=group_memberships)

User.owned_groups = relationship('Group', back_populates='admin')


class Project(Base):
    __tablename__ = 'projects'
    id = Column(Integer, primary_key=True)
    bugtracker_url = Column(String)
    description = Column(String)
    home_url = Column(String)
    mailinglist_url = Column(String)
    owner_id = Column(Integer)
    owner_type = Column(String)
    slug = Column(String)
    title = Column(String)
    
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    user_id = Column(Integer, ForeignKey('users.id'))
    user = relationship('User', back_populates='projects')

    wiki_enabled = Column(Integer)
    site_id = Column(Integer, ForeignKey('sites.id'))
    site = relationship('Site')

User.projects = relationship('Project', back_populates='user')

class Repository(Base):
    __tablename__ = 'repositories'
    id = Column(Integer, primary_key=True)
    description = Column(String)
    name = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship('Project', back_populates='repositories')

    user_id = Column(Integer, ForeignKey('users.id'))
    user = relationship('User', back_populates='repositories')

    wiki_permissions = Column(Integer)

Project.repositories = relationship('Repository', back_populates='project')
User.repositories = relationship('Repository', back_populates='user')