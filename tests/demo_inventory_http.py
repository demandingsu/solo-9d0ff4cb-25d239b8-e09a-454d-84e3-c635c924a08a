"""通过真实 HTTP 试办盘点索引：发布 -> 合组 -> 拆组 -> 编组期间查询/办理。

用法：
  HANDOVER_DB=/tmp/demo_inv.db /tmp/venv/bin/python -m uvicorn app.main:app &
  /tmp/venv/bin/python -I tests/demo_inventory_http.py
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def show(title, payload):
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=10)

    def evidence(eid, a, b):
        c.post("/evidence", json={"evidence_id": eid, "reg_a": a,
                                  "reg_b": b}).raise_for_status()

    def supplied_pair(prefix, a, b):
        for suffix, mid in (("1", "m1"), ("2", "m2")):
            evidence(f"{prefix}{suffix}a", a, f"{prefix}{mid}")
            evidence(f"{prefix}{suffix}b", f"{prefix}{mid}", b)
        r = c.post("/handovers", json={"reg_a": a, "reg_b": b})
        r.raise_for_status()
        assert r.json()["status"] == "supplied", r.text

    # 两组正在供数的配对，外加一对待补交接
    supplied_pair("AB", "A", "B")
    supplied_pair("CD", "C", "D")
    r = c.post("/handovers", json={"reg_a": "E", "reg_b": "F"})
    assert r.json()["status"] == "pending"

    r = c.post("/inventory/publish")
    r.raise_for_status()
    show("首次发布（4 组：A-B、C-D、E、F）", r.json())
    a_key = c.get("/inventory/A").json()["specimen_key"]

    # 合组：B-C 变成供数配对，两组并成一组，组键取最早分配的成员键
    supplied_pair("BC", "B", "C")
    r = c.post("/inventory/publish")
    r.raise_for_status()
    show("合组后发布（A/B/C/D 一组，新键 0）", r.json())
    merged = c.get(f"/inventory/{a_key}").json()
    show(f"用旧明细里的键 {a_key} 查合组后归属", merged)
    assert {m["reg"] for m in merged["members"]} == {"A", "B", "C", "D"}
    assert merged["group_key"] == a_key

    # 拆组：撤回 B-C 当前依据的一条证据 -> B-C 暂停 -> 重新发布后拆开
    handovers = c.get("/handovers").json()
    bc = [h for h in handovers if {h["reg_a"], h["reg_b"]} == {"B", "C"}][0]
    victim = bc["chain1"]["evidence"][0]
    c.post(f"/evidence/{victim}/withdraw").raise_for_status()
    r = c.post("/inventory/publish")
    r.raise_for_status()
    show("拆组后发布", r.json())
    b = c.get("/inventory/B").json()
    show("B 的当前归属（组键仍是 A 的键，因 A、B 仍供数）", b)
    c_res = c.get("/inventory/C").json()
    show("C 的当前归属（C-D 仍供数，C 的永久键作这组组键）", c_res)
    assert {m["reg"] for m in b["members"]} == {"A", "B"}
    assert {m["reg"] for m in c_res["members"]} == {"C", "D"}
    assert c_res["group_key"] == "SK-00000003"

    # 编组期间查询与办理都可用（这里按顺序演示：先补证重核、再发布）
    evidence("EF2a", "E", "Em1")
    evidence("EF2b", "Em1", "F")
    evidence("EF3a", "E", "Em2")
    evidence("EF3b", "Em2", "F")
    handovers = c.get("/handovers").json()
    ef = [h for h in handovers if {h["reg_a"], h["reg_b"]} == {"E", "F"}][0]
    r = c.post(f"/handovers/{ef['id']}/reverify")
    r.raise_for_status()
    assert r.json()["status"] == "supplied"
    r = c.post("/inventory/publish")
    r.raise_for_status()
    e = c.get("/inventory/E").json()
    show("E-F 转为供数后发布，E 的组", e)
    assert {m["reg"] for m in e["members"]} == {"E", "F"}

    listing = c.get("/inventory").json()
    show("当前已发布索引一览", listing)
    assert listing["batch"]["batch_id"] == r.json()["batch_id"]

    print("\n全部盘点索引试办步骤通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
