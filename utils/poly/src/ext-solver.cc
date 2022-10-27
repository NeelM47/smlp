/* SPDX-License-Identifier: Apache-2.0
 *
 * Copyright 2022 Franz Brausse <franz.brausse@manchester.ac.uk>
 * Copyright 2022 The University of Manchester
 */

#include "ext-solver.hh"
#include "dump-smt2.hh"

#include <unistd.h> /* pipe(), dup2() */
#include <fcntl.h>  /* O_CLOEXEC */

using namespace smlp;

namespace {

struct Pipe {

	file rd, wr;

	explicit Pipe(int flags = 0)
	{
		int rw[2];
		if (pipe2(rw, flags))
			throw std::error_code(errno, std::system_category());
		rd = file(rw[0], "r");
		wr = file(rw[1], "w");
	}
};

}

process::process(const char *cmd)
{
	Pipe i, o, e;
	pid = fork();
	if (pid == -1)
		throw std::error_code(errno, std::system_category());
	if (!pid) {
		i.wr.close();
		o.rd.close();
		e.rd.close();
		if (dup2(fileno(i.rd), STDIN_FILENO) == -1)
			throw std::error_code(errno, std::system_category());
		if (dup2(fileno(o.wr), STDOUT_FILENO) == -1)
			throw std::error_code(errno, std::system_category());
		if (dup2(fileno(e.wr), STDERR_FILENO) == -1)
			throw std::error_code(errno, std::system_category());
		execlp("sh", "sh", "-c", cmd, NULL);
		throw std::error_code(errno, std::system_category());
	}
	i.rd.close();
	o.wr.close();
	e.wr.close();
	in = move(i.wr);
	out = move(o.rd);
	err = move(e.rd);
}

process::~process()
{
	if (pid != -1)
		kill(pid, SIGTERM);
}

str ext_solver::get_info(const char *what)
{
	opt<es::sexpr> reply;
	fprintf(in, "(get-info %s)\n", what);
	reply = out_s.next();
	assert(reply);
	assert(reply->size() == 2);
	assert(std::get<es::slit>((*reply)[0]) == what);
	const es::slit &s = std::get<es::slit>((*reply)[1]);
	assert(s.length() > 0);
	assert(s[0] == '"');
	assert(s[s.length()-1] == '"');
	return s.substr(1, s.length() - 2);
}

ext_solver::ext_solver(const char *cmd, const char *logic)
: process(cmd)
, out_s((ungetc(' ', out), out))
{
	setvbuf(in, NULL, _IOLBF, 0);

	fprintf(in, "(set-option :print-success false)\n");
	fprintf(in, "(set-option :produce-models true)\n");

	name = get_info(":name");
	version = get_info(":version");
	fprintf(stderr, "ext-solver pid %d: %s %s\n",
	        pid, name.c_str(), version.c_str());

	if (logic)
		fprintf(in, "(set-logic %s)\n", logic);
}

void ext_solver::declare(const domain &d)
{
	assert(!n_vars);
	dump_smt2(in, d);
	n_vars = size(d);
}

static kay::Q Q_from_smt2(const es::arg &s)
{
	using namespace kay;
	using es::slit;
	using es::arg;
	using es::sexpr;

	if (const slit *sls = std::get_if<slit>(&s))
		return Q_from_str(str(*sls).data());
	const auto &[sl,num,den] = as_tuple_ex<slit,arg,slit>(std::get<sexpr>(s));
	assert(sl == "/");
	Q n;
	if (const slit *nss = std::get_if<slit>(&num)) {
		n = Q_from_str(str(*nss).data());
	} else {
		const auto &[sgn,ns] = as_tuple_ex<slit,slit>(std::get<sexpr>(num));
		assert(sgn == "+" || sgn == "-");
		n = Q_from_str(str(ns).data());
		if (sgn == "-")
			neg(n);
	}
	return n / Q_from_str(str(den).data());
}

static pair<str,sptr<term2>> parse_smt2_asgn(const es::sexpr &a)
{
	using es::slit;
	using es::arg;
	using es::sexpr;

	if (size(a) == 3) {
		/* (= name cnst) */
		const auto &[eq,var,s] = as_tuple_ex<slit,slit,arg>(a);
		assert(eq == "=");
		return { var, make2t(cnst2 { Q_from_smt2(s) }) };
	}
	str v, t;
	arg b = slit("");
	if (size(a) == 4) {
		/* (define-const name type cnst) */
		const auto &[def,var,ty,s] = as_tuple_ex<slit,slit,slit,arg>(a);
		assert(def == "define-const");
		v = var;
		t = ty;
		b = s;
	} else {
		/* (define-fun name () type cnst) */
		assert(size(a) == 5);
		const auto &[def,var,none,ty,s] =
			as_tuple_ex<slit,slit,sexpr,slit,arg>(a);
		assert(def == "define-fun");
		assert(size(none) == 0);
		v = var;
		t = ty;
		b = s;
	}

	if (t == "Real")
		return { v, make2t(cnst2 { Q_from_smt2(b) }) };
	assert(t == "Int");
	return { v, make2t(cnst2 { kay::Z(std::get<slit>(b).c_str()) }) };
}

result ext_solver::check()
{
	using es::slit;
	using es::arg;
	using es::sexpr;

	fprintf(in, "(check-sat)\n");
	out_s.skip_space();
	if (out_s.c == '(') {
		opt<sexpr> e = out_s.compound();
		assert(e);
		es::formatter f;
		f.f = stderr;
		f.emit(*e);
		abort();
	}
	opt<slit> res = out_s.atom();
	assert(res);
	if (*res == "unsat")
		return unsat {};
	if (*res == "unknown")
		return unknown { get_info(":reason-unknown") };
	assert(*res == "sat");

	fprintf(in, "(get-model)\n");
	hmap<str,sptr<term2>> m;
	if (name == "Yices") {
		for (size_t i=0; i<n_vars; i++) {
			opt<sexpr> n = out_s.next();
			assert(n);
			const auto &[eq,var,s] = as_tuple_ex<slit,slit,arg>(*n);
			assert(eq == "=");
			auto [it,ins] = m.emplace(var, make2t(cnst2 { Q_from_smt2(s) }));
			assert(ins);
		}
		return sat { move(m) };
	}
	opt<sexpr> no = out_s.next();
	assert(no);
	const sexpr &n = *no;
	size_t off = 0;
	if (name == "cvc4") {
		assert(std::get<slit>(n[0]) == "model");
		off = 1;
	}
	assert(size(n) == off+n_vars);
	for (size_t i=0; i<n_vars; i++) {
		auto [it,ins] = m.insert(parse_smt2_asgn(std::get<sexpr>(n[off+i])));
		assert(ins);
	}
	return sat { move(m) };
}

void ext_solver::add(const form2 &f)
{
	fprintf(in, "(assert ");
	dump_smt2(in, f);
	fprintf(in, ")\n");
}

