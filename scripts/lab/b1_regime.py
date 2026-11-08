"""Daily-bar test of B1 (buy yesterday's main-board limit-up at the next open) and of selling held limit-ups at the
next open vs close, split by market state known at the close (limit-up count and failed-board rate, quantile
within the past 250 days). Run: PYTHONPATH=. backend/.venv/Scripts/python.exe scripts/lab/b1_regime.py (E054)."""
import os, polars as pl, numpy as np
from datetime import date
from backend.app.warehouse.store import warehouse
MAIN=["600","601","603","605","000","001","002","003"]
b=(warehouse("live").scan("bars").filter(pl.col("code").str.slice(0,3).is_in(MAIN)).select("code","date","open","high","low","close").collect()
   .unique(["code","date"]).sort("code","date"))
b=b.with_columns(pl.col("close").shift(1).over("code").alias("pc"))
b=b.with_columns(((pl.col("pc")*1.1*100).round(0)/100).alias("up"))
b=b.with_columns((pl.col("close")>=pl.col("up")-1e-6).alias("zt"),(pl.col("high")>=pl.col("up")-1e-6).alias("touch"))
for k in (1,2): b=b.with_columns(pl.col("open").shift(-k).over("code").alias(f"o{k}"),pl.col("close").shift(-k).over("code").alias(f"c{k}"))
mk=b.group_by("date").agg(pl.col("zt").sum().alias("n_zt"),(pl.col("touch")&~pl.col("zt")).sum().alias("n_zb")).sort("date")
mk=mk.with_columns((pl.col("n_zb")/(pl.col("n_zt")+pl.col("n_zb"))).alias("zb_rate"))
# state terciles by expanding history only (no look-ahead): rank of today's value within the past 250 days
def roll_q(s, win=250):
    v=s.to_numpy(); out=np.full(len(v),np.nan)
    for i in range(60,len(v)):
        h=v[max(0,i-win):i]; out[i]=(h<v[i]).mean()
    return pl.Series(out)
mk=mk.with_columns(roll_q(mk["n_zt"]).alias("q_zt"),roll_q(mk["zb_rate"]).alias("q_zb"))
mk=mk.with_columns(pl.when(pl.col("q_zt")>=0.67).then(pl.lit("hot")).when(pl.col("q_zt")<0.33).then(pl.lit("cold")).otherwise(pl.lit("mid")).alias("heat"),
                   pl.when(pl.col("q_zb")>=0.67).then(pl.lit("hi_fail")).otherwise(pl.lit("lo_fail")).alias("fail"))
t=b.filter(pl.col("zt")).join(mk.select("date","heat","fail","n_zt","zb_rate"),on="date")
t=t.with_columns(((pl.col("o1")/pl.col("close")-1)*100).alias("gap1"))
t=t.with_columns(((pl.col("o2")/pl.col("o1")-1)*100).alias("b1_o2"), ((pl.col("c1")/pl.col("o1")-1)*100).alias("b1_c1"),
                 ((pl.col("c1")/pl.col("close")-1)*100).alias("hold_c1"), ((pl.col("o1")/pl.col("close")-1)*100).alias("sell_o1"))
t=t.with_columns(pl.when(pl.col("date")<date(2026,7,1)).then(pl.lit("PRE")).otherwise(pl.lit("DEV")).alias("period")).drop_nulls(["q_zt"]) if False else t.with_columns(pl.when(pl.col("date")<date(2026,7,1)).then(pl.lit("PRE")).otherwise(pl.lit("DEV")).alias("period"))
t=t.filter(pl.col("heat").is_not_null())
COST=0.15
b1=t.filter(pl.col("gap1").is_between(2,7,closed="left") & pl.col("o2").is_not_null())
print("B1 daily proxy: yesterday's limit-up, open gap 2~7%, buy open, sell next open (cost",COST,"%)")
print(b1.group_by("period","heat").agg(pl.len().alias("n"),(pl.col("b1_o2")-COST).mean().round(2).alias("o_to_next_o"),(pl.col("b1_c1")-COST/2).mean().round(2).alias("o_to_c"),(pl.col("b1_o2")>0).mean().round(2).alias("win")).sort("period","heat"))
print(b1.group_by("period","fail").agg(pl.len().alias("n"),(pl.col("b1_o2")-COST).mean().round(2).alias("o_to_next_o")).sort("period","fail"))
hold=t.filter(pl.col("o2").is_not_null() & pl.col("o1").is_not_null())
print("\nHeld limit-up: day-after open sell vs day-after close sell (+ = selling at the open was better)")
print(hold.group_by("period","heat").agg(pl.len().alias("n"),(pl.col("sell_o1")-pl.col("hold_c1")).mean().round(2).alias("open_minus_close"),((pl.col("o2")/pl.col("c1")-1)*100).mean().round(2).alias("overnight_after_d1")).sort("period","heat"))
print("\nlast days:", mk.tail(1).select("date","n_zt","zb_rate","heat","fail").rows())
print("\nAll gap bands, buy D1 open sell D2 open, cost 0.15%")
bands=t.filter(pl.col("o2").is_not_null()).with_columns(pl.col("gap1").cut([-5,-2,0,2,4,7,9.5],labels=["<-5","-5~-2","-2~0","0~2","2~4","4~7","7~9.5",">=9.5"]).alias("band"))
print(bands.group_by("band","period").agg(pl.len().alias("n"),(pl.col("b1_o2")-COST).mean().round(2).alias("ret"),(pl.col("b1_o2")>0).mean().round(2).alias("win")).sort("band","period"))
