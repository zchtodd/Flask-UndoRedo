"""
Flask-UndoRedo
-------------

Add undo/redo functionality for Flask applications using SQLAlchemy.
This package is still in a beta phase.
"""
from setuptools import setup


setup(
    name="Flask-UndoRedo",
    version="1.0.4",
    license="MIT",
    url="https://github.com/zchtodd/Flask-UndoRedo",
    author="Zach Todd",
    author_email="zchtodd@gmail.com",
    description="Add undo/redo functionality for Flask applications using SQLAlchemy.",
    long_description=__doc__,
    py_modules=["flask_undoredo"],
    zip_safe=False,
    include_package_data=True,
    platforms="any",
    install_requires=[
        "Flask",
        "SQLAlchemy"
    ],
    classifiers=[
        "Environment :: Web Environment",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
        "Topic :: Software Development :: Libraries :: Python Modules"
    ]
)
