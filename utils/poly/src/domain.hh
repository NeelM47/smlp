/* SPDX-License-Identifier: Apache-2.0
 *
 * Copyright 2022 Franz Brausse <franz.brausse@manchester.ac.uk>
 * Copyright 2022 The University of Manchester
 */

#pragma once

#include "expr2.hh"

namespace smlp {

/* Closed interval with rational endpoints */
struct ival {

	kay::Q lo, hi;

	ival(kay::Q v = 0)
	: ival(v, v)
	{}

	ival(kay::Q lo, kay::Q hi)
	: lo(move(lo))
	, hi(move(hi))
	{
		assert(this->lo <= this->hi);
	}

	friend kay::Q length(const ival &i)
	{
		return i.hi - i.lo;
	}

	friend kay::Q mid(const ival &i)
	{
		return (i.lo + i.hi) / 2;
	}

	bool contains(const kay::Z &v) const
	{
		return lo <= v && v <= hi;
	}

	bool contains(const kay::Q &v) const
	{
		return lo <= v && v <= hi;
	}
};

/* Explicit list of rational values */
struct list { vec<kay::Q> values; };

struct entire {};

/* A component (of the domain) is either an interval or a list of rational
 * values */
struct component {

	sumtype<entire,ival,list> range;
	enum { INT, REAL } type;

	bool contains(const kay::Q &v) const
	{
		return range.match(
		[](const entire &) { return true; },
		[&](const ival &i) { return i.contains(v); },
		[&](const list &l) {
			for (const kay::Q &q : l.values)
				if (q == v)
					return true;
			return false;
		}
		);
	}
};

static inline bool is_real(const sumtype<entire,ival,list> &c)
{
	if (c.get<entire>() || c.get<ival>())
		return true;
	for (const kay::Q &q : c.get<list>()->values)
		if (q.get_den() != 1)
			return true;
	return 1 || false; /* always real: Z3 falls back to a slow method otherwise */
}

/* Translates a component 'rng' and the appropriate variable name 'var' into a
 * constraint in form of a 'form2' formula. */
form2 domain_constraint(const str &var, const component &c);

/* The domain is an (ordered) list of pairs (name, component) */
struct domain : vec<pair<str,component>> {

	const component * operator[](const std::string_view &s) const
	{
		for (const auto &[n,c] : *this)
			if (n == s)
				return &c;
		return nullptr;
	}
};

form2 domain_constraints(const domain &d);

/* Parses the DOMAIN-FILE, see poly.cc for details. */
domain parse_simple_domain(FILE *f);

}
