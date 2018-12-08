'''This module generates `Rewrite` impls for each AST node type.

By default, the generated impls simply walk recursively over their two input
ASTs, checking for differences (in the `rewrite_recycled` case) but otherwise
having no side effects.  Rewriting is accomplished by marking some AST node
types as "splice points" with the `#[rewrite_splice]`, and then implementing
the `Splice` trait for those types.  The generated impls will call `Splice`
methods to perform the actual rewriting when needed (for `rewrite_recycled`,
when a difference is detected between ASTs; for `rewrite_fresh`, when a node is
found to originate in the old code).

# Trait control attributes

For each of the traits listed below:

- `#[rewrite_gen=T1,T2,T3]` on a node type triggers generation of impls for the
  named traits.

- `#[rewrite_custom=T1,T2,T3]` suppresses impl generation, but allows other
  code generators to assume that handwritten impls will be provided.

- `#[rewrite_skip=T1,T2,T3]` suppresses impl generation and does not assume
  that handwritten ones exist.

The traits are:

- `Rewrite`: generated by default for all types.  Has no specific dependencies,
  but the default list of rewrite strategies to try is based on the presence of
  other traits (see below).

- `SeqItem`: generated only where requested.  The generated impl is suitable
  for types with `id: NodeId` fields only.  Requires that `Splice`,
  `PrintParse`, `RecoverChildren`, and `Rewrite` impls also be present.

- `MaybeRewriteSeq`: generated by default for all types.  Behavior changes if a
  `SeqItem` impl is available.

- `Recursive`: generated by default for all struct and enum types.  Has no
  dependencies that can be checked here, but each non-`#[rewrite=ignore]` field
  must implement `Rewrite`, otherwise the generated code will fail to
  typecheck.

- `PrintParse`: never generated - handwritten impls only.

- `Splice`: never generated - handwritten impls only.

- `Recover`: never generated - handwritten impls only.

- `RecoverChildren`: generated by default for all types.  Behavior changes if
  `Recover` is available on the node type.

The generated `Rewrite` impl will by default try either `equal` or `recursive`
(depending on type kind), then `print`.  If one of these strategies is
unavailabe (due to missing impls), it will be skipped.  If no strategies are
available, code generation raises an error.

For the three built-in strategies:

- `equal` requires no traits.

- `recursive` requires `Recursive`.

- `print` requires `PrintParse`, `RecoverChildren`, and `Splice`.

Built-in strategy selection can be altered using `#[rewrite_strategies]` or
`#[rewrite_extra_strategies]` (see below).


# Other trait-related attributes

- `#[rewrite_strategies=s1,s2,s3]`: When generating a `Rewrite` impl for this
  node type, run the named strategies in order.  This completely overrides the
  automatic strategy selection normally performed by the `Rewrite` generator.

- `#[rewrite_extra_strategies=s1,s2,s3]`: When generating a `Rewrite` impl for
  this node type, add the named strategies to the list of strategies to try.
  The strategies will be inserted after `equal`/`recursive` and before `print`.

- `#[rewrite_print]`: Alias for `#[rewrite_custom=PrintParse,Splice]`.  These
  are the traits necessary to enable use of the `print` strategy.

- `#[rewrite_print_recover]`: Alias for `#[rewrite_print]` plus
  `#[rewrite_custom=Recover]`.

- `#[rewrite_seq_item]`: Alias for `#[rewrite_gen=SeqItem]`.

- `#[rewrite_ignore]`: On a type, ignore nodes of this type; on a field, ignore
  the contents of this field.  Perform no side effects and always return
  success, in all impls.


# Sequence rewriting attributes

These affect the behavior of generated `Recursive` impls.

- `#[seq_rewrite]`: On a field, use sequence rewriting to handle changes in
  this field.  This only makes sense on fields of type `&[T]` (or equivalent),
  where `T` has a `SeqItem` impl.

- `#[seq_rewrite_outer_span=expr]`: On a field, when invoking sequence
  rewriting for this field, use the provided expression to compute the "outer
  span" for the sequence.  The outer span should cover the entire sequence; if
  the sequence is empty, it should be an empty span at the location where new
  items should be inserted.  The outer span is used to handle insertions at the
  beginning or end of a sequence and to handle insertion into a
  previously-empty sequence.

  In `expr`, `self.foo` is replaced with the local variable containing the
  value of field `foo`, and otherwise the expression is pasted into the output
  code unchanged.

  Implies `#[seq_rewrite]`.


# Expression precedence attributes

These affect the behavior of generated `Recursive` and `RecoverChildren` impls.

- `#[prec_contains_expr]`: When entering a child of this node type, by default
  set expr precedence to `RESET` (don't parenthesize).  The expr precedence can
  be overridden using other `prec` attributes on specific fields.

- `#[prec=name]`: When entering this child node, set expr precedence to
  `PREC_[name]` (if `name` is all-caps) or to the precedence of
  `AssocOp::[name]` (otherwise).

- `#[prec_inc=name]`: Like `#[prec=name]`, but add 1 to the precedence value
  so that the subexpr will be parenthesized if it has the same type as the
  current expr.

- `#[prec_first=name]`: On a field containing a sequence, use a different
  precedence value for the first element of the sequence.  `name` is parsed the
  same as in `#[prec=name]`.

- `#[prec_left_of_binop=op]`, `#[prec_right_of_binop=op]`: Use the appropriate
  precedence for the left/right operand of a binary operator. This should be a
  `rewrite::ExprPrec` variant. The argument `op` should be the name of the
  field containing the binop.

- `#[prec_special=kind]`: Apply special parenthesization rules, in addition to
  normal precedence.  `kind` should be the name of a `rewrite::ExprPrec`
  variant: `Cond` for exprs in conditional-like positions, or `Callee` for
  exprs in function-call callee positions.
'''

from datetime import datetime
import re
from textwrap import indent, dedent

from ast import *
from util import *


def prec_name_to_expr(name, inc):
    inc_str = '' if not inc else ' + 1'
    if name.isupper():
        # If all letters are uppercase, it's a precedence constant from
        # syntax::util::parser
        return 'parser::PREC_%s%s' % (name, inc_str)
    else:
        # If some letters are lowercase, it's an AssocOp variant name.
        return 'parser::AssocOp::%s.precedence() as i8%s' % (name, inc_str)

def field_prec_expr(f, first, suffix='1'):
    # First, figure out the "normal" precedence expression.
    prec_val = 'parser::PREC_RESET'

    prec = f.attrs.get('prec')
    if prec:
        prec_val = prec_name_to_expr(prec, False)

    prec_inc = f.attrs.get('prec_inc')
    if prec_inc:
        prec_val = prec_name_to_expr(prec_inc, True)

    left_of = f.attrs.get('prec_left_of_binop')
    if left_of:
        # Refer to `op1` instead of `op`, to get the binop as it appear in the
        # new AST.
        return 'binop_left_prec(%s)' % (left_of + suffix)

    right_of = f.attrs.get('prec_right_of_binop')
    if right_of:
        return 'binop_right_prec(%s)' % (right_of + suffix)

    prec_first = f.attrs.get('prec_first')
    if first and prec_first:
        prec_val = prec_name_to_expr(prec_first, False)

    # Now apply `prec_special`, if present
    ctor = f.attrs.get('prec_special', 'Normal')
    return 'ExprPrec::%s(%s)' % (ctor, prec_val)

SELF_FIELD_RE = re.compile(r'\bself.([a-zA-Z0-9_]+)\b')
def rewrite_field_expr(expr, fmt):
    def repl(m):
        # If `self.foo` has type `T`, then the local variable `foo1` has type
        # `&T`.  We add a deref to correct for this.
        var_name = fmt % m.group(1)
        return '(*%s)' % var_name
    return SELF_FIELD_RE.sub(repl, expr)

DEFAULT_GEN_TRAITS = {'Rewrite', 'MaybeRewriteSeq', 'RecoverChildren'}
DEFAULT_STRUCT_ENUM_GEN_TRAITS = {'Recursive'}

def type_has_impl(d, trait):
    skip = d.attrs.get('rewrite_skip')
    if skip is not None and trait in skip.split(','):
        return False

    gen = d.attrs.get('rewrite_gen')
    if gen is not None and trait in gen.split(','):
        return True

    custom = d.attrs.get('rewrite_custom')
    if custom is not None and trait in custom.split(','):
        return True

    if 'rewrite_print' in d.attrs and trait in ('PrintParse', 'Splice'):
        return True

    if 'rewrite_print_recover' in d.attrs and trait in ('PrintParse', 'Splice', 'Recover'):
        return True

    if 'rewrite_seq_item' in d.attrs and trait == 'SeqItem':
        return True

    if trait in DEFAULT_GEN_TRAITS:
        return True

    if isinstance(d, (Struct, Enum)) and trait in DEFAULT_STRUCT_ENUM_GEN_TRAITS:
        return True

    return False

def type_needs_generated_impl(d, trait):
    skip = d.attrs.get('rewrite_skip')
    if skip is not None and trait in skip.split(','):
        return False

    gen = d.attrs.get('rewrite_gen')
    if gen is not None and trait in gen.split(','):
        return True

    if 'rewrite_seq_item' in d.attrs and trait == 'SeqItem':
        return True

    if trait in DEFAULT_GEN_TRAITS:
        return True

    if isinstance(d, (Struct, Enum)) and trait in DEFAULT_STRUCT_ENUM_GEN_TRAITS:
        return True

    return False

def get_rewrite_strategies(d):
    strats_str = d.attrs.get('rewrite_strategies')
    if strats_str is not None:
        return strats_str.split(',')

    strats = []

    if isinstance(d, Flag):
        strats.append('equal')
    else:
        if type_has_impl(d, 'Recursive'):
            strats.append('recursive')

    extra_strats = d.attrs.get('rewrite_extra_strategies')
    if extra_strats is not None:
        strats.extend(extra_strats.split(','))

    if all(type_has_impl(d, t) for t in ('PrintParse', 'RecoverChildren', 'Splice')):
        strats.append('print')

    return strats


@linewise
def do_rewrite_impl(d):
    if 'rewrite_ignore' in d.attrs:
        yield '#[allow(unused)]'
        yield 'impl Rewrite for %s {' % d.name
        yield '  fn rewrite(old: &Self, new: &Self, mut rcx: RewriteCtxtRef) -> bool {'
        yield '    // Rewrite mode: ignore'
        yield '    true'
        yield '  }'
        yield '}'
        return

    yield '#[allow(unused)]'
    yield 'impl Rewrite for %s {' % d.name
    yield '  fn rewrite(old: &Self, new: &Self, mut rcx: RewriteCtxtRef) -> bool {'
    if has_field(d, 'id'):
        yield '    trace!("{:?}: rewrite: begin (%s)", new.id);' % d.name
    for strat in get_rewrite_strategies(d):
        yield '    let mark = rcx.mark();'
        if has_field(d, 'id'):
            yield '    trace!("{:?}: rewrite: try %s", new.id);' % strat
        yield '    let ok = strategy::%s::rewrite(old, new, rcx.borrow());' % strat
        yield '    if ok {'
        if has_field(d, 'id'):
            yield '      trace!("{:?}: rewrite: %s succeeded", new.id);' % strat
        yield '      return true;'
        yield '    } else {'
        if has_field(d, 'id'):
            yield '      trace!("{:?}: rewrite: %s FAILED", new.id);' % strat
        yield '      rcx.rewind(mark);'
        yield '    }'
        yield ''
    if has_field(d, 'id'):
        yield '    trace!("{:?}: rewrite: ran out of strategies!", new.id);'
    yield '    false'
    yield '  }'
    yield '}'

@linewise
def generate_rewrite_impls(decls):
    yield '// AUTOMATICALLY GENERATED - DO NOT EDIT'
    yield '// Produced %s by process_ast.py' % (datetime.now(),)
    yield ''

    for d in decls:
        if type_needs_generated_impl(d, 'Rewrite'):
            yield do_rewrite_impl(d)


@linewise
def do_recursive_body(se, target1, target2):
    contains_expr = 'prec_contains_expr' in se.attrs

    yield 'match (%s, %s) {' % (target1, target2)
    for v, path in variants_paths(se):
        yield '  (&%s,' % struct_pattern(v, path, '1')
        yield '   &%s) => {' % struct_pattern(v, path, '2')

        for f in v.fields:
            if 'rewrite_ignore' in f.attrs:
                continue

            # Figure out what function to call to rewrite this field
            seq_rewrite_mode = f.attrs.get('seq_rewrite')
            if seq_rewrite_mode is None and 'seq_rewrite_outer_span' in f.attrs:
                seq_rewrite_mode = ''   # enabled, default mode

            if seq_rewrite_mode is None:
                mk_rewrite = lambda old, new: \
                        'Rewrite::rewrite({old}, {new}, rcx.borrow())'.format(
                                old=old, new=new)
            else:
                outer_span_expr = f.attrs.get('seq_rewrite_outer_span')
                if outer_span_expr is not None:
                    # Replace `self.foo` with `foo2`, since we want the *old*
                    # outer span.
                    outer_span_expr = rewrite_field_expr(outer_span_expr, '%s2')
                else:
                    outer_span_expr = 'DUMMY_SP'
                mk_rewrite = lambda old, new: \
                        'rewrite_seq({old}, {new}, {outer}, rcx.borrow())'.format(
                                old=old, new=new, outer=outer_span_expr)


            # Generate the code for the recursive call, including expr
            # precedence bookkeeping.
            yield '    ({'

            if 'prec_first' in f.attrs:
                yield '      let old = rcx.replace_expr_prec(%s);' % \
                        field_prec_expr(f, True)
                yield '      let ok = Rewrite::rewrite(&%s1[0], &%s2[0], ' \
                        'rcx.borrow());' % (f.name, f.name)
                yield '      rcx.replace_expr_prec(%s);' % field_prec_expr(f, False)
                rewrite_expr = mk_rewrite('&%s1[1..]' % f.name, '&%s2[1..]' % f.name)
                yield '      let ok = ok && %s;' % rewrite_expr
                yield '      rcx.replace_expr_prec(old);'
                yield '      ok'
            else:
                if contains_expr:
                    yield '      let old = rcx.replace_expr_prec(%s);' % \
                            field_prec_expr(f, False)
                rewrite_expr = mk_rewrite('%s1' % f.name, '%s2' % f.name)
                yield '      let ok = %s;' % rewrite_expr
                if contains_expr:
                    yield '      rcx.replace_expr_prec(old);'
                yield '      ok'

            yield '    }) &&'

        yield '    true'
        yield '  }'
    yield '  (_, _) => false,'
    yield '}'

@linewise
def do_recursive_impl(d):
    if 'rewrite_ignore' in d.attrs:
        yield '#[allow(unused)]'
        yield 'impl Recursive for %s {' % d.name
        yield '  fn recursive(old: &Self, new: &Self, mut rcx: RewriteCtxtRef) -> bool {'
        yield '    true'
        yield '  }'
        yield '}'

    yield '#[allow(unused)]'
    yield 'impl Recursive for %s {' % d.name
    yield '  fn recursive(old: &Self, new: &Self, mut rcx: RewriteCtxtRef) -> bool {'
    yield indent(do_recursive_body(d, 'old', 'new'), '    ')
    yield '  }'
    yield '}'

@linewise
def generate_recursive_impls(decls):
    yield '// AUTOMATICALLY GENERATED - DO NOT EDIT'
    yield '// Produced %s by process_ast.py' % (datetime.now(),)
    yield ''

    for d in decls:
        if type_needs_generated_impl(d, 'Recursive'):
            yield do_recursive_impl(d)


@linewise
def do_recover_children_match(d):
    if not isinstance(d, (Struct, Enum)) or 'rewrite_ignore' in d.attrs:
        return

    contains_expr = 'prec_contains_expr' in d.attrs

    yield 'match (reparsed, new) {'
    for v, path in variants_paths(d):
        yield '  (&%s,' % struct_pattern(v, path, '_r')
        yield '   &%s) => {' % struct_pattern(v, path, '_n')
        for f in v.fields:
            if 'rewrite_ignore' in f.attrs:
                continue

            if 'prec_first' in f.attrs:
                yield '    let old = rcx.replace_expr_prec(%s);' % \
                        field_prec_expr(f, True, suffix='_n')
                yield '    RecoverChildren::recover_node_and_children(' \
                        '&%s_r[0], &%s_n[0], rcx.borrow());' % (f.name, f.name)
                yield '    rcx.replace_expr_prec(%s);' % \
                        field_prec_expr(f, False, suffix='_n')
                yield '    RecoverChildren::recover_node_and_children(' \
                        '&%s_r[1..], &%s_n[1..], rcx.borrow());' % (f.name, f.name)
                yield '    rcx.replace_expr_prec(old);'
            else:
                if contains_expr:
                    yield '    let old = rcx.replace_expr_prec(%s);' % \
                            field_prec_expr(f, False, suffix='_n')
                yield '    RecoverChildren::recover_node_and_children(' \
                        '%s_r, %s_n, rcx.borrow());' % (f.name, f.name)
                if contains_expr:
                    yield '    rcx.replace_expr_prec(old);'

        yield '  },'
    yield '  _ => panic!("new and reparsed ASTs don\'t match"),'
    yield '}'

@linewise
def do_recover_children_impl(d):
    impl_recover = type_has_impl(d, 'Recover')

    yield '#[allow(unused)]'
    yield 'impl RecoverChildren for %s {' % d.name
    yield '  fn recover_children(reparsed: &Self, new: &Self, mut rcx: RewriteCtxtRef) {'
    yield indent(do_recover_children_match(d), '    ')
    yield '  }'
    yield '  fn recover_node_and_children(reparsed: &Self, new: &Self, mut rcx: RewriteCtxtRef) {'
    if impl_recover:
        yield '    if recover(None, reparsed, new, rcx.borrow()) {'
        yield '      return;'
        yield '    }'
    yield '    <Self as RecoverChildren>::recover_children(reparsed, new, rcx);'
    yield '  }'
    yield '  fn recover_node_restricted(old_span: Span, reparsed: &Self, new: &Self, mut rcx: RewriteCtxtRef) {'
    if impl_recover:
        yield '    if recover(Some(old_span), reparsed, new, rcx.borrow()) {'
        yield '      return;'
        yield '    }'
    yield '    <Self as RecoverChildren>::recover_children(reparsed, new, rcx);'
    yield '  }'
    yield '}'

@linewise
def generate_recover_children_impls(decls):
    yield '// AUTOMATICALLY GENERATED - DO NOT EDIT'
    yield '// Produced %s by process_ast.py' % (datetime.now(),)
    yield ''

    for d in decls:
        if type_needs_generated_impl(d, 'RecoverChildren'):
            yield do_recover_children_impl(d)


@linewise
def do_seq_item_impl(d):
    yield '#[allow(unused)]'
    yield 'impl SeqItem for %s {' % d.name
    yield '  fn seq_item_id(&self) -> SeqItemId {'
    yield '    SeqItemId::Node(self.id)'
    yield '  }'
    yield '}'

@linewise
def generate_seq_item_impls(decls):
    yield '// AUTOMATICALLY GENERATED - DO NOT EDIT'
    yield '// Produced %s by process_ast.py' % (datetime.now(),)
    yield ''

    for d in decls:
        if type_needs_generated_impl(d, 'SeqItem'):
            yield do_seq_item_impl(d)


@linewise
def do_maybe_rewrite_seq_impl(d):
    supported = type_has_impl(d, 'SeqItem')

    for ty in (d.name, 'P<%s>' % d.name):
        yield '#[allow(unused)]'
        yield 'impl MaybeRewriteSeq for %s {' % ty
        if supported:
            yield '  fn maybe_rewrite_seq(old: &[Self],'
            yield '                       new: &[Self],'
            yield '                       outer_span: Span,'
            yield '                       rcx: RewriteCtxtRef) -> bool {'
            yield '    trace!("try sequence rewriting for %s");' % d.name
            yield '    rewrite_seq(old, new, outer_span, rcx)'
            yield '  }'
        yield '}'

@linewise
def generate_maybe_rewrite_seq_impls(decls):
    yield '// AUTOMATICALLY GENERATED - DO NOT EDIT'
    yield '// Produced %s by process_ast.py' % (datetime.now(),)
    yield ''

    for d in decls:
        if type_needs_generated_impl(d, 'MaybeRewriteSeq'):
            yield do_maybe_rewrite_seq_impl(d)
