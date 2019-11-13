import re

from zope.sqlalchemy import register

from sqlalchemy import Column, Integer, DateTime, engine_from_config
from sqlalchemy.sql.expression import func

from sqlalchemy.orm import scoped_session, sessionmaker, eagerload_all
from sqlalchemy.ext.declarative import declarative_base, declared_attr


class ModelError(Exception):

    def __init__(self, errors):
        super(ModelError, self).__init__('\n'.join(errors))
        self.errors = errors


class _Base(object):

    @declared_attr
    def __tablename__(cls):
        # CamelCase to underscore cast
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', cls.__name__)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    __table_args__ = {'mysql_engine': 'InnoDB',
                      'mysql_charset': 'utf8'
                      }

    @property
    def session(self):
        return SessionFactory.get_object_session(self)

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=func.now())

    @classmethod
    def by_id(cls, session, id):
        return cls.first(session, where=(cls.id == id,))

    @classmethod
    def find(cls, session, join=None, where=None, order_by=None, limit=None,
             offset=None, count=None, eagerload=None, group=None):
        qry = cls.build_query(session, join, where, order_by, limit,
                              offset, count, eagerload, group)
        return qry.scalar() if count is not None else qry.all()

    @classmethod
    def first(cls, session, join=None, where=None, order_by=None,
              eagerload=None, group=None):
        return cls.build_query(session, join, where, order_by,
                               eagerload=eagerload, group=group).first()

    @classmethod
    def all(cls, session, page_size=1000, order_by=None):
        offset = 0
        order_by = order_by or cls.id
        while True:
            page = cls.find(session, order_by=order_by,
                            limit=page_size, offset=offset)
            for m in page:
                yield m
            session.flush()
            if len(page) != page_size:
                raise StopIteration()
            offset += page_size

    @classmethod
    def build_query(cls, session, join=None, where=None, order_by=None,
                    limit=None, offset=None, count=None, eagerload=None,
                    group=None):

        aggregating = False
        if count is not None:
            aggregating = True
            query = session.query(func.count(count)).select_from(cls)
        else:
            query = session.query(cls)

        if join:
            if isinstance(join, (list, tuple)):
                for j in join:
                    query = query.join(j)
            else:
                query = query.join(join)

        if where:
            for filter in where:
                query = query.filter(filter)

        if eagerload and not aggregating:
            for relname in eagerload:
                query = query.options(eagerload_all(relname))

        if order_by is not None:
            if count is None:
                if isinstance(order_by, (list, tuple)):
                    query = query.order_by(*order_by)
                else:
                    query = query.order_by(order_by)
        if limit:
            query = query.limit(limit)
        if offset:
            query = query.offset(offset)

        if group:
            group_clause = []
            for key in group:
                group_clause.append(getattr(cls, key))
            query = query.group_by(*group_clause)

        return query

    def validate(self, session, **kwargs):
        """
        return True or raise a :class:`ModelError` Exception
        """
        return True

    def __getitem__(self, key):
        return getattr(self, key)


class Database(object):
    databases = {}

    @classmethod
    def register(cls, name):
        if name not in cls.databases:
            cls.databases[name] = declarative_base(cls=_Base)
        return cls.databases[name]

    @classmethod
    def get(cls, name):
        return cls.databases[name]


class SessionFactory(object):

    sessions = {}

    @classmethod
    def register(cls, name, scoped, engine):
        if scoped:
            cls.sessions[name] = scoped_session(sessionmaker(bind=engine))
            register(cls.sessions[name])
        else:
            cls.sessions[name] = sessionmaker(bind=engine)
        return cls.sessions[name]

    @classmethod
    def get(cls, name):
        return cls.sessions[name]

    @classmethod
    def get_object_session(cls, object_):
        try:
            return cls.sessions[object_.__database__].object_session(object_)
        except KeyError:
            return None


def create_engine(db_name, settings, prefix='sqlalchemy.', scoped=False):
    engine = engine_from_config(settings, prefix)

    DBSession = SessionFactory.register(db_name, scoped, engine)
    DBSession.configure(bind=engine)
    Database.get(db_name).metadata.bind = engine

    return engine


def dispose_engine(db_name):
    Database.get(db_name).metadata.bind.dispose()
