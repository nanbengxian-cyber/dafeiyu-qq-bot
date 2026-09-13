import tempfile
import unittest
from pathlib import Path
from dsh_action import ActionStore, related

class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ActionStore(Path(self.tmp.name)/'action.db')
        for i in range(2):
            self.store.consider('g','u',str(i),'路由器信号测试',1000+i,eligible=False)
    def tearDown(self):
        self.tmp.cleanup()
    def reserve(self):
        return self.store.consider('g','u','3','路由器信号测试',1003)[0]
    def test_success_not_attempt_opens_window(self):
        p = self.reserve()
        self.assertNotIn('window',self.store.snapshot('g'))
        self.assertTrue(self.store.sent('g',p['token'],1004))
        self.assertFalse(self.store.sent('g',p['token'],1005))
        self.assertEqual(self.store.snapshot('g')['opens'],1)
    def test_restart_dedup(self):
        self.reserve()
        other = ActionStore(self.store.path)
        self.assertEqual(other.consider('g','u','3','路由器信号测试',1005)[1],'duplicate')
    def test_failure_release(self):
        p=self.reserve()
        self.store.release('g',p['token'])
        self.assertNotIn('window',self.store.snapshot('g'))
        self.assertIsNotNone(self.store.consider('g','u','4','路由器信号测试',1005)[0])
    def test_related_continuation(self):
        p=self.reserve(); self.store.sent('g',p['token'],1004)
        p,r=self.store.consider('g','u','4','路由器信号测试结果出来了',1030,eligible=False)
        self.assertTrue(p['continuation'],r)
    def test_unrelated_not_continuation(self):
        p=self.reserve(); self.store.sent('g',p['token'],1004)
        self.assertIsNone(self.store.consider('g','u','4','今天买了苹果',1030,eligible=False)[0])
    def test_boundary_removes_personal_candidates(self):
        self.reserve()
        self.store.consider('g','u','4','路由器信号测试',1010,forbidden=True)
        self.assertFalse(self.store.snapshot('g')['queue'])
    def test_waiting_candidate(self):
        p=self.reserve(); self.store.sent('g',p['token'],1004)
        self.store.consider('g','v','5','火锅牛肉味道很好',1020)
        self.assertTrue(self.store.snapshot('g')['queue'])
        p,r=self.store.consider('g','v','6','火锅牛肉味道确实不错',1200)
        self.assertEqual(p['motive'],'deferred_interest',r)
    def test_revoke_clears_pending_and_window(self):
        p=self.reserve(); self.store.sent('g',p['token'],1004)
        self.store.revoke('g','u')
        s=self.store.snapshot('g')
        self.assertNotIn('window',s); self.assertFalse(s['queue'])

    def test_ignored_requires_activity_filter(self):
        p=self.reserve(); self.store.sent('g',p['token'],1004)
        # quiet group, no later traffic: not counted as rejection
        self.store.consider('g','u','9','无关内容',1500,eligible=True,cooldown=0)
        self.assertEqual(self.store.snapshot('g')['ignored'],0)
    def test_legacy_pending_row_without_continuation_key(self):
        # 旧 schema 写下的 pending 缺 continuation/uid 字段时不能崩。
        p=self.reserve()
        con=self.store.connect()
        with con:
            con.execute('BEGIN IMMEDIATE')
            s=self.store._load(con,'g')
            s['pending']=dict(token=p['token'], expires=10**12)
            self.store._save(con,'g',s)
        self.assertTrue(self.store.sent('g',p['token'],2000))
        self.assertTrue(self.store.valid('g','x',0) is False)

    def test_group_isolation(self):
        self.reserve()
        self.assertFalse(self.store.snapshot('other')['queue'])
    def test_expired_pending(self):
        p=self.reserve()
        self.assertFalse(self.store.valid('g',p['token'],1200))
    def test_topic_requires_evidence(self):
        self.assertFalse(related('然后呢','路由器信号测试'))

if __name__=='__main__':
    unittest.main()
