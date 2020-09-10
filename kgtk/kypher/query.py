"""
Kypher queries over KGTK graphs.
"""

import sys
import os.path
import io
import re
import pprint

import sh

import kgtk.kypher.parser as parser
from   kgtk.kypher.sqlstore import sql_quote_ident
from   kgtk.value.kgtkvalue import KgtkValue

pp = pprint.PrettyPrinter(indent=4)


### TO DO:

# - implement query parameters to more easily pass in string literals
# - more intelligent index creation
# - investigate redundant join clauses
# - header column dealiasing/normalization
# - bump graph timestamps when they get queried
# - allow order-by on column aliases (currently they are undefined variables)
# - (not) exists pattern handling
# - null-value handling and testing
# - handle properties that are ambiguous across graphs
# - graphs fed in from stdin
# - graph naming independent from files, so we don't have to have source data files
#   available after import for querying
# - --create and --remove to instantiate and add/remove edge patterns from result bindings
# - --with clause to compute derived values to use by --create and --remove


### Utilities

def listify(x):
    return (hasattr(x, '__iter__') and not isinstance(x, str) and list(x)) or (x and [x]) or []

def dwim_to_string_para(x):
    """Try to coerce `x' to a KGTK string value that can be passed as a query parameter.
    """
    x = str(x)
    m = KgtkValue.strict_string_re.match(x)
    if m is not None:
        return x
    # if we have an enclosing pair of quotes, remove them:
    if x.startswith('"') and x.endswith('"'):
        x = x[1:-1]
    x = re.sub(r'(?P<char>["\|])', r"\\\g<char>", x)
    return '"%s"' % x

def dwim_to_lqstring_para(x):
    """Try to coerce `x' to a KGTK LQ-string value that can be passed as a query parameter.
    """
    x = str(x)
    m = KgtkValue.strict_language_qualified_string_re.match(x)
    if m is not None:
        return x
    atpos = x.rfind('@')
    if atpos > 0:
        text = x[0:atpos]
        # this allows an empty or invalid language:
        lang = x[atpos+1:]
        # if we have an enclosing pair of quotes, remove them:
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]
        text = re.sub(r"(?P<char>['\|])", r"\\\g<char>", text)
        return "'%s'@%s" % (text, lang)
    raise Exception("cannot coerce `%s' into a language-qualified string" % x)


### Query translation:

# An expression in Kypher can be (`+' means handled fully, `o' partially):
# (from https://neo4j.com/docs/cypher-manual/current/syntax/expressions/)
#
# o A decimal (integer or float) literal: 13, -40000, 3.14, 6.022E23.
#   - HC: 6.022E23 fails in the grammar
# + A hexadecimal integer literal (starting with 0x): 0x13af, 0xFC3A9, -0x66eff
#   - HC: get converted into decimal
# + An octal integer literal (starting with 0): 01372, 02127, -05671.
#   - HC: get converted into decimal
# + A string literal: 'Hello', "World".
# + A boolean literal: true, false, TRUE, FALSE.
#   - HC: get converted into 0/1
# + A variable: n, x, rel, myFancyVariable, `A name with weird stuff in it[]!`.
# + A property: n.prop, x.prop, rel.thisProperty, myFancyVariable.`(weird property name)`
# - A dynamic property: n["prop"], rel[n.city + n.zip], map[coll[0]].
#   - HC: not doable in SQL, amounts to a function or column variable
# - A parameter: $param, $0
# + A list of expressions: ['a', 'b'], [1, 2, 3], ['a', 2, n.property, $param], [ ].
#   - HC: only lists of literals
# + A function call: length(p), nodes(p).
# + An aggregate function: avg(x.prop), count(*).
# - A path-pattern: (a)-->()<--(b).
# + An operator application: 1 + 2 and 3 < 4.
# + A predicate expression is an expression that returns true or false: a.prop = 'Hello', length(p) > 10, exists(a.name).
# - An existential subquery is an expression that returns true or false: EXISTS { MATCH (n)-[r]→(p) WHERE p.name = 'Sven' }.
# + A regular expression: a.name =~ 'Tim.*'
#   - HC: SQLite supports LIKE and GLOB (which both have different regexp syntax),
#     and REGEXP and MATCH through user-defined functions (we support =~ via kgtk_regex)
# - A case-sensitive string matching expression: a.surname STARTS WITH 'Sven', a.surname ENDS WITH 'son' or a.surname CONTAINS 'son'
#   - HC: would need to be implemented via a user-defined function
# - A CASE expression.

# Using properties to restrict on "wide" columns:
#
# Example - unrestricted:
#
#    (a)-[:loves]->(b)
#
# Example - qualified:
#
#    (a {nationality: "Austria"})-[:loves]->(b)
#
# This could mean:
#    {'node1': <Variable a>, 'node1;nationality': "Austria", 'label': "loves", 'node2': <Variable b>}
#
#    (a)-[:loves {graph: "g1"}]->(b)
#
# This could mean:
#    {'node1': <Variable a>, 'label': "loves", 'graph': "g1", 'node2': <Variable b>}
#
# Assumption: if we access something via a property, it will always be accessed via a column,
# not via a normalized edge; if data has mixed representation for some edges, it has to be
# normalized one way or the other first for the query to get all results.  If not, it will
# only pick up the representation used in the query, other edges will be ignored.
#
# For structured literals, we assume their fields are implied/virtual wide columns that aren't
# materialized.  For example:
#
#    (id)-[:P580]->(time {`kgtk:year`: year})
#    where year <= 2010
#
# which would be the same as (if we named the accessors like our column names):
#
#    (id)-[:P580]->(time)
#    where kgtk_year(time) <= 2010


class KgtkQuery(object):

    def __init__(self, files, store, query=None,
                 match='()', where=None, ret='*',
                 order=None, skip=None, limit=None,
                 parameters={}, loglevel=0):
        self.files = [os.path.realpath(f) for f in listify(files)]
        self.store = store
        self.loglevel = loglevel
        self.parameters = parameters
        if query is None:
            # supplying a query through individual clause arguments might be a bit easier,
            # since they can be in any order, can have defaults, are easier to shell-quote, etc.:
            query = ''
            query += match and ' MATCH ' + match or ''
            query += where and ' WHERE ' + where or ''
            query += ret and ' RETURN ' + ret or ''
            query += order and ' ORDER BY ' + order or ''
            query += skip and ' SKIP ' + skip or ''
            query += limit and ' LIMIT ' + limit or ''
        self.query = parser.intern(query)
        self.match_clauses = self.query.get_match_clauses()
        self.where_clause = self.query.get_where_clause()
        self.return_clause = self.query.get_return_clause()
        self.order_clause = self.query.get_order_clause()
        self.skip_clause = self.query.get_skip_clause()
        self.limit_clause = self.query.get_limit_clause()
        self.default_graph = self.files[0]
        # do this after we parsed the query, so we get syntax errors right away:
        for file in self.files:
            store.add_graph(file)
        self.graph_handle_map = {}
        self.result_header = None

    def log(self, level, message):
        if self.loglevel >= level:
            print(message)

    def map_graph_handle_to_file(self, handle):
        """Performes a greedy mapping of `handle' to either a full file name
        or the first file basename that contains `handle' as a substring.
        If handle contains a numeric suffix, we also check its prefix portion.
        For example, handle `g12' is also matched as `g' in the file basename.
        """
        files = self.files
        hmap = self.graph_handle_map
        if handle in hmap:
            return hmap[handle]
        base_handle = handle
        m = re.search('[0-9]+$', handle)
        if m is not None and m.start() > 0:
            base_handle = handle[0:m.start()]
        mapped_files = hmap.values()
        for file in files:
            if file not in mapped_files:
                key = file
                if handle == key:
                    hmap[handle] = file
                    return file
                key = os.path.basename(file)
                if key.find(handle) >= 0 or key.find(base_handle) >= 0:
                    hmap[handle] = file
                    return file
        raise Exception("failed to uniquely map handle `%s' onto one of %s" % (handle, files))

    def get_parameter_value(self, name):
        value = self.parameters.get(name)
        if value is None:
            raise Exception("undefined query parameter: `%s'" % name)
        return value

    def get_pattern_clause_graph(self, clause):
        node1 = clause[0]
        graph = node1.graph
        if graph is not None:
            graph = graph.name
        else:
            graph = self.default_graph
        return self.store.get_file_graph(self.map_graph_handle_to_file(graph))

    # in case we have aliases which could be different in every graph, stubs for now:
    def get_node1_column(self, graph):
        return 'node1'
    def get_node2_column(self, graph):
        return 'node2'
    def get_label_column(self, graph):
        return 'label'
    def get_id_column(self, graph):
        return 'id'

    def get_literal_parameter(self, literal, litmap):
        """Return a parameter placeholder such as `?12?' that will be mapped to `literal'
        and will later be replaced with a query parameter at the appropriate position.
        """
        if literal in litmap:
            return litmap[literal]
        else:
            placeholder = '???%d??' % len(litmap)
            litmap[literal] = placeholder
            return placeholder

    def replace_literal_parameters(self, raw_query, litmap):
        """Replace the named literal placeholders in `raw_query' with positional
        parameters and build a list of actual parameters to substitute for them.
        """
        query = io.StringIO()
        parameters = []
        # reverse `litmap' to map placeholders onto literal values:
        litmap = {p: l for l, p in litmap.items()}
        for token in re.split('\\?\\?', raw_query):
            if token.startswith('?'):
                parameters.append(litmap['??' + token + '??'])
                token = '?'
            query.write(token)
        return query.getvalue(), parameters
                 
    def register_clause_variable(self, query_var, sql_var, varmap, joins):
        """Register a reference to the Kypher variable `query_var' which corresponds to the
        SQL clause variable `sql_var' represented as `(graph, column)' where `graph' is a
        table alias for the relevant graph specific to the current clause.  If this is the
        first reference to `query_var', simply add it to `varmap'.  Otherwise, find the best
        existing reference to equiv-join it with and record the necessary join in `joins'.
        """
        sql_vars = varmap.get(query_var)
        if sql_vars is None:
            varmap[query_var] = set([sql_var])
        else:
            this_graph, this_col = sql_var
            best_var = None
            # TO DO: further optimizations are possible here, for example, we might want to prefer
            # a self-join on the same column, since it might reduce the number of indexes needed:
            for equiv_var in sql_vars:
                equiv_graph, equiv_col = equiv_var
                if best_var is None:
                    best_var = equiv_var
                elif this_graph == equiv_graph:
                    # we match on graph and clause, since clause is encoded in graph:
                    best_var = equiv_var
                    break
                else:
                    best_var = equiv_var
            # not sure if they could ever be equal, but just in case:
            if sql_var != best_var:
                varmap[query_var].add(sql_var)
                equiv = [best_var, sql_var]
                equiv.sort()
                joins.add(tuple(equiv))
        
    def pattern_clause_to_sql(self, clause, graph, litmap, varmap, restrictions, joins):
        node1 = clause[0]
        rel = clause[1]
        node2 = clause[2]
        
        node1col = self.get_node1_column(graph)
        if node1.labels is not None:
            para = self.get_literal_parameter(node1.labels[0], litmap)
            restrictions.add(((graph, node1col), para))
        if node1.variable is not None and not isinstance(node1.variable, parser.AnonymousVariable):
            self.register_clause_variable(node1.variable.name, (graph, node1col), varmap, joins)

        node2col = self.get_node2_column(graph)
        if node2.labels is not None:
            para = self.get_literal_parameter(node2.labels[0], litmap)
            restrictions.add(((graph, node2col), para))
        if node2.variable is not None and not isinstance(node2.variable, parser.AnonymousVariable):
            self.register_clause_variable(node2.variable.name, (graph, node2col), varmap, joins)
            
        labelcol = self.get_label_column(graph)
        idcol = self.get_id_column(graph)
        if rel.labels is not None:
            para = self.get_literal_parameter(rel.labels[0], litmap)
            restrictions.add(((graph, labelcol), para))
        if rel.variable is not None and not isinstance(rel.variable, parser.AnonymousVariable):
            self.register_clause_variable(rel.variable.name, (graph, idcol), varmap, joins)

    def pattern_props_to_sql(self, pattern, graph, column, litmap, varmap, restrictions, joins):
        # `pattern' is a node or relationship pattern for `graph.column'.  `column' should be 'node1', `node2' or `id'.
        props = getattr(pattern, 'properties', None)
        if props is None or len(props) == 0:
            return
        # if we need to access a property, we need to register anonymous variables as well:
        self.register_clause_variable(pattern.variable.name, (graph, column), varmap, joins)
        for prop, expr in props.items():
            # TO DO: figure out how to better abstract property to column mapping (also see below):
            propcol = isinstance(pattern, parser.RelationshipPattern) and prop  or  column + ';' + prop
            # TRICKY/TO DO: if the property value is a standalone variable, we register it as a free
            # variable before evaluating it, since properties can be ambiguous across different graphs
            # and only within a clause do we know which graph is actually meant.  Think about this
            # some more, this issue comes up in the time-machine use case:
            if isinstance(expr, parser.Variable):
                self.register_clause_variable(expr.name, (graph, propcol), varmap, joins)
            expr = self.expression_to_sql(expr, litmap, varmap)
            restrictions.add(((graph, propcol), expr))

    def pattern_clause_props_to_sql(self, clause, graph, litmap, varmap, restrictions, joins):
        node1 = clause[0]
        node1col = self.get_node1_column(graph)
        self.pattern_props_to_sql(node1, graph, node1col, litmap, varmap, restrictions, joins)
        node2 = clause[2]
        node2col = self.get_node2_column(graph)
        self.pattern_props_to_sql(node2, graph, node2col, litmap, varmap, restrictions, joins)
        rel = clause[1]
        idcol = self.get_id_column(graph)
        self.pattern_props_to_sql(rel, graph, idcol, litmap, varmap, restrictions, joins)

    OPERATOR_TABLE = {
        parser.Add: '+', parser.Sub: '-', parser.Multi: '*', parser.Div: '/',
        parser.Eq: '=', parser.Neq: '!=', parser.Lt: '<', parser.Gt: '>',
        parser.Lte: '<=', parser.Gte: '>=',
        parser.Not: 'NOT', parser.And: 'AND', parser.Or: 'OR',
    }

    def is_kgtk_operator(self, op):
        """Return True if `op' is a special KGTK function or virtual property.
        """
        return str(op).upper().startswith('KGTK_')

    def expression_to_sql(self, expr, litmap, varmap):
        """Translate a Kypher expression `expr' into its SQL equivalent.
        """
        expr_type = type(expr)
        if expr_type == parser.Literal:
            return self.get_literal_parameter(expr.value, litmap)
        elif expr_type == parser.Parameter:
            value = self.get_parameter_value(expr.name)
            return self.get_literal_parameter(value, litmap)
        
        elif expr_type == parser.Variable:
            query_var = expr.name
            if varmap is None:
                # for cases where external variables are not allowed (e.g. LIMIT):
                raise Exception('Illegal context for variable: %s' % query_var)
            if query_var == '*':
                return query_var
            sql_vars = varmap.get(query_var)
            if sql_vars is None:
                raise Exception('Undefined variable: %s' % query_var)
            graph, col = list(sql_vars)[0]
            return '%s.%s' % (graph, sql_quote_ident(col))
        
        elif expr_type == parser.List:
            # we only allow literals in lists, Cypher also supports variables:
            elements = [self.expression_to_sql(elt, litmap, None) for elt in expr.elements]
            return '(' + ', '.join(elements) + ')'
        
        elif expr_type == parser.Minus:
            arg = self.expression_to_sql(expr.arg, litmap, varmap)
            return '(- %s)' % arg
        elif expr_type in (parser.Add, parser.Sub, parser.Multi, parser.Div):
            arg1 = self.expression_to_sql(expr.arg1, litmap, varmap)
            arg2 = self.expression_to_sql(expr.arg2, litmap, varmap)
            op = self.OPERATOR_TABLE[expr_type]
            return '(%s %s %s)' % (arg1, op, arg2)
        elif expr_type == parser.Hat:
            raise Exception("Unsupported operator: `^'")
        
        elif expr_type in (parser.Eq, parser.Neq, parser.Lt, parser.Gt, parser.Lte, parser.Gte):
            arg1 = self.expression_to_sql(expr.arg1, litmap, varmap)
            arg2 = self.expression_to_sql(expr.arg2, litmap, varmap)
            op = self.OPERATOR_TABLE[expr_type]
            return '(%s %s %s)' % (arg1, op, arg2)
        elif expr_type == parser.Not:
            arg = self.expression_to_sql(expr.arg, litmap, varmap)
            return '(NOT %s)' % arg
        elif expr_type in (parser.And, parser.Or):
            arg1 = self.expression_to_sql(expr.arg1, litmap, varmap)
            arg2 = self.expression_to_sql(expr.arg2, litmap, varmap)
            op = self.OPERATOR_TABLE[expr_type]
            return '(%s %s %s)' % (arg1, op, arg2)
        elif expr_type == parser.Xor:
            raise Exception("Unsupported operator: `XOR'")
        elif expr_type == parser.Case:
            # TO DO: implement, has the same syntax as SQL:
            raise Exception("Unsupported operator: `CASE'")
        
        elif expr_type == parser.Call:
            function = expr.function
            if function.upper() == 'CAST':
                # special-case SQLite CAST which isn't directly supported by Cypher:
                if len(expr.args) == 2 and isinstance(expr.args[1], parser.Variable):
                    arg = self.expression_to_sql(expr.args[0], litmap, varmap)
                    typ = expr.args[1].name
                    return 'CAST(%s AS %s)' % (arg, typ)
                else:
                    raise Exception("Illegal CAST expression")
            args = [self.expression_to_sql(arg, litmap, varmap) for arg in expr.args]
            distinct = expr.distinct and 'DISTINCT ' or ''
            self.store.load_user_function(function, error=False)
            return function + '(' + distinct + ', '.join(args) + ')'
        
        elif expr_type == parser.Expression2:
            arg1 = expr.arg1
            arg2 = expr.arg2
            if isinstance(arg1, parser.Variable):
                var = self.expression_to_sql(arg1, litmap, varmap)
                for proplook in arg2:
                    if not isinstance(proplook, parser.PropertyLookup):
                        var = None; break
                    prop = proplook.property
                    if self.is_kgtk_operator(prop) and self.store.is_user_function(prop):
                        self.store.load_user_function(prop)
                        var = prop + '(' + var + ')'
                    # TO DO: figure out how to better abstract property to column mapping:
                    elif var.upper().endswith('."ID"'):
                        # we are referring to the relation ID, subsitute it with the prop column:
                        var = var[:-3] + prop + '"'
                    else:
                        # we must be referring to a node-path column such as node1;name or node2;creator:
                        # TO DO: check existance of column here instead of waiting for SQLite to error
                        var = var[:-1] + ';' + prop + '"'
                else:
                    return var
            raise Exception("Unhandled property lookup expression: " + str(expr))
        
        elif expr_type == parser.Expression3:
            arg1 = self.expression_to_sql(expr.arg1, litmap, varmap)
            arg2 = self.expression_to_sql(expr.arg2, litmap, varmap)
            op = expr.operator.upper()
            if op in ('IN'):
                return '(%s %s %s)' % (arg1, op, arg2)
            elif op in ('REGEX'):
                self.store.load_user_function('KGTK_REGEX')
                return 'KGTK_REGEX(%s, %s)' % (arg1, arg2)
            else:
                raise Exception('Unhandled operator: %s' % str(op))
        else:
            raise Exception('Unhandled expression type: %s' % str(parser.object_to_tree(expr)))

    def where_clause_to_sql(self, where_clause, litmap, varmap):
        if where_clause is None:
            return ''
        else:
            return self.expression_to_sql(where_clause.expression, litmap, varmap)

    def return_clause_to_sql_selection(self, clause, litmap, varmap):
        select = clause.distinct and 'DISTINCT ' or ''
        first = True
        # Cypher does not have a 'GROUP BY' clause but instead uses non-aggregate return columns
        # that precede an aggregate function as grouping keys, so we have to keep track of those:
        agg_info = []
        for item in clause.items:
            expr = self.expression_to_sql(item.expression, litmap, varmap)
            select += first and expr or (', ' + expr)
            first = False
            # check if this item calls an aggregation function or not: if it does then preceding columns
            # that aren't aggregates are used for grouping, if it doesn't this column might be used for grouping:
            is_agg = parser.has_element(
                item.expression, lambda x: isinstance(x, parser.Call) and self.store.is_aggregate_function(x.function))
            if item.name is not None:
                select += ' ' + sql_quote_ident(item.name)
                agg_info.append(not is_agg and item.name or None)
            else:
                agg_info.append(not is_agg and expr or None)
                
        # we only need to group if there is at least one aggregate column and
        # at least one regular column before one of the aggregate columns:
        first_reg = len(agg_info)
        last_agg = -1
        for col, aggi in enumerate(agg_info):
            if aggi is not None:
                first_reg = min(col, first_reg)
            else:
                last_agg = max(col, last_agg)
        if last_agg > first_reg:
            group_by = [col for col in agg_info[0:last_agg] if col is not None]
            group_by = 'GROUP BY ' + ', '.join(group_by)
        else:
            group_by = None
        return select, group_by

    def order_clause_to_sql(self, order_clause, litmap, varmap):
        if order_clause is None:
            return None
        items = []
        for sort_item in order_clause.items:
            expr = self.expression_to_sql(sort_item.expression, litmap, varmap)
            direction = sort_item.direction.upper()
            items.append(expr + (direction.startswith('ASC') and '' or (' ' + direction)))
        return 'ORDER BY ' + ', '.join(items)
    
    def limit_clauses_to_sql(self, skip_clause, limit_clause, litmap, varmap):
        if skip_clause is None and limit_clause is None:
            return None
        limit = 'LIMIT'
        if limit_clause is not None:
            limit += ' ' + self.expression_to_sql(limit_clause.expression, litmap, None)
        else:
            limit += ' -1'
        if skip_clause is not None:
            limit += ' OFFSET ' + self.expression_to_sql(skip_clause.expression, litmap, None)
        return limit

    def translate_to_sql(self):
        graphs = set()        # the set of graph table names referenced by this query
        litmap = {}           # maps Kypher literals onto parameter placeholders
        varmap = {}           # maps Kypher variables onto representative (graph, col) SQL columns
        restrictions = set()  # maps (graph, col) SQL columns onto literal restrictions
        joins = set()         # maps equivalent SQL column pairs (avoiding dupes and redundant flips)
        parameters = None     # maps ? parameters in sequence onto actual query parameters
        
        # translate clause top-level info:
        for i, clause in enumerate(self.match_clauses):
            graph = self.get_pattern_clause_graph(clause)
            graph_alias = '%s_c%d' % (graph, i+1) # per-clause graph table alias for self-joins
            graphs.add((graph, graph_alias))
            self.pattern_clause_to_sql(clause, graph_alias, litmap, varmap, restrictions, joins)
            
        # translate properties:
        for i, clause in enumerate(self.match_clauses):
            graph = self.get_pattern_clause_graph(clause)
            graph_alias = '%s_c%d' % (graph, i+1) # per-clause graph table alias for self-joins
            self.pattern_clause_props_to_sql(clause, graph_alias, litmap, varmap, restrictions, joins)
            
        select, group_by = self.return_clause_to_sql_selection(self.return_clause, litmap, varmap)
        graph_tables = ', '.join([g + ' ' + a for g, a in sorted(list(graphs))])
        query = io.StringIO()
        query.write('SELECT %s\nFROM %s' % (select, graph_tables))
        
        if len(restrictions) > 0 or len(joins) > 0 or self.where_clause is not None:
            query.write('\nWHERE TRUE')
        for (g, c), val in sorted(list(restrictions)):
            query.write('\nAND %s.%s=%s' % (g, sql_quote_ident(c), val))
        alias_to_graph = {alias: graph for graph, alias in graphs}
        for (g1, c1), (g2, c2) in sorted(list(joins)):
            query.write('\nAND %s.%s=%s.%s' % (g1, sql_quote_ident(c1), g2, sql_quote_ident(c2)))

        # ensure that we have relevant indices:
        # TO DO: think about this some more, we might need some manual control as well; this should
        # go into its separate function; to do this right we need some approximate analysis of the
        # query, e.g., for the join we'll generally only need an index on one of the involved tables:
        if len(joins) > 0:
            for (g1, c1), (g2, c2) in joins:
                # ensure we have indices on joined columns - the ID check needs to be generalized:
                self.store.ensure_graph_index(alias_to_graph[g1], c1, unique=c1.upper()=='ID')
                self.store.ensure_graph_index(alias_to_graph[g2], c2, unique=c2.upper()=='ID')
        elif len(restrictions) > 0:
            # if we don't have any joins, we might need indexes on restricted columns:
            for (g, c), val in restrictions:
                self.store.ensure_graph_index(alias_to_graph[g], c, unique=c.upper()=='ID')
            
        where = self.where_clause_to_sql(self.where_clause, litmap, varmap)
        where and query.write('\nAND ' + where)
        group_by and query.write('\n' + group_by)
        order = self.order_clause_to_sql(self.order_clause, litmap, varmap)
        order and query.write('\n' + order)
        limit = self.limit_clauses_to_sql(self.skip_clause, self.limit_clause, litmap, varmap)
        limit and query.write('\n' + limit)
        query = query.getvalue().replace(' TRUE\nAND', '')
        query, parameters = self.replace_literal_parameters(query, litmap)
        
        self.log(1, '\nSQL: %s\nPARAS: %s\n' % (query.replace('\n', '\n     '), parameters))
        return query, parameters

    def execute(self):
        query, params = self.translate_to_sql()
        result = self.store.execute(query, params)
        self.result_header = [c[0] for c in result.description]
        return result


"""
>>> store = cq.SqliteStore('/tmp/graphstore.sqlite3.db', create=True)
>>> graph = '/home/hans/Documents/kgtk/code/kgtk/kgtk/kypher/.work/data/graph.tsv'

>>> query = cq.KgtkQuery(graph, store, match='(a)-[:loves]->(b)')
>>> list(query.execute())
[('Hans', 'loves', 'Molly', 'e11'), ('Otto', 'loves', 'Susi', 'e12'), ('Joe', 'loves', 'Joe', 'e14')]

>>> query = cq.KgtkQuery(graph, store, match='(a)-[:loves]->(b)-[:loves]->(a)')
>>> list(query.execute())
[('Joe', 'loves', 'Joe', 'e14', 'Joe', 'loves', 'Joe', 'e14')]

>>> query = cq.KgtkQuery(graph, store, match='(a)-[:loves]->(a)-[:loves]->(a)')
>>> list(query.execute())
[('Joe', 'loves', 'Joe', 'e14', 'Joe', 'loves', 'Joe', 'e14')]

>>> query = cq.KgtkQuery(graph, store, loglevel=1,
                         match='g: (a)-[:loves]->(a), (a)-[r2:name]->(n)')
>>> list(query.execute())
SQL: SELECT *
     FROM graph_1 graph_1_c1, graph_1 graph_1_c2
     WHERE graph_1_c1."label"=?
     AND graph_1_c2."label"=?
     AND graph_1_c1."node1"=graph_1_c1."node2"
     AND graph_1_c1."node1"=graph_1_c2."node1"
PARAS: ['loves', 'name']
[('Joe', 'loves', 'Joe', 'e14', 'Joe', 'name', '"Joe"', 'e23')]
>>> 

# return clause translation:

>>> query = cq.KgtkQuery(graph, store, loglevel=1,
                         match='g: (a)-[:loves]->(a), (a)-[r2:name]->(n)', 
                         ret="distinct a as node1, 'loves' as label, n as node2, r2 as id")

>>> cp.pp.pprint(query.return_clause.to_tree())
(   'Return',
    {   'distinct': False,
        'items': [   (   'ReturnItem',
                         {   'expression': ('Variable', {'name': 'a'}),
                             'name': 'node1'}),
                     (   'ReturnItem',
                         {   'expression': (   'Expression2',
                                               {   'arg1': (   'Variable',
                                                               {'name': 'r2'}),
                                                   'arg2': [   (   'PropertyLookup',
                                                                   {   'property': 'label'})]}),
                             'name': 'label'}),
                     (   'ReturnItem',
                         {   'expression': ('Variable', {'name': 'n'}),
                             'name': 'node2'}),
                     (   'ReturnItem',
                         {   'expression': ('Variable', {'name': 'r2'}),
                             'name': 'id'})]})

>>> list(query.execute())
SQL: SELECT DISTINCT graph_1_c2."node1" "node1", ? "label", graph_1_c2."node2" "node2", graph_1_c2."id" "id"
     FROM graph_1 graph_1_c1, graph_1 graph_1_c2
     WHERE graph_1_c1."label"=?
     AND graph_1_c2."label"=?
     AND graph_1_c1."node1"=graph_1_c1."node2"
     AND graph_1_c1."node1"=graph_1_c2."node1"
PARAS: ['loves', 'loves', 'name']
[('Joe', 'loves', '"Joe"', 'e23')]
>>> query.result_header
['node1', 'label', 'node2', 'id']
"""
