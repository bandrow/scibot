from pathlib import Path
from datetime import datetime
import json
from h import models
from h.db import init
from h.util.uri import normalize as uri_normalize
from h.db.types import _get_hex_from_urlsafe, _get_urlsafe_from_hex, URLSafeUUID
from sqlalchemy import create_engine
from sqlalchemy.sql import text
from sqlalchemy.orm.session import sessionmaker
from sqlalchemy.dialects.postgresql import ARRAY
from hyputils.hypothesis import Memoizer
from scibot import config
from scibot.anno import quickload, quickuri, add_doc_all
from scibot.utils import makeSimpleLogger
from interlex.core import makeParamsValues  # FIXME probably need a common import ...
from IPython import embed


def getSession(dburi=config.dbUri(), echo=False):
    engine = create_engine(dburi, echo=echo)

    Session = sessionmaker()
    Session.configure(bind=engine)
    session = Session()
    return session


def init_scibot(database):
    dburi = config.dbUri(user='scibot-admin', database=database)
    #dburi = dbUri('postgres')
    engine = create_engine(dburi)
    init(engine, should_create=True, authority='scicrunch')

    Session = sessionmaker()
    Session.configure(bind=engine)
    session = Session()
    file = Path(__file__).parent / '../sql/permissions.sql'
    with open(file.as_posix(), 'rt') as f:
        sql = f.read()
    #args = dict(database=database)
    # FIXME XXX evil replace
    sql_icky = sql.replace(':database', f'"{database}"')
    session.execute(sql_icky)
    session.commit()


class DbQueryFactory:
    """ parent class for creating converters for queries with uniform results """

    convert = tuple()
    query = ''

    def ___new__(cls, *args, **kwargs):
        return super().__new__(cls)

    def __new__(cls, session):
        newcls = cls.bindSession(session)
        newcls.__new__ = cls.___new__
        return newcls

    @classmethod
    def bindSession(cls, session):
        # this approach seems better than overloading what __new__ does
        # and doing unexpected things in new
        classTypeInstance = type(cls.__name__.replace('Factory',''),
                                 (cls,),
                                 dict(session=session))
        return classTypeInstance

    def __init__(self, condition=''):
        self.condition = condition

    def execute(self, params=None, raw=False):
        if params is None:
            params = {}
        gen = self.session.execute(self.query + ' ' + self.condition, params)
        first = next(gen)
        if raw:
            yield first
            yield from gen
        else:
            Result = namedtuple(self.__class__.__name__ + 'Result', list(first.keys()))  # TODO check perf, seems ok?
            for result in chain((first,), gen):
                yield Result(*(c(v) if c else v for c, v in zip(self.convert, result)))

    def __call__(self, params=None):
        return self.execute(params)

    def __iter__(self):
        """ works for cases without params """
        return self.execute()


class AnnoSyncFactory(Memoizer, DbQueryFactory):
    log = makeSimpleLogger('scibot.db.sync')
    convert = (datetime.isoformat,)
    query = 'SELECT updated FROM annotation ORDER BY updated DESC LIMIT 1'

    def __init__(self, api_token=config.api_token, username=config.username,
                 group=config.group, memoization_file=None, condition=''):
        super().__init__(memoization_file, api_token=api_token, username=username, group=group)
        self.condition = condition

    def sync_annos(self, search_after=None, stop_at=None):
        """ batch sync """
        if self.memoization_file is None:
            try:
                last_updated = next(self)
            except StopIteration:
                last_updated = None
            rows = list(self.yield_from_api(search_after=last_updated, stop_at=stop_at))
        else:
            rows = [a._row for a in self.get_annos()]

        datas = [quickload(r) for r in rows]
        self.log.debug(f'quickload complete for {len(rows)} rows')

        uris = {uri_normalize(j['uri']):quickuri(j)
                for j in sorted(rows,
                                # newest first so that the oldest value will overwrite
                                key=lambda j:j['created'],
                                reverse=True)}
        self.log.debug('uris done')

        dbdocs = {uri:add_doc_all(uri_normalize(uri), created, updated, claims)
                for uri, (created, updated, claims) in uris.items()}
        self.log.debug('dbdocs done')

        vals = list(dbdocs.values())
        self.session.add_all(vals)  # this is super fast locally and hangs effectively forever remotely :/ wat
        self.log.debug('add all done')
        embed()
        self.session.flush()  # get ids without commit
        self.log.debug('flush done')

        """
        a = [models.Annotation(**d,
                                document_id=dbdocs[uri_normalize(d['target_uri'])].id)
                for d in datas]  # slow
        self.log.debug('making annotations')
        self.session.add_all(a)
        self.log.debug('adding all annotations')

        """
        def fix_reserved(k):
            if k == 'references':
                k = '"references"'

            return k

        keys = [fix_reserved(k) for k in datas[0].keys()] + ['document_id']
        def type_fix(k, v):  # TODO is this faster or is type_fix?
            if isinstance(v, dict):
                return json.dumps(v)  # FIXME perf?
            elif isinstance(v, list):
                if any(isinstance(e, dict) for e in v):
                    return json.dumps(v)  # FIXME perf?
            return v

        def make_vs(d):
            document_id = dbdocs[uri_normalize(d['target_uri'])].id
            return [type_fix(k, v) for k, v in d.items()] + [document_id],  # don't miss the , to make this a value set

        def make_types(d):
            def inner(k):
                if k == 'id':
                    return URLSafeUUID
                elif k == 'references':
                    return ARRAY(URLSafeUUID)
                else:
                    return None
            return [inner(k) for k in d] + [None]  # note this is continuous there is no comma

        values_sets = [make_vs(d) for d in datas]
        types = [make_types(d) for d in datas]
        self.log.debug('values sets done')

        *values_templates, values, bindparams = makeParamsValues(*values_sets, types=types)
        sql = text(f'INSERT INTO annotation ({", ".join(keys)}) VALUES {", ".join(values_templates)}')
        sql = sql.bindparams(*bindparams)
        #sql = sql.bindparams(bindparam('id', URLSafeUUID), bindparam('references', ARRAY(URLSafeUUID)))
        try:
            self.session.execute(sql, values)
        except BaseException as e:
            embed()

        self.log.debug('execute done')
        #"""

        self.session.flush()
        self.log.debug('flush done')

        embed()
        return
        self.session.commit()
        self.log.debug('commit done')


    def sync_anno_stream(self,search_after=None, stop_at=None):
        """ streaming one anno at a time version of sync """
        for row in self.yield_from_api(search_after=last_updated, stop_at=stop_at):
            yield row, 'TODO'
            continue
            # TODO
            a = [models.Annotation(**d,
                                   document_id=dbdocs[uri_normalize(d['target_uri'])].id)
                 for d in datas]  # slow
            self.log.debug('making annotations')
            self.session.add_all(a)
            self.log.debug('adding all annotations')




def uuid_to_urlsafe(uuid):
    return _get_urlsafe_from_hex(uuid.hex)


class AnnoQueryFactory(DbQueryFactory):
    convert = (
        uuid_to_urlsafe,
        datetime.isoformat,
        datetime.isoformat,
        None,
        lambda userid: split_user(userid)['username'],
        None,
        lambda lst: [uuid_to_urlsafe(uuid) for uuid in lst],
    )
    query = ('SELECT id, created, updated, target_uri, userid, tags, a.references '
             'FROM annotation AS a')


def bindSession(cls, session):
    return cls.bindSession(session)
