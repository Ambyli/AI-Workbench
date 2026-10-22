"""The sidecar's only state (signals/cache.py): the feature LRU keyed by text
digests, and the embedded example sets keyed by fingerprint."""

from __future__ import annotations

from signals.cache import READY, WARMING, ExampleSets, FeatureCache, text_key


def test_text_key_is_a_digest_of_every_part_with_boundaries():
    key = text_key("pii", "model@1", "some text")
    assert isinstance(key, bytes) and len(key) == 32
    assert key == text_key("pii", "model@1", "some text")
    assert key != text_key("pii", "model@2", "some text")           # another model, another key
    assert key != text_key("embed", "model@1", "some text")
    assert text_key("a", "bc") != text_key("ab", "c")              # parts cannot run into each other
    assert b"some text" not in key


def test_feature_cache_is_a_bounded_lru():
    cache = FeatureCache(2)
    cache.put(b"a", 1)
    cache.put(b"b", 2)
    assert cache.get(b"a") == 1                                     # a is now the most recent
    cache.put(b"c", 3)
    assert b"b" not in cache and cache.get(b"a") == 1 and cache.get(b"c") == 3
    assert len(cache) == 2
    cache.put(b"d", None)                                           # None is "nothing computed", never stored
    assert b"d" not in cache
    cache.clear()
    assert len(cache) == 0


def test_a_zero_sized_cache_keeps_nothing():
    cache = FeatureCache(0)
    cache.put(b"a", 1)
    assert cache.get(b"a") is None and len(cache) == 0


def test_example_sets_warm_once_per_fingerprint_and_items():
    sets = ExampleSets(8)
    entry, warm = sets.lookup("fp", "digest-1")
    assert warm and entry.state == WARMING
    again, warm = sets.lookup("fp", "digest-1")
    assert again is entry and not warm                              # warming already: do not start twice
    assert sets.ready(entry, ["a"], ["local-code"], "vectors")
    ready, warm = sets.lookup("fp", "digest-1")
    assert ready.state == READY and not warm and ready.ids == ["a"]
    # The same fingerprint with other items: embedded again, never answered from the old set.
    other, warm = sets.lookup("fp", "digest-2")
    assert warm and other is not entry and other.state == WARMING
    assert not sets.ready(entry, ["a"], ["x"], "stale")             # the replaced entry cannot publish


def test_example_sets_evict_ready_sets_first_and_hold_at_most_the_cap():
    sets = ExampleSets(2)
    a, _ = sets.lookup("a", "1")
    sets.ready(a, [], [], None)
    b, _ = sets.lookup("b", "1")                                    # still warming
    c, _ = sets.lookup("c", "1")
    assert len(sets) == 2
    assert sets.lookup("b", "1") == (b, False)                      # the warming one survived; `a` went
    assert sets.lookup("c", "1") == (c, False)
    assert not sets.ready(a, ["x"], ["y"], None)


def test_a_failed_warming_is_forgotten_so_the_next_request_tries_again():
    sets = ExampleSets(8)
    entry, _ = sets.lookup("fp", "d")
    sets.discard(entry)
    assert len(sets) == 0
    again, warm = sets.lookup("fp", "d")
    assert warm and again is not entry
    assert sets.states() == {WARMING: 1, READY: 0}
