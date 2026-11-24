"""盘点索引 API 端到端测试（FastAPI TestClient + 临时 SQLite）。

运行：/tmp/venv/bin/python -I tests/test_inventory.py
"""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient  # noqa: E402


class InventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HANDOVER_DB"] = os.path.join(self.tmp.name, "test.db")
        for name in list(sys.modules):
            if name.startswith("app."):
                del sys.modules[name]
        from app.main import app  # noqa: E402

        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    def add_evidence(self, eid, a, b):
        r = self.client.post(
            "/evidence", json={"evidence_id": eid, "reg_a": a, "reg_b": b}
        )
        self.assertEqual(r.status_code, 201, r.text)

    def supplied_pair(self, prefix, a, b):
        """录入两条独立链，让 a-b 成为正在供数的配对。"""
        for suffix, mid in (("1", "m1"), ("2", "m2")):
            self.add_evidence(f"{prefix}{suffix}a", a, f"{prefix}{mid}")
            self.add_evidence(f"{prefix}{suffix}b", f"{prefix}{mid}", b)
        r = self.client.post("/handovers", json={"reg_a": a, "reg_b": b})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["status"], "supplied", r.text)
        return r.json()

    def publish(self):
        r = self.client.post("/inventory/publish")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def lookup(self, query):
        return self.client.get(f"/inventory/{query}")

    # ------------------------------------------------------------------
    def test_publish_assigns_keys_and_groups_connected_pairs(self):
        self.supplied_pair("AB", "A", "B")
        self.supplied_pair("CD", "C", "D")
        # 待补交接：两端登记号也入索引，但没连上供数配对 -> 各自成组
        r = self.client.post("/handovers", json={"reg_a": "E", "reg_b": "F"})
        self.assertEqual(r.json()["status"], "pending")

        batch = self.publish()
        self.assertEqual(batch["handover_count"], 3)
        self.assertEqual(batch["supplied_count"], 2)
        self.assertEqual(batch["member_count"], 6)
        self.assertEqual(batch["group_count"], 4)  # AB、CD、E、F
        self.assertEqual(batch["new_key_count"], 6)

        # 首次入索引的键按登记号升序分配
        a = self.lookup("A").json()
        self.assertEqual(a["specimen_key"], "SK-00000001")
        self.assertEqual(a["group_key"], "SK-00000001")
        self.assertEqual([m["reg"] for m in a["members"]], ["A", "B"])
        self.assertEqual(self.lookup("B").json()["specimen_key"],
                         "SK-00000002")
        self.assertEqual(self.lookup("C").json()["group_key"],
                         "SK-00000003")
        self.assertEqual(self.lookup("E").json()["group_key"],
                         "SK-00000005")
        self.assertEqual([m["reg"] for m in
                          self.lookup("E").json()["members"]], ["E"])
        self.assertEqual(self.lookup("F").json()["reg"], "F")

        # 按永久键、按组键都能查到同一登记号的当前归属与全组成员
        by_key = self.lookup("SK-00000002").json()
        self.assertEqual(by_key["reg"], "B")
        self.assertEqual(by_key["group_key"], "SK-00000001")
        self.assertEqual({m["reg"] for m in by_key["members"]}, {"A", "B"})
        by_group = self.lookup("SK-00000001").json()
        self.assertEqual(by_group["reg"], "A")
        self.assertEqual({m["reg"] for m in by_group["members"]}, {"A", "B"})

        # 列表接口按组返回
        listing = self.client.get("/inventory").json()
        self.assertEqual(listing["batch"]["batch_id"], batch["batch_id"])
        self.assertEqual(len(listing["groups"]), 4)

        # 查不到
        self.assertEqual(self.lookup("NOPE").status_code, 404)

    def test_merge_existing_groups_keeps_keys_and_earliest_group_key(self):
        self.supplied_pair("AB", "A", "B")
        self.supplied_pair("CD", "C", "D")
        self.publish()
        self.assertEqual(self.lookup("A").json()["group_key"], "SK-00000001")
        self.assertEqual(self.lookup("C").json()["group_key"], "SK-00000003")

        # 后补 B-C 供数配对，把两组连起来
        self.supplied_pair("BC", "B", "C")
        batch = self.publish()
        self.assertEqual(batch["new_key_count"], 0)
        self.assertEqual(batch["group_count"], 1)

        merged = self.lookup("A").json()
        self.assertEqual(merged["group_key"], "SK-00000001")  # 最早分配的键
        self.assertEqual({m["reg"] for m in merged["members"]},
                         {"A", "B", "C", "D"})
        # 旧明细里留的 C 的永久键仍指向 C，且给出当前组键
        c = self.lookup("SK-00000003").json()
        self.assertEqual(c["reg"], "C")
        self.assertEqual(c["specimen_key"], "SK-00000003")
        self.assertEqual(c["group_key"], "SK-00000001")
        self.assertEqual({m["reg"] for m in c["members"]},
                         {"A", "B", "C", "D"})

    def test_split_group_after_supply_pair_pauses(self):
        self.supplied_pair("AB", "A", "B")
        self.supplied_pair("BC", "B", "C")
        self.publish()
        self.assertEqual({m["reg"] for m in
                          self.lookup("A").json()["members"]},
                         {"A", "B", "C"})
        self.assertEqual(self.lookup("A").json()["group_key"], "SK-00000001")

        # 撤回 B-C 当前依据中的一条证据 -> B-C 暂停供数 -> 拆组
        bc = self.client.get("/handovers").json()
        bc = [h for h in bc if {h["reg_a"], h["reg_b"]} == {"B", "C"}][0]
        victim = bc["chain1"]["evidence"][0]
        r = self.client.post(f"/evidence/{victim}/withdraw")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            [h["status"] for h in r.json()["handovers"]], ["paused"])

        batch = self.publish()
        self.assertEqual(batch["supplied_count"], 1)
        self.assertEqual(batch["group_count"], 2)
        ab = self.lookup("A").json()
        self.assertEqual({m["reg"] for m in ab["members"]}, {"A", "B"})
        self.assertEqual(ab["group_key"], "SK-00000001")
        # C 的永久键不变，拆组后成为自己组的组键
        c = self.lookup("SK-00000003").json()
        self.assertEqual(c["reg"], "C")
        self.assertEqual(c["group_key"], "SK-00000003")
        self.assertEqual([m["reg"] for m in c["members"]], ["C"])
        # B-C 的两端仍在索引里（交接申请仍存在，只是暂停）
        self.assertEqual(self.lookup("B").json()["reg"], "B")

    def test_new_keys_continue_after_existing_and_group_by_earliest(self):
        self.supplied_pair("CD", "C", "D")
        self.publish()
        self.assertEqual(self.lookup("C").json()["specimen_key"],
                         "SK-00000001")

        # 后一批出现字典序更小的 A：新键在已有键之后分配，不按字典序回填
        self.supplied_pair("AB", "A", "B")
        batch = self.publish()
        self.assertEqual(batch["new_key_count"], 2)
        self.assertEqual(self.lookup("A").json()["specimen_key"],
                         "SK-00000003")
        self.assertEqual(self.lookup("B").json()["specimen_key"],
                         "SK-00000004")

        # 把 A-B 与 C-D 连成一组：组键取最早分配的 C 的键
        self.supplied_pair("BC", "B", "C")
        self.publish()
        merged = self.lookup("A").json()
        self.assertEqual(merged["group_key"], "SK-00000001")
        self.assertEqual({m["reg"] for m in merged["members"]},
                         {"A", "B", "C", "D"})

    def test_verification_conflict_keeps_previous_index(self):
        self.supplied_pair("AB", "A", "B")
        first = self.publish()

        from app import db as db_module

        original = db_module.Database._connect_write

        def racing(self):
            # 模拟取数之后、发布写事务之前，另一个人提交了新的交接
            self.create_handover("C", "D")
            return original(self)

        db_module.Database._connect_write = racing
        try:
            r = self.client.post("/inventory/publish")
        finally:
            db_module.Database._connect_write = original

        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("沿用上期", r.json()["detail"])
        # 上期索引原样可查，没有新批次、没有为 C/D 分配键
        listing = self.client.get("/inventory").json()
        self.assertEqual(listing["batch"]["batch_id"], first["batch_id"])
        self.assertEqual(self.lookup("A").json()["group_key"],
                         "SK-00000001")
        self.assertEqual(self.lookup("C").status_code, 404)

        # 冲突消除后可正常发布，C/D 作为独立组入索引
        batch = self.publish()
        self.assertEqual(batch["new_key_count"], 2)
        self.assertEqual(self.lookup("C").json()["group_key"],
                         "SK-00000003")

    def test_write_failure_rolls_back_index_and_keys(self):
        self.supplied_pair("AB", "A", "B")
        first = self.publish()

        # 增加新登记号，再让发布在写入中途失败
        self.supplied_pair("CD", "C", "D")

        from app import db as db_module

        original = db_module._build_inventory_plan

        def broken(snapshot):
            plan = original(snapshot)
            # 让同一批新键出现重复分配序号，写入键表时违反唯一约束
            plan["new_keys"] = plan["new_keys"] + plan["new_keys"][:1]
            return plan

        db_module._build_inventory_plan = broken
        try:
            r = self.client.post("/inventory/publish")
        finally:
            db_module._build_inventory_plan = original

        self.assertEqual(r.status_code, 500)
        # 索引、键表、批次一起回滚：上期内容可查，C/D 无键
        listing = self.client.get("/inventory").json()
        self.assertEqual(listing["batch"]["batch_id"], first["batch_id"])
        self.assertEqual(self.lookup("A").json()["group_key"],
                         "SK-00000001")
        self.assertEqual(self.lookup("C").status_code, 404)
        # 回滚未占号：下次成功发布时 C/D 仍按升序领 SK-3、SK-4
        batch = self.publish()
        self.assertEqual(batch["new_key_count"], 2)
        self.assertEqual(self.lookup("C").json()["specimen_key"],
                         "SK-00000003")

    def test_publish_before_any_index_and_lookup_404(self):
        self.assertEqual(self.lookup("A").status_code, 404)
        listing = self.client.get("/inventory").json()
        self.assertIsNone(listing["batch"])
        self.assertEqual(listing["groups"], [])
        batch = self.publish()
        self.assertEqual(batch["member_count"], 0)
        self.assertEqual(self.client.get("/inventory").json()["groups"], [])

    def test_query_works_while_publish_is_grouping(self):
        """编组期间：已发布索引照常可查，交接照常可办。"""
        self.supplied_pair("AB", "A", "B")
        first = self.publish()

        from app import db as db_module

        entered = threading.Event()
        release = threading.Event()
        original = db_module._build_inventory_plan

        def slow(snapshot):
            entered.set()
            self.assertTrue(release.wait(timeout=10))
            return original(snapshot)

        db_module._build_inventory_plan = slow
        result = {}

        def run_publish():
            result["response"] = self.client.post("/inventory/publish")

        thread = threading.Thread(target=run_publish)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=10))
            # 编组阶段不持写锁：查询读到的是上期索引
            during = self.client.get("/inventory")
            self.assertEqual(during.status_code, 200, during.text)
            self.assertEqual(during.json()["batch"]["batch_id"],
                             first["batch_id"])
            # 编组期间仍可办理交接；这会改变登记号集合，故本次发布
            # 核对失败、沿用上期，而不是把半份数据写进索引。
            r = self.client.post("/handovers",
                                 json={"reg_a": "C", "reg_b": "D"})
            self.assertEqual(r.status_code, 201, r.text)
        finally:
            release.set()
            thread.join(timeout=20)

        self.assertEqual(result["response"].status_code, 409,
                         result["response"].text)
        # 上期索引原样可查
        listing = self.client.get("/inventory").json()
        self.assertEqual(listing["batch"]["batch_id"], first["batch_id"])
        self.assertEqual(self.lookup("A").json()["group_key"],
                         "SK-00000001")
        self.assertEqual(self.lookup("C").status_code, 404)
        # 重新发布后把编组期间新办的交接纳入
        batch = self.publish()
        self.assertEqual(batch["handover_count"], 2)
        self.assertEqual(self.lookup("C").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
