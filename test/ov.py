import json,sys,time
d=json.load(sys.stdin); c=d["cluster"]
print("me=%s leader=%s quorum=%s %s/%s fenced=%s v%s" % (c["me"],c["leader"],c["has_quorum"],c["alive"],c["members"],c["fenced"],c["version"]))
for n in d["nodes"]: print("  node %s: online=%s down=%s services=%s" % (n["name"],n["online"],n["down_for"],n["services"]))
for s in d["services"]:
  sy=s["sync"]; print("  svc %s: node=%s status=%s phase=%s reason=%s sync_ago=%s err=%r starterr=%r" % (s["name"],s["node"],s["status"],s["phase"],s["reason"], round(time.time()-sy["last_ok"]) if sy["last_ok"] else None, sy["last_error"][:200], s["error"][:200]))
for e in d["events"][:7]: print("  ev", time.strftime("%H:%M:%S", time.localtime(e["ts"])), e["severity"], e["message"][:150])
