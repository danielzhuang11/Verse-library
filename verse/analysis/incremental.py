from collections import defaultdict
from dataclasses import dataclass
from pprint import pp
from typing import Any, DefaultDict, List, Tuple, Optional, Dict
from verse.agents.base_agent import BaseAgent
from verse.analysis import AnalysisTreeNode
from intervaltree import IntervalTree
import itertools, copy, numpy as np

from verse.analysis.dryvr import _EPSILON
# from verse.analysis.simulator import PathDiffs
from verse.parser.parser import ControllerIR, ModePath

@dataclass
class CachedTransition:
    inits: Dict[str, List[float]]
    transition: int
    disc: List[str]
    cont: List[float]
    paths: List[ModePath]

@dataclass
class CachedSegment:
    trace: List[List[float]]
    asserts: List[str]
    transitions: List[CachedTransition]
    controller: ControllerIR
    run_num: int
    node_id: int

@dataclass
class CachedReachTrans:
    inits: Dict[str, List[float]]
    transition: int
    mode: List[str]
    dest: List[float]
    reset: List[float]
    reset_idx: List[int]
    paths: List[ModePath]

@dataclass
class CachedRTTrans:
    asserts: List[str]
    transitions: List[CachedReachTrans]
    controller: ControllerIR
    run_num: int
    node_id: int

def to_simulate(old_agents: Dict[str, BaseAgent], new_agents: Dict[str, BaseAgent], cached: Dict[str, CachedSegment]) -> Tuple[Dict[str, CachedSegment], Any]: #s/Any/PathDiffs/
    assert set(old_agents.keys()) == set(new_agents.keys())
    removed_paths, added_paths, reset_changed_paths = [], [], []
    for agent_id, old_agent in old_agents.items():
        new_agent = new_agents[agent_id]
        old_ctlr, new_ctlr = old_agent.controller, new_agent.controller
        assert old_ctlr.args == new_ctlr.args
        def group_by_var(ctlr: ControllerIR) -> Dict[str, List[ModePath]]:
            grouped = defaultdict(list)
            for path in ctlr.paths:
                grouped[path.var].append(path)
            return dict(grouped)
        old_grouped, new_grouped = group_by_var(old_ctlr), group_by_var(new_ctlr)
        if set(old_grouped.keys()) != set(new_grouped.keys()):
            raise NotImplementedError("different variable outputs")
        for var, old_paths in old_grouped.items():
            new_paths = new_grouped[var]
            for old, new in itertools.zip_longest(old_paths, new_paths):
                if new == None:
                    removed_paths.append(old)
                elif old.cond != new.cond:
                    added_paths.append((new_agent, new))
                elif old.val != new.val:
                    reset_changed_paths.append(new)
    new_cache = {}
    for agent_id in cached:
        segment = copy.deepcopy(cached[agent_id])
        new_transitions = []
        for trans in segment.transitions:
            removed = False
            for path in trans.paths:
                if path in removed_paths:
                    removed = True
                for rcp in reset_changed_paths:
                    if path.cond == rcp.cond:
                        path.val = rcp.val
            if not removed:
                new_transitions.append(trans)
        new_cache[agent_id] = segment
    return new_cache, added_paths

def convert_sim_trans(agent_id, transit_agents, inits, transition, trans_ind):
    if agent_id in transit_agents:
        return [CachedTransition(inits, trans_ind, mode, init, paths) for _id, mode, init, paths in transition]
    else:
        return []

def convert_reach_trans(agent_id, transit_agents, inits, transition, trans_ind):
    if agent_id in transit_agents:
        return [CachedReachTrans(inits, trans_ind, mode, dest, reset, reset_idx, paths) for _id, mode, dest, reset, reset_idx, paths in transition]
    else:
        return []

def combine_all(inits):
    return [[min(a) for a in np.transpose(np.array(inits)[:, 0])],
            [max(a) for a in np.transpose(np.array(inits)[:, 1])]]

@dataclass
class CachedTube:
    tube: List[List[List[float]]]

    def __eq__(self, other) -> bool:
        if other is None:
            return False
        return (self.tube == other.tube).any()

class SimTraceCache:
    def __init__(self):
        self.cache: DefaultDict[tuple, IntervalTree] = defaultdict(IntervalTree)

    def add_segment(self, agent_id: str, node: AnalysisTreeNode, transit_agents: List[str], trace: List[List[float]], transition, trans_ind: int, run_num: int):
        key = (agent_id,) + tuple(node.mode[agent_id])
        init = node.init[agent_id]
        tree = self.cache[key]
        assert_hits = node.assert_hits or {}
        # pp(('add seg', agent_id, *node.mode[agent_id], *init))
        for i, val in enumerate(init):
            if i == len(init) - 1:
                transitions = convert_sim_trans(agent_id, transit_agents, node.init, transition, trans_ind)
                entry = CachedSegment(trace, assert_hits.get(agent_id), transitions, node.agent[agent_id].controller, run_num, node.id)
                tree[val - _EPSILON:val + _EPSILON] = entry
                return entry
            else:
                next_level_tree = IntervalTree()
                tree[val - _EPSILON:val + _EPSILON] = next_level_tree
                tree = next_level_tree
        raise Exception("???")

    @staticmethod
    def iter_tree(tree, depth: int) -> List[List[float]]:
        if depth == 0:
            return [[(i.begin + i.end) / 2, (i.data.run_num, i.data.node_id, [t.transition for t in i.data.transitions])] for i in tree]
        res = []
        for i in tree:
            mid = (i.begin + i.end) / 2
            subs = SimTraceCache.iter_tree(i.data, depth - 1)
            res.extend([mid] + sub for sub in subs)
        return res

    def get_cached_inits(self, n: int):
        inits = defaultdict(list)
        for key, tree in self.cache.items():
            inits[key[0]].extend((*key[1:], *init) for init in self.iter_tree(tree, n))
        inits = dict(inits)
        return inits

    def check_hit(self, agent_id: str, mode: Tuple[str], init: List[float]) -> Optional[CachedSegment]:
        key = (agent_id,) + tuple(mode)
        if key not in self.cache:
            return None
        tree = self.cache[key]
        for cont in init:
            next_level_entries = list(tree[cont])
            if len(next_level_entries) == 0:
                return None
            tree = min(next_level_entries, key=lambda e: (e.end + e.begin) / 2 - cont).data
        assert isinstance(tree, CachedSegment)
        return tree

class TubeCache:
    def __init__(self):
        self.cache: DefaultDict[tuple, IntervalTree] = defaultdict(IntervalTree)

    def add_tube(self, agent_id: str, mode: Tuple[str], init: List[List[float]], trace: List[List[List[float]]]):
        key = (agent_id,) + tuple(mode)
        init = list(map(list, zip(*init)))
        tree = self.cache[key]
        for i, (low, high) in enumerate(init):
            if i == len(init) - 1:
                entry = CachedTube(trace)
                tree[low:high + _EPSILON] = entry
                return entry
            else:
                next_level_tree = IntervalTree()
                tree[low:high + _EPSILON] = next_level_tree
                tree = next_level_tree
        raise Exception("???")

    def check_hit(self, agent_id: str, mode: Tuple[str], init: List[List[float]]) -> Optional[CachedTube]:
        key = (agent_id,) + tuple(mode)
        if key not in self.cache:
            return None
        tree = self.cache[key]
        for low, high in list(map(list, zip(*init))):
            next_level_entries = [t for t in tree[low:high + _EPSILON] if t.begin <= low and high <= t.end]
            if len(next_level_entries) == 0:
                return None
            tree = min(next_level_entries, key=lambda e: low - e.begin + e.end - high).data
        assert isinstance(tree, CachedTube)
        return tree

class ReachTubeCache:
    def __init__(self):
        self.cache: DefaultDict[tuple, IntervalTree] = defaultdict(IntervalTree)

    def add_tube(self, agent_id: str, init: Dict[str, List[List[float]]], node: AnalysisTreeNode, transit_agents: List[str], transition, trans_ind: int, run_num: int):
        key = (agent_id,) + tuple(node.mode[agent_id])
        tree = self.cache[key]
        assert_hits = node.assert_hits or {}
        # pp(('add seg', agent_id, *node.mode[agent_id], *init))
        init = list(map(tuple, zip(*init[agent_id])))
        for i, (low, high) in enumerate(init):
            if i == len(init) - 1:
                transitions = convert_reach_trans(agent_id, transit_agents, node.init, transition, trans_ind)
                entry = CachedRTTrans(assert_hits.get(agent_id), transitions, node.agent[agent_id].controller, run_num, node.id)
                tree[low:high + _EPSILON] = entry
                return entry
            else:
                next_level_tree = IntervalTree()
                tree[low:high + _EPSILON] = next_level_tree
                tree = next_level_tree
        raise Exception("???")

    def check_hit(self, agent_id: str, mode: Tuple[str], init: List[float]) -> Optional[CachedRTTrans]:
        key = (agent_id,) + tuple(mode)
        if key not in self.cache:
            return None
        tree = self.cache[key]
        for low, high in list(map(tuple, zip(*init))):
            next_level_entries = [t for t in tree[low:high + _EPSILON] if t.begin <= low and high <= t.end]
            if len(next_level_entries) == 0:
                return None
            tree = min(next_level_entries, key=lambda e: low - e.begin + e.end - high).data
        assert isinstance(tree, CachedRTTrans)
        return tree

    @staticmethod
    def iter_tree(tree, depth: int) -> List[List[float]]:
        if depth == 0:
            return [[(i.begin + i.end) / 2, (i.data.run_num, i.data.node_id, [t.transition for t in i.data.transitions])] for i in tree]
        res = []
        for i in tree:
            mid = (i.begin + i.end) / 2
            subs = ReachTubeCache.iter_tree(i.data, depth - 1)
            res.extend([mid] + sub for sub in subs)
        return res

    def get_cached_inits(self, n: int):
        inits = defaultdict(list)
        for key, tree in self.cache.items():
            inits[key[0]].extend((*key[1:], *init) for init in self.iter_tree(tree, n))
        inits = dict(inits)
        return inits
