from __future__ import absolute_import
import unittest
from sqlbuilder.mini import P, Sql, compile

__all__ = ('TestMini',)


class TestCase(unittest.TestCase):

    maxDiff = None


class TestMini(TestCase):

    def test_mini(self):

        sql = [
            'SELECT', [
                'author.id', 'author.first_name', 'author.last_name'
            ],
            'FROM', [
                'author', 'INNER JOIN', ['book as b', 'ON', 'b.author_id = author.id']
            ],
            'WHERE', [
                'b.status', '==', P('new')
            ],
            'ORDER BY', [
                'author.first_name', 'author.last_name'
            ]
        ]

        # Let change query
        sql[sql.index('SELECT') + 1].append('author.age')

        self.assertEqual(
            compile(sql),
            ('SELECT author.id, author.first_name, author.last_name, author.age FROM author INNER JOIN book as b ON b.author_id = author.id WHERE b.status == %s ORDER BY author.first_name, author.last_name', ['new'])
        )

    def test_mini_precompiled(self):

        sql = [
            'SELECT', [
                'author.id', 'author.first_name', 'author.last_name'
            ],
            'FROM', [
                'author', 'INNER JOIN', ['book as b', 'ON', 'b.author_id = author.id']
            ],
            'WHERE', [
                'b.status == %(status)s'
            ],
            'ORDER BY', [
                'author.first_name', 'author.last_name'
            ]
        ]

        # Let change query
        sql[sql.index('SELECT') + 1].append('author.age')

        sql_str = compile(sql)[0]
        self.assertEqual(
            (sql_str, {'status': 'new'}),
            ('SELECT author.id, author.first_name, author.last_name, author.age FROM author INNER JOIN book as b ON b.author_id = author.id WHERE b.status == %(status)s ORDER BY author.first_name, author.last_name', {'status': 'new'})
        )

    def test_mini_sql(self):
        sql = [
            'SELECT', [
                'author.id', 'author.first_name', 'author.last_name'
            ],
            'FROM', [
                'author', 'INNER JOIN', [
                    '(', 'SELECT', [
                        'book.title'
                    ],
                    'FROM', [
                        'book'
                    ],
                    ')', 'AS b', 'ON', 'b.author_id = author.id'
                ],
            ],
            'WHERE', [
                'b.status', '==', P('new')
            ],
            'ORDER BY', [
                'author.first_name', 'author.last_name'
            ]
        ]

        sql = Sql(sql)
        sql.prepend_to(
            ['FROM', ('INNER JOIN', 0), lambda x: x.index('SELECT')],
            ['book.id', 'book.pages']
        )
        sql.append_to(
            ['FROM', 'INNER JOIN', 'SELECT'],
            ['book.date']
        )
        sql.insert_after(
            ['FROM', 'INNER JOIN', (lambda x: x.index('FROM') + 1), ],
            ['WHERE', ['b.pages', '>', P(100)]]
        )
        sql.insert_before(
            ['FROM', 'INNER JOIN', 'WHERE', 'b.pages'],
            ['b.pages', '<', P(500), 'AND']
        )

        self.assertEqual(
            compile(sql),
            ('SELECT author.id, author.first_name, author.last_name FROM author INNER JOIN ( SELECT book.id, book.pages, book.title, book.date FROM book WHERE b.pages < %s AND b.pages > %s ) AS b ON b.author_id = author.id WHERE b.status == %s ORDER BY author.first_name, author.last_name', [500, 100, 'new'])
        )
