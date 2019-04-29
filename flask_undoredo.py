import json
import enum

from sqlalchemy import (
    Column,
    Boolean,
    Integer,
    String,
    create_engine,
    and_,
    event,
    func,
)
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.orm import sessionmaker, scoped_session

from sqlalchemy.sql import dml, select, delete
from sqlalchemy.ext.declarative import declarative_base


Base = declarative_base()


class EnumEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, enum.Enum):
            return obj.name
        return json.JSONEncoder.default(self, obj)


class UndoRedoMixin(object):
    id = Column(Integer, primary_key=True)

    object_type = Column(String, nullable=False)
    stack_id = Column(Integer, index=True, nullable=False)
    capture_id = Column(Integer, index=True, nullable=False)

    stmt = Column(String, nullable=False)
    _params = Column(String, nullable=False)

    @property
    def params(self):
        return json.loads(self._params)

    @params.setter
    def params(self, params):
        self._params = json.dumps(params, cls=EnumEncoder)


class UndoAction(Base, UndoRedoMixin):
    __tablename__ = "undo_action"

    active = Column(Boolean, default=True, nullable=False)


class RedoAction(Base, UndoRedoMixin):
    __tablename__ = "redo_action"

    active = Column(Boolean, default=False, nullable=False)


class UndoRedoContext(object):
    def __init__(self, app_session, session, object_type, stack_id):
        self.app_session = app_session
        self.app_engine = self.app_session.get_bind()
        self.session = session
        self.object_type = object_type
        self.stack_id = stack_id
        self.last_capture = 0

    def before_exec(self, conn, clauseelement, multiparams, params):
        if not isinstance(clauseelement, (dml.Delete, dml.Update)):
            return

        if multiparams and multiparams[0]:
            return

        query = select([clauseelement.table])
        if clauseelement._whereclause is not None:
            query = query.where(clauseelement._whereclause)

        stmt_redo = clauseelement.compile(dialect=DefaultDialect())

        self.session.add(
            RedoAction(
                object_type=self.object_type,
                stack_id=self.stack_id,
                capture_id=self.last_capture + 1,
                stmt=str(stmt_redo),
                params=stmt_redo.params,
            )
        )

        if isinstance(clauseelement, dml.Delete):
            for row in conn.execute(query):
                stmt_undo = (
                    dml.Insert(clauseelement.table)
                    .values(**{k: v for (k, v) in row.items() if v is not None})
                    .compile(dialect=DefaultDialect())
                )

                self.session.add(
                    UndoAction(
                        object_type=self.object_type,
                        stack_id=self.stack_id,
                        capture_id=self.last_capture + 1,
                        stmt=str(stmt_undo),
                        params=stmt_undo.params,
                    )
                )
        elif isinstance(clauseelement, dml.Update):
            for row in conn.execute(query):
                stmt_undo = (
                    dml.Update(clauseelement.table)
                    .values(
                        **{
                            column.name: row[column.name]
                            for column in clauseelement.parameters.keys()
                        }
                    )
                    .where(
                        and_(
                            *[
                                column.__eq__(row[column.name])
                                for column in clauseelement.table.primary_key.columns.values()
                            ]
                        )
                    )
                    .compile(dialect=DefaultDialect())
                )

                self.session.add(
                    UndoAction(
                        object_type=self.object_type,
                        stack_id=self.stack_id,
                        capture_id=self.last_capture + 1,
                        stmt=str(stmt_undo),
                        params=stmt_undo.params,
                    )
                )

    def after_exec(self, conn, clauseelement, multiparams, params, result):
        if isinstance(clauseelement, dml.Insert):
            new_pk = dict(
                zip(
                    clauseelement.table.primary_key.columns.keys(),
                    result.inserted_primary_key,
                )
            )

            where_clause = and_(
                *[
                    column.__eq__(value)
                    for (column, value) in zip(
                        clauseelement.table.primary_key.columns.values(),
                        result.inserted_primary_key,
                    )
                ]
            )

            stmt_redo = clauseelement.values(
                {
                    **{c.name: c.default.arg for c in result.prefetch_cols()},
                    **{c.name: c.server_default.arg for c in result.postfetch_cols()},
                    **{k: v for (k, v) in multiparams[0].items() if v is not None},
                    **new_pk,
                }
            ).compile(dialect=DefaultDialect())

            stmt_undo = (
                delete(clauseelement.table)
                .where(where_clause)
                .compile(dialect=DefaultDialect())
            )

            self.session.add(
                RedoAction(
                    object_type=self.object_type,
                    stack_id=self.stack_id,
                    capture_id=self.last_capture + 1,
                    stmt=str(stmt_redo),
                    params=stmt_redo.params,
                )
            )

            self.session.add(
                UndoAction(
                    object_type=self.object_type,
                    stack_id=self.stack_id,
                    capture_id=self.last_capture + 1,
                    stmt=str(stmt_undo),
                    params=stmt_undo.params,
                )
            )

    def __enter__(self):
        self.last_capture = (
            self.session.query(UndoAction)
            .filter_by(object_type=self.object_type, stack_id=self.stack_id)
            .with_entities(func.coalesce(func.max(UndoAction.capture_id), 0))
            .scalar()
        )

        event.listen(self.app_engine, "before_execute", self.before_exec)
        event.listen(self.app_engine, "after_execute", self.after_exec)

    def __exit__(self, exc_type, exc_val, exc_tb):
        event.remove(self.app_engine, "before_execute", self.before_exec)
        event.remove(self.app_engine, "after_execute", self.after_exec)

        self.session.commit()
        self.session.close()


class UndoRedo(object):
    def __init__(self, app=None):
        self.app = app
        self.app_engine = None

        if app is not None:
            self.init_app(app)

        self.session = None

    def init_app(self, app):
        engine = create_engine(app.config["UNDO_REDO_DATABASE_URI"])

        try:
            Base.metadata.create_all(engine, checkfirst=True)
        except:
            pass

        Base.metadata.bind = engine
        self.DBSession = sessionmaker(bind=engine)

    def get_session(self):
        session_obj = scoped_session(self.DBSession)
        self.session = session_obj()

    def clear_history(self, object_type, stack_id):
        self.get_session()

        self.session.query(UndoAction).filter_by(
            object_type=object_type, stack_id=stack_id, active=False
        ).delete()

        self.session.query(RedoAction).filter_by(
            object_type=object_type, stack_id=stack_id, active=True
        ).delete()

        self.session.commit()
        self.session.close()

    def capture(self, app_session, object_type, stack_id):
        self.get_session()
        self.clear_history(object_type, stack_id)
        return UndoRedoContext(app_session, self.session, object_type, stack_id)

    def get_actions(self, model, object_type, stack_id, agg_func=func.max):
        subquery = (
            self.session.query(model)
            .filter_by(object_type=object_type, stack_id=stack_id, active=True)
            .with_entities(agg_func(model.capture_id).label("capture_id"))
            .subquery()
        )

        return self.session.query(model).join(
            subquery,
            and_(
                model.object_type == object_type,
                model.capture_id == subquery.c.capture_id,
            ),
        )

    def undo(self, session, object_type, stack_id):
        self.get_session()

        undo_actions = self.get_actions(UndoAction, object_type, stack_id).all()
        for undo_action in undo_actions:
            session.execute(undo_action.stmt, undo_action.params)
            undo_action.active = False

            self.session.add(undo_action)

        if undo_actions:
            self.session.query(RedoAction).filter_by(
                object_type=object_type, capture_id=undo_actions[0].capture_id
            ).update({"active": True})

        active_undo = (
            self.session.query(UndoAction)
            .filter_by(object_type=object_type, stack_id=stack_id, active=True)
            .count()
        )

        active_redo = (
            self.session.query(RedoAction)
            .filter_by(object_type=object_type, stack_id=stack_id, active=True)
            .count()
        )

        self.session.commit()
        self.session.close()
        return (active_undo, active_redo)

    def redo(self, session, object_type, stack_id):
        self.get_session()

        redo_actions = self.get_actions(
            RedoAction, object_type, stack_id, func.min
        ).all()

        for redo_action in redo_actions:
            session.execute(redo_action.stmt, redo_action.params)
            redo_action.active = False

            self.session.add(redo_action)

        if redo_actions:
            self.session.query(UndoAction).filter_by(
                object_type=object_type, capture_id=redo_actions[0].capture_id
            ).update({"active": True})

        active_undo = (
            self.session.query(UndoAction)
            .filter_by(object_type=object_type, stack_id=stack_id, active=True)
            .count()
        )

        active_redo = (
            self.session.query(RedoAction)
            .filter_by(object_type=object_type, stack_id=stack_id, active=True)
            .count()
        )

        self.session.commit()
        self.session.close()
        return (active_undo, active_redo)
