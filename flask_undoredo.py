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
from sqlalchemy.orm import sessionmaker, scoped_session

from sqlalchemy.sql import dml, select, delete
from sqlalchemy.ext.declarative import declarative_base

from flask import Blueprint


Base = declarative_base()


class UndoRedoAction(Base):
    __tablename__ = "undo_redo_action"

    id = Column(Integer, primary_key=True)
    stack_id = Column(Integer, index=True, nullable=False)
    capture_id = Column(Integer, index=True, nullable=False)

    stmt_undo = Column(String, nullable=False)
    stmt_redo = Column(String, nullable=False)
    redo = Column(Boolean, default=False, nullable=False)


class UndoRedoContext(object):
    def __init__(self, app_engine, session, stack_id):
        self.app_engine = app_engine
        self.session = session
        self.stack_id = stack_id
        self.last_capture = 0

    def before_exec(self, conn, clauseelement, multiparams, params):
        if not isinstance(clauseelement, (dml.Delete, dml.Update)):
            return

        query = select([clauseelement.table])
        if clauseelement._whereclause is not None:
            query = query.where(clauseelement._whereclause)

        stmt_redo = clauseelement.compile(compile_kwargs={"literal_binds": True})

        if isinstance(clauseelement, dml.Delete):
            for row in conn.execute(query):
                stmt_undo = (
                    dml.Insert(clauseelement.table)
                    .values(**row)
                    .compile(compile_kwargs={"literal_binds": True})
                )

                self.session.add(
                    UndoRedoAction(
                        stack_id=self.stack_id,
                        capture_id=self.last_capture + 1,
                        stmt_undo=str(stmt_undo),
                        stmt_redo=str(stmt_redo),
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
                    .compile(compile_kwargs={"literal_binds": True})
                )

                self.session.add(
                    UndoRedoAction(
                        stack_id=self.stack_id,
                        capture_id=self.last_capture + 1,
                        stmt_undo=str(stmt_undo),
                        stmt_redo=str(stmt_redo),
                    )
                )

    def after_exec(self, conn, clauseelement, multiparams, params, result):
        if isinstance(clauseelement, dml.Insert):
            stmt_undo = (
                delete(clauseelement.table)
                .where(
                    and_(
                        *[
                            column.__eq__(value)
                            for (column, value) in zip(
                                clauseelement.table.primary_key.columns.values(),
                                result.inserted_primary_key,
                            )
                        ]
                    )
                )
                .compile(compile_kwargs={"literal_binds": True})
            )

            stmt_redo = clauseelement.values(multiparams).compile(
                compile_kwargs={"literal_binds": True}
            )

            self.session.add(
                UndoRedoAction(
                    stack_id=self.stack_id,
                    capture_id=self.last_capture + 1,
                    stmt_undo=str(stmt_undo),
                    stmt_redo=str(stmt_redo),
                )
            )

    def __enter__(self):
        self.last_capture = (
            self.session.query(UndoRedoAction)
            .filter_by(stack_id=self.stack_id)
            .with_entities(func.coalesce(func.max(UndoRedoAction.capture_id), 0))
            .scalar()
        )

        event.listen(self.app_engine, "before_execute", self.before_exec)
        event.listen(self.app_engine, "after_execute", self.after_exec)

    def __exit__(self, exc_type, exc_val, exc_tb):
        event.listen(self.app_engine, "before_execute", self.before_exec)
        event.remove(self.app_engine, "after_execute", self.after_exec)

        self.session.commit()
        self.session.close()


class UndoRedo(object):
    def __init__(
        self,
        app=None,
        url_prefix="/undoredo",
        endpoint="undoredo",
        database_url="sqlite:///undoredo.db",
    ):
        self.app = app
        self.app_engine = None
        self.url_prefix = url_prefix
        self.endpoint = endpoint
        self.database_url = database_url

        if app is not None:
            self.init_app(app)

        self.session = None

    def init_app(self, app, app_engine=sqlalchemy.engine.Engine):
        engine = create_engine(self.database_url)

        Base.metadata.create_all(engine, checkfirst=True)
        Base.metadata.bind = engine

        self.DBSession = sessionmaker(bind=engine)

        blueprint = Blueprint(self.endpoint, __name__, url_prefix=self.url_prefix)
        blueprint.add_url_rule(
            "/<int:stack_id>/undo/", "undo", self.undo, methods=["POST"]
        )
        blueprint.add_url_rule(
            "/<int:stack_id>/redo/", "redo", self.redo, methods=["POST"]
        )

        app.register_blueprint(blueprint)
        self.app_engine = app_engine

    def get_session(self):
        session_obj = scoped_session(self.DBSession)
        self.session = session_obj()

    def capture(self, stack_id):
        self.get_session()
        return UndoRedoContext(self.session, stack_id)

    def get_actions(self, stack_id, agg_func, redo_flag):
        subquery = (
            self.session.query(UndoRedoAction)
            .filter_by(stack_id=stack_id, redo=redo_flag)
            .with_entities(agg_func(UndoRedoAction.capture_id).label("capture_id"))
            .subquery()
        )

        return self.session.query(UndoRedoAction).join(
            subquery, UndoRedoAction.capture_id == subquery.c.capture_id
        )

    def undo(self, stack_id):
        self.get_session()

        for row in self.get_actions(stack_id, func.max, False):
            self.app_engine.execute(row.stmt_undo)

            row.redo = True
            self.session.add(row)

        self.session.commit()
        self.session.close()
        return "", 200

    def redo(self, stack_id):
        self.get_session()

        for row in self.get_actions(stack_id, func.min, True):
            self.app_engine.execute(row.stmt_redo)

            row.redo = False
            self.session.add(row)

        self.session.commit()
        self.session.close()
        return "", 200
