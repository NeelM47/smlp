#!/usr/bin/env python3

import pandas as pd
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed

from smlp.util.func import const

def f_weigh(x, sigma, x0):
	return 1/(1+np.exp(-2*sigma*(x-x0)))

def time_win_sz(tw):
	x = tw['Timing']
	return max(x) - min(x) + 1

#def inv_f_weigh_002(y, x0):
#	return 25 * np.log(-y/(y-1)) + x0

rx_delta_a = 2.346
rx_time_a  = 4.882
tx_delta_a = 15.6
tx_time_a  = rx_time_a

class Tform:
	def __init__(self, is_rx):
		if is_rx:
			self.delta_a, self.time_a = rx_delta_a, rx_time_a
		else:
			self.delta_a, self.time_a = tx_delta_a, tx_time_a

	def fd(self, d):
		return f_weigh(d, 0.02 / self.delta_a, 100)

	def ft(self, t):
		return f_weigh(t, 0.02 / self.time_a, 100)

	def f(self, t, d):
		return self.ft(t) * self.fd(d)

	def obj(self, tw, t):
		wd = tw['delta']
		wd.index = tw['Timing']
		delta = min(wd)
		#delta_area = delta * len(wd)
		return self.f(t, delta)

	def ff(self, v, time_window_rad):
		w = pd.DataFrame(columns=['trad','area'], index=v.index)
		for i in range(len(v)):
			r = v.iloc[i]
			tw = v[abs(v['Timing'] - r['Timing']) <= time_window_rad]
			w.iloc[i,1] = self.obj(tw, time_win_sz(tw))
			w.iloc[i,0] = time_window_rad
		w[v.columns] = v
		return w

	def tform(self, kv, time_window_radii, log):
		g = kv[1]
		log(1, 'tform %s' % (kv[0],))
		v = g.sort_values(by=['Timing'], ascending=True)
		return pd.DataFrame().append([self.ff(v, rad) for rad in time_window_radii])


def prep_area(inp, is_rx, out, log=const(None), max_workers=None, mp_context=None):

	#if is_rx: # rx
	#	delta_a, time_a = rx_delta_a, rx_time_a
	#else: # tx
	#	delta_a, time_a = tx_delta_a, tx_time_a
	#
	## delta
	#fd = lambda d: f_weigh(d, 0.02 / delta_a, 100)
	#ft = lambda t: f_weigh(t, 0.02 / time_a, 100)
	##inv_ft = lambda y: inv_f_weigh_002(y, 19)
	##inv_fd = lambda y: inv_f_weigh_002(y, 16)
	#f = lambda t, d: ft(t) * fd(d)
	#
	##def inv_f_d(t):
	##	return lambda y: inv_ft(y / ft(t))
	#
	#def obj(tw, t):
	#	wd = tw['delta']
	#	wd.index = tw['Timing']
	#	delta = min(wd)
	#	#delta_area = delta * len(wd)
	#	return f(t, delta)
	#
	#def time_win_sz(tw):
	#	x = tw['Timing']
	#	return max(x) - min(x) + 1
	#
	#def ff(v, time_window_rad):
	#	w = pd.DataFrame(columns=['trad','area'], index=v.index)
	#	for i in range(len(v)):
	#		r = v.iloc[i]
	#		tw = v[abs(v['Timing'] - r['Timing']) <= time_window_rad]
	#		w.iloc[i,1] = obj(tw, time_win_sz(tw))
	#		w.iloc[i,0] = time_window_rad
	#	w[v.columns] = v
	#	return w

	time_window_radii = [100 + i for i in [-30,-20,-10,0,10,20,30]]

	#def tform(kv):
	#	g = kv[1]
	#	log(1, 'tform %s' % (kv[0],))
	#	v = g.sort_values(by=['Timing'], ascending=True)
	#	return pd.DataFrame().append([ff(v, rad) for rad in time_window_radii])

	data = pd.read_csv(inp)
	data = data.drop_duplicates()

	grid_cols = [c for c in data.columns if c not in ('Timing','Area','delta')]
	grid = data.groupby(grid_cols)

	tform = Tform(is_rx)

	with ProcessPoolExecutor(max_workers=max_workers, mp_context=mp_context) as ex:
		wrhdr = True
		fut2res = [ex.submit(tform.tform, kv, time_window_radii, log=log) for kv in grid]
		for fut in as_completed(fut2res):
			fut.result().to_csv(out, index=False, header=wrhdr)
			wrhdr = False
	# pd.concat([tform(kv) for kv in grid]).write_csv(sys.stdout, index=False)

if __name__ == '__main__':
	import sys
	from smlp.util.prog import *

	a = sys.argv
	n = len(a)

	if n < 2 or n > 3 or a[1] not in ('rx','tx'):
		die(1, 'usage: %s {rx|tx} [N_PROC] < DATA.csv > ADJB.csv' % a[0])

	prep_area(sys.stdin, a[1] == 'rx', sys.stdout, log=verbosify(log),
	          max_workers=int(a[2]) if n > 2 else None)