import unittest

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from flask import Flask
from .flask_undoredo import UndoAction, RedoAction, UndoRedo

Base = declarative_base()



class Widget(Base):
    __tablename__ = "widget"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)


class UndoRedoTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        Base.metadata.create_all(self.engine)
        Base.metadata.bind = self.engine

        Session = sessionmaker(bind=self.engine)
        self.session = Session()

        self.undo_redo = UndoRedo()

        app = Flask(__name__)
        app.config["UNDO_REDO_DATABASE_URI"] = "sqlite:///:memory:"

        self.app_context = app.app_context()
        self.app_context.push()

        self.undo_redo.init_app(app)
        self.addCleanup(self.detach)

    def detach(self):
        self.app_context.pop()

    def test_undo_redo_updates(self):
        self.session.add(Widget(name="Foo"))
        self.session.add(Widget(name="Bar"))
        self.session.flush()

        with self.undo_redo.capture(self.engine, "widget", 1):
            self.session.query(Widget).filter_by(name = "Foo").update({"name": "Baz"})

        self.assertEqual(self.session.query(Widget.name).all(), [("Baz",), ("Bar",)])

        undo, redo = self.undo_redo.undo(self.session, "widget", 1)

        self.assertEqual(undo, 0)
        self.assertEqual(redo, 1)
        self.assertEqual(self.session.query(Widget.name).all(), [("Foo",), ("Bar",)])

        undo, redo = self.undo_redo.redo(self.session, "widget", 1)

        self.assertEqual(undo, 1)
        self.assertEqual(redo, 0)
        self.assertEqual(self.session.query(Widget.name).all(), [("Baz",), ("Bar",)])

    def test_undo_redo_inserts(self):
        for name in ("Foo", "Bar", "Baz"):
            with self.undo_redo.capture(self.engine, "widget", 1):
                self.session.add(Widget(name=name))
                self.session.commit()

        expected = [["Foo", "Bar", "Baz"], ["Foo", "Bar"], ["Foo"], []]
        for i in range(0, 4):
            widgets = [widget.name for widget in self.session.query(Widget).all()]

            self.undo_redo.undo(self.session, "widget", 1)
            self.assertEqual(widgets, expected[i])

        for i in range(3, 0, -1):
            widgets = [widget.name for widget in self.session.query(Widget).all()]

            self.undo_redo.redo(self.session, "widget", 1)
            self.assertEqual(widgets, expected[i])

    def test_undo_redo_deletes(self):
        self.session.add_all((Widget(name="Foo"), Widget(name="Bar"), Widget(name="Baz")))
        self.session.flush()

        for name in ("Foo", "Bar", "Baz"):
            with self.undo_redo.capture(self.engine, "widget", 1):
                self.session.query(Widget).filter_by(name=name).delete()
                self.session.commit()

        expected = [[], ["Baz"], ["Bar", "Baz"], ["Foo", "Bar", "Baz"]]
        for i in range(0, 4):
            widgets = [widget.name for widget in self.session.query(Widget).all()]
            self.assertEqual(len(widgets), i)

            self.undo_redo.undo(self.session, "widget", 1)
            self.assertEqual(widgets, expected[i])

        for i in range(3, 0, -1):
            widgets = [widget.name for widget in self.session.query(Widget).all()]
            self.assertEqual(len(widgets), i)

            self.undo_redo.redo(self.session, "widget", 1)
            self.assertEqual(widgets, expected[i])

    def test_clear_history(self):
        for name in ("Foo", "Bar", "Baz"):
            with self.undo_redo.capture(self.engine, "widget", 1):
                self.session.add(Widget(name=name))
                self.session.commit()

        self.undo_redo.undo(self.session, "widget", 1)
        self.undo_redo.clear_history("widget", 1)
