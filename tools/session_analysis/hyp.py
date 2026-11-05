#!/usr/bin/env python3
"""Permutation tests of scope / work-type / agent-family hypotheses on the share of
real non-convergence. Exploratory: uncorrected p-values, small n."""
import json,sys,re,glob,random,collections as C
S=sys.argv[1]; random.seed(11)
d=json.load(open(f"{S}/issues_full.json")); d=d.get("data",d); d=d.get("issues",d) if isinstance(d,dict) else d
I={i["key"]:i for i in d}
T={r["key"]:r for r in map(json.loads,open(f"{S}/issue_table.jsonl"))}
L={}
for f in glob.glob(f"{S}/labels/batch_*.json"):
    for r in json.load(open(f)): L[r["key"]]=r
conv=[k for k in T if T[k]["outcome"]=="converged_first_pass"]
real=[k for k,r in L.items() if "primary_mode" in r and r["primary_mode"] not in("process_hygiene","insufficient_evidence","genuine_negative_result","other") and not r.get("process_artifact")]
pop=conv+real; y={k:(1 if k in real else 0) for k in pop}
def perm(flag):
    a=[k for k in pop if flag(k)]; b=[k for k in pop if not flag(k)]
    if len(a)<5 or len(b)<5: return None
    ra=sum(y[k] for k in a)/len(a); rb=sum(y[k] for k in b)/len(b)
    obs=ra-rb; ys=[y[k] for k in pop]; n=len(a); cnt=0
    for _ in range(5000):
        random.shuffle(ys); pa=sum(ys[:n])/n; pb=sum(ys[n:])/(len(ys)-n)
        cnt+= abs(pa-pb)>=abs(obs)
    return len(a),ra,len(b),rb,cnt/5000
def show(name,flag):
    r=perm(flag)
    print(f"  {name:34}", "n/a" if not r else f"flag n={r[0]:<3} {r[1]:>5.0%} | other n={r[2]:<3} {r[3]:>5.0%} | p~{r[4]:.3f}")
body={k:(I[k].get("body") or "") for k in I}
children=C.Counter(I[k].get("parent") for k in I if I[k].get("parent"))
refs=C.defaultdict(list)
for k,b in body.items():
    for m in set(re.findall(r"TAM-\d+",b)):
        if m!=k and I[k]["created_at"]>I.get(m,{"created_at":"9"})["created_at"]: refs[m].append(k)
def parts(k):
    b=body[k]; return len(re.findall(r"^\s*(\d+[.)]|[-*])\s",b,re.M))>=5 or len(re.findall(r"\band\b|;",b))>=8
print("== 2. SCOPE (rate of REAL non-convergence among converged+real; n=%d, real=%d)"%(len(pop),len(real)))
base=len(real)/len(pop); print(f"  baseline {base:.0%}")
show("multi-part body",parts)
show("is a parent (has children)",lambda k:children[k]>0)
show("spawned >=1 later follow-up issue",lambda k:len(refs[k])>=1)
show("spawned >=3 later follow-ups",lambda k:len(refs[k])>=3)
show("title >100 chars",lambda k:len(I[k]["title"])>100)
show("title lists several things (',' or '+')",lambda k:len(re.findall(r"[,+]|\band\b",I[k]["title"]))>=3)
print("  most follow-ups among real:",sorted(((len(refs[k]),k) for k in real),reverse=True)[:5])

def wtype(k):
    t=I[k]["title"].lower(); lb=set(I[k].get("labels") or [])
    if re.search(r"\b(eval|benchmark|compare|validate|probe|gate|check|test)\b",t) or lb&{"evaluation","benchmark","validation"}: return "evaluation/benchmark"
    if re.search(r"\b(train|fine-tune|finetune|retrain|run)\b",t) or lb&{"training","experiment"}: return "training/experiment"
    if re.search(r"dataset|label|relabel|annotat|ctdp|enrich|pseudo-gt|curat|download|data",t) or lb&{"data","dataset","ctdp","label-quality","data-platform"}: return "data/labels"
    if re.search(r"tensorrt|trt|ptq|int8|quant|orin|export|onnx|docker|slurm|migrat|deploy",t) or lb&{"tensorrt","migration","deploy"}: return "export/infra"
    if re.search(r"helios|simulat|unreal|ue5|stream",t) or lb&{"helios","simulator"}: return "simulator/helios"
    return "other(impl/spec/docs)"
def fam(k):
    a=T[k]["top_actor"] or ""
    return "codex" if a.startswith("codex") else ("claude/copilot" if a.startswith(("claude","copilot")) else "named-agent")
print("\n== 3. WORK TYPE")
tab=C.defaultdict(lambda:[0,0])
for k in pop: tab[wtype(k)][0]+=1; tab[wtype(k)][1]+=y[k]
for t,(n,r) in sorted(tab.items(),key=lambda kv:-kv[1][1]/kv[1][0]): print(f"  {t:24} n={n:<4} real non-conv {r/n:>4.0%}  ({r})")
print("  permutation p for each type vs rest:")
for t in tab: show(t,lambda k,t=t:wtype(k)==t)
print("\n  agent family (overall):")
tf=C.defaultdict(lambda:[0,0])
for k in pop: tf[fam(k)][0]+=1; tf[fam(k)][1]+=y[k]
for t,(n,r) in tf.items(): print(f"    {t:16} n={n:<4} {r/n:>4.0%}")
print("  work type x agent family (n>=8 cells), real non-conv rate:")
cell=C.defaultdict(lambda:[0,0])
for k in pop: cell[(wtype(k),fam(k))][0]+=1; cell[(wtype(k),fam(k))][1]+=y[k]
for (t,f),(n,r) in sorted(cell.items()):
    if n>=8: print(f"    {t:24}{f:16} n={n:<4}{r/n:>5.0%}")

print("\n== robustness")
def bullets(k): return len(re.findall(r"^\s*(\d+[.)]|[-*])\s",body[k],re.M))
def ands(k): return len(re.findall(r"\band\b|;",body[k]))
for name,fn in [("bullets>=3",lambda k:bullets(k)>=3),("bullets>=5",lambda k:bullets(k)>=5),("bullets>=8",lambda k:bullets(k)>=8),
                ("and/; >=5",lambda k:ands(k)>=5),("and/; >=8",lambda k:ands(k)>=8),("and/; >=12",lambda k:ands(k)>=12),
                ("body length >1000",lambda k:len(body[k])>1000),("body length >2000",lambda k:len(body[k])>2000)]: show(name,fn)
print("  agent family:"); 
for f in ("codex","claude/copilot","named-agent"): show(f,lambda k,f=f:fam(k)==f)
# is the multi-part effect separate from codex?
print("  multi-part within families:")
for f in ("codex","claude/copilot","named-agent"):
    ks=[k for k in pop if fam(k)==f]; a=[k for k in ks if parts(k)]; b=[k for k in ks if not parts(k)]
    print(f"    {f:15} multi-part n={len(a):<3}{(sum(y[k] for k in a)/len(a) if a else 0):>5.0%} | other n={len(b):<3}{(sum(y[k] for k in b)/len(b) if b else 0):>5.0%}")
print("  codex share among multi-part:",f"{sum(fam(k)=='codex' for k in pop if parts(k))}/{sum(parts(k) for k in pop)}")
