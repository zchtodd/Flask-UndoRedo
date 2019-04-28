# Flask-UndoRedo

Add undo/redo functionality for Flask applications that use SQLAlchemy.   

Flask-UndoRedo is a Flask extension that uses the SQLAlchemy event API to record inserts, updates, and deletes in order to later undo or redo them.

# Installation
Installation via pip:
    
    pip install Flask-UndoRedo
    
# Usage
The following is an example of integrating Flask-UndoRedo into application initialization.

```
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_undoredo import UndoRedo

db = SQLAlchemy()
undo_redo = UndoRedo()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
app.config["UNDO_REDO_DATABASE_URI"] = "sqlite:///:memory:"

db.init_app(app)
undo_redo.init_app(app)
```

The string value associated with the <code>UNDO_REDO_DATABASE_URI</code> configuration key is used to connect to the desired database.  The value is expected to be in the same format as that provided to SQLAlchemy.

Flask-UndoRedo will create an <code>undo_action</code> and a <code>redo_action</code> table in the public schema when the <code>init_app</code> method is first called.  The Flask-UndoRedo API provides <code>capture</code>, <code>undo</code>, and <code>redo</code> methods available on the <code>undo_redo</code> instance.

The <code>capture</code> method returns a context manager.  SQL queries executed within this context are captured and logged to the <code>undo_action</code> and <code>redo_action</code> tables.

```
db.session.flush()
with undo_redo.capture(db.session, "document", document.id):
    Document.query.filter_by(name=name).delete()
```

The <code>capture</code> method requires a session with which to execute its own queries, an object type, as well as an integer value to associate the capture session with.  These values should be provided to the <code>undo</code> and <code>redo</code> methods in order to manipulate the correct history.

```
undo, redo = undo_redo.undo(db.session, "document", document.id)
undo, redo = undo_redo.redo(db.session, "document", document.id)
```

Both methods return the count of available undo and redo actions as of when that call was completed.  Calling either method will cause SQL statements that were generated and logged earlier during a capture session to be executed.  Inserts will be issued to negate deletes, deletes to undo inserts, and inverse updates for earlier updates.

# Limitations

As of this writing, Flask-UndoRedo does not support all SQLAlchemy ORM queries.  Please refer to the test cases as a guide to what statements have been proven to work.

Please read http://www.codetodd.com/undo-redo-for-flask-and-sqlalchemy/ for more discussion of limitations and implementation details.
