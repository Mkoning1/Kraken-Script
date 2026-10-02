import math, random, itertools

class OrderNotFound(Exception): pass
class InsufficientFunds(Exception): pass

def gen(n, start, seed, t0):
    rnd = random.Random(seed); out=[]; p=start
    regimes=[(0.0005,0.004),(-0.0003,0.005),(0.0,0.003),(0.0012,0.007)]
    for i in range(n):
        mu, vol = regimes[((i+seed*131)//700) % 4]
        if rnd.random()<0.02: vol*=3
        o=p; r=rnd.gauss(mu,vol); c=o*math.exp(r)
        h=max(o,c)*math.exp(abs(rnd.gauss(0,vol/2))); l=min(o,c)*math.exp(-abs(rnd.gauss(0,vol/2)))
        if rnd.random()<0.002: l*=0.9   # flash crash
        v=abs(rnd.gauss(100,20))*(1+abs(r)/vol*0.6)
        out.append([t0+i*900, o,h,l,c,v]); p=c
    return out

def agg(c15, k):
    out=[]
    for j in range(0, len(c15)-k+1, k):
        g=c15[j:j+k]
        out.append([g[0][0], g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4], sum(x[5] for x in g)])
    return out

class MockKraken:
    def __init__(self, symbols, n, fee=0.004, slip=0.0008, eur=100.0, seed0=1):
        self.t0 = 1_700_006_400 - (1_700_006_400 % 14400)
        self.data = {}
        for i,(s,p) in enumerate(symbols.items()):
            c=gen(n,p,i+seed0,self.t0); self.data[s]={15:c,60:agg(c,4),240:agg(c,16)}
        self.k = 0; self.fee=fee; self.slip=slip
        self.free = {"EUR": eur}; self.used = {}
        self.orders = {}; self.ids = itertools.count(1); self.calls=[]
        self.markets = {s: {"symbol": s, "base": s.split("/")[0], "quote": "EUR", "active": True, "type": "spot",
                            "limits": {"amount": {"min": 0.0001}, "cost": {"min": 0.5}}} for s in symbols}
    # --- tijd ---
    def now(self): return self.t0 + self.k*900
    def set_k(self, k):
        for kk in range(self.k, k):
            self.k = kk+1
            for o in self.orders.values():
                if o["status"]=="open" and o.get("stop"):
                    c=self.data[o["symbol"]][15][kk]
                    if c[3] <= o["stop"]:
                        px=min(c[1], o["stop"])*(1-self.slip); self._fill_sell(o, px, reserved=True)
    def price(self, s): return self.data[s][15][self.k-1][4]
    # --- publieke api ---
    def load_markets(self): return self.markets
    def market(self, s): return self.markets[s]
    def fetch_tickers(self): return {s: {"last": self.price(s), "quoteVolume": 1e6+i} for i,s in enumerate(self.data)}
    def fetch_ohlcv(self, s, timeframe, limit=720):
        tf={"15m":15,"1h":60,"4h":240}[timeframe]; arr=self.data[s][tf]
        closed = self.k*900 // (tf*60)
        return [[r[0]*1000]+r[1:] for r in arr[max(0,closed-limit):closed+1]]
    def amount_to_precision(self, s, x): return f"{math.floor(x*1e8)/1e8:.8f}"
    def price_to_precision(self, s, x): return f"{x:.8g}"
    # --- private api ---
    def fetch_balance(self):
        total={c: self.free.get(c,0)+self.used.get(c,0) for c in set(self.free)|set(self.used)}
        return {"free": dict(self.free), "used": dict(self.used), "total": total}
    def create_order(self, s, typ, side, amount, price=None, params={}):
        self.calls.append((s, side, amount, dict(params)))
        base=self.markets[s]["base"]; amount=float(amount)
        if params.get("validate")=="true": return {"id": None, "info": {"descr": "validated"}}
        oid=str(next(self.ids)); o={"id":oid,"symbol":s,"side":side,"amount":amount,"status":"open","filled":0,"remaining":amount}
        if "stopLossPrice" in params:
            if self.free.get(base,0) < amount-1e-12: raise InsufficientFunds(f"stop: {base} {self.free.get(base,0)} < {amount}")
            self.free[base]-=amount; self.used[base]=self.used.get(base,0)+amount
            o["stop"]=float(params["stopLossPrice"]); self.orders[oid]=o; return {"id":oid}
        px=self.price(s)
        if side=="buy":
            px*=1+self.slip; cost=amount*px; fee=cost*self.fee
            if self.free["EUR"] < cost+fee-1e-9: raise InsufficientFunds(f"EUR {self.free['EUR']:.2f} < {cost+fee:.2f}")
            self.free["EUR"]-=cost+fee; self.free[base]=self.free.get(base,0)+amount
            o.update(status="closed",filled=amount,remaining=0,average=px,cost=cost,fee={"cost":fee,"currency":"EUR"})
        else:
            if self.free.get(base,0) < amount-1e-12: raise InsufficientFunds(f"{base} {self.free.get(base,0)} < {amount}")
            self._fill_sell(o, px*(1-self.slip), reserved=False)
        self.orders[oid]=o; return {"id":oid}
    def _fill_sell(self, o, px, reserved):
        base=self.markets[o["symbol"]]["base"]; a=o["amount"]
        if reserved: self.used[base]-=a
        else: self.free[base]-=a
        cost=a*px; fee=cost*self.fee; self.free["EUR"]+=cost-fee
        o.update(status="closed",filled=a,remaining=0,average=px,cost=cost,fee={"cost":fee,"currency":"EUR"})
    def fetch_order(self, oid, s): return dict(self.orders[oid])
    def cancel_order(self, oid, s=None):
        o=self.orders.get(oid)
        if not o or o["status"]!="open": raise OrderNotFound(oid)
        s=o["symbol"]; base=self.markets[s]["base"]; self.used[base]-=o["amount"]; self.free[base]=self.free.get(base,0)+o["amount"]
        o["status"]="canceled"

def _foo(self): return [o for o in self.orders.values() if o["status"]=="open"]
MockKraken.fetch_open_orders = _foo
MockKraken.fetch_closed_orders = lambda self, s=None, since=None, limit=None: [o for o in self.orders.values() if o["status"]=="closed"][:limit or 10]
