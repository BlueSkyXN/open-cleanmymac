"""Observable behavior and operation counts for the slimming changes."""
from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

from openclean import engine, processes
from openclean.core import serialization
from openclean.core.models import Action, Detector, Provenance, Strategy, StrategyPack
from openclean.models import Item, ScanIssue, ScanResult
from openclean.progress import ProgressTaskSpec, WeightedProgress
from openclean.strategies import registry


@dataclass
class _Leaf:
    value: int
    tags: list[str] = field(default_factory=list)


class OverlapSlimTests(unittest.TestCase):
    def test_nearest_parent_subtracts_each_subtree_once(self):
        root = Item(Path('/fixture'), 100, 'root')
        child = Item(Path('/fixture/gap/child'), 30, 'child')
        leaf = Item(Path('/fixture/gap/child/leaf'), 10, 'leaf')
        result = engine.finalize_overlapping_result(ScanResult([leaf, root, child]))
        by_category = {item.category: item for item in result.items}
        self.assertEqual({k: v.size for k, v in by_category.items()}, {'root': 70, 'child': 20, 'leaf': 10})
        self.assertFalse(by_category['root'].actionable)
        self.assertFalse(by_category['child'].actionable)
        self.assertTrue(by_category['leaf'].actionable)
        self.assertEqual(root.size, 100)

    def test_normal_paths_do_not_use_pairwise_descendant_comparisons(self):
        items = [Item(Path(f'/fixture/cache-{i}'), 1, 'cache') for i in range(300)]
        with mock.patch.object(engine, '_is_descendant', side_effect=AssertionError('quadratic path')):
            result = engine.finalize_overlapping_result(ScanResult(items))
        self.assertEqual(result.total, 300)
        self.assertEqual(len(result.items), 300)

    def test_relative_dot_remains_unrelated_to_relative_children(self):
        root, child = Item(Path('.'), 100, 'root'), Item(Path('a/b'), 30, 'child')
        result = engine.finalize_overlapping_result(ScanResult([root, child]))
        self.assertEqual(result.total, 130)
        self.assertTrue(all(item.actionable for item in result.items))

    def test_double_slash_preserves_original_order_sensitive_counting(self):
        root = Item(Path('/a/b'), 1000, 'root')
        double = Item(Path('//a/b/c'), 100, 'double')
        regular = Item(Path('/a/b/c'), 200, 'regular')
        for candidates, root_size in (([root, double, regular], 700), ([root, regular, double], 800)):
            with self.subTest(order=[i.category for i in candidates]):
                result = engine.finalize_overlapping_result(ScanResult(candidates))
                self.assertEqual(next(i.size for i in result.items if i.category == 'root'), root_size)

    def test_subset_diagnostics_do_not_own_entire_subtrees(self):
        root = Item(Path('/fixture'), 100, 'root')
        subset = Item(Path('/fixture/subset'), 20, 'subset', actionable=False,
                      resource_kind='filesystem_subset', diagnostic_kind='codex_transient')
        leaf = Item(Path('/fixture/subset/leaf'), 10, 'leaf')
        issue = ScanIssue('fixture', 'partial')
        original = ScanResult([root, subset, leaf], [issue], cancelled=True)
        result = engine.finalize_overlapping_result(original)
        by_category = {i.category: i for i in result.items}
        self.assertEqual(by_category['root'].size, 90)
        self.assertIs(by_category['subset'], subset)
        self.assertFalse(result.complete)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.issues, [issue])
        self.assertIsNot(result.issues, original.issues)

    def test_equal_priority_keeps_first_and_readonly_wins_tie(self):
        first = Item(Path('/fixture'), 30, 'first', domain='ai')
        second = replace(first, size=40, category='second')
        diagnostic = replace(first, actionable=False, diagnostic_kind='retention', category='readonly')
        self.assertEqual(engine.finalize_overlapping_result(ScanResult([first, second])).items, [first])
        self.assertEqual(engine.finalize_overlapping_result(ScanResult([second, diagnostic, first])).items, [diagnostic])


class ProgressSlimTests(unittest.TestCase):
    @staticmethod
    def exercise(progress):
        progress.start()
        progress.task('a').advance(3)
        progress.task('a').set_fraction(0.6)
        progress.task('a').complete()
        progress.task('b').fail()
        progress.task('c').cancel()
        progress.cancel()

    def test_no_callback_skips_snapshot_construction_but_preserves_state(self):
        specs = tuple(ProgressTaskSpec(name, name) for name in 'abcd')
        quiet, observed = WeightedProgress(specs), WeightedProgress(specs, callback=lambda _: None)
        with mock.patch.object(quiet, '_snapshot_locked', side_effect=AssertionError('unused snapshot')):
            self.exercise(quiet)
        self.exercise(observed)
        self.assertEqual(quiet.snapshot(), observed.snapshot())

    def test_falsey_observer_still_receives_every_update(self):
        class Observer(list):
            def __bool__(self):
                return False

            def __call__(self, snapshot):
                self.append(snapshot)

        observer = Observer()
        progress = WeightedProgress((ProgressTaskSpec('a', 'a'),), callback=observer)
        progress.start()
        progress.task('a').advance()
        progress.task('a').complete()
        self.assertEqual(len(observer), 3)
        self.assertEqual([s.sequence for s in observer], [0, 1, 2])

    def test_no_callback_concurrent_updates_remain_exact(self):
        progress = WeightedProgress((ProgressTaskSpec('a', 'a'),))
        def advance():
            for _ in range(100):
                progress.task('a').advance()
        threads = [threading.Thread(target=advance) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(progress.snapshot().processed_items, 400)
        self.assertEqual(progress.snapshot().sequence, 400)


class DecoderSlimTests(unittest.TestCase):
    def test_annotations_resolve_once_per_class_per_decode(self):
        with mock.patch.object(serialization, 'get_type_hints', wraps=serialization.get_type_hints) as hints:
            values = serialization.decode_value(tuple[_Leaf, ...], [{'value': i} for i in range(30)])
        self.assertEqual(hints.call_count, 1)
        self.assertEqual([v.value for v in values], list(range(30)))
        self.assertIsNot(values[0].tags, values[1].tags)

    def test_annotations_are_not_cached_between_calls(self):
        self.assertEqual(serialization.decode_dataclass(_Leaf, {'value': 3}).value, 3)
        with mock.patch.dict(_Leaf.__annotations__, {'value': str}):
            self.assertEqual(serialization.decode_dataclass(_Leaf, {'value': 'new'}).value, 'new')
            with self.assertRaises(ValueError):
                serialization.decode_dataclass(_Leaf, {'value': 3})
        self.assertEqual(serialization.decode_dataclass(_Leaf, {'value': 3}).value, 3)

    def test_list_and_tuple_preserve_type_and_do_not_alias_inputs(self):
        raw = [{'value': 1, 'tags': ['a']}]
        for annotation, expected_type in ((list[_Leaf], list), (tuple[_Leaf, ...], tuple)):
            result = serialization.decode_value(annotation, raw)
            self.assertIs(type(result), expected_type)
            result[0].tags.append('b')
            self.assertEqual(raw[0]['tags'], ['a'])

    def test_required_defaults_boolean_and_unknown_fields_still_fail(self):
        cases = [({'value': 1}, True, 'missing tags'),
                 ({'value': True}, False, 'invalid value'),
                 ({'value': 1, 'extra': 2}, False, 'unknown fields')]
        for data, require_all, message in cases:
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, message):
                serialization.decode_dataclass(_Leaf, data, require_all=require_all)

    def test_union_selection_and_failure_text_are_unchanged(self):
        self.assertEqual(serialization.decode_value(int | str, '3'), '3')
        self.assertIsNone(serialization.decode_value(int | None, None))
        with self.assertRaisesRegex(ValueError, 'value: invalid value for int \\| None'):
            serialization.decode_value(int | None, True)


class AllocationSlimTests(unittest.TestCase):
    def test_duplicate_lsof_handles_allocate_one_aggregate_per_inode(self):
        record = 'f1\nD0x1\ni2\ns100\nn/fixture/deleted (deleted)\n'
        with mock.patch.object(processes, '_DeletedOpenAggregate', wraps=processes._DeletedOpenAggregate) as factory:
            snapshot = processes.parse_deleted_open_files('p42\ncfixture\n' + record * 100)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(len(snapshot.files), 1)
        self.assertEqual(snapshot.files[0].handle_count, 100)
        self.assertEqual(snapshot.files[0].logical_size, 100)

    def test_external_pack_is_downgraded_once_and_still_readonly(self):
        strategy = Strategy(id='codex.fixture', version=1, pack='codex', status='trusted',
                            detector=Detector('codex_transient'), action=Action('move_to_trash', True),
                            provenance=(Provenance('personal_experience'),))
        pack = StrategyPack('codex', (strategy,))
        with mock.patch.object(registry, 'load_pack', return_value=pack), \
             mock.patch.object(registry, '_downgrade_external_pack', wraps=registry._downgrade_external_pack) as downgrade:
            loaded = registry.StrategyRegistry.load(('codex',), packs_dir=Path('/fixture'))
        self.assertEqual(downgrade.call_count, 1)
        self.assertEqual(loaded.get(strategy.id).status, 'active')
        self.assertFalse(loaded.get(strategy.id).action.supported)
        self.assertFalse(loaded.action_approved(loaded.get(strategy.id)))

    def test_strategy_lookup_keeps_highest_version_across_nested_pack_names(self):
        common = dict(detector=Detector('retention'), action=Action('report_only'),
                      provenance=(Provenance('personal_experience'),), status='active')
        first = Strategy(id='a.b.cache', version=1, pack='a', **common)
        second = Strategy(id='a.b.cache', version=2, pack='a.b', **common)
        loaded = registry.StrategyRegistry((StrategyPack('a', (first,)), StrategyPack('a.b', (second,))))
        self.assertEqual(loaded.get('a.b.cache'), second)
        with self.assertRaisesRegex(registry.StrategyError, '未知策略'):
            loaded.get('missing')


if __name__ == '__main__':
    unittest.main()
