#!/usr/bin/env python
#
# Copyright (c) 2011 Ivan Zakrevsky and contributors.
import os.path
from setuptools import setup, find_packages
import metadata

app_name = metadata.name
version = metadata.version

setup(
    name = app_name,
    version = version,

    packages = find_packages(),

    author = "Ivan Zakrevsky",
    author_email = "ivzak@yandex.ru",
    description = "SQLBuilder",
    long_description=open(os.path.join(os.path.dirname(__file__), 'README.rst')).read(),
    license = "LGPLv2+ License",
    keywords = "SQL",
    classifiers = [
        'Development Status :: 5 - Production/Stable',
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU Lesser General Public License v2 or later (LGPLv2+)',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
    url = "https://bitbucket.org/evotech/%s" % app_name,
)
