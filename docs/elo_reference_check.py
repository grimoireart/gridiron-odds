import csv, math
ALIAS={'STL':'LA','SD':'LAC','OAK':'LV'}
rows=list(csv.DictReader(open('games.csv')))
rows=[r for r in rows if r['home_score']!='']
rows.sort(key=lambda r:(r['gameday'],r['gametime']))
def wp(d): return 1/(1+10**(-d/400))
def run(K=20,HFA=40,REV=0.4,REST=2, holdout=2025, start=2010):
    R={}; season=None; preds=[]
    for r in rows:
        s=int(r['season'])
        if s!=season:
            for t in R: R[t]=1500+(R[t]-1500)*(1-REV)
            season=s
        h=ALIAS.get(r['home_team'],r['home_team']); a=ALIAS.get(r['away_team'],r['away_team'])
        R.setdefault(h,1500); R.setdefault(a,1500)
        neutral = r['location']=='Neutral'
        rest = (float(r['home_rest'] or 7)-float(r['away_rest'] or 7)) if r['home_rest'] and r['away_rest'] else 0
        d=R[h]-R[a]+(0 if neutral else HFA)+REST*rest
        p=wp(d)
        hs,as_=int(r['home_score']),int(r['away_score'])
        res=1 if hs>as_ else 0 if hs<as_ else 0.5
        m=abs(hs-as_)
        dfav = d if res==1 else -d if res==0 else abs(d)
        mult=math.log(m+1)*2.2/(0.001*dfav+2.2)
        delta=K*mult*(res-p)
        if s==holdout:
            preds.append((p,res,r['home_moneyline'],r['away_moneyline'],r['game_type']))
        R[h]+=delta; R[a]-=delta
    return preds
def ml(x):
    x=float(x); return 100/(x+100) if x>0 else -x/(-x+100)
preds=run()
n=len(preds); 
acc=sum(1 for p,res,*_ in preds if (p>=.5)==(res==1))/n
brier=sum((p-res)**2 for p,res,*_ in preds)/n
ll=-sum(res*math.log(p)+(1-res)*math.log(1-p) for p,res,*_ in preds)/n
v=[(ml(hm)/(ml(hm)+ml(am)),res) for p,res,hm,am,_ in preds if hm and am]
vacc=sum(1 for q,res in v if (q>=.5)==(res==1))/len(v); vb=sum((q-res)**2 for q,res in v)/len(v)
vll=-sum(res*math.log(q)+(1-res)*math.log(1-q) for q,res in v)/len(v)
print(f"n={n} elo acc={acc:.3f} brier={brier:.4f} ll={ll:.3f} | vegas n={len(v)} acc={vacc:.3f} brier={vb:.4f} ll={vll:.3f}")
