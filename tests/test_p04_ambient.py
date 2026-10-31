"""自然浮现与余韵的隔离合成验收，不调用模型。"""
import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import unittest

from thalemora.lifecycle import AmbientEngine


class AmbientEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ambient.sqlite'
        self.engine = AmbientEngine(self.path)
        self.now = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)
        self.rows = [self.card(1)]

    def card(self, cid, **changes):
        row = dict(card_id=cid, revision=1, current_revision_id=1,
                   status='fresh', importance=10, affinity=.8, arousal=.3,
                   keywords=['海边'], created_at=self.now.isoformat())
        row.update(changes)
        return row

    def step(self, key, **changes):
        args = dict(epoch='epoch', verified_memory_rows=self.rows,
                    now=self.now.isoformat(), enabled=True, seed=20,
                    context_text='今天又想去海边走走')
        args.update(changes)
        return self.engine.step(key, **args)

    def echoes(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(
                'SELECT card_id, strength, unrelated FROM ambient_echoes '
                'ORDER BY card_id').fetchall()

    def test_default_disabled_does_not_create_database(self):
        self.assertFalse(self.path.exists())
        result = self.engine.step('off', epoch='epoch', verified_memory_rows=self.rows,
                                  now=self.now.isoformat())
        self.assertEqual(result, dict(ambient=None, resonance=[], reason='disabled'))
        self.assertFalse(self.path.exists())

    def test_seed_and_real_topic_cue(self):
        result = self.step('cue')
        self.assertEqual(result['ambient']['card_id'], 1)
        self.assertIn('topic_cue', result['ambient']['reason'])
        other = AmbientEngine(Path(self.tmp.name) / 'repeat.sqlite')
        self.assertEqual(result, other.step(
            'cue', epoch='epoch', verified_memory_rows=self.rows,
            now=self.now.isoformat(), enabled=True, seed=20,
            context_text='今天又想去海边走走'))
        no_cue = self.step('no-cue', epoch='other',
                           verified_memory_rows=[self.card(2)], context_text='今天很平常')
        self.assertIsNone(no_cue['ambient'])

    def test_proactive_without_topic_cue_uses_proactive_probability(self):
        # 没有话题线索的主动回忆不应再叠加普通闲聊的 8% 稀有门槛。
        proactive = self.step('proactive-no-cue', epoch='proactive',
                              mode='proactive', context_text='平常的一天')
        self.assertEqual(1, proactive['ambient']['card_id'])
        normal = self.step('normal-no-cue', epoch='normal', context_text='平常的一天')
        self.assertIsNone(normal['ambient'])

    def test_related_focus_correction_and_distress_suppress(self):
        self.assertIsNotNone(self.step('start')['ambient'])
        for key, changes, reason in (
            ('related', dict(related_card_ids=[99]), 'related_present'),
            ('focus', dict(context_text='先修复海边相关代码'), 'focused_task'),
            ('correction', dict(context_text='海边的事情你记反了'), 'correction'),
            ('distress', dict(context_text='想到海边我很难受'), 'acute_distress'),
            ('distress_sad', dict(context_text='刚才的事让我很难过'), 'acute_distress'),
            ('correction_denial', dict(context_text='不是让你讲海边这个'), 'correction'),
            ('focused_direct', dict(context_text='请直接解释海边这段代码'), 'focused_task'),
        ):
            with self.subTest(key=key):
                result = self.step(key, **changes)
                self.assertIsNone(result['ambient'])
                self.assertEqual(result['resonance'], [])
                self.assertEqual(result['reason'], reason)

    def test_cooldown_and_epoch_generation_idempotence(self):
        first = self.step('same')
        self.assertIsNotNone(first['ambient'])
        before = self.echoes()
        self.assertEqual(first, self.step('same', spoken_card_ids=[1]))
        self.assertEqual(before, self.echoes())
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM ambient_turns').fetchone()[0], 1)
        blocked = self.step('same', epoch='new',
                            now=(self.now + dt.timedelta(hours=72, seconds=-1)).isoformat())
        self.assertIsNone(blocked['ambient'])
        eligible = self.step('same', epoch='third',
                             now=(self.now + dt.timedelta(hours=72)).isoformat())
        self.assertEqual(eligible['ambient']['card_id'], 1)

    def test_echo_decay_spoken_capacity_and_unrelated_expiry(self):
        self.assertIsNotNone(self.step('start')['ambient'])
        first = self.step('turn1')
        self.assertAlmostEqual(first['resonance'][0]['strength'], .72)
        second = self.step('turn2')
        self.assertEqual(second['resonance'], [])
        self.assertAlmostEqual(self.echoes()[0][1], .72 ** 2)
        self.step('turn3')
        self.assertEqual(self.echoes()[0][2], 3)
        self.step('turn4')
        self.assertEqual(self.echoes(), [])

        self.rows = [self.card(2), self.card(3), self.card(4)]
        self.assertIsNotNone(self.step('fresh')['ambient'])
        active = self.echoes()[0][0]
        self.step('spoken', spoken_card_ids=[active])
        saved = {cid: strength for cid, strength, _ in self.echoes()}
        self.assertAlmostEqual(saved[active], .72 * .45)
        # 真实用户重提维持余韵，第三张卡不能挤入已有两条的状态。
        for turn in range(3):
            result = self.step(f'capacity{turn}', mode='proactive',
                               user_reactivated_card_ids=[2, 3, 4])
            self.assertLessEqual(len(result['resonance']), 2)
            self.assertLessEqual(len(self.echoes()), 2)
        self.assertEqual(len(self.echoes()), 2)

    def test_affinity_arousal_revision_and_visible_filters(self):
        for label, changes in (
            ('affinity', dict(affinity=.34)),
            ('arousal', dict(affinity=None, arousal=.85)),
            ('revision', dict(revision=1, current_revision_id=2)),
        ):
            with self.subTest(label=label):
                self.assertIsNone(self.step(label, verified_memory_rows=[self.card(1, **changes)])['ambient'])
        self.assertIsNone(self.step('visible', visible_card_ids=[1])['ambient'])
        self.assertIsNotNone(self.step('valid')['ambient'])
        for key in ('valid', 'next'):
            with self.subTest(replay=key):
                result = self.step(key, visible_card_ids=[1])
                self.assertIsNone(result['ambient'])
                self.assertEqual(result['resonance'], [])
                result = self.step(key, verified_memory_rows=[self.card(1, revision=2, current_revision_id=2)])
                self.assertIsNone(result['ambient'])
                self.assertEqual(result['resonance'], [])


if __name__ == '__main__':
    unittest.main()
