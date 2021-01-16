
import socket, asyncio, selectors, functools, os, signal, argparse, sys, logging
import time
from asyncio.subprocess import PIPE
from concurrent.futures import CancelledError as cf_CancelledError
try:
	from asyncio.exceptions import CancelledError as as_CancelledError
except:
	pass
from subprocess import CalledProcessError

import protocol.pb_pb2 as proto

VERSION = 1

def fmt_address(sockaddr):
	return '%s:%s' % socket.getnameinfo(sockaddr, 0)

# self.handle_request(conn, id, req)
class Connection:
	def __init__(self, rd, wr, handle_request):
		super().__init__()
		self._pending = {}
		self._wr = wr
		self._reqs = 0
		self.log = logging.getLogger(fmt_address(wr.transport.get_extra_info('peername')))

		loop = asyncio.get_event_loop()

		async def rd_msg_dispatch():
			while not rd.at_eof():
				self.log.debug('rd_msg_dispatch 1')
				n = await rd.read(4)
				if len(n) == 0:
					logging.warning('rd_msg_dispatch: eof, why?')
					break
				self.log.debug('rd_msg_dispatch 2: %s', n)
				n = int.from_bytes(n, 'big')
				msg = await rd.read(n)
				if len(msg) != n:
					self.log.critical('short read, possibly not synchronized comms, aborting...')
					break
				s = proto.Smlp.FromString(msg)
				self.log.debug('rd_msg_dispatch 3: %s', s)
				assert s.version == VERSION
				if s.HasField('reply'):
					fut = self._pending[s.msg_id]
					del self._pending[s.msg_id]
					fut.set_result(s)
				if s.HasField('request'):
					loop.create_task(handle_request(self, s.msg_id, s.request))
			self.log.info('rd_msg_dipatch fini')

		self._rd_msg_task = loop.create_task(rd_msg_dispatch())

	@property
	def rd_msg_task(self):
		return self._rd_msg_task

	def close(self):
		self._rd_msg_task.cancel()
		self._wr.close()

	async def __aenter__(self):
		return self

	async def __aexit__(self, exc_type, exc_value, traceback):
		self.close()
		await self._wr.wait_closed()

	def _wr_msg_nowait(self, msg):
		self._wr.write(len(msg).to_bytes(4, 'big'))
		self._wr.write(msg)
		self.log.info('sent msg %s', msg)

	async def wait_wr_drain(self):
		await self._wr.drain()

	async def _wr_msg(self, msg):
		self._wr_msg_nowait(msg)
		await self.wait_wr_drain()

	async def wait_send_reply(self, msg_id, rep):
		await self._wr_msg(proto.Smlp(version=VERSION, msg_id=msg_id, reply=rep)
		                             .SerializeToString())

	def _next_id(self):
		msg_id = self._reqs
		self._reqs += 1
		return msg_id

	# this is dangerous: can lead to blow up of write buffer if not occasionally
	# .wait_wr_drain()
	def _send_request(self, req):
		loop = asyncio.get_event_loop()
		fut = loop.create_future()
		msg_id = self._next_id()
		self._pending[msg_id] = fut
		s = proto.Smlp(version=VERSION, msg_id=msg_id, request=req)
		self._wr_msg_nowait(s.SerializeToString())
		return fut, msg_id

	async def wait_send_request(self, req):
		fut, msg_id = self._send_request(req)
		await self.wait_wr_drain()
		return fut, msg_id

	# returns an asyncio.Future representing the state of the incoming reply
	# to the request with message id `msg_id`
	def get_pending(self, msg_id):
		return self._pending.get(msg_id)

	async def wait_pending(self):
		v = self._pending.values()
		if len(v) > 0:
			await asyncio.wait(v)

	async def request_wait_reply(self, req):
		fut, msg_id = await self.wait_send_request(req)
		await fut
		s = fut.result()
		self.log.info('request got reply: %s', s)
		assert s.version == VERSION
		assert s.msg_id == msg_id
		assert s.HasField('reply')
		assert not s.HasField('request')
		return s.reply


async def smtlib_script_request(conn, script):
	req = proto.Request(stdin=script)
	req.type = req.Type.SMTLIB_SCRIPT
	rep = await conn.request_wait_reply(req)
	assert rep.type == rep.Type.SMTLIB_REPLY
	if rep.cmd.status == 0:
		return rep.cmd.stdout
	raise CalledProcessError(returncode=rep.cmd.status, cmd=(conn, script),
	                         output=rep.cmd.stdout, stderr=rep.cmd.stderr)

async def smtlib_name(conn):
	return await smtlib_script_request(conn, b'(get-info :name)')

async def smtlib_version(conn):
	return await smtlib_script_request(conn, b'(get-info :version)')


class Pool:
	# returns either a pair (prid,instance) or None to signify end-of-problem
	async def pop(self):
		pass

	# called by the Server; if result is None, an exception occurred
	def push(self, prid, result):
		pass

	async def wait_empty(self):
		pass


class ConnectedWorker:
	def __init__(self, worker):
		self.conn = worker

	


class Server:
	def __init__(self, pool):
		self._pool = pool

	# connection is closed on return
	async def feed(self, worker):
		#n, v = await asyncio.gather(smtlib_name(worker), smtlib_version(worker))
		#n = await smtlib_name(worker)
		#v = await smtlib_version(worker)
		#logging.info('client runs %s v%s', n, v)
		pong = await worker.request_wait_reply(proto.Request(type=proto.Request.Type.PING))
		if pong.type != proto.Reply.Type.PONG:
			worker.log.critical('worker does not reply to ping, disconnecting')
			return

		try:
			nv = await smtlib_script_request(worker,
			                                 b'(get-info :name)(get-info :version)')
		except CalledProcessError as e:
			worker.log.critical('worker\'s SMT command fails smtlib2 sanity check, ' +
			                    'disconnecting: ' +
			                    'exited with %d on input %s, stdout: %s, stderr: %s',
			                    e.returncode, e.cmd[1], e.stdout, e.stderr)
			await worker.wait_send_request(proto.Request(type=proto.Request.Type.CLIENT_QUIT))
			return

		worker.log.info('client runs %s', nv)
		while True:
			pr = await self._pool.pop()
			if pr is None:
				break
			worker.log.info('submitting instance %s', pr.id)
			try:
				res = await smtlib_script_request(worker, pr.instance)
			except:
				worker.log.exception('error computing instance %s', pr.id)
				res = None
			worker.log.info('got result for instance %s', pr.id)
			self._pool.push(pr.id, res)
		worker.log.info('pool empty, closing connection')

	async def accepted(self, rd, wr):
		claddr = fmt_address(wr.transport.get_extra_info('peername'))
		logging.info('client %s accepted', claddr)
		try:
			async with Connection(rd, wr, self.handle_request) as worker:
				await self.feed(worker)
				await worker.wait_pending()
			#await wr.wait_closed()
		except ConnectionResetError as e:
			logging.warning('connection lost to client %s', claddr)
		#except as_CancelledError:
		#	raise
		#except:
		#	logging.exception('server accepted exception')
		#	raise
		#finally:
		#	logging.info('done with client %s', claddr)

	def handle_request(self, conn, msg_id, req):
		conn.log.error('unhandled request: %s', req)
		r = proto.Reply()
		r.type = r.Type.ERROR
		r.code = r.Code.UNKNOWN_REQ
		conn.reply(msg_id, r)


class Client:
	def __init__(self, args):
		self.conn = None
		self.args = args;
		self._working = False

	async def connected(self, rd, wr):
		assert self.conn is None
		tp = wr.transport
		logging.info('client %s connected to %s',
		             fmt_address(tp.get_extra_info('sockname')),
		             fmt_address(tp.get_extra_info('peername')))
		self.conn = Connection(rd, wr, self.handle_request)
		await self.conn.rd_msg_task

	async def handle_request(self, conn, msg_id, req):
		conn.log.info('got request %s', req)
		r = proto.Reply()
		a = time.perf_counter()

		if req.type == req.Type.PING:
			r.type = r.Type.PONG
			r.code = r.Code.BUSY if self._working else r.Code.IDLE

		elif req.type == req.Type.CLIENT_QUIT:
			conn.close()
			return

		elif req.type == req.Type.SMTLIB_SCRIPT:

			if self._working:
				r.type = r.Type.ERROR
				r.code = r.Code.BUSY
				r.error_msg = 'busy'

			else:
				self._working = True
				r.type = r.Type.SMTLIB_REPLY
				proc = await asyncio.create_subprocess_exec(*self.args,
				                                            stdin=PIPE,
				                                            stdout=PIPE,
				                                            stderr=PIPE)
				r.cmd.stdout, r.cmd.stderr = await proc.communicate(req.stdin)
				r.cmd.status = proc.returncode
				self._working = False

		else:
			r.type = r.Type.ERROR
			r.code = r.Code.UNKNOWN_REQ
			r.error_msg = 'request not understood'

		conn.log.info('handling request took %gs', time.perf_counter() - a)
		await conn.wait_send_reply(msg_id, r)


# precondition: pool not empty
async def server(host, port, pool):
	server = await asyncio.start_server(Server(pool).accepted, host, port)
	logging.info('server listening on %s',
	             ', '.join(map(lambda s: fmt_address(s.getsockname()),
	                           server.sockets)))
	#async with server:
	#	await server.serve_forever()
	await pool.wait_empty()
	server.close()
	await server.wait_closed()

async def client(host, port, args):
	logging.info('client args: %s', args)
	try:
		#import z3
		rd, wr = await asyncio.open_connection(host, port)
		await Client(args).connected(rd, wr)
	except OSError:
		logging.error('error connecting to %s:%d', host, port)
		raise
	except ConnectionRefusedError:
		logging.error('error connecting to %s:%d: connection refused', host, port)
		raise

HOST = None
PORT = 1337

def distribute(pool, config=None):
	if config is None:
		config = {}
	host = config.get('host', HOST)
	port = config.get('port', PORT)
	return server(host, port, pool)

import shlex

def parse_args(argv):
	class LogLevel(argparse.Action):
		def __call__(self, parser, namespace, values, option_string=None):
			l = getattr(logging, values.upper(), None)
			if not isinstance(l, int):
				raise ValueError('Invalid log level: %s' % values)
			logging.basicConfig(level=l)

	class ClientCommand(argparse.Action):
		def __init__(self, *args, **kwds):
			super().__init__(*args, **kwds)

		def __call__(self, parser, namespace, values, option_string=None):
			setattr(namespace, self.dest, shlex.split(values))

	p = argparse.ArgumentParser(prog=argv[0])
	p.add_argument('-c', '--client', default=None, metavar='CMD', type=str,
	               action=ClientCommand, help='start client mode')
	p.add_argument('-H', '--host', default=HOST, type=str)
	p.add_argument('-P', '--port', default=PORT, type=int)
	p.add_argument('-v', '--log-level', metavar='LVL', type=str, action=LogLevel)
	args = p.parse_args(argv[1:])
	return args

class Instance:
	def __init__(self):
		self.parent = None
		self.dom = None
		self.codom = None
		self.obj = None

# forwarding Pool; uses a database object to cache/persist results
class StoredPool(Pool):
	def __init__(self, parent, db):
		self._parent = parent
		self._db = db

	async def pop(self):
		while True:
			pr = await self._parent.pop()
			if pr is None or pr.id not in self._db:
				return pr
			self._parent.push(pr.id, self._db[pr.id])

	def push(self, prid, result):
		if result is not None:
			self._db[prid] = result
		self._parent.push(prid, result)

	def wait_empty(self):
		return self._parent.wait_empty()


class UNSAT:
	pass

class SAT:
	def __init__(self, model=None):
		self.model = model

from typing import Mapping, Sequence, Tuple

# assumes LC_ALL=*.UTF-8
def smtlib2_instance(logic : str,
                     cnst_decls : Mapping[str, str], # name -> type
                     cnst_defs : Mapping[str, Tuple[str,str]], # name -> (type, term)
                     assert_terms : Sequence[str], # term
                     need_model : bool,
                     timeout : int=None) -> bytes:
	r = ''
	r += '(set-option :print-success false)\n'
	if timeout is not None:
		r += '(set-option :timeout %s)\n' % timeout
	if need_model:
		r += '(set-option :produce-models true)\n'
	r += '(set-logic %s)\n' % logic
	for n,ty in cnst_decls.items():
		r += '(declare-fun %s () %s)\n' % (n,ty)
	for n,(ty,tm) in cnst_defs.items():
		r += '(define-fun %s () %s %s)\n' % (n,ty,tm)
	for tm in assert_terms:
		r += '(assert %s)\n' % tm
	r += '(check-sat)\n'
	if need_model:
		r += '(get-model)\n'
	r += '(exit)'
	return r.encode()

class SMLP:
	def __init__(self, config_decls, input_decls, eta, theta, phi):
		self.configs = config_decls
		self.inputs  = input_decls
		self.eta     = eta
		self.theta   = theta
		self.phi     = phi

	def candidate(self, real_pred):
		# TODO: return SMT instance: Ep Eq eta /\ theta /\ phi(real_pred)
		pass

	def counterex(self, model, real_pred):
		# TODO: return SMT instance: Eq theta[model|p] /\ not phi(real_pred)
		pass

	def exclude(self, model):
		return SMLP(self.eta and not model, self.theta, self.phi)

async def enumerate_sol(solver, smlp):
	while True:
		sol = await solver.solve(smlp.exists())
		if isinstance(sol, UNSAT):
			break
		yield sol
		smlp = smlp.exclude(sol)

# asynchronous generator
async def threshold1(solver, smlp, th, prec):
	ex = smlp
	al = smlp
	while True:
		hi = await solver.solve(ex.candidate(lambda x: x >= th + prec))
		if isinstance(hi, UNSAT):
			break
		lo = await solver.solve(al.at(hi).exists(lambda x: x >= th))
		if isinstance(lo, UNSAT):
			yield hi
		ex = ex.exclude(hi)


if __name__ == "__main__":
	class TestPool(Pool):
		def __init__(self, loop):
			self.queue = ['']
			self.empty = loop.create_future()

		async def pop(self):
			# notify self.wait_empty()
			self.empty.set_result(None)

		async def wait_empty(self):
			await self.empty

	def sighandler(task, sig):
		logging.critical('got signal %s, terminating...', sig.name)
		for task in asyncio.all_tasks():
			task.cancel()
		#task.cancel()

	try:
		args = parse_args(sys.argv)
	except ValueError as e:
		print('error:', e, file=sys.stderr)
		sys.exit(1)

	loop = asyncio.get_event_loop()

	if args.client is not None:
		coro = client(args.host, args.port, args.client)
	else:
		coro = server(args.host, args.port, StoredPool(TestPool(loop), dict()))

	#task = asyncio.ensure_future(coro) # loop.create_task(coro)
	task = coro

	for sig in map(lambda n: getattr(signal, n), ('SIGINT','SIGTERM')):
		loop.add_signal_handler(sig, functools.partial(sighandler, None, sig))

	try:
		loop.run_until_complete(task)
	except cf_CancelledError:
		# from sighandler
		logging.info('cf cancelled, aborting...')
	except as_CancelledError:
		# from client
		logging.info('as cancelled, aborting...')
	except:
		logging.exception("error")
	finally:
		loop.run_until_complete(loop.shutdown_asyncgens())
		#loop.close()
