from sqlalchemy import create_engine, ForeignKey
from sqlalchemy.orm import relationship, sessionmaker

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String

Base = declarative_base()

def setup_session(path_to_db):
    engine = create_engine('sqlite:///' + path_to_db)
    Session = sessionmaker()
    Session.configure(bind=engine)
    return Session()


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    login = Column(String)
    email = Column(String)
    fullname = Column(String)

    def __str__(self):
        return '{} <{}>'.format(self.fullname, self.email)

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
    
    user_id = Column(Integer, ForeignKey('users.id'))
    user = relationship('User', back_populates='projects')

    wiki_enabled = Column(String)

User.projects = relationship('Project', back_populates='user')

class Repository(Base):
    __tablename__ = 'repositories'
    id = Column(Integer, primary_key=True)
    description = Column(String)
    name = Column(String)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship('Project', back_populates='repositories')

    user_id = Column(Integer, ForeignKey('users.id'))
    user = relationship('User', back_populates='repositories')

    wiki_permissions = Column(Integer)

Project.repositories = relationship('Repository', back_populates='project')
User.repositories = relationship('Repository', back_populates='user')