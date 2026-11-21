"""Execution grouping only: never replaces canonical paid jobs or their terms."""
import hashlib
from .operations import canonical


def register(worker,key,created_at,deadline,origin):
    box=worker.inbox
    box.db.execute("BEGIN IMMEDIATE")
    try:
        record=box.get("obligation:"+key) or dict(created_at=created_at,deadline=deadline,origins={})
        old=record["origins"].get(origin)
        if old is not None and old!=deadline: raise ValueError("ZK_ORIGINAL_DEADLINE_CHANGED")
        record["origins"][origin]=deadline; record["deadline"]=min(record["deadline"],deadline)
        box.put("obligation:"+key,record); box.db.commit()
    except BaseException: box.db.rollback();raise


def select(worker,observations,now):
    if not worker.c.get("batching"):return None
    box=worker.inbox; active=box.get("execution_group")
    if active and active["state"]=="ACTIVE":
        statuses={k:s for k,_,s in box.needs()}
        if all(statuses.get(k) in ("READY","COMPLETED") for k in active["need_ids"]):
            active["state"]="COVERED";box.put("group:"+active["id"],active);box.put("execution_group",active);active=None
    if not observations:return None
    ids=[key for key,_ in observations]
    for key in ids:
        if box.get("obligation:"+key) is None:
            register(worker,key,now,now+int(worker.c["batching"]["local_service_seconds"]),"local")
    box.db.execute("BEGIN IMMEDIATE")
    try:
        if not active or active["state"]!="ACTIVE":
            sequence=box.get("group_sequence") or 0
            group_id=hashlib.sha256(canonical(["SATOSHIFI_SPV_EXECUTION_GROUP_V1",box.get("scope"),sequence,sorted(ids)]).encode()).hexdigest()
            active=dict(id=group_id,need_ids=[],deadline=min(box.get("obligation:"+key)["deadline"] for key in ids),
                created_at=now,payment_jobs=[],attempts=[],state="ACTIVE")
            box.put("group_sequence",sequence+1)
        active["need_ids"]=sorted(set(active["need_ids"])|set(ids))
        deadlines=[box.get("obligation:"+key)["deadline"] for key in active["need_ids"]]
        deadlines.extend(int(box.get("service:"+key)["deadline"]) for key in active["need_ids"] if box.get("service:"+key))
        active["deadline"]=min(active["deadline"],*deadlines)
        active["payment_jobs"]=sorted(set(active["payment_jobs"])|{
            box.get("service:"+key)["service_job_id"] for key in active["need_ids"] if box.get("service:"+key)})
        for key in ids:box.put("need_group:"+key,active["id"])
        box.put("execution_group",active);box.put("group:"+active["id"],active);box.db.commit()
    except BaseException:box.db.rollback();raise
    return active


def attempt(worker,group,attempt_id):
    if not group:return
    if attempt_id not in group["attempts"]:group["attempts"].append(attempt_id)
    worker.inbox.put("execution_group",group);worker.inbox.put("group:"+group["id"],group)
