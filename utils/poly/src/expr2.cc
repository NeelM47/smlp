/* SPDX-License-Identifier: Apache-2.0
 *
 * Copyright 2022 Franz Brausse <franz.brausse@manchester.ac.uk>
 * Copyright 2022 The University of Manchester
 */

#include "expr2.hh"

using namespace smlp;

expr2 smlp::unroll(const expr &e, const hmap<str,fun<expr2(vec<expr2>)>> &funs)
{
	return e.match<expr2>(
	[&](const name &n) { return n; },
	[&](const cnst &c) {
		if (c.value == "None")
			return cnst2 { kay::Z(0) };
		if (c.value.find('.') == str::npos &&
		    c.value.find('e') == str::npos &&
		    c.value.find('E') == str::npos)
			return cnst2 { kay::Z(c.value) };
		return cnst2 { kay::Q_from_str(str(c.value).data()) };
	},
	[&](const bop &b) {
		return bop2 {
			b.op,
			make2e(unroll(*b.left, funs)),
			make2e(unroll(*b.right, funs)),
		};
	},
	[&](const uop &u) {
		return uop2 {
			u.op,
			make2e(unroll(*u.operand, funs)),
		};
	},
	[&](const call &c) {
		vec<expr2> args;
		args.reserve(c.args.size());
		for (const expr &e : c.args)
			args.push_back(unroll(e, funs));
		auto f = funs.find(c.func->get<name>()->id);
		assert(f != funs.end());
		return f->second(move(args));
	}
	);
}

sptr<form2> smlp::subst(const sptr<form2> &f, const hmap<str,sptr<expr2>> &repl)
{
	return f->match(
	[&](const prop2 &p){
		sptr<expr2> l = subst(p.left, repl);
		sptr<expr2> r = subst(p.right, repl);
		return l == p.left && r == p.right
		     ? f : make2f(prop2 { p.cmp, move(l), move(r) });
	},
	[&](const lbop2 &b){
		vec<sptr<form2>> a = b.args;
		bool changed = false;
		for (sptr<form2> &o : a) {
			sptr<form2> q = subst(o, repl);
			changed |= o == q;
			o = move(q);
		}
		return !changed ? f : make2f(lbop2 { b.op, move(a) });
	},
	[&](const lneg2 &n){
		sptr<form2> m = subst(n.arg, repl);
		return m == n.arg ? f : make2f(lneg2 { move(m) });
	}
	);
}

sptr<expr2> smlp::subst(const sptr<expr2> &e, const hmap<str,sptr<expr2>> &repl)
{
	return e->match(
	[&](const name &n) {
		auto it = repl.find(n.id);
		return it == repl.end() ? e : it->second;
	},
	[&](const bop2 &b) {
		sptr<expr2> l = subst(b.left, repl);
		sptr<expr2> r = subst(b.right, repl);
		return l == b.left && r == b.right
		     ? e : make2e(bop2 { b.op, move(l), move(r) });
	},
	[&](const uop2 &u) {
		sptr<expr2> a = subst(u.operand, repl);
		return a == u.operand ? e : make2e(uop2 { u.op, move(a) });
	},
	[&](const cnst2 &) { return e; },
	[&](const ite2 &i) {
		sptr<form2> c = subst(i.cond, repl);
		sptr<expr2> y = subst(i.yes, repl);
		sptr<expr2> n = subst(i.no, repl);
		return c == i.cond && y == i.yes && n == i.no
		     ? e : make2e(ite2 { move(c), move(y), move(n) });
	}
	);
}
