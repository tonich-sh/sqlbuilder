# -*- coding: utf-8 -*-
# Some ideas from http://code.google.com/p/py-smart-sql-constructor/ , but implementation another.
# Pay attention also to excellent lightweight SQLBuilder
# of Storm ORM http://bazaar.launchpad.net/~storm/storm/trunk/view/head:/storm/expr.py
from __future__ import absolute_import

import collections
import copy
import operator
import types

from sqlbuilder.smartsql.compiler import Compiler, State, cached_compile, compile
from sqlbuilder.smartsql.constants import CONTEXT, DEFAULT_DIALECT, LOOKUP_SEP, MAX_PRECEDENCE, OPERATORS, PLACEHOLDER
from sqlbuilder.smartsql.exceptions import Error, MaxLengthError, OperatorNotFound
from sqlbuilder.smartsql.expressions import (
    Operable, Expr, ExprList, CompositeExpr, Param, Parentheses, OmitParentheses, expr_repr, datatypeof, compile_exprlist
)
from sqlbuilder.smartsql.factory import factory, Factory
from sqlbuilder.smartsql.operator_registry import OperatorRegistry, operator_registry
from sqlbuilder.smartsql.operators import (
    Binary, NamedBinary, NamedCompound, Add, Sub, Mul, Div, Gt, Lt, Ge, Le, And, Or,
    Eq, Ne, Is, IsNot, In, NotIn, RShift, LShift, compile_binary
)
from sqlbuilder.smartsql.pycompat import str, string_types
from sqlbuilder.smartsql.utils import Undef, UndefType, is_allowed_attr, is_list, opt_checker, same, warn

SPACE = " "
Placeholder = Param
Condition = Binary
NamedCondition = NamedBinary


class EscapeForLike(Expr):

    __slots__ = ('expr',)

    escape = "!"
    escape_map = tuple(  # Ordering is important!
        (i, "!{0}".format(i)) for i in ('!', '_', '%')
    )

    def __init__(self, expr):
        Operable.__init__(self)
        self.expr = expr


@compile.when(EscapeForLike)
def compile_escapeforlike(compile, expr, state):
    escaped = expr.expr
    for k, v in expr.escape_map:
        escaped = func.Replace(escaped, Value(k), Value(v))
    compile(escaped, state)


class Like(NamedBinary):
    __slots__ = ('escape',)
    sql = 'LIKE'

    def __init__(self, left, right, escape=Undef):
        """
        :type escape: str | Undef
        """
        Operable.__init__(self)
        self.left = left
        self.right = right
        if isinstance(right, EscapeForLike):
            self.escape = right.escape
        else:
            self.escape = escape


class ILike(Like):
    __slots__ = ()
    sql = 'ILIKE'


@compile.when(Like)
def compile_like(compile, expr, state):
    compile_binary(compile, expr, state)
    if expr.escape is not Undef:
        state.sql.append(' ESCAPE ')
        compile(Value(expr.escape) if isinstance(expr.escape, string_types) else expr.escape, state)


class FieldList(ExprList):
    __slots__ = ()

    def __init__(self, *args):
        # if args and is_list(args[0]):
        #     return self.__init__(*args[0])
        Operable.__init__(self)
        self.sql, self.data = ", ", list(args)


@compile.when(FieldList)
def compile_fieldlist(compile, expr, state):
    # state.push('context', CONTEXT.COLUMN)
    compile_exprlist(compile, expr, state)
    # state.pop()


class Concat(ExprList):

    __slots__ = ('_ws', )

    def __init__(self, *args):
        super(Concat, self).__init__(*args)
        self.sql = ' || '
        self._ws = None

    def ws(self, sep=None):
        if sep is None:
            return self._ws
        self._ws = sep
        self.sql = ', '
        return self


class Array(ExprList):  # TODO: use composition instead of inheritance, to solve ambiguous of __getitem__()???
    __slots__ = ()

    def __init__(self, *args):
        Operable.__init__(self)
        self.sql, self.data = ", ", list(args)


@compile.when(Array)
def compile_array(compile, expr, state):
    if not expr.data:
        state.sql.append("'{}'")
    state.sql.append("ARRAY[{0}]".format(compile_exprlist(compile, expr, state)))


@compile.when(Concat)
def compile_concat(compile, expr, state):
    if not expr.ws():
        return compile_exprlist(compile, expr, state)
    state.sql.append('concat_ws(')
    compile(expr.ws(), state)
    for a in expr:
        state.sql.append(expr.sql)
        compile(a, state)
    state.sql.append(')')


class Prefix(Expr):

    __slots__ = ('expr', )

    def __init__(self, prefix, expr):
        Expr.__init__(self, prefix)
        self.expr = expr


@compile.when(Prefix)
def compile_prefix(compile, expr, state):
    state.sql.append(expr.sql)
    state.sql.append(SPACE)
    compile(expr.expr, state)


class NamedPrefix(Prefix):
    __slots__ = ()

    def __init__(self, expr):
        Operable.__init__(self)
        self.expr = expr


class Not(NamedPrefix):
    __slots__ = ()
    sql = 'NOT'


class All(NamedPrefix):
    __slots__ = ()
    sql = 'ALL'


class Distinct(NamedPrefix):
    __slots__ = ()
    sql = 'DISTINCT'


class Exists(NamedPrefix):
    __slots__ = ()
    sql = 'EXISTS'


class Unary(Prefix):
    __slots__ = ()


@compile.when(Unary)
def compile_unary(compile, expr, state):
    state.sql.append(expr.sql)
    compile(expr.expr, state)


class NamedUnary(Unary):
    __slots__ = ()

    def __init__(self, expr):
        self.expr = expr


class Pos(NamedUnary):
    __slots__ = ()
    sql = '+'


class Neg(NamedUnary):
    __slots__ = ()
    sql = '-'


class Postfix(Expr):
    __slots__ = ('expr', )

    def __init__(self, expr, postfix):
        Expr.__init__(self, postfix)
        self.expr = expr


@compile.when(Postfix)
def compile_postfix(compile, expr, state):
    compile(expr.expr, state)
    state.sql.append(SPACE)
    state.sql.append(expr.sql)


class NamedPostfix(Postfix):
    __slots__ = ()

    def __init__(self, expr):
        Operable.__init__(self)
        self.expr = expr


class OrderDirection(NamedPostfix):
    __slots__ = ()

    def __init__(self, expr):
        Operable.__init__(self)
        if isinstance(expr, OrderDirection):
            expr = expr.expr
        self.expr = expr


class Asc(OrderDirection):
    __slots__ = ()
    sql = 'ASC'


class Desc(OrderDirection):
    __slots__ = ()
    sql = 'DESC'


class Ternary(Expr):
    __slots__ = ('second_sql', 'first', 'second', 'third')

    def __init__(self, first, sql, second, second_sql, third):
        Expr.__init__(self, sql)
        self.first = first
        self.second = second
        self.second_sql = second_sql
        self.third = third


@compile.when(Ternary)
def compile_ternary(compile, expr, state):
    compile(expr.first, state)
    state.sql.append(SPACE)
    state.sql.append(expr.sql)
    state.sql.append(SPACE)
    compile(expr.second, state)
    state.sql.append(SPACE)
    state.sql.append(expr.second_sql)
    state.sql.append(SPACE)
    compile(expr.third, state)


class NamedTernary(Ternary):
    __slots__ = ()

    def __init__(self, first, second, third):
        Operable.__init__(self)
        self.first = first
        self.second = second
        self.third = third


class Between(NamedTernary):
    __slots__ = ()
    sql = 'BETWEEN'
    second_sql = 'AND'


class NotBetween(Between):
    __slots__ = ()
    sql = 'NOT BETWEEN'


class Case(Expr):
    __slots__ = ('cases', 'expr', 'default')

    def __init__(self, cases, expr=Undef, default=Undef):
        Operable.__init__(self)
        self.cases = cases
        self.expr = expr
        self.default = default


@compile.when(Case)
def compile_case(compile, expr, state):
    state.sql.append('CASE')
    if expr.expr is not Undef:
        state.sql.append(SPACE)
        compile(expr.expr, state)
    for clause, value in expr.cases:
        state.sql.append(' WHEN ')
        compile(clause, state)
        state.sql.append(' THEN ')
        compile(value, state)
    if expr.default is not Undef:
        state.sql.append(' ELSE ')
        compile(expr.default, state)
    state.sql.append(' END ')


class Callable(Expr):

    __slots__ = ('expr', 'args')

    def __init__(self, expr, *args):
        Operable.__init__(self)
        self.expr = expr
        self.args = ExprList(*args).join(", ")


@compile.when(Callable)
def compile_callable(compile, expr, state):
    compile(expr.expr, state)
    state.sql.append('(')
    compile(expr.args, state)
    state.sql.append(')')


class NamedCallable(Callable):
    __slots__ = ()

    def __init__(self, *args):
        Operable.__init__(self)
        self.args = ExprList(*args).join(", ")


@compile.when(NamedCallable)
def compile_namedcallable(compile, expr, state):
    state.sql.append(expr.sql)
    state.sql.append('(')
    compile(expr.args, state)
    state.sql.append(')')


class Cast(NamedCallable):
    __slots__ = ("expr", "type",)
    sql = "CAST"

    def __init__(self, expr, type):
        Operable.__init__(self)
        self.expr = expr
        self.type = type


@compile.when(Cast)
def compile_cast(compile, expr, state):
    state.sql.append(expr.sql)
    state.sql.append('(')
    compile(expr.expr, state)
    state.sql.append(' AS ')
    state.sql.append(expr.type)
    state.sql.append(')')


class Constant(Expr):

    __slots__ = ()

    def __init__(self, const):
        Expr.__init__(self, const.upper())

    def __call__(self, *args):
        return Callable(self, *args)


@compile.when(Constant)
def compile_constant(compile, expr, state):
    state.sql.append(expr.sql)


class ConstantSpace(object):

    __slots__ = ()

    def __getattr__(self, attr):
        return Constant(attr)


class MetaFieldSpace(type):

    def __instancecheck__(cls, instance):
        return isinstance(instance, Field)

    def __subclasscheck__(cls, subclass):
        return issubclass(subclass, Field)

    def __getattr__(cls, key):
        if key[:2] == '__':
            raise AttributeError
        parts = key.split(LOOKUP_SEP, 2)
        prefix, name, alias = parts + [None] * (3 - len(parts))
        if name is None:
            prefix, name = name, prefix
        f = cls(name, prefix)
        return f.as_(alias) if alias else f

    def __call__(cls, *a, **kw):
        return Field(*a, **kw)


class F(MetaFieldSpace("NewBase", (object, ), {})):
    pass


class MetaField(type):

    def __getattr__(cls, key):
        if key[:2] == '__':
            raise AttributeError
        parts = key.split(LOOKUP_SEP, 2)
        prefix, name, alias = parts + [None] * (3 - len(parts))
        if name is None:
            prefix, name = name, prefix
        f = cls(name, prefix)
        return f.as_(alias) if alias else f


class Field(MetaField("NewBase", (Expr,), {})):
    # It's a field, not column, because prefix can be alias of subquery.
    # It also can be a field of composite column.
    __slots__ = ('_name', '_prefix', '__cached__')  # TODO: m_* prefix instead of _* prefix?

    def __init__(self, name, prefix=None, datatype=None):
        Operable.__init__(self, datatype)
        if isinstance(name, string_types):
            if name == '*':
                name = Constant(name)
            else:
                name = Name(name)
        self._name = name
        if isinstance(prefix, string_types):
            prefix = Table(prefix)
        self._prefix = prefix
        self.__cached__ = {}


@compile.when(Field)
@cached_compile
def compile_field(compile, expr, state):
    if expr._prefix is not None:
        state.auto_tables.append(expr._prefix)  # it's important to know the concrete alias of table.
        compile(expr._prefix, state)
        state.sql.append('.')
    compile(expr._name, state)


class Subfield(Expr):

    __slots__ = ('parent', 'name')

    def __init__(self, parent, name):
        Operable.__init__(self)
        self.parent = parent
        if isinstance(name, string_types):
            name = Name(name)
        self.name = name


@compile.when(Subfield)
def compile_subfield(compile, expr, state):
    parent = expr.parent
    if True:  # get me from context
        parent = Parentheses(parent)
    compile(parent)
    state.sql.append('.')
    compile(expr.name, state)


class ArrayItem(Expr):

    __slots__ = ('array', 'key')

    def __init__(self, array, key):
        Operable.__init__(self)
        self.array = array
        assert isinstance(key, slice)
        self.key = key


@compile.when(ArrayItem)
def compile_arrayitem(compile, expr, state):
    compile(expr.array)
    state.sql.append("[")
    state.sql.append("{0:d}".format(expr.key.start))
    if expr.key.stop is not None:
        state.sql.append(", ")
        state.sql.append("{0:d}".format(expr.key.stop))
    state.sql.append("]")


class Alias(Expr):

    __slots__ = ('expr', 'sql')

    def __init__(self, alias, expr=None):
        self.expr = expr
        if isinstance(alias, string_types):
            alias = Name(alias)
        super(Alias, self).__init__(alias)


@compile.when(Alias)
def compile_alias(compile, expr, state):
    try:
        render_column = issubclass(state.callers[1], FieldList)
        # render_column = state.context == CONTEXT.COLUMN
    except IndexError:
        pass
    else:
        if render_column:
            compile(expr.expr, state)
            state.sql.append(' AS ')
    compile(expr.sql, state)


@factory.register
class MetaTableSpace(type):

    def __instancecheck__(cls, instance):
        return isinstance(instance, Table)

    def __subclasscheck__(cls, subclass):
        return issubclass(subclass, Table)

    def __getattr__(cls, key):
        if key.startswith('__'):
            raise AttributeError
        parts = key.split(LOOKUP_SEP, 1)
        name, alias = parts + [None] * (2 - len(parts))
        table = cls.__factory__.Table(name)
        return table.as_(alias) if alias else table

    def __call__(cls, name, *a, **kw):
        return cls.__factory__.Table(name, *a, **kw)


@factory.register
class T(factory.MetaTableSpace("NewBase", (object, ), {})):
    pass


class MetaTable(type):

    def __getattr__(cls, key):
        if key[:2] == '__':
            raise AttributeError
        parts = key.split(LOOKUP_SEP, 1)
        name, alias = parts + [None] * (2 - len(parts))
        table = cls(name)
        return table.as_(alias) if alias else table


class FieldProxy(object):

    def __init__(self, table):
        self.__table = table

    def __getattr__(self, key):
        if key[:2] == '__':
            raise AttributeError
        return self.__table.get_field(key)

    __call__ = __getattr__
    __getitem__ = __getattr__

# TODO: Schema support. Not only for table.
# A database contains one or more named schemas, which in turn contain tables.
# Schemas also contain other kinds of named objects, including data types, functions, and operators.
# http://www.postgresql.org/docs/9.4/static/ddl-schemas.html
# Ideas: S.public(T.user), S('public', T.user)


@factory.register
class Table(MetaTable("NewBase", (object, ), {})):
    # Variants:
    # tb.as_ => Field(); tb().as_ => instancemethod() ???
    # author + InnerJoin + book + On + author.id == book.author_id
    # Add __call__() method to Field/Alias
    # Use sys._getframe(), compiler.visitor.ASTVisitor or tokenize.generate_tokens() to get context for Table.__getattr__()

    __slots__ = ('_name', '__cached__', 'f', '_fields', '__factory__')

    def __init__(self, name, *fields):
        if isinstance(name, string_types):
            name = Name(name)
        self._name = name
        self.__cached__ = {}
        self.f = FieldProxy(self)
        self._fields = collections.OrderedDict()
        for f in fields:
            self._append_field(f)

    def as_(self, alias):
        return factory.get(self).TableAlias(alias, self)

    def inner_join(self, right):
        return factory.get(self).TableJoin(self).inner_join(right)

    def left_join(self, right):
        return factory.get(self).TableJoin(self).left_join(right)

    def right_join(self, right):
        return factory.get(self).TableJoin(self).right_join(right)

    def full_join(self, right):
        return factory.get(self).TableJoin(self).full_join(right)

    def cross_join(self, right):
        return factory.get(self).TableJoin(self).cross_join(right)

    def join(self, join_type, obj):
        return factory.get(self).TableJoin(self).join(join_type, obj)

    def on(self, cond):
        return factory.get(self).TableJoin(self).on(cond)

    def hint(self, expr):
        return factory.get(self).TableJoin(self).hint(expr)

    def natural(self):
        return factory.get(self).TableJoin(self).natural()

    def using(self, *fields):
        return factory.get(self).TableJoin(self).using(*fields)

    def _append_field(self, field):
        self._fields[field._name] = field
        field.prefix = self

    def get_field(self, key):
        cache = self.f.__dict__
        if key in cache:
            return cache[key]

        parts = key.split(LOOKUP_SEP, 1)
        name, alias = parts + [None] * (2 - len(parts))

        if name in cache:
            f = cache[name]
        else:
            f = self._fields[name] if name in self._fields else Field(name, self)
            cache[name] = f
        if alias:
            f = f.as_(alias)
            cache[key] = f
        return f

    def __getattr__(self, key):
        if key[:2] == '__' or key in Table.__slots__:
            raise AttributeError
        return self.get_field(key)

    def __getitem__(self, key):
        return self.get_field(key)

    def __repr__(self):
        return expr_repr(self)

    __and__ = same('inner_join')
    __add__ = same('left_join')
    __sub__ = same('right_join')
    __or__ = same('full_join')
    __mul__ = same('cross_join')


@compile.when(Table)
def compile_table(compile, expr, state):
    compile(expr._name, state)


@factory.register
class TableAlias(Table):

    __slots__ = ('_table',)

    def __init__(self, name, table=None, *fields):
        Table.__init__(self, name, *fields)
        self._table = table
        if not fields and isinstance(table, Table):
            for f in table._fields.values():
                self._append_field(copy.copy(f))

    def as_(self, alias):
        return type(self)(alias, self._table)


@compile.when(TableAlias)
def compile_tablealias(compile, expr, state):
    # if expr._table is not None and state.context == CONTEXT.TABLE:
    try:
        render_table = expr._table is not None and issubclass(state.callers[1], TableJoin)
        # render_table = expr._table is not None and state.context == CONTEXT.TABLE
    except IndexError:
        pass
    else:
        if render_table:
            compile(expr._table, state)
            state.sql.append(' AS ')
    compile(expr._name, state)


@factory.register
class TableJoin(object):

    __slots__ = ('_table', '_join_type', '_on', '_left', '_hint', '_nested', '_natural', '_using', '__factory__')

    # TODO: support for ONLY http://www.postgresql.org/docs/9.4/static/tutorial-inheritance.html

    def __init__(self, table_or_alias, join_type=None, on=None, left=None):
        self._table = table_or_alias
        self._join_type = join_type
        self._on = on
        self._left = left
        self._hint = None
        self._nested = False
        self._natural = False
        self._using = None

    def inner_join(self, right):
        return self.join("INNER JOIN", right)

    def left_join(self, right):
        return self.join("LEFT OUTER JOIN", right)

    def right_join(self, right):
        return self.join("RIGHT OUTER JOIN", right)

    def full_join(self, right):
        return self.join("FULL OUTER JOIN", right)

    def cross_join(self, right):
        return self.join("CROSS JOIN", right)

    def join(self, join_type, right):
        if not isinstance(right, TableJoin) or right.left():
            right = type(self)(right, left=self)
        right = right.left(self).join_type(join_type)
        return right

    def left(self, left=None):
        if left is None:
            return self._left
        self._left = left
        return self

    def join_type(self, join_type):
        self._join_type = join_type
        return self

    def on(self, cond):
        if self._on is not None:
            c = self.__class__(self)  # TODO: Test me.
        else:
            c = self
        c._on = cond
        return c

    def natural(self):
        self._natural = True
        return self

    def using(self, *fields):
        self._using = ExprList(*fields).join(", ")
        return self

    def __call__(self):
        self._nested = True
        c = self.__class__(self)
        return c

    def hint(self, expr):
        if isinstance(expr, string_types):
            expr = Expr(expr)
        self._hint = OmitParentheses(expr)
        return self

    def __copy__(self):
        dup = copy.copy(super(TableJoin, self))
        for a in ['_hint', ]:
            setattr(dup, a, copy.copy(getattr(dup, a, None)))
        return dup

    def __repr__(self):
        return expr_repr(self)

    as_nested = same('__call__')
    group = same('__call__')
    __and__ = same('inner_join')
    __add__ = same('left_join')
    __sub__ = same('right_join')
    __or__ = same('full_join')
    __mul__ = same('cross_join')


@compile.when(TableJoin)
def compile_tablejoin(compile, expr, state):
    if expr._nested:
        state.sql.append('(')
    if expr._left is not None:
        compile(expr._left, state)
    if expr._join_type:
        state.sql.append(SPACE)
        if expr._natural:
            state.sql.append('NATURAL ')
        state.sql.append(expr._join_type)
        state.sql.append(SPACE)
    state.push('context', CONTEXT.TABLE)
    compile(expr._table, state)
    state.pop()
    if expr._on is not None:
        state.sql.append(' ON ')
        compile(expr._on, state)
    elif expr._using is not None:
        state.sql.append(' USING ')
        compile(expr._using, state)
    if expr._hint is not None:
        state.sql.append(SPACE)
        compile(expr._hint, state)
    if expr._nested:
        state.sql.append(')')


class Result(object):
    """Default implementation of Query class.

    It uses the Bridge pattern to separate implementation from interface.
    """

    compile = compile

    def __init__(self, compile=None):
        if compile is not None:
            self.compile = compile
        self._query = None

    def execute(self):
        return self.compile(self._query)

    select = count = insert = update = delete = execute

    def __call__(self, query):
        c = self  # Don't clone here to keep link to cache in this instance
        c._query = query
        return c

    def clone(self):
        c = copy.copy(super(Result, self))
        c._query = None
        return c

    def __iter__(self):
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError

    def __getitem__(self, key):
        if isinstance(key, slice):
            offset = key.start or 0
            limit = key.stop - offset if key.stop else None
        else:
            offset, limit = key, 1
        return self._query.limit(offset, limit)

    __copy__ = clone


class Executable(object):

    result = Result()  # IoC

    def __init__(self, result=None):
        """ Query class.

        It uses the Bridge pattern to separate implementation from interface.

        :param result: Object of implementation.
        :type result: Result
        """
        if result is not None:
            self.result = result
        else:
            self.result = self.result.clone()

    def clone(self, *attrs):
        c = super(Executable, self).clone(*attrs)
        c.result = c.result.clone()
        return c

    def __getattr__(self, name):
        """Delegates unknown attributes to object of implementation."""
        try:
            return super(Executable, self).__getattr__(name)
        except AttributeError:
            if hasattr(self.result, name):
                attr = getattr(self.result, name)
                if isinstance(attr, types.MethodType):
                    c = self.clone()
                    return getattr(c.result(c), name)
                else:
                    return attr
            raise AttributeError


@factory.register
class Select(Expr):

    def __init__(self, tables=None):
        """ Select class.

        :param tables: tables
        :type tables: Table, TableAlias, TableJoin or None
        """
        Operable.__init__(self)
        self._distinct = ExprList().join(", ")
        self._fields = FieldList().join(", ")
        if tables is not None:
            if not isinstance(tables, TableJoin):
                tables = factory.get(self).TableJoin(tables)
        self._tables = tables
        self._where = None
        self._having = None
        self._group_by = ExprList().join(", ")
        self._order_by = ExprList().join(", ")
        self._limit = None
        self._offset = None
        self._for_update = False

    def tables(self, tables=None):
        if tables is None:
            return self._tables
        c = self.clone('_tables')
        c._tables = tables if isinstance(tables, TableJoin) else factory.get(c).TableJoin(tables)
        return c

    @opt_checker(["reset", ])
    def distinct(self, *args, **opts):
        if not args and not opts:
            return self._distinct

        if args:
            if is_list(args[0]):
                return self.distinct(*args[0], reset=True)
            elif args[0] is True and not opts.get("reset"):
                return self.distinct(*args, reset=True)
            elif args[0] is False:
                return self.distinct(reset=True)

        c = self.clone('_distinct')
        if opts.get("reset"):
            c._distinct.reset()
        if args:
            c._distinct.extend(args)
        return c

    @opt_checker(["reset", ])
    def fields(self, *args, **opts):
        # Why not name the "args" by "expressions", or "exprs" or "fields"?
        # Because it wil be a lie. The argument can be a list of expressions.
        # The name "argument" is not entirely clear, but truthful and not misleading.
        if not args and not opts:
            return self._fields

        if args and is_list(args[0]):
            return self.fields(*args[0], reset=True)

        c = self.clone('_fields')
        if opts.get("reset"):
            c._fields.reset()
        if args:
            c._fields.extend([Field(f) if isinstance(f, string_types) else f for f in args])
        return c

    def on(self, cond):
        # TODO: Remove?
        c = self.clone()
        if not isinstance(c._tables, TableJoin):
            raise Error("Can't set on without join table")
        c._tables = c._tables.on(cond)
        return c

    def where(self, cond=None, op=operator.and_):
        if cond is None:
            return self._where
        c = self.clone()
        if c._where is None or op is None:
            c._where = cond
        else:
            c._where = op(c._where, cond)
        return c

    def or_where(self, cond):
        warn('or_where(cond)', 'where(cond, op=operator.or_)')
        return self.where(cond, op=operator.or_)

    @opt_checker(["reset", ])
    def group_by(self, *args, **opts):
        if not args and not opts:
            return self._group_by

        if args and is_list(args[0]):
            return self.group_by(*args[0], reset=True)

        c = self.clone('_group_by')
        if opts.get("reset"):
            c._group_by.reset()
        if args:
            c._group_by.extend(args)
        return c

    def having(self, cond=None, op=operator.and_):
        if cond is None:
            return self._having
        c = self.clone()
        if c._having is None or op is None:
            c._having = cond
        else:
            c._having = op(self._having, cond)
        return c

    def or_having(self, cond):
        warn('or_having(cond)', 'having(cond, op=operator.or_)')
        return self.having(cond, op=operator.or_)

    @opt_checker(["desc", "reset", ])
    def order_by(self, *args, **opts):
        if not args and not opts:
            return self._order_by

        if args and is_list(args[0]):
            return self.order_by(*args[0], reset=True)

        c = self.clone('_order_by')
        if opts.get("reset"):
            c._order_by.reset()
        if args:
            wraps = Desc if opts.get("desc") else Asc
            c._order_by.extend([f if isinstance(f, (Asc, Desc)) else wraps(f) for f in args])
        return c

    def limit(self, *args, **kwargs):
        if not args and not kwargs:
            return (self._offset, self._limit)
        c = self.clone()
        if args:
            if len(args) < 2:
                args = (0,) + args
            c._offset, c._limit = args
        else:
            c._limit = kwargs.get('limit')
            c._offset = kwargs.get('offset', 0)
        return c

    def as_table(self, alias):
        return factory.get(self).TableAlias(alias, self)

    def clone(self, *attrs):
        c = copy.copy(super(Select, self))
        # if not attrs:
        #     attrs = ('_fields', '_tables', '_group_by', '_order_by')
        for a in attrs:
            setattr(c, a, copy.copy(getattr(c, a, None)))
        return c

    columns = same('fields')
    __copy__ = same('clone')


@compile.when(Select)
def compile_query(compile, expr, state):
    state.push("auto_tables", [])  # this expr can be a subquery
    state.sql.append("SELECT ")
    if expr.distinct():
        state.sql.append("DISTINCT ")
        if expr.distinct()[0] is not True:
            state.sql.append("ON ")
            compile(Parentheses(expr._distinct), state)
            state.sql.append(SPACE)
    compile(expr.fields(), state)

    tables_sql_pos = len(state.sql)
    tables_params_pos = len(state.params)

    if expr.where():
        state.sql.append(" WHERE ")
        compile(expr.where(), state)
    if expr.group_by():
        state.sql.append(" GROUP BY ")
        compile(expr.group_by(), state)
    if expr.having():
        state.sql.append(" HAVING ")
        compile(expr.having(), state)
    if expr.order_by():
        state.sql.append(" ORDER BY ")
        compile(expr.order_by(), state)
    if expr._limit is not None:
        state.sql.append(" LIMIT ")
        compile(expr._limit, state)
    if expr._offset:
        state.sql.append(" OFFSET ")
        compile(expr._offset, state)
    if expr._for_update:
        state.sql.append(" FOR UPDATE")

    state.push('join_tables', [])
    state.push('sql', [])
    state.push('params', [])
    state.sql.append(" FROM ")
    compile(expr.tables(), state)
    tables_sql = state.sql
    tables_params = state.params
    state.pop()
    state.pop()
    state.pop()
    state.sql[tables_sql_pos:tables_sql_pos] = tables_sql
    state.params[tables_params_pos:tables_params_pos] = tables_params
    state.pop()


@factory.register
class Query(Executable, Select):

    def __init__(self, tables=None, result=None):
        """ Query class.

        It uses the Bridge pattern to separate implementation from interface.

        :param tables: tables
        :type tables: Table, TableAlias, TableJoin or None
        :param result: Object of implementation.
        :type result: Result
        """
        Select.__init__(self, tables)
        Executable.__init__(self, result)

    @opt_checker(["distinct", "for_update"])
    def select(self, *args, **opts):
        c = self.clone()
        if args:
            c = c.fields(*args)
        if opts.get("distinct"):
            c = c.distinct(True)
        if opts.get("for_update"):
            c._for_update = True
        return c.result(c).select()
        # Never clone result. It should have back link to Query instance.
        # State of Result should be corresponding to state of Query object.
        # We need clone both Result and Query synchronously.

    def count(self, **kw):
        return self.result(SelectCount(self, **kw)).count()

    def insert(self, key_values=None, **kw):
        kw.setdefault('table', self._tables)
        kw.setdefault('fields', self._fields)
        return self.result(factory.get(self).Insert(map=key_values, **kw)).insert()

    def insert_many(self, fields, values, **kw):
        # Deprecated
        return self.insert(fields=fields, values=values, **kw)

    def update(self, key_values=None, **kw):
        kw.setdefault('table', self._tables)
        kw.setdefault('fields', self._fields)
        kw.setdefault('where', self._where)
        kw.setdefault('order_by', self._order_by)
        kw.setdefault('limit', self._limit)
        return self.result(factory.get(self).Update(map=key_values, **kw)).update()

    def delete(self, **kw):
        kw.setdefault('table', self._tables)
        kw.setdefault('where', self._where)
        kw.setdefault('order_by', self._order_by)
        kw.setdefault('limit', self._limit)
        return self.result(factory.get(self).Delete(**kw)).delete()

    def as_set(self, all=False):
        return factory.get(self).Set(self, all=all, result=self.result)

    def set(self, *args, **kwargs):
        warn('set([all=False])', 'as_set([all=False])')
        return self.as_set(*args, **kwargs)

    def raw(self, sql, params=()):
        return factory.get(self).Raw(sql, params, result=self.result)

    def __getitem__(self, key):
        return self.result(self).__getitem__(key)

    def __len__(self):
        return self.result(self).__len__()

    def __iter__(self):
        return self.result(self).__iter__()


QuerySet = Query


@factory.register
class SelectCount(Query):

    def __init__(self, q, table_alias='count_list', field_alias='count_value'):
        Query.__init__(self, q.order_by(reset=True).as_table(table_alias))
        self._fields.append(func.Count(Constant('1')).as_(field_alias))


@factory.register
class Raw(Query):

    def __init__(self, sql, params, result=None):
        Query.__init__(self, result=result)
        self._raw = OmitParentheses(Expr(sql, params))


@compile.when(Raw)
def compile_raw(compile, expr, state):
    compile(expr._raw, state)
    if expr._limit is not None:
        state.sql.append(" LIMIT ")
        compile(expr._limit, state)
    if expr._offset:
        state.sql.append(" OFFSET ")
        compile(expr._offset, state)


class Modify(object):

    def __repr__(self):
        return expr_repr(self)


@factory.register
class Insert(Modify):

    def __init__(self, table, map=None, fields=None, values=None, ignore=False, on_duplicate_key_update=None):
        self.table = table
        self.fields = FieldList(*(k if isinstance(k, Expr) else Field(k) for k in (map or fields)))
        self.values = (tuple(map.values()),) if map else values
        self.ignore = ignore
        self.on_duplicate_key_update = tuple(
            (k if isinstance(k, Expr) else Field(k), v)
            for k, v in on_duplicate_key_update.items()
        ) if on_duplicate_key_update else None


@compile.when(Insert)
def compile_insert(compile, expr, state):
    state.sql.append("INSERT ")
    state.sql.append("INTO ")
    compile(expr.table, state)
    state.sql.append(SPACE)
    compile(Parentheses(expr.fields), state)
    if isinstance(expr.values, Query):
        state.sql.append(SPACE)
        compile(expr.values, state)
    else:
        state.sql.append(" VALUES ")
        compile(ExprList(*expr.values).join(', '), state)
    if expr.ignore:
        state.sql.append(" ON CONFLICT DO NOTHING")
    elif expr.on_duplicate_key_update:
        state.sql.append(" ON CONFLICT DO UPDATE SET ")
        first = True
        for f, v in expr.on_duplicate_key_update:
            if first:
                first = False
            else:
                state.sql.append(", ")
            compile(f, state)
            state.sql.append(" = ")
            compile(v, state)


@factory.register
class Update(Modify):

    def __init__(self, table, map=None, fields=None, values=None, ignore=False, where=None, order_by=None, limit=None):
        self.table = table
        self.fields = FieldList(*(k if isinstance(k, Expr) else Field(k) for k in (map or fields)))
        self.values = tuple(map.values()) if map else values
        self.ignore = ignore
        self.where = where
        self.order_by = order_by
        self.limit = limit


@compile.when(Update)
def compile_update(compile, expr, state):
    state.sql.append("UPDATE ")
    if expr.ignore:
        state.sql.append("IGNORE ")
    compile(expr.table, state)
    state.sql.append(" SET ")
    first = True
    for f, v in zip(expr.fields, expr.values):
        if first:
            first = False
        else:
            state.sql.append(", ")
        compile(f, state)
        state.sql.append(" = ")
        compile(v, state)
    if expr.where:
        state.sql.append(" WHERE ")
        compile(expr.where, state)
    if expr.order_by:
        state.sql.append(" ORDER BY ")
        compile(expr.order_by, state)
    if expr.limit is not None:
        state.sql.append(" LIMIT ")
        compile(expr.limit, state)


@factory.register
class Delete(Modify):

    def __init__(self, table, where=None, order_by=None, limit=None):
        self.table = table
        self.where = where
        self.order_by = order_by
        self.limit = limit


@compile.when(Delete)
def compile_delete(compile, expr, state):
    state.sql.append("DELETE FROM ")
    compile(expr.table, state)
    if expr.where:
        state.sql.append(" WHERE ")
        compile(expr.where, state)
    if expr.order_by:
        state.sql.append(" ORDER BY ")
        compile(expr.order_by, state)
    if expr.limit is not None:
        state.sql.append(" LIMIT ")
        compile(expr.limit, state)


@factory.register
class Set(Query):

    def __init__(self, *exprs, **kw):
        super(Set, self).__init__(result=kw.get('result'))
        if 'op' in kw:
            self.sql = kw['op']
        self._all = kw.get('all', False)  # Use All() instead?
        self._exprs = ExprList()
        for expr in exprs:
            self.add(expr)

    def add(self, other):
        if (isinstance(other, self.__class__) and
                other._all == self._all and
                other._limit is None and
                other._offset is None):
            for expr in other._exprs:
                self.add(expr)
            if other._for_update:
                self._for_update = other._for_update
        else:
            self._exprs.append(other)
            # TODO: reset _order_by, _for_update?

    def _op(self, cls, *others):
        if not getattr(self, 'sql', None):
            c = cls(*self._exprs, all=self._all)
            c._limit = self._limit
            c._offset = self._offset
            c._order_by = self._order_by
            c._for_update = self._for_update
        elif self.__class__ is not cls:
            c = cls(self, all=self._all)  # TODO: Should be here "all"?
        else:
            c = self.clone()
        for other in others:
            c.add(other)
        return c

    def union(self, *others):
        return self._op(factory.get(self).Union, *others)

    def intersection(self, *others):
        return self._op(factory.get(self).Intersect, *others)

    def difference(self, *others):
        return self._op(factory.get(self).Except, *others)

    # FIXME: violates the interface contract, changing the semantic of its interface
    __or__ = same('union')
    __and__ = same('intersection')
    __sub__ = same('difference')

    def all(self, all=True):
        self._all = all
        return self

    def clone(self, *attrs):
        c = Query.clone(self, *attrs)
        c._exprs = copy.copy(c._exprs)
        return c


@factory.register
class Union(Set):
    __slots__ = ()
    sql = 'UNION'


@factory.register
class Intersect(Set):
    __slots__ = ()
    sql = 'INTERSECT'


@factory.register
class Except(Set):
    __slots__ = ()
    sql = 'EXCEPT'


@compile.when(Set)
def compile_set(compile, expr, state):
    if expr._all:
        op = ' {0} ALL '.format(expr.sql)
    else:
        op = ' {0} '.format(expr.sql)
    # TODO: add tests for nested sets.
    state.precedence += 0.5  # to correct handle sub-set with limit, offset
    compile(expr._exprs.join(op), state)
    state.precedence -= 0.5
    if expr._order_by:
        state.sql.append(" ORDER BY ")
        compile(expr._order_by, state)
    if expr._limit is not None:
        state.sql.append(" LIMIT ")
        compile(expr._limit, state)
    if expr._offset:
        state.sql.append(" OFFSET ")
        compile(expr._offset, state)
    if expr._for_update:
        state.sql.append(" FOR UPDATE")


class Name(object):

    __slots__ = ('name', )

    def __init__(self, name=None):
        self.name = name

    def __repr__(self):
        return expr_repr(self)


class NameCompiler(object):

    _translation_map = (
        ("\\", "\\\\"),
        ("\000", "\\0"),
        ('\b', '\\b'),
        ('\n', '\\n'),
        ('\r', '\\r'),
        ('\t', '\\t'),
        ("%", "%%")
    )
    _delimiter = '"'
    _escape_delimiter = '"'
    _max_length = 63

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, '_{}'.format(k), v)

    def __call__(self, compile, expr, state):
        state.sql.append(self._delimiter)
        name = expr.name
        name = name.replace(self._delimiter, self._escape_delimiter + self._delimiter)
        for k, v in self._translation_map:
            name = name.replace(k, v)
        if len(name) > self._get_max_length(state):
            raise MaxLengthError("The length of name {0!r} is more than {1}".format(name, self._max_length))
        state.sql.append(name)
        state.sql.append(self._delimiter)

    def _get_max_length(self, state):
        # Max length can depend on context.
        return self._max_length

compile_name = NameCompiler()
compile.when(Name)(compile_name)


class Value(object):

    __slots__ = ('value', )

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return expr_repr(self)


class ValueCompiler(object):

    _translation_map = (
        ("\\", "\\\\"),
        ("\000", "\\0"),
        ('\b', '\\b'),
        ('\n', '\\n'),
        ('\r', '\\r'),
        ('\t', '\\t'),
        ("%", "%%")
    )
    _delimiter = "'"
    _escape_delimiter = "'"

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, '_{}'.format(k), v)

    def __call__(self, compile, expr, state):
        state.sql.append(self._delimiter)
        value = str(expr.value)
        value = value.replace(self._delimiter, self._escape_delimiter + self._delimiter)
        for k, v in self._translation_map:
            value = value.replace(k, v)
        state.sql.append(value)
        state.sql.append(self._delimiter)


compile_value = ValueCompiler()
compile.when(Value)(compile_value)


def qn(name, compile):
    return compile(Name(name))[0]

A, C, E, P, TA, Q, QS = Alias, Condition, Expr, Placeholder, TableAlias, Query, Query
func = const = ConstantSpace()

compile.set_precedence(270, '.')
compile.set_precedence(260, '::')
compile.set_precedence(250, '[', ']')  # array element selection
compile.set_precedence(240, Pos, Neg, (Unary, '+'), (Unary, '-'), '~')  # unary minus
compile.set_precedence(230, '^')
compile.set_precedence(220, Mul, Div, (Binary, '*'), (Binary, '/'), (Binary, '%'))
compile.set_precedence(210, Add, Sub, (Binary, '+'), (Binary, '-'))
compile.set_precedence(200, LShift, RShift, '<<', '>>')
compile.set_precedence(190, '&')
compile.set_precedence(180, '#')
compile.set_precedence(170, '|')
compile.set_precedence(160, Is, 'IS')
compile.set_precedence(150, (Postfix, 'ISNULL'), (Postfix, 'NOTNULL'))
compile.set_precedence(140, '(any other)')  # all other native and user-defined operators
compile.set_precedence(130, In, NotIn, 'IN')
compile.set_precedence(120, Between, 'BETWEEN')
compile.set_precedence(110, 'OVERLAPS')
compile.set_precedence(100, Like, ILike, 'LIKE', 'ILIKE', 'SIMILAR')
compile.set_precedence(90, Lt, Gt, '<', '>')
compile.set_precedence(80, Le, Ge, Ne, '<=', '>=', '<>', '!=')
compile.set_precedence(70, Eq, '=')
compile.set_precedence(60, Not, 'NOT')
compile.set_precedence(50, And, 'AND')
compile.set_precedence(40, Or, 'OR')
compile.set_precedence(30, Set, Union, Intersect, Except)
compile.set_precedence(20, Select, Query, SelectCount, Raw, Insert, Update, Delete)
compile.set_precedence(10, Expr)
compile.set_precedence(None, All, Distinct)


from sqlbuilder.smartsql.datatypes import AbstractType, BaseType
operator_registry.register(OPERATORS.ADD, (BaseType, BaseType), BaseType, Add)
operator_registry.register(OPERATORS.SUB, (BaseType, BaseType), BaseType, Sub)
operator_registry.register(OPERATORS.MUL, (BaseType, BaseType), BaseType, Mul)
operator_registry.register(OPERATORS.DIV, (BaseType, BaseType), BaseType, Div)
operator_registry.register(OPERATORS.GT, (BaseType, BaseType), BaseType, Gt)
operator_registry.register(OPERATORS.LT, (BaseType, BaseType), BaseType, Lt)
operator_registry.register(OPERATORS.GE, (BaseType, BaseType), BaseType, Ge)
operator_registry.register(OPERATORS.LE, (BaseType, BaseType), BaseType, Le)
operator_registry.register(OPERATORS.AND, (BaseType, BaseType), BaseType, And)
operator_registry.register(OPERATORS.OR, (BaseType, BaseType), BaseType, Or)
operator_registry.register(OPERATORS.EQ, (BaseType, BaseType), BaseType, Eq)
operator_registry.register(OPERATORS.NE, (BaseType, BaseType), BaseType, Ne)
operator_registry.register(OPERATORS.IS, (BaseType, BaseType), BaseType, Is)
operator_registry.register(OPERATORS.IS_NOT, (BaseType, BaseType), BaseType, IsNot)
operator_registry.register(OPERATORS.IN, (BaseType, BaseType), BaseType, In)
operator_registry.register(OPERATORS.NOT_IN, (BaseType, BaseType), BaseType, NotIn)
operator_registry.register(OPERATORS.RSHIFT, (BaseType, BaseType), BaseType, RShift)
operator_registry.register(OPERATORS.LSHIFT, (BaseType, BaseType), BaseType, LShift)
operator_registry.register(OPERATORS.LIKE, (BaseType, BaseType), BaseType, Like)
operator_registry.register(OPERATORS.ILIKE, (BaseType, BaseType), BaseType, ILike)
