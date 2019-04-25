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


Base = declarative_base()


class UndoAction(Base):
    __tablename__ = "undo_action"

    id = Column(Integer, primary_key=True)
    stack_id = Column(Integer, index=True, nullable=False)
    capture_id = Column(Integer, index=True, nullable=False)
    active = Column(Boolean, default=True, nullable=False)

    stmt = Column(String, nullable=False)


class RedoAction(Base):
    __tablename__ = "redo_action"

    id = Column(Integer, primary_key=True)
    stack_id = Column(Integer, index=True, nullable=False)
    capture_id = Column(Integer, index=True, nullable=False)
    active = Column(Boolean, default=False, nullable=False)

    stmt = Column(String, nullable=False)


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

        self.session.add(
            RedoAction(
                stack_id=self.stack_id,
                capture_id=self.last_capture + 1,
                stmt=str(stmt_redo),
            )
        )

        if isinstance(clauseelement, dml.Delete):
            for row in conn.execute(query):
                stmt_undo = (
                    dml.Insert(clauseelement.table)
                    .values(**row)
                    .compile(compile_kwargs={"literal_binds": True})
                )

                self.session.add(
                    UndoAction(
                        stack_id=self.stack_id,
                        capture_id=self.last_capture + 1,
                        stmt=str(stmt_undo),
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
                    UndoAction(
                        stack_id=self.stack_id,
                        capture_id=self.last_capture + 1,
                        stmt=str(stmt_undo),
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
                    **{k: v for (k, v) in multiparams[0].items() if v is not None},
                    **new_pk,
                }
            ).compile(compile_kwargs={"literal_binds": True})

            stmt_undo = (
                delete(clauseelement.table)
                .where(where_clause)
                .compile(compile_kwargs={"literal_binds": True})
            )

            self.session.add(
                RedoAction(
                    stack_id=self.stack_id,
                    capture_id=self.last_capture + 1,
                    stmt=str(stmt_redo),
                )
            )

            self.session.add(
                UndoAction(
                    stack_id=self.stack_id,
                    capture_id=self.last_capture + 1,
                    stmt=str(stmt_undo),
                )
            )

    def __enter__(self):
        self.last_capture = (
            self.session.query(UndoAction)
            .filter_by(stack_id=self.stack_id)
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

    def clear_history(self, stack_id):
        self.get_session()

        self.session.query(UndoAction).filter_by(active=False).delete()
        self.session.query(RedoAction).filter_by(active=True).delete()

        self.session.commit()
        self.session.close()

    def capture(self, engine, stack_id):
        self.get_session()
        self.clear_history(stack_id)
        return UndoRedoContext(engine, self.session, stack_id)

    def get_actions(self, model, stack_id, agg_func=func.max):
        subquery = (
            self.session.query(model)
            .filter_by(stack_id=stack_id, active=True)
            .with_entities(agg_func(model.capture_id).label("capture_id"))
            .subquery()
        )

        return self.session.query(model).join(
            subquery, model.capture_id == subquery.c.capture_id
        )

    def undo(self, session, stack_id):
        self.get_session()

        undo_actions = self.get_actions(UndoAction, stack_id).all()
        for undo_action in undo_actions:
            session.execute(undo_action.stmt)
            undo_action.active = False

            self.session.add(undo_action)

        if undo_actions:
            self.session.query(RedoAction).filter_by(
                capture_id=undo_actions[0].capture_id
            ).update({"active": True})

        active_undo = self.session.query(UndoAction).filter_by(active=True).count()
        active_redo = self.session.query(RedoAction).filter_by(active=True).count()

        self.session.commit()
        self.session.close()
        return (active_undo, active_redo)

    def redo(self, session, stack_id):
        self.get_session()

        redo_actions = self.get_actions(RedoAction, stack_id, func.min).all()
        for redo_action in redo_actions:
            session.execute(redo_action.stmt)
            redo_action.active = False

            self.session.add(redo_action)

        if redo_actions:
            self.session.query(UndoAction).filter_by(
                capture_id=redo_actions[0].capture_id
            ).update({"active": True})

        active_undo = self.session.query(UndoAction).filter_by(active=True).count()
        active_redo = self.session.query(RedoAction).filter_by(active=True).count()

        self.session.commit()
        self.session.close()
        return (active_undo, active_redo)
